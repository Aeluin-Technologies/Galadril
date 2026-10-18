"""Bazel resolves and verifies wheels before this immutable offline catalog is built."""

import hashlib
import json
import sys
import zipfile
from email import message_from_bytes
from pathlib import Path


def main() -> None:
    output, *filenames = sys.argv[1:]
    wheels: dict[str, object] = {}
    for filename in filenames:
        path = Path(filename)
        with zipfile.ZipFile(path) as archive:
            metadata_path = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            )
            metadata = message_from_bytes(archive.read(metadata_path))
        name = str(metadata["Name"]).lower().replace("_", "-")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if name in wheels:
            raise ValueError("Duplicate locked wheel")
        wheels[name] = {
            "filename": path.name,
            "version": str(metadata["Version"]),
            "sha256": digest,
            "requirements": metadata.get_all("Requires-Dist", []),
        }
    Path(output).write_text(json.dumps({"wheels": wheels}, sort_keys=True))


if __name__ == "__main__":
    main()
