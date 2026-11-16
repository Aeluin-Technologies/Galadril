#!/usr/bin/env bash
# Verify the exact executable kernel; an unavailable verifier fails this gate.
set -euo pipefail

verifier="${VERUS_BIN:-verus}"
required='0.2026.10.04.426d8b0'
if ! command -v "$verifier" >/dev/null 2>&1; then
    printf 'Verus %s is required; set VERUS_BIN to the official executable.\n' "$required" >&2
    exit 1
fi
version="$("$verifier" --version)"
if [[ "$version" != *"$required"* ]]; then
    printf 'Expected Verus %s, got %s\n' "$required" "$version" >&2
    exit 1
fi
proof="${TEST_SRCDIR}/${TEST_WORKSPACE}/platform/ontology/rust/proofs/ifc.rs"
"$verifier" "$proof" --no-cheating --compile -o "${TEST_TMPDIR}/ifc-verified"
