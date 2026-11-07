"""Build the reviewed catalog resource from the editable LaTeX sources."""

import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    source = root / "latex"
    assets = [
        {"path": path.name, "data": path.read_text(encoding="utf-8")}
        for path in sorted(source.iterdir())
        if path.is_file() and path.suffix in {".tex", ".cls"}
    ]
    template = {
        "name": "Contentieux administratif — recoursadmin",
        "description": (
            "Projet LaTeX pour mémoire, requête et courrier au greffe. "
            "Renseigner information.tex et choisir l'acte dans main.tex."
        ),
        "entrypoint": "main.tex",
        "assets": assets,
    }
    root.joinpath("recoursadmin.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
