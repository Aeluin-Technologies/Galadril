load(
    "@rules_oci//oci:defs.bzl",
    "oci_image",
    "oci_image_index",
    "oci_load",
    "oci_push",
)

load(
    "//build/private:nuxt_pnpm_layer.bzl",
    "nuxt_pnpm_layer",
)

_DOCKER_BUILD_EXEC_PROPERTIES = {
    "init-dockerd": "true",
    "workload-isolation-type": "firecracker",
}

def define_nuxt_oci_image(
        name,
        srcs,
        base_amd64,
        base_arm64,
        image_repository,
        version_tags,
        image_title,
        source_repository,
        build_script = "build",
        pnpm_version = "11.6.0",
        port = 3000,
        workdir = "/src",
        visibility = ["//visibility:public"]):
    package_dir = workdir.removeprefix("/")

    nuxt_pnpm_layer(
        name = name + "_layer",
        build_script = build_script,
        builder_image = "docker.io/amd64/node:26.5.0-trixie",
        container_platform = "linux/amd64",
        exec_properties = _DOCKER_BUILD_EXEC_PROPERTIES,
        package_dir = package_dir,
        pnpm_version = pnpm_version,
        srcs = srcs,
        visibility = ["//visibility:private"],
    )

    common_env = {
        "HOST": "0.0.0.0",
        "NITRO_HOST": "0.0.0.0",
        "NITRO_PORT": str(port),
        "NODE_ENV": "production",
        "PORT": str(port),
    }

    common_labels = {
        "org.opencontainers.image.source": source_repository,
        "org.opencontainers.image.title": image_title,
    }

    oci_image(
        name = name + "_amd64",
        base = base_amd64,
        cmd = [
            ".output/server/index.mjs",
        ],
        env = common_env,
        exposed_ports = [
            "%d/tcp" % port,
        ],
        labels = common_labels,
        tars = [
            ":" + name + "_layer",
        ],
        workdir = workdir,
        visibility = visibility,
    )

    oci_image(
        name = name + "_arm64",
        base = base_arm64,
        cmd = [
            ".output/server/index.mjs",
        ],
        env = common_env,
        exposed_ports = [
            "%d/tcp" % port,
        ],
        labels = common_labels,
        tars = [
            ":" + name + "_layer",
        ],
        workdir = workdir,
        visibility = visibility,
    )

    oci_image_index(
        name = name,
        images = [
            ":" + name + "_amd64",
            ":" + name + "_arm64",
        ],
        visibility = visibility,
    )

    oci_load(
        name = name + "_load_amd64",
        image = ":" + name + "_amd64",
        repo_tags = [
            "local/%s:amd64" % image_title,
        ],
        visibility = visibility,
    )

    oci_load(
        name = name + "_load_arm64",
        image = ":" + name + "_arm64",
        repo_tags = [
            "local/%s:arm64" % image_title,
        ],
        visibility = visibility,
    )

    oci_push(
        name = name + "_push",
        image = ":" + name,
        remote_tags = version_tags,
        repository = image_repository,
        visibility = visibility,
    )
