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
            "Choisir l’acte dans draft.tex. Le compilateur renseigne information.tex."
        ),
        "execution_context": "law.administratif",
        "entrypoint": "main.tex",
        "assets": assets,
        "variables": {
            key: {"kind": "text", "required": required}
            for key, required in (
                ("project_administration_name", True),
                ("project_administration_address", True),
                ("project_administration_representative", True),
                ("project_court_name", True),
                ("project_court_registry_address", True),
                ("project_case_number", False),
                ("project_signature_city", False),
                ("profile_first_name", True),
                ("profile_last_name", True),
                ("profile_address", True),
                ("profile_title", False),
                ("profile_email", False),
                ("profile_capacity", False),
            )
        },
    }
    root.joinpath("recoursadmin.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
