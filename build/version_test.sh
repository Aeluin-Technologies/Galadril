#!/usr/bin/env bash
set -euo pipefail

VERSION_FILE="${TEST_SRCDIR}/${TEST_WORKSPACE}/VERSION.txt"

if [[ ! -f "${VERSION_FILE}" ]]; then
    printf 'VERSION.txt not found: %s\n' "${VERSION_FILE}" >&2
    exit 1
fi

VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"

if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    printf 'Invalid semantic version: %s\n' "${VERSION}" >&2
    exit 1
fi

printf 'Valid version: %s\n' "${VERSION}"
