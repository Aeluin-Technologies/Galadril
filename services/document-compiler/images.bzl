"""Compiler images preserve separate API, supervisor, and worker dependencies."""

load("@bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_push")
load("@rules_pkg//:pkg.bzl", "pkg_tar")
load("//build:oci_test.bzl", "multiarch_index_test")


def document_compiler_images():
    for name, binary, repository in [
        ("api", "document-compiler", "galadril-document-compiler"),
        ("supervisor", "compiler-supervisor", "galadril-document-supervisor"),
        ("worker", "compiler-worker", "galadril-document-worker"),
    ]:
        pkg_tar(name = name + "_layer", srcs = [":" + binary])
        oci_image(
            name = name + "_linux",
            base = "@distroless_cc",
            entrypoint = ["/" + binary],
            tars = [":" + name + "_layer"]
            + (
                ["//build/document_compiler:runtime_layer"]
                if name == "worker"
                else []
            ),
            visibility = ["//visibility:public"],
        )
        for arch in ["amd64", "arm64"]:
            platform_transition_filegroup(
                name = name + "_" + arch,
                srcs = [":" + name + "_linux"],
                target_platform = "//:linux_" + arch,
            )
        oci_image_index(
            name = name + "_images",
            images = [":" + name + "_amd64", ":" + name + "_arm64"],
        )
        oci_push(
            name = "push" if name == "api" else "push_" + name,
            image = ":" + name + "_images",
            remote_tags = "//:stamped_tags",
            repository = "ghcr.io/aeluin-technologies/" + repository,
            visibility = ["//visibility:public"],
        )
        multiarch_index_test(
            name = name + "_multiarch_index_test",
            target_under_test = ":" + name + "_images",
        )
