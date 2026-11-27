#!/usr/bin/env bash
# Protects converted host extension points from first-party dispatch branches.
set -euo pipefail

for source_file in "$@"; do
  if grep -nE 'galadril_plugins[.](law|utils)|"(law|utils)[.][A-Za-z0-9_.-]+' "$source_file"; then
    printf 'Plugin-specific reference in %s\n' "$source_file" >&2
    exit 1
  else
    status=$?
    if [[ $status != 1 ]]; then
      exit "$status"
    fi
  fi
done
