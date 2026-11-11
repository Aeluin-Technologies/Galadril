"""Build a portable Galadril plugin archive using the server's skill sources."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from galadril_mcp_service.skills import (
    CLIENT_ROOT,
    legal_root,
    skill_files,
    skill_folders,
)


def package_plugin(output: Path) -> None:
    """Keep archive paths relative to the plugin manifest and reproducible."""
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:

        def write(name: str, data: bytes) -> None:
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)

        for name in ("plugin.json", "mcp.json", "assets/galadril.png"):
            write(name, (CLIENT_ROOT / name).read_bytes())
        for folder in skill_folders("unified"):
            for relative, source in skill_files(folder):
                write(f"skills/{folder.name}/{relative}", source.read_bytes())
        root = legal_root()
        for name in ("LICENSE.md", "README.md"):
            write(
                f"licenses/librejustice/{name}",
                root.joinpath(name).read_bytes(),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    package_plugin(parser.parse_args().output)


if __name__ == "__main__":
    main()
