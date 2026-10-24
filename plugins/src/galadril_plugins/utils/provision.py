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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create private keys for the official utils email service."
    )
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    initialize(arguments.directory)
    print(
        "Private email service keys created. Configure the service before connecting accounts."
    )


if __name__ == "__main__":
    main()
