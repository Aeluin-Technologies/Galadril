"""Expose the selected Bazel Rust toolchain to native Python wheel actions."""


def _rust_wheel_toolchain_impl(ctx):
    rust = ctx.toolchains["@rules_rust//rust:toolchain"]
    if rust.cargo == None:
        fail("The Rust toolchain must provide Cargo for native Python wheels")
    return [
        DefaultInfo(files = rust.all_files),
        platform_common.TemplateVariableInfo({
            "RUST_WHEEL_BIN_DIR": rust.cargo.dirname,
            "RUST_WHEEL_RUSTC": rust.rustc.path,
        }),
    ]


rust_wheel_toolchain = rule(
    implementation = _rust_wheel_toolchain_impl,
    toolchains = ["@rules_rust//rust:toolchain"],
)
