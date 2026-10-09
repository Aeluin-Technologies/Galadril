"""Guards remote Docker scheduling without starting a container or a VM."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

_ExecutionInfo = provider(fields = ["properties"])

def _execution_info_impl(_target, ctx):
    return [_ExecutionInfo(properties = ctx.rule.attr.exec_properties)]

_execution_info = aspect(implementation = _execution_info_impl)

def _check_buildx(ctx, platform):
    env = analysistest.begin(ctx)
    properties = analysistest.target_under_test(env)[_ExecutionInfo].properties
    asserts.equals(env, "true", properties.get("enable-vfs"), "OCI outputs exceed Firecracker's fixed workspace disk")
    asserts.equals(env, "40GB", properties.get("EstimatedFreeDiskBytes"))
    asserts.equals(env, "60m", properties.get("default-timeout"), "Cold OCI exports can exceed 30 minutes over VFS")
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
    extra_target_under_test_aspects = [_execution_info],
)

amd64_scheduling_test = analysistest.make(
    _amd64_impl,
    config_settings = dict(_REMOTE_SETTINGS, **{"//command_line_option:platforms": str(Label("//:linux_amd64"))}),
    extra_target_under_test_aspects = [_execution_info],
)

def _docker_disk_impl(ctx):
    env = analysistest.begin(ctx)
    properties = analysistest.target_under_test(env)[_ExecutionInfo].properties
    asserts.equals(env, "30GB", properties.get("test.EstimatedFreeDiskBytes"), "PostgreSQL image extraction needs explicit scratch space")
    return analysistest.end(env)

docker_disk_test = analysistest.make(
    _docker_disk_impl,
    extra_target_under_test_aspects = [_execution_info],
)
