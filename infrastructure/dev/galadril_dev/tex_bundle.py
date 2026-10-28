"""Prepare the complete public Tectonic distribution without extracting tar paths."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import struct
import tarfile
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from threading import Event
from typing import BinaryIO, Protocol, cast

SOURCE_URL = "https://data1b.fullyjustified.net/tlextras-2022.0r0.tar"
SOURCE_SHA256 = (
    "425685e124746c15ba9bb8e0596bdaad98fce886afa347fbcf9ec0e9acd7fe79"
)
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_FILES = 300000
CHUNK_BYTES = 64 * 1024


class BundleError(RuntimeError):
    """A public resource bundle must be complete and verified before publication."""


class Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class BundleResult:
    sha256: str
    files: int
    source_sha256: str


class HashingReader:
    __slots__ = ("source", "digest", "count", "stop")

    def __init__(self, source: Readable, stop: Event | None) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.count = 0
        self.stop = stop

    def read(self, size: int = -1) -> bytes:
        if self.stop is not None and self.stop.is_set():
            raise BundleError("Bundle preparation cancelled")
        if size < 0:
            raise BundleError("Unbounded source read refused")
        data = self.source.read(size)
        self.count += len(data)
        if self.count > MAX_SOURCE_BYTES:
            raise BundleError("Bundle source exceeds the distribution limit")
        self.digest.update(data)
        return data


def pack(
    source: Readable,
    destination: Path,
    expected_sha256: str,
    *,
    progress: Callable[[int, int], None] | None = None,
    stop: Event | None = None,
) -> BundleResult:
    """Stream files into TTB v1 and retain any existing verified bundle on failure."""
    if destination.is_symlink():
        raise BundleError("Bundle directory must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    lock_path = destination / ".bundle.lock"
    if lock_path.is_symlink():
        raise BundleError("Bundle lock must not be a symlink")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _pack(source, destination, expected_sha256, progress, stop)


def _pack(
    source: Readable,
    destination: Path,
    expected: str,
    progress: Callable[[int, int], None] | None,
    stop: Event | None,
) -> BundleResult:
    partial = destination / "bundle.ttb.part"
    for path in (
        partial,
        destination / "bundle.ttb",
        destination / "bundle.sha256",
        destination / "provenance.json",
    ):
        if path.is_symlink():
            raise BundleError("Bundle files must not be symlinks")
    entries: dict[str, str] = {}
    reader = HashingReader(source, stop)
    try:
        with partial.open("w+b") as output:
            output.write(bytes(70))
            with tarfile.open(
                fileobj=cast(BinaryIO, reader), mode="r|"
            ) as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    name = member.name
                    path = PurePosixPath(name)
                    if (
                        not member.isfile()
                        or path.is_absolute()
                        or ".." in path.parts
                        or any(
                            character in name
                            for character in ("\r", "\n", "\x00")
                        )
                        or name in entries
                    ):
                        raise BundleError(
                            "Invalid or duplicate public resource path"
                        )
                    if len(entries) >= MAX_FILES or member.size > 512 * 1024**2:
                        raise BundleError("Public resource limits exceeded")
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise BundleError("Missing public resource bytes")
                    offset = output.tell()
                    digest = hashlib.sha256()
                    count = 0
                    with (
                        handle,
                        gzip.GzipFile(
                            fileobj=output, mode="wb", filename="", mtime=0
                        ) as compressed,
                    ):
                        while data := handle.read(CHUNK_BYTES):
                            count += len(data)
                            digest.update(data)
                            compressed.write(data)
                    if count != member.size:
                        raise BundleError("Truncated public resource")
                    entries[name] = (
                        f"{offset} {output.tell() - offset} {count} {digest.hexdigest()} {name}"
                    )
                    if progress is not None and len(entries) % 10000 == 0:
                        progress(len(entries), output.tell())
            while reader.read(CHUNK_BYTES):
                pass
            if not entries or reader.digest.hexdigest() != expected:
                raise BundleError("Official bundle source checksum mismatch")
            index = (
                "[DEFAULTSEARCH]\nmain\n[SEARCH:main]\n/\n[FILELIST]\n"
                + "\n".join(entries[name] for name in sorted(entries))
                + "\n"
            ).encode()
            index_offset = output.tell()
            compressed_index = gzip.compress(index, mtime=0)
            output.write(compressed_index)
            output.seek(0)
            output.write(
                struct.pack(
                    "<14sIQII32sI",
                    b"tectonicbundle",
                    1,
                    index_offset,
                    len(compressed_index),
                    len(index),
                    hashlib.sha256(index).digest(),
                    0,
                )
            )
            output.flush()
            os.fsync(output.fileno())
            output.seek(0)
            result = BundleResult(
                hashlib.file_digest(output, "sha256").hexdigest(),
                len(entries),
                expected,
            )
        partial.replace(destination / "bundle.ttb")
        _publish(destination / "bundle.sha256", result.sha256 + "\n")
        _publish(
            destination / "provenance.json",
            json.dumps(asdict(result), indent=2) + "\n",
        )
        return result
    except (OSError, tarfile.TarError, ValueError) as error:
        raise BundleError(
            f"Bundle preparation failed ({type(error).__name__})"
        ) from None
    finally:
        partial.unlink(missing_ok=True)


def _publish(path: Path, content: str) -> None:
    partial = path.with_suffix(path.suffix + ".part")
    if path.is_symlink() or partial.is_symlink():
        raise BundleError("Bundle metadata must not be symlinked")
    partial.write_text(content)
    partial.replace(path)


def cached(destination: Path) -> BundleResult | None:
    bundle = destination / "bundle.ttb"
    metadata = destination / "provenance.json"
    if not bundle.is_file() or not metadata.is_file():
        return None
    if bundle.is_symlink() or metadata.is_symlink():
        raise BundleError("Bundle cache must not contain symlinks")
    value: object = json.loads(metadata.read_text())
    if not isinstance(value, dict):
        raise BundleError("Invalid bundle provenance")
    result = BundleResult(
        str(value.get("sha256", "")),
        int(value.get("files", 0)),
        str(value.get("source_sha256", "")),
    )
    with bundle.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if result.sha256 != digest or result.source_sha256 != SOURCE_SHA256:
        raise BundleError("Bundle cache does not match its verified provenance")
    return result


def download(
    destination: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    stop: Event | None = None,
) -> BundleResult:
    """The downloader runs outside the compiler's secret-free networkless workers."""
    existing = cached(destination)
    if existing is not None:
        return existing
    request = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": "Galadril-Tectonic-Bootstrap/1"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with contextlib.closing(opener.open(request, timeout=60)) as response:
        if response.status != 200 or not response.geturl().startswith(
            "https://data1b.fullyjustified.net/"
        ):
            raise BundleError("Unexpected official distribution response")
        return pack(
            response, destination, SOURCE_SHA256, progress=progress, stop=stop
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = download(
        args.destination,
        progress=lambda files, size: print(
            json.dumps(
                {"event": "bundle.progress", "files": files, "bytes": size}
            ),
            flush=True,
        ),
    )
    print(
        json.dumps(
            {
                "event": "bundle.ready",
                "files": result.files,
                "sha256": result.sha256,
            }
        )
    )


if __name__ == "__main__":
    main()
