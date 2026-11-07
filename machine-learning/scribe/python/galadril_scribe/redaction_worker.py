"""Processes private documents in a bounded child without provider credentials."""

import json
import resource
import sys


def main() -> None:
    try:
        if sys.platform != "linux":
            raise ValueError(
                "Native document workers require Linux resource limits"
            )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        for limit, maximum in (
            (resource.RLIMIT_CPU, 8),
            (resource.RLIMIT_AS, 1024 * 1024 * 1024),
        ):
            _, hard = resource.getrlimit(limit)
            ceiling = (
                maximum
                if hard == resource.RLIM_INFINITY
                else min(maximum, hard)
            )
            resource.setrlimit(limit, (ceiling, ceiling))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        from galadril_scribe.redaction_contracts import (
            MAX_WIRE_BYTES,
            RedactionInput,
        )
        from galadril_scribe.redaction_engine import project_document

        payload = sys.stdin.buffer.read(MAX_WIRE_BYTES + 1)
        if len(payload) > MAX_WIRE_BYTES:
            raise ValueError("Input limit")
        request = RedactionInput.model_validate_json(payload)
        result = project_document(request)
        sys.stdout.write(result.model_dump_json())
    except Exception:
        sys.stdout.write(json.dumps({"error": "redaction_refused"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
