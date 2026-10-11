"""Platform constraint targets used by multi-arch builds."""

_LINUX_PYTHON_FLAGS = [
    "--@aspect_rules_py//uv/private/constraints/platform:platform_libc=glibc",
    "--@aspect_rules_py//uv/private/constraints/platform:platform_version=2.35",
]

def define_platforms(name = "platforms"):
    """Defines repository-local platforms.

    Args:
      name: A unique name for this macro instance.
    """
    native.platform(
        name = "linux_amd64",
        constraint_values = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        flags = _LINUX_PYTHON_FLAGS,
        visibility = ["//visibility:public"],
    )

    native.platform(
        name = "linux_arm64",
        constraint_values = [
            "@platforms//os:linux",
            "@platforms//cpu:arm64",
        ],
        flags = _LINUX_PYTHON_FLAGS,
        visibility = ["//visibility:public"],
    )
