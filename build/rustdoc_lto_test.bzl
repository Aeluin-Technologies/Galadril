"""Checks that release doctests link native executables instead of LLVM objects."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

def _rustdoc_lto_impl(ctx):
    env = analysistest.begin(ctx)
    actions = [action for action in analysistest.target_actions(env) if action.mnemonic == "RustdocTestWriter"]
    asserts.equals(env, 1, len(actions), "Expected the executable doctest runner")
    for action in actions:
        flags = [argument for argument in action.argv if argument.startswith("-Clinker-plugin-lto")]
        asserts.true(env, not flags or flags[-1] == "-Clinker-plugin-lto=no", "The last LTO flag must not delegate bitcode to GCC/BFD: " + str(flags))
        asserts.true(env, "-Clto=thin" in action.argv, "Keep the release ThinLTO setting")
    return analysistest.end(env)

rustdoc_lto_test = analysistest.make(
    _rustdoc_lto_impl,
    config_settings = {
        "//command_line_option:compilation_mode": "opt",
        str(Label("@rules_rust//rust/settings:lto")): "thin",
    },
)
