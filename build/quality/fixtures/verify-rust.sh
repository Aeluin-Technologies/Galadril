#!/usr/bin/env bash
# Each negative control must fail with its own diagnostic.
set -euo pipefail

expect_failure() {
    local diagnostic=$1
    shift
    local output
    if output=$("$@" 2>&1); then
        printf 'Expected failure from %s\n' "$*" >&2
        return 1
    fi
    if ! grep -Fq -- "$diagnostic" <<< "$output"; then
        printf '%s\n' "$output" >&2
        return 1
    fi
}

expect_failure 'Diff in ' bazel build //build/quality/fixtures:format_violation --config=engineering-rust --output_groups=rustfmt_checks "$@"
expect_failure 'clippy::unwrap-used' bazel build //build/quality/fixtures:lint_violation --config=engineering-rust --output_groups=clippy_checks "$@"
expect_failure 'mismatched types' bazel build //build/quality/fixtures:type_violation "$@"
expect_failure 'unresolved link' bazel build //build/quality/fixtures:documentation_violation "$@"
