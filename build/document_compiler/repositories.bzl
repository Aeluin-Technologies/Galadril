"""Pinned native headers and static archives avoid executor system packages."""


def _native_sysroot_impl(ctx):
    ar = ctx.which("ar")
    if not ar:
        fail("ar is required to unpack pinned Debian development packages")
    for index in range(len(ctx.attr.urls)):
        directory = "package_{}".format(index)
        ctx.file(directory + "/.keep", "")
        ctx.download(
            ctx.attr.urls[index],
            output = directory + "/package.deb",
            sha256 = ctx.attr.sha256s[index],
        )
        result = ctx.execute(
            [ar, "x", ctx.path(directory + "/package.deb")],
            working_directory = str(ctx.path(directory)),
        )
        if result.return_code:
            fail("Native package extraction failed: " + result.stderr)
        data = [
            path
            for path in ctx.path(directory).readdir()
            if path.basename.startswith("data.tar")
        ]
        if len(data) != 1:
            fail("Native package must contain exactly one data archive")
        ctx.extract(data[0], output = ".")
        ctx.delete(directory)
    ctx.file("marker", "native-development-root\n")
    directory = "usr/lib/" + ctx.attr.triple
    libraries = [
        "fontconfig",
        "uuid",
        "freetype",
        "png16",
        "expat",
        "brotlidec",
        "brotlicommon",
        "bz2",
        "z",
        "graphite2",
        "icuuc",
        "icudata",
    ]
    imports = []
    for name in libraries:
        static = directory + "/lib" + name + ".a"
        if ctx.path(static).exists:
            imports.append(
                "cc_import(name = 'lib_{}', static_library = '{}')".format(
                    name, static
                )
            )
        else:
            shared = directory + "/lib" + name + ".so"
            if not ctx.path(shared).exists:
                fail("Pinned native package lacks library: " + name)
            imports.append(
                "cc_import(name = 'lib_{}', shared_library = '{}')".format(
                    name, shared
                )
            )
    ctx.file(
        "BUILD.bazel",
        "\n".join(
            [
                'load("@rules_cc//cc:cc_import.bzl", "cc_import")',
                'load("@rules_cc//cc:cc_library.bzl", "cc_library")',
                'load("@rules_pkg//pkg:tar.bzl", "pkg_tar")',
                'package(default_visibility = ["//visibility:public"])',
                'exports_files(["marker"])',
                'filegroup(name = "headers", srcs = glob(["usr/include/**"]))',
                'filegroup(name = "runtime", srcs = glob(["usr/lib/**/*.so*", "lib/**/*.so*", "usr/share/doc/**/copyright"]))',
                'pkg_tar(name = "runtime_layer", srcs = [":runtime"], package_dir = "/", strip_prefix = "/external/'
                + ctx.name
                + '")',
            ]
            + imports
            + [
                'cc_library(name = "native", deps = '
                + repr([":lib_" + name for name in libraries])
                + ', linkopts = ["-lm", "-ldl", "-lpthread", "-lstdc++"])',
            ]
        ),
    )


native_sysroot = repository_rule(
    implementation = _native_sysroot_impl,
    attrs = {
        "urls": attr.string_list(mandatory = True),
        "sha256s": attr.string_list(mandatory = True),
        "triple": attr.string(mandatory = True),
    },
)


def _latex_fixture_impl(ctx):
    manifest = json.decode(ctx.read(ctx.attr.manifest))
    downloads = []
    for name, metadata in manifest.items():
        start = metadata["start"]
        end = start + metadata["length"] - 1
        downloads.append(
            ctx.download(
                "https://data1b.fullyjustified.net/tlextras-2022.0r0.tar",
                output = "content/" + name,
                sha256 = metadata["sha256"],
                canonical_id = "tlextras-2022.0r0/" + name,
                headers = {
                    "Range": "bytes={}-{}".format(start, end),
                    "User-Agent": "curl/8.7.1",
                },
                block = False,
            )
        )
    for download in downloads:
        download.wait()
    ctx.file("manifest.json", json.encode(manifest))
    ctx.file(
        "BUILD.bazel",
        'package(default_visibility = ["//visibility:public"])\nexports_files(["manifest.json"])\nfilegroup(name = "files", srcs = glob(["content/*"]) + ["manifest.json"])\n',
    )


latex_fixture = repository_rule(
    implementation = _latex_fixture_impl,
    attrs = {"manifest": attr.label(mandatory = True)},
)
