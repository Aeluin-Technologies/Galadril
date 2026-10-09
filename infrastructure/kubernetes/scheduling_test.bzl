"""Guards host execution for Kubernetes tests requiring kernel keyrings."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

_SchedulingInfo = provider(fields = ["tags", "properties"])

def _scheduling_info_impl(_target, ctx):
    return [_SchedulingInfo(
        tags = ctx.rule.attr.tags,
        properties = ctx.rule.attr.exec_properties,
    )]

_scheduling_info = aspect(implementation = _scheduling_info_impl)

def _ambient_scheduling_impl(ctx):
    env = analysistest.begin(ctx)
    scheduling = analysistest.target_under_test(env)[_SchedulingInfo]
    asserts.true(env, "no-remote-exec" in scheduling.tags, "The shared Firecracker kernel lacks the keyring sysctls required by kubelet")
    asserts.false(env, "test.init-dockerd" in scheduling.properties)
    return analysistest.end(env)

ambient_scheduling_test = analysistest.make(
    _ambient_scheduling_impl,
    extra_target_under_test_aspects = [_scheduling_info],
)
