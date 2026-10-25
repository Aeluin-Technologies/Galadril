"""Create deployment secrets once without printing their values."""

import argparse
import base64
import json
import os
import secrets
from pathlib import Path


def initialize(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, value in {
        "service-key": secrets.token_urlsafe(48) + "\n",
        "tracking-key": secrets.token_urlsafe(48) + "\n",
        "keyring.json": json.dumps(
            {
                "active": "v1",
                "keys": {
                    "v1": base64.b64encode(secrets.token_bytes(32)).decode(
                        "ascii"
                    )
                },
            }
        )
        + "\n",
    }.items():
        fd = os.open(
            directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(fd, "w") as target:
            target.write(value)


def initialize_tracking(directory: Path) -> None:
    """Add capture authority without rotating keys protecting existing mail."""
    if not directory.is_dir():
        raise FileNotFoundError("Initialize the email private directory first")
    fd = os.open(
        directory / "tracking-key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(fd, "w") as target:
        target.write(secrets.token_urlsafe(48) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create private keys for the official utils email service."
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--tracking-only",
        action="store_true",
        help="Add only a missing capture key to an existing installation",
    )
    arguments = parser.parse_args()
    if arguments.tracking_only:
        initialize_tracking(arguments.directory)
    else:
        initialize(arguments.directory)
    print(
        "Private email service keys created. Configure the service before connecting accounts."
    )


if __name__ == "__main__":
    main()
