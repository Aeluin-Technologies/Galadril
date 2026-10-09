"""Checks Cargo build scripts and runners use the same execution configuration."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

_ExecutableConfiguration = provider(fields = ["root", "script_root", "runner_root"])

def _executable_configuration_impl(target, ctx):
    tool = getattr(ctx.rule.attr, "tool", None)
    if tool != None:
        return [tool[_ExecutableConfiguration]]
    script = getattr(ctx.rule.attr, "script", None)
    runner = getattr(ctx.rule.attr, "_cargo_build_script_runner", None)
    executable = target[DefaultInfo].files_to_run.executable
    if runner != None:
        return [_ExecutableConfiguration(
            script_root = script[_ExecutableConfiguration].root,
            runner_root = runner[_ExecutableConfiguration].root,
        )]
    if script != None:
        return [_ExecutableConfiguration(root = script[_ExecutableConfiguration].root)]
    return [_ExecutableConfiguration(root = executable.root.path)]

_executable_configuration = aspect(
    implementation = _executable_configuration_impl,
    attr_aspects = ["tool", "script", "_cargo_build_script_runner"],
)

def _build_script_tool_fixture_impl(_ctx):
    return [DefaultInfo()]

build_script_tool_fixture = rule(
    implementation = _build_script_tool_fixture_impl,
    attrs = {"tool": attr.label(cfg = "exec", mandatory = True)},
)

def _build_script_impl(ctx):
    env = analysistest.begin(ctx)
    configuration = analysistest.target_under_test(env)[_ExecutableConfiguration]
    asserts.equals(
        env,
        configuration.runner_root,
        configuration.script_root,
        "Cargo build.rs must be compiled for the runner's execution platform",
    )
    return analysistest.end(env)

_EXECUTION_PLATFORMS = [
    str(Label("//build:rbe_network_linux_x86_64")),
    str(Label("//build:rbe_network_linux_arm64")),
]

arm64_build_script_test = analysistest.make(
    _build_script_impl,
    config_settings = {
        "//command_line_option:extra_execution_platforms": _EXECUTION_PLATFORMS,
        "//command_line_option:platforms": str(Label("//:linux_arm64")),
    },
    extra_target_under_test_aspects = [_executable_configuration],
)

amd64_build_script_test = analysistest.make(
    _build_script_impl,
    config_settings = {
        "//command_line_option:extra_execution_platforms": _EXECUTION_PLATFORMS,
        "//command_line_option:platforms": str(Label("//:linux_amd64")),
    },
    extra_target_under_test_aspects = [_executable_configuration],
)
