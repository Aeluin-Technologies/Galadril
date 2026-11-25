"""Plugin sources carry contracts without implementing a second GOL compiler."""

import unittest

from galadril_plugins import PluginRegistry
from galadril_plugins.ontologies import (
    GolPackageManifest,
    GolSource,
    OntologyPackage,
)
from pydantic import ValidationError


class Packages(unittest.TestCase):
    def test_catalog_ships_sources_instead_of_a_second_ontology_model(
        self,
    ) -> None:
        packages = PluginRegistry.discover().ontology_packages()
        self.assertEqual(len(packages), 9)
        for package in packages:
            with self.subTest(package=package["id"]):
                self.assertNotIn("ontologies", package)
                self.assertNotIn("imports", package)
                self.assertTrue(package["files"])
                for source in package["files"]:
                    self.assertTrue(source["path"].endswith(".gol"))
                    self.assertIn("ontology ", source["text"])
                    if package["id"] != "law.contentieux":
                        self.assertIn(
                            'resource: "gol_resource";', source["text"]
                        )

    def test_mail_contracts_and_mapping_metadata_remain_explicit(self) -> None:
        mail = next(
            package
            for package in PluginRegistry.discover().ontology_packages()
            if package["id"] == "communication.mail"
        )
        self.assertEqual(
            mail["contracts"]["utils.input.email"]["targets"],
            ["communication.mail::Message"],
        )
        self.assertIn("communication.mail::MessageBcc", mail["bindings"])
        self.assertEqual(
            mail["bindings"]["communication.mail::MessageBcc"]["visibility"],
            "mailbox_owner",
        )
        self.assertIn(
            'import type gal.time::Instant version "1.0.0";',
            mail["files"][0]["text"],
        )

    def test_law_contracts_preserve_restricted_fields_and_dependency_imports(
        self,
    ) -> None:
        packages = PluginRegistry.discover().ontology_packages()
        socle = next(item for item in packages if item["id"] == "law.socle")
        self.assertIn("classification: Restricted;", socle["files"][0]["text"])
        self.assertIn("compartments: [Legal];", socle["files"][0]["text"])
        admin = next(
            item for item in packages if item["id"] == "law.administratif"
        )
        self.assertIn("@label(LegalData)", admin["files"][0]["text"])
        self.assertIn(
            'import law.contentieux version "0.4.0" as LawContentieux;',
            admin["files"][0]["text"],
        )
        self.assertNotIn("extends", admin["files"][0]["text"])

    def test_catalog_is_deterministic(self) -> None:
        registry = PluginRegistry.discover()
        self.assertEqual(
            registry.ontology_packages(), registry.ontology_packages()
        )

    def test_source_paths_cannot_escape_packaged_data(self) -> None:
        for path in (
            "../secret.gol",
            "/tmp/a.gol",
            "folder//a.gol",
            "old.json",
            "a\\b.gol",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                GolSource(path=path, text="ontology demo version 1 {}")

    def test_legacy_semantic_packages_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            OntologyPackage.model_validate(
                {
                    "id": "sample.core",
                    "name": "sample",
                    "version": "1",
                    "ontologies": [],
                }
            )
        with self.assertRaises(ValidationError):
            GolPackageManifest(
                id="sample.core",
                name="sample",
                version="1",
                files=("a.gol", "a.gol"),
            )


if __name__ == "__main__":
    unittest.main()
