#!/usr/bin/env bash
set -euo pipefail

INPUT="${1:?Missing VERSION.txt path}"
OUTPUT="${2:?Missing output path}"

VERSION="$(tr -d '[:space:]' < "${INPUT}")"

if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    printf 'Invalid semantic version: %s\n' "${VERSION}" >&2
    exit 1
fi

printf 'latest\n%s\n' "${VERSION}" > "${OUTPUT}"
