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


def package_plugin(output: Path, *, chatgpt_app_id: str | None = None) -> None:
    """Keep archive paths relative to the plugin manifest and reproducible."""
    if chatgpt_app_id is not None and not re.fullmatch(
        r"(?:plugin_)?asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*", chatgpt_app_id
    ):
        raise ValueError(
            "Expected the technical ID of a registered ChatGPT MCP server."
        )

    manifest = (CLIENT_ROOT / "plugin.json").read_bytes()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:

        def write(name: str, data: bytes) -> None:
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)

        if chatgpt_app_id is None:
            write("plugin.json", manifest)
            write("mcp.json", (CLIENT_ROOT / "mcp.json").read_bytes())
        else:
            portable = json.loads(manifest)
            interface = portable["extensions"]["com.openai"]["interface"]
            interface.update(
                longDescription=portable["description"],
                developerName="Aeluin Technologies",
                capabilities=["Read", "Write"],
            )
            native = {
                "name": portable["name"],
                "version": portable["version"],
                "description": portable["description"],
                "author": {"name": interface["developerName"]},
                "interface": interface,
                "skills": "./skills/",
                "apps": "./.app.json",
            }
            write(
                ".codex-plugin/plugin.json",
                json.dumps(native, indent=2).encode() + b"\n",
            )
            apps = {
                "apps": {
                    "galadril": {
                        "id": chatgpt_app_id.removeprefix("plugin_"),
                        "required": True,
                    }
                }
            }
            write(".app.json", json.dumps(apps, indent=2).encode() + b"\n")
        icon = "assets/galadril.png"
        write(icon, (CLIENT_ROOT / icon).read_bytes())
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
    parser.add_argument(
        "--chatgpt-app-id",
        help="Technical ID of a registered ChatGPT MCP server.",
    )
    args = parser.parse_args()
    try:
        package_plugin(args.output, chatgpt_app_id=args.chatgpt_app_id)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
