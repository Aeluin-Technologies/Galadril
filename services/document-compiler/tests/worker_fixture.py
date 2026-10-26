"""Build a deterministic offline fixture from hash-pinned official TeX files."""

import gzip
import hashlib
import json
import struct
from pathlib import Path


def offline_bundle(manifest_path: Path, destination: Path) -> None:
    """TTB fixtures use reviewed upstream files without runtime downloads."""
    manifest = json.loads(manifest_path.read_text())
    payload = bytearray(70)
    entries = []
    for name, metadata in sorted(manifest.items()):
        data = (manifest_path.parent / "content" / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("Fixture digest mismatch")
        if name == "language.dat":
            data = b"english hyphen.tex\n"
        compressed = gzip.compress(data, mtime=0)
        entries.append(
            f"{len(payload)} {len(compressed)} {len(data)} {hashlib.sha256(data).hexdigest()} {name}"
        )
        payload.extend(compressed)
    index = (
        "[DEFAULTSEARCH]\nmain\n[SEARCH:main]\n/\n[FILELIST]\n"
        + "\n".join(entries)
        + "\n"
    ).encode()
    compressed = gzip.compress(index, mtime=0)
    header = struct.pack(
        "<14sIQII32sI",
        b"tectonicbundle",
        1,
        len(payload),
        len(compressed),
        len(index),
        hashlib.sha256(index).digest(),
        0,
    )
    payload[:70] = header
    payload.extend(compressed)
    destination.write_bytes(payload)
