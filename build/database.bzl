"""Routes Docker builds to supported workers while retaining image platforms."""

load("@aspect_rules_buildx//buildx:defs.bzl", "buildx_build")

def database_build(name, dockerfile, srcs):
    """Uses x86_64 Firecracker remotely and the host Docker engine locally.

    Args:
        name: Name of the selected image layout target.
        dockerfile: Label of the database Dockerfile.
        srcs: Declared build context directories.
    """
    for suffix, constraints in [
        ("local", []),
        # The shared ARM64 pool cannot run Docker-in-Firecracker. BuildKit's
        # bundled QEMU builds ARM64 images on the available x86_64 workers.
        ("arm64", ["@platforms//os:linux", "@platforms//cpu:x86_64"]),
        ("amd64", ["@platforms//os:linux", "@platforms//cpu:x86_64"]),
    ]:
        buildx_build(
            name = name + "_" + suffix,
            dockerfile = dockerfile,
            srcs = srcs,
            buildx_flags = ["--provenance=mode=max", "--sbom=true"],
            execution_requirements = {"no-sandbox": "1", "requires-network": "1"},
            exec_compatible_with = constraints,
            exec_properties = {
                "EstimatedComputeUnits": "6",
                "EstimatedFreeDiskBytes": "40GB",
                "default-timeout": "60m",
                # OCI exports exceed the separate ext4 workspace's 2 GB slack.
                "enable-vfs": "true",
                "init-dockerd": "true",
                "workload-isolation-type": "firecracker",
            },
            tags = ["manual"],
            target_compatible_with = ["@platforms//os:linux"],
        )
    native.alias(
        name = name,
        visibility = ["//database/tests:__pkg__"],
        actual = select({
            ":remote_arm64": ":" + name + "_arm64",
            ":remote_amd64": ":" + name + "_amd64",
            "//conditions:default": ":" + name + "_local",
        }),
    )

def _oci_layout_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)
    regctl = ctx.toolchains["@rules_oci//oci:regctl_toolchain_type"].regctl_info.binary
    jq = ctx.toolchains["@aspect_bazel_lib//lib:jq_toolchain_type"].jqinfo.bin
    args = ctx.actions.args()
    if ctx.attr.platform:
        if len(ctx.files.images) != 1:
            fail("A selected platform requires exactly one OCI layout")
        args.add_all(["image", "copy", "--platform", ctx.attr.platform])
        args.add("ocidir://" + ctx.files.images[0].path)
        args.add("ocidir://" + output.path)
    else:
        args.add_all(["index", "create", "ocidir://" + output.path])

        # BuildKit stores attestations alongside runnable platforms in its index.
        for platform in ["linux/amd64", "linux/arm64", "unknown/unknown"]:
            args.add("--platform", platform)
        for image in ctx.files.images:
            args.add("--ref", "ocidir://" + image.path)
    ctx.actions.run_shell(
        command = """set -euo pipefail
"$1" "${@:4}"
# rules_oci publishes the first layout entry; referrer discovery tags are auxiliary.
"$2" -e '.manifests |= map(select(.annotations["org.opencontainers.image.ref.name"] == "latest")) | if (.manifests | length) == 1 then . else error("Expected one publishable OCI index") end' "$3/index.json" > "$3/index.json.tmp"
mv "$3/index.json.tmp" "$3/index.json"
""",
        arguments = [regctl.path, jq.path, output.path, args],
        inputs = ctx.files.images,
        tools = [regctl, jq],
        outputs = [output],
        mnemonic = "DatabaseOCILayout",
        progress_message = "Assembling database OCI layout %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

database_oci_layout = rule(
    implementation = _oci_layout_impl,
    attrs = {
        "images": attr.label_list(allow_files = True, mandatory = True),
        "platform": attr.string(),
    },
    toolchains = [
        "@aspect_bazel_lib//lib:jq_toolchain_type",
        "@rules_oci//oci:regctl_toolchain_type",
    ],
    doc = "Uses regctl to preserve attested indexes or select a testable platform.",
)
