"""Build a portable Galadril plugin archive using the server's skill sources."""

import argparse
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from galadril_mcp_service.skills import (
    CLIENT_ROOT,
    legal_root,
    skill_files,
    skill_folders,
)

DEFAULT_CHATGPT_APP_ID = "plugin_asdk_app_6af5f40dc3208191b60534eb3ea28b75"


def package_plugin(
    output: Path, *, chatgpt_app_id: str | None = DEFAULT_CHATGPT_APP_ID
) -> None:
    """Keep archive paths relative to the plugin manifest and reproducible."""
    app_id = chatgpt_app_id.removeprefix("plugin_") if chatgpt_app_id else None
    if chatgpt_app_id is not None and (
        app_id is None
        or not re.fullmatch(r"asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*", app_id)
    ):
        raise ValueError("Expected a registered ChatGPT app ID (asdk_app_…).")

    manifest = json.loads((CLIENT_ROOT / "plugin.json").read_bytes())
    mcp = json.loads((CLIENT_ROOT / "mcp.json").read_bytes())
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:

        def write(name: str, data: bytes) -> None:
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)

        def write_json(name: str, data: dict[str, object]) -> None:
            write(name, (json.dumps(data, indent=2) + "\n").encode())

        if app_id is not None:
            extension = manifest["extensions"]["com.openai"]
            extension["apps"] = "./.app.json"
            write_json(
                ".app.json",
                {
                    "apps": {
                        f"dev-{app_id.removeprefix('asdk_app_')}": {
                            "id": app_id
                        }
                    }
                },
            )
            write_json(
                ".codex-plugin/plugin.json",
                {
                    "name": manifest["name"],
                    "version": manifest["version"],
                    "description": manifest["description"],
                    "author": manifest["author"],
                    "interface": {
                        **extension["interface"],
                        "longDescription": manifest["description"],
                        "capabilities": [],
                    },
                    "skills": "./skills",
                    "apps": "./.app.json",
                    "mcpServers": "./.mcp.json",
                },
            )
            write_json(".mcp.json", {"mcpServers": mcp["mcpServers"]})
        write_json("plugin.json", manifest)
        write_json("mcp.json", mcp)
        write(
            "assets/galadril.png",
            (CLIENT_ROOT / "assets/galadril.png").read_bytes(),
        )
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--chatgpt-app-id",
        default=DEFAULT_CHATGPT_APP_ID,
        help="Registered ChatGPT app ID or its plugin_ form.",
    )
    mode.add_argument(
        "--portable", action="store_true", help="Omit the hosted app binding."
    )
    args = parser.parse_args()
    try:
        package_plugin(
            args.output,
            chatgpt_app_id=None if args.portable else args.chatgpt_app_id,
        )
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
