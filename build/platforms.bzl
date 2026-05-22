"""Platform constraint targets used by multi-arch builds."""

def define_platforms():
    """Defines repository-local platforms."""
    native.platform(
        name = "linux_amd64",
        constraint_values = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        visibility = ["//visibility:public"],
    )

    native.platform(
        name = "linux_arm64",
        constraint_values = [
            "@platforms//os:linux",
            "@platforms//cpu:arm64",
        ],
        visibility = ["//visibility:public"],
    )
