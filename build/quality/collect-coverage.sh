#!/usr/bin/env bash
# Reject a stale LCOV left by an interrupted or failed Bazel invocation.
set -euo pipefail

artifact_dir=$(mkdir -p -- "$1" && cd -- "$1" && pwd)
shift
workspace=$(bazel info workspace "$@")
execution_root=$(bazel info execution_root "$@")
output_root=$(bazel info output_path "$@")
cd "$workspace"

before=$(bazel run //build/quality:check "$@" -- fingerprint)
report="$output_root/_coverage/_coverage_report.dat"
events_dir=$(mktemp -d)
trap 'rm -rf -- "$events_dir"' EXIT
bazel query --lockfile_mode=error --query_file=build/quality/coverage.query \
    --output=label > "$events_dir/targets"
test -s "$events_dir/targets"
rm -f -- "$report"

coverage_status=0
bazel coverage "$@" \
    --target_pattern_file="$events_dir/targets" --combined_report=lcov \
    --instrumentation_filter=//... --instrument_test_targets --keep_going \
    --build_event_json_file="$events_dir/events.json" || coverage_status=$?

if [[ ! -s "$report" ]]; then
    printf 'Bazel coverage emitted no combined LCOV (exit %s)\n' "$coverage_status" >&2
    exit 1
fi

bazel run //build/quality:check "$@" -- capture \
    --lcov "$report" --events "$events_dir/events.json" --before "$before" \
    --artifact "$artifact_dir" --execution-root "$execution_root" \
    --execution-root /buildbuddy-execroot
exit "$coverage_status"
