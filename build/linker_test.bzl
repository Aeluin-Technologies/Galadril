"""Guards native Linux links against Gold's ARM64 erratum-handler crash."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

def _linux_linker_impl(ctx):
    env = analysistest.begin(ctx)
    actions = [action for action in analysistest.target_actions(env) if action.mnemonic == "Rustc"]
    asserts.equals(env, 1, len(actions), "Expected the Registry executable's Rust link action")
    for action in actions:
        linker_flags = [argument for argument in action.argv if "fuse-ld=" in argument]
        asserts.true(env, bool(linker_flags), "Select the Linux linker explicitly")
        for flag in linker_flags:
            asserts.true(env, flag.endswith("-fuse-ld=bfd"), "Native Linux links must use BFD: " + flag)
    return analysistest.end(env)

_EXECUTION_PLATFORMS = [
    str(Label("//build:rbe_network_linux_x86_64")),
    str(Label("//build:rbe_network_linux_arm64")),
]

amd64_linker_test = analysistest.make(
    _linux_linker_impl,
    config_settings = {
        "//command_line_option:extra_execution_platforms": _EXECUTION_PLATFORMS,
        "//command_line_option:platforms": str(Label("//:linux_amd64")),
    },
)

arm64_linker_test = analysistest.make(
    _linux_linker_impl,
    config_settings = {
        "//command_line_option:extra_execution_platforms": _EXECUTION_PLATFORMS,
        "//command_line_option:platforms": str(Label("//:linux_arm64")),
    },
)
