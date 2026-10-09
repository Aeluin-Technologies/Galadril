"""Guards remote Docker scheduling without starting a container or a VM."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

def _check_buildx(ctx, platform):
    env = analysistest.begin(ctx)
    actions = [action for action in analysistest.target_actions(env) if action.mnemonic == "BuildX"]
    asserts.equals(env, 1, len(actions))
    for action in actions:
        asserts.true(env, platform in action.argv, "Buildx must preserve the requested image architecture")
        asserts.true(
            env,
            any(["buildx_linux-amd64/" in argument for argument in action.argv]),
            "Docker actions must use x86_64 workers: the shared ARM64 pool has no Firecracker executors",
        )
    return analysistest.end(env)

def _arm64_impl(ctx):
    return _check_buildx(ctx, "linux/arm64")

def _amd64_impl(ctx):
    return _check_buildx(ctx, "linux/amd64")

_REMOTE_SETTINGS = {
    "//command_line_option:extra_execution_platforms": [
        str(Label("@toolchains_buildbuddy//platforms:linux_x86_64")),
        str(Label("@toolchains_buildbuddy//platforms:linux_arm64")),
    ],
    str(Label("//database:remote_build")): True,
}

arm64_scheduling_test = analysistest.make(
    _arm64_impl,
    config_settings = dict(_REMOTE_SETTINGS, **{"//command_line_option:platforms": str(Label("//:linux_arm64"))}),
)

amd64_scheduling_test = analysistest.make(
    _amd64_impl,
    config_settings = dict(_REMOTE_SETTINGS, **{"//command_line_option:platforms": str(Label("//:linux_amd64"))}),
)
