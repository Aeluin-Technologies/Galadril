"""Enforces rustdoc diagnostics for the first-party crates in a package."""

load("@rules_rust//rust:defs.bzl", "rust_doc")

def engineering_rust_docs(crates):
    """Expose documentation builds as a single quality output."""
    docs = []
    for crate in crates:
        name = crate + "_engineering_doc"
        rust_doc(
            name = name,
            crate = ":" + crate,
            testonly = True,
            rustdoc_flags = ["-Dwarnings"],
        )
        docs.append(":" + name)

    native.filegroup(
        name = "engineering_docs",
        testonly = True,
        srcs = docs,
        visibility = ["//build/quality:__pkg__"],
    )
