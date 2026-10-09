"""Exposes the upstream Buildx action script without building an image."""

_BuildxScript = provider(fields = ["command"])

def _capture_impl(target, _ctx):
    actions = [action for action in target.actions if action.mnemonic == "BuildX"]
    if len(actions) != 1:
        fail("Expected exactly one Buildx action")
    return [_BuildxScript(command = actions[0].argv[2])]

_capture = aspect(implementation = _capture_impl)

_SETTINGS = {
    "//command_line_option:platforms": [str(Label("//:linux_amd64"))],
    "//command_line_option:extra_execution_platforms": [str(Label("@toolchains_buildbuddy//platforms:linux_x86_64"))],
    str(Label("//database:remote_build")): True,
}

def _platform_impl(_settings, _attr):
    return _SETTINGS

_platform = transition(
    implementation = _platform_impl,
    inputs = [],
    outputs = _SETTINGS.keys(),
)

def _fixture_impl(ctx):
    script = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(script, ctx.attr.srcs[0][_BuildxScript].command)
    return [DefaultInfo(files = depset([script]))]

buildx_fixture = rule(
    implementation = _fixture_impl,
    attrs = {
        "srcs": attr.label_list(aspects = [_capture], cfg = _platform),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
)
