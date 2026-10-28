"""Verifies Linux wheel selection without compiling Python build tools."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

def _wheel_impl(ctx):
    env = analysistest.begin(ctx)
    files = analysistest.target_under_test(env)[DefaultInfo].files.to_list()
    asserts.equals(env, 1, len(files))
    for file in files:
        if file.basename.startswith("cryptography-"):
            asserts.true(
                env,
                "-cp311-abi3-" in file.basename,
                "Python 3.13 must not select a Python 3.15 stable ABI wheel",
            )
        asserts.false(
            env,
            file.is_directory,
            "Use the locked wheel instead of recursively bootstrapping Maturin",
        )
        asserts.true(env, file.basename.endswith(".whl"))
    return analysistest.end(env)

linux_amd64_wheel_test = analysistest.make(
    _wheel_impl,
    config_settings = {
        "//command_line_option:platforms": str(Label("//:linux_amd64")),
    },
)

linux_arm64_wheel_test = analysistest.make(
    _wheel_impl,
    config_settings = {
        "//command_line_option:platforms": str(Label("//:linux_arm64")),
    },
)
