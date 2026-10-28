"""Private state is never written through symlinks or partially published."""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


class BootstrapError(RuntimeError):
    """Expose controlled operational failures without raw credentials."""


def directory(path: Path) -> None:
    """Reject redirected state before tightening its access permissions."""
    if path.is_symlink():
        raise BootstrapError(
            "Development state must not contain symlinked directories"
        )
    if not path.exists():
        directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise BootstrapError("Expected a development state directory")


def private_directory(path: Path) -> None:
    directory(path)
    path.chmod(0o700)


def write(path: Path, data: bytes, *, public: bool = False) -> None:
    """Atomic replacement prevents readers from observing half-written keys."""
    if path.is_symlink():
        raise BootstrapError("Refusing a symlinked development file")
    private_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=".prepare-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644 if public else 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> bytes:
    """A copied state directory must not redirect secret reads elsewhere."""
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("Development file is missing or symlinked")
    return path.read_bytes()


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Serialize setup across processes before reading or creating private keys."""
    if path.is_symlink():
        raise BootstrapError("Development lock must not be symlinked")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
