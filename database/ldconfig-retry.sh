#!/bin/sh
# Bound retries to the intermittent QEMU SIGSEGV without hiding package errors.

attempt=1
while :; do
    if "${0}.distrib" "$@"; then
        exit 0
    else
        status=$?
    fi
    if [ "$status" -ne 139 ] || [ "$attempt" -ge 5 ]; then
        exit "$status"
    fi
    printf 'ldconfig: emulator SIGSEGV; retry %s/5\n' "$attempt" >&2
    attempt=$((attempt + 1))
done
