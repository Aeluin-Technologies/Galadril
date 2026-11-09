"""Store MCP provider credentials outside generated Compose configuration."""

from __future__ import annotations

import getpass
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from .files import BootstrapError, write


@dataclass(frozen=True, slots=True)
class Credential:
    filename: str
    variable: str
    target: str
    fields: tuple[tuple[str, str], ...]


CREDENTIALS = {
    "piste-oauth": Credential(
        "piste-oauth-key",
        "LAW_PISTE_OAUTH_FILE",
        "/run/secrets/piste-oauth",
        (
            ("CLIENT_ID", "PISTE OAuth client ID"),
            ("SECRET_KEY", "PISTE OAuth client secret"),
        ),
    ),
    "piste-api": Credential(
        "piste-key",
        "LAW_PISTE_CREDENTIALS_FILE",
        "/run/secrets/piste-key",
        (("API_KEY", "PISTE API key for Judilibre"),),
    ),
    "librejustice": Credential(
        "librejustice-key",
        "LIBREJUSTICE_API_KEY_FILE",
        "/run/secrets/librejustice-key",
        (("", "LibreJustice API key"),),
    ),
}


def private_file(path: Path) -> stat.StatResult | None:
    """Inspect the mount source without following links or reading key material."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        return None
    return info


def _read_fields(fields: tuple[tuple[str, str], ...]) -> bytes | None:
    """Abort before writing when an entry is incomplete or cannot be masked."""
    lines: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        for name, label in fields:
            try:
                value = getpass.getpass(f"{label} (hidden, blank cancels): ")
            except (getpass.GetPassWarning, EOFError):
                raise BootstrapError(
                    "A private terminal is required for credential entry"
                ) from None
            if not value:
                return None
            if len(value) > 2048 or any(
                not "!" <= char <= "~" for char in value
            ):
                raise BootstrapError(
                    "Credential must be a single printable token"
                )
            lines.append(f"{name}={value}" if name else value)
    return ("\n".join(lines) + "\n").encode()


def configure(directory: Path, console: Console) -> None:
    """Let the operator add only the provider credentials available to them."""
    for name, credential in CREDENTIALS.items():
        status = (
            "configured"
            if private_file(directory / credential.filename)
            else "unconfigured"
        )
        console.print(f"{name}: {status}")
    while (
        choice := Prompt.ask(
            "MCP credential",
            choices=[*CREDENTIALS, "done"],
            default="done",
            console=console,
        )
    ) != "done":
        credential = CREDENTIALS[choice]
        destination = directory / credential.filename
        if destination.is_symlink():
            raise BootstrapError("Refusing a symlinked credential file")
        if destination.exists() and not Confirm.ask(
            "Replace the existing credential?", default=False, console=console
        ):
            continue
        if (data := _read_fields(credential.fields)) is not None:
            write(destination, data)
            console.print(f"Saved {choice} credentials.")
