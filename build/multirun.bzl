"""Convenience multirun entrypoints."""

load("@rules_multirun//:defs.bzl", "multirun")

def define_multirun():
    """Defines multirun entrypoints for local dev orchestration."""
    multirun(
        name = "all",
        commands = [
            "//services/gateway:gateway",
            "//services/intake:intake",
            "//platform/vision/src/galadril_vision:vision",
        ],
        jobs = 0,
    )

    multirun(
        name = "push",
        commands = [
            "//services/gateway:push",
            "//services/intake:push",
            "//platform/vision/src/galadril_vision:push",
        ],
        jobs = 0,
    )
