#!/usr/bin/env bash
set -euo pipefail

root=$PWD
stage=$(mktemp -d "${TEST_TMPDIR}/weaver.XXXXXXXX")
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/semconv" "$stage/registry"
cp -RL "$(dirname "$3")/." "$stage/semconv"
cp -RL "$(dirname "$2")/." "$stage/registry"
mkdir "$stage/templates"
cp -RL "$(dirname "$(dirname "$4")")/." "$stage/templates"
cd "$stage"
"$root/$1" registry generate --v2 --quiet -r registry -t templates contracts output
cmp output/contracts.py "$root/$5"
cmp output/contracts.rs "$root/$6"
cmp output/contracts.ts "$root/$7"
cmp output/contracts.py "$root/$8"
cmp output/contracts.rs "$root/$9"
cmp output/contracts.ts "$root/${10}"
