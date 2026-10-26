"""Keep coverage file writers outside executables restricted by the kernel."""

def _without_coverage_impl(_settings, _attr):
    return {"//command_line_option:collect_code_coverage": False}

_without_coverage = transition(
    implementation = _without_coverage_impl,
    inputs = [],
    outputs = ["//command_line_option:collect_code_coverage"],
)

def _isolated_artifacts_impl(ctx):
    files = depset(transitive = [src[DefaultInfo].files for src in ctx.attr.srcs])
    runfiles = ctx.runfiles(transitive_files = files).merge_all([
        src[DefaultInfo].default_runfiles
        for src in ctx.attr.srcs
    ])
    return [DefaultInfo(files = files, runfiles = runfiles)]

isolated_artifacts = rule(
    implementation = _isolated_artifacts_impl,
    attrs = {
        "srcs": attr.label_list(
            mandatory = True,
            cfg = _without_coverage,
            doc = "Executables and their dependencies must retain production behavior under coverage.",
        ),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
    doc = "Forwards production artifacts without linking LLVM profile writers into the sandbox.",
)
