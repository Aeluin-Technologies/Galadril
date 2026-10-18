"""OCI application layers omit wheel scripts; install the locked uv executable explicitly."""

import shutil
import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    destination, wheel = sys.argv[1:]
    with ZipFile(wheel) as archive:
        matches = [
            name
            for name in archive.namelist()
            if name.endswith(".data/scripts/uv")
        ]
        if len(matches) != 1:
            raise ValueError(
                "Locked uv wheel must contain exactly one executable"
            )
        with (
            archive.open(matches[0]) as source,
            Path(destination).open("wb") as output,
        ):
            shutil.copyfileobj(source, output)
    Path(destination).chmod(0o555)


if __name__ == "__main__":
    main()
