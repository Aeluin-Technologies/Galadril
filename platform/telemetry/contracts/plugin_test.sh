#!/usr/bin/env bash
set -euo pipefail

root=$PWD
stage=$(mktemp -d "${TEST_TMPDIR}/weaver.XXXXXXXX")
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/semconv" "$stage/registry" "$stage/plugin"
cp -RL "$(dirname "$3")/." "$stage/semconv"
cp -RL "$(dirname "$2")/." "$stage/registry"
cp -RL "$(dirname "$4")/." "$stage/plugin"
cd "$stage"
"$root/$1" registry check --v2 -r plugin -p "$root/$5"
sed 's/galadril.plugin.example.normalize/galadril.plugin.other.normalize/' \
  plugin/operations.yaml > wrong.yaml
mv wrong.yaml plugin/operations.yaml
if "$root/$1" registry check --v2 -r plugin -p "$root/$5" > failure.log 2>&1; then
  printf '%s\n' 'A plugin declared a span in the namespace of another plugin.'
  exit 1
fi
grep -q plugin_span_namespace failure.log
