#!/usr/bin/env bash
set -euo pipefail

root=$PWD
stage=$(mktemp -d "${TEST_TMPDIR}/weaver.XXXXXXXX")
trap 'rm -rf "$stage"' EXIT
weaver="$root/$1"
mkdir "$stage/semconv" "$stage/registry"
cp -RL "$(dirname "$3")/." "$stage/semconv"
cp -RL "$(dirname "$2")/." "$stage/registry"
cd "$stage"
"$weaver" registry check --v2 -r registry
"$weaver" registry diff --v2 -r registry --baseline-registry semconv --format json > diff.json
test -s diff.json
