load("@aspect_rules_lint//format:defs.bzl", "format_multirun", "format_test")

def define_format_targets():
    format_multirun(
        name = "format",
    )

    format_test(
        name = "format_test",
        no_sandbox = True,
        workspace = "//:MODULE.bazel",
    )
