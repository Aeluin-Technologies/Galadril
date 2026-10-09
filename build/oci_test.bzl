"""Guards publication of both supported Linux image architectures."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

_ImageInputs = provider(fields = ["platforms"])

def _image_inputs_impl(_target, ctx):
    if hasattr(ctx.rule.attr, "target_platform"):
        return [_ImageInputs(platforms = [ctx.rule.attr.target_platform.label])]
    platforms = []
    for image in getattr(ctx.rule.attr, "images", []):
        platforms.extend(image[_ImageInputs].platforms)
    return [_ImageInputs(platforms = platforms)]

_image_inputs = aspect(
    implementation = _image_inputs_impl,
    attr_aspects = ["images"],
)

def _multiarch_impl(ctx):
    env = analysistest.begin(ctx)
    asserts.equals(
        env,
        [Label("//:linux_amd64"), Label("//:linux_arm64")],
        analysistest.target_under_test(env)[_ImageInputs].platforms,
        "Published indexes must include both platform-transitioned images",
    )
    return analysistest.end(env)

multiarch_index_test = analysistest.make(
    _multiarch_impl,
    config_settings = {
        "//command_line_option:extra_execution_platforms": [
            str(Label("@toolchains_buildbuddy//platforms:linux_x86_64")),
            str(Label("@toolchains_buildbuddy//platforms:linux_arm64")),
        ],
    },
    extra_target_under_test_aspects = [_image_inputs],
)
