"""Packaged template discovery never imports executable plugin bindings."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import galadril_plugins
from galadril_plugins import PluginRegistry


class PackagedTemplatesTest(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("tectonic") and shutil.which("qpdf"),
        "Requires Tectonic and qpdf",
    )
    def test_packaged_filings_compile_with_a_native_signature_field(
        self,
    ) -> None:
        sources = Path(galadril_plugins.__file__).parent / "law" / "latex"
        for draft, field in (
            ("courrier", "galadril:sender"),
            ("memoire", "galadril:claimant"),
            ("requete", "galadril:claimant"),
        ):
            with (
                self.subTest(draft=draft),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                shutil.copytree(sources, root, dirs_exist_ok=True)
                (root / "information.tex").write_text("", encoding="utf-8")
                (root / "draft.tex").write_text(
                    rf"\input{{{draft}}}", encoding="utf-8"
                )
                subprocess.run(
                    ["tectonic", "--only-cached", "--untrusted", "main.tex"],
                    cwd=root,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                pdf = json.loads(
                    subprocess.check_output(
                        [
                            "qpdf",
                            "--json",
                            "--json-key=acroform",
                            root / "main.pdf",
                        ],
                        timeout=30,
                    )
                )
                fields = pdf["acroform"]["fields"]
                self.assertEqual(
                    [
                        (item["fullname"], item["fieldtype"], item["value"])
                        for item in fields
                    ],
                    [(field, "/Sig", None)],
                )
                pages = int(
                    subprocess.check_output(
                        ["qpdf", "--show-npages", root / "main.pdf"], timeout=30
                    )
                )
                self.assertEqual(
                    fields[0]["pageposfrom1"],
                    pages if draft == "courrier" else pages - 1,
                )

    def test_pengolo_receives_the_legal_method_and_reference_material(
        self,
    ) -> None:
        skills = {
            skill.capability.id: skill.instructions
            for skill in PluginRegistry.discover().skills()
        }
        case_law = skills["law.skill.jurisprudence"]
        self.assertIn("case_chronology", case_law)
        self.assertIn("FR.TA.JUGE_REFERES", case_law)
        norms = skills["law.skill.veille"]
        self.assertIn("valid_from", norms)
        self.assertIn("The renumbering blind spot", norms)
        self.assertIn("UNTS", norms)

    def test_law_template_and_specialists_are_discoverable(self) -> None:
        registry = PluginRegistry.discover()
        workers = [
            skill
            for skill in registry.skills()
            if skill.capability.plugin_id == "law"
            and skill.capability.execution_profile.role == "specialist"
        ]
        self.assertEqual(len(workers), 8)
        templates = [
            entry
            for entry in registry.document_templates()
            if entry["plugin_id"] == "law"
        ]
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["execution_context"], "law.administratif")
        template = templates[0]["template"]
        self.assertIsInstance(template, dict)
        assert isinstance(template, dict)
        self.assertEqual(template["scope"], "plugin")
        self.assertEqual(template["plugin"], "law")
        self.assertEqual(template["entrypoint"], "main.tex")
        assets = template["assets"]
        assert isinstance(assets, list)
        sources = Path(galadril_plugins.__file__).parent / "law" / "latex"
        paths = set()
        for asset in assets:
            assert isinstance(asset, dict)
            path, data = asset["path"], asset["data"]
            assert isinstance(path, str) and isinstance(data, list)
            paths.add(path)
            self.assertEqual(data, list(sources.joinpath(path).read_bytes()))
        self.assertEqual(
            paths,
            {
                "recoursadmin.cls",
                "main.tex",
                "information.tex",
                "draft.tex",
                "requete.tex",
                "memoire.tex",
                "courrier.tex",
            },
        )
        skills = {
            skill.capability.id: skill.instructions
            for skill in registry.skills()
            if skill.capability.plugin_id == "law"
        }
        self.assertEqual(
            set(skills),
            {
                "law.skill.coordination",
                "law.skill.raisonnement",
                "law.skill.faits",
                "law.skill.recevabilite",
                "law.skill.jurisprudence",
                "law.skill.veille",
                "law.skill.partie-adverse",
                "law.skill.redaction",
                "law.skill.citations",
                "law.skill.relecture",
            },
        )
        prompt = skills["law.skill.raisonnement"]
        examples = prompt.split("\n### ")[1:]
        self.assertEqual(len(examples), 3)
        self.assertNotIn("### Jean Lessi", skills["law.skill.redaction"])
        for example in examples:
            self.assertEqual(
                sum(line.startswith("> «") for line in example.splitlines()), 1
            )
            self.assertIn(
                "https://www.conseil-etat.fr/fr/arianeweb/CRP/conclusion/",
                example,
            )
        self.assertFalse(
            any(plugin == "law" for plugin, _ in registry.services())
        )

    def test_law_date_is_an_explicit_required_case_binding(self) -> None:
        templates = PluginRegistry.discover().document_templates()
        entry = next(item for item in templates if item["plugin_id"] == "law")
        template = entry["template"]
        assert isinstance(template, dict)
        variables = template["variables"]
        assert isinstance(variables, dict)
        self.assertEqual(
            variables.get("project_document_date"),
            {"kind": "text", "required": True, "choices": []},
        )
        assets = template["assets"]
        assert isinstance(assets, list)
        information = next(
            item
            for item in assets
            if isinstance(item, dict) and item["path"] == "information.tex"
        )
        data = information["data"]
        assert isinstance(data, list)
        self.assertTrue(
            all(isinstance(value, int) and 0 <= value <= 255 for value in data)
        )
        self.assertIn(
            rb"\DateDepot{<<galadril:project_document_date>>}",
            bytes(value for value in data if isinstance(value, int)),
        )

    @unittest.skipUnless(
        shutil.which("tectonic") and shutil.which("qpdf"),
        "Requires Tectonic and qpdf",
    )
    def test_signature_fields_are_optional_named_and_empty(self) -> None:
        sources = Path(galadril_plugins.__file__).parent / "law" / "latex"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(sources / "recoursadmin.cls", root)
            for enabled in (False, True):
                setup = r"\GaladrilEnableSignatureFields" if enabled else ""
                (root / "signature.tex").write_text(
                    r"\documentclass{recoursadmin}"
                    + "\n"
                    + setup
                    + "\n"
                    + r"\begin{document}\SignatureRequerant\newpage"
                    + (
                        r"\GaladrilSignatureSlot{counsel}{136bp}{34bp}"
                        if enabled
                        else "{}Suite"
                    )
                    + r"\end{document}",
                    encoding="utf-8",
                )
                compilation = subprocess.run(
                    [
                        "tectonic",
                        "--only-cached",
                        "--untrusted",
                        "signature.tex",
                    ],
                    cwd=root,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(
                    compilation.returncode,
                    0,
                    compilation.stderr.decode(errors="replace"),
                )
                pdf = subprocess.check_output(
                    [
                        "qpdf",
                        "--json",
                        "--json-key=acroform",
                        root / "signature.pdf",
                    ],
                    timeout=30,
                )
                fields = json.loads(pdf)["acroform"]["fields"]
                self.assertEqual(
                    [
                        (
                            f["fullname"],
                            f["fieldtype"],
                            f["pageposfrom1"],
                            f["value"],
                        )
                        for f in fields
                    ],
                    [
                        ("galadril:claimant", "/Sig", 1, None),
                        ("galadril:counsel", "/Sig", 2, None),
                    ]
                    if enabled
                    else [],
                )

    def test_packaged_classes_are_discovered_with_trusted_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "galadril_plugins"
            shutil.copytree(Path(galadril_plugins.__file__).parent, package)
            shutil.copytree(
                Path(__file__).parent / "fixtures/fixture", package / "fixture"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import sys
from galadril_plugins import PluginRegistry
inventory = [
    entry for entry in PluginRegistry.discover().document_templates()
    if entry['plugin_id'] == 'fixture'
]
assert len(inventory) == 1
entry = inventory[0]
assert entry['plugin_id'] == 'fixture'
assert entry['plugin_version'] == '1.0.0'
assert entry['template']['scope'] == 'plugin'
assert entry['template']['plugin'] == 'fixture'
assert 'owner' not in entry['template']
assert any(asset['path'] == 'letter.cls' for asset in entry['template']['assets'])
assert 'galadril_plugins.fixture.handlers' not in sys.modules
assert 'galadril_plugins.fixture.scribe' not in sys.modules
""",
                ],
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join((directory, *sys.path)),
                },
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )


if __name__ == "__main__":
    unittest.main()
