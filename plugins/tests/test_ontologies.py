"""Packaged ontologies are immutable data; they never select tenant types."""

import json
import unittest

from galadril_plugins import PluginRegistry
from galadril_plugins.ontologies import OntologyPackage


class Packages(unittest.TestCase):
    def test_mail_package_has_separate_linked_modules(self) -> None:
        packages = PluginRegistry.discover().ontology_packages()
        mail = next(p for p in packages if p["id"] == "utils.mail")
        self.assertEqual(len(mail["ontologies"]), 5)
        resources = {
            r["resource_id"]: r
            for o in mail["ontologies"]
            for r in o["resources"]
        }
        for role in ("sender", "to", "cc", "bcc"):
            self.assertEqual(
                resources["mail.message." + role]["attributes"]["target_type"],
                "mail.person",
            )
        self.assertEqual(
            resources["mail.message.bcc"]["attributes"]["visibility"],
            "mailbox_owner",
        )
        self.assertEqual(
            resources["mail.message.attachments"]["attributes"]["target_type"],
            "document.attachment",
        )
        self.assertFalse(
            any(identifier.startswith("tenant.") for identifier in resources)
        )

    def test_contracts_cannot_name_unknown_resources(self) -> None:
        with self.assertRaises(ValueError):
            OntologyPackage.model_validate(
                {
                    "id": "fixture.mail",
                    "name": "Mail",
                    "version": "1",
                    "ontologies": [{"resources": []}],
                    "contracts": {
                        "fixture.input.mail": {
                            "mode": "required",
                            "resources": ["missing.type"],
                        }
                    },
                }
            )

    def package(self, modules: list[dict[str, object]]) -> OntologyPackage:
        return OntologyPackage.model_validate(
            {
                "id": "fixture.mail",
                "name": "Mail",
                "version": "1",
                "ontologies": modules,
            }
        )

    def test_exact_serialized_byte_boundary(self) -> None:
        seed = self.package([{"resources": [], "padding": ""}])
        size = len(json.dumps(seed.model_dump(mode="json")).encode())
        for delta in (-1, 0, 1):
            modules = [
                {"resources": [], "padding": "a" * (512 * 1024 - size + delta)}
            ]
            with self.subTest(delta=delta):
                if delta > 0:
                    with self.assertRaises(ValueError):
                        self.package(modules)
                else:
                    result = self.package(modules)
                    self.assertEqual(
                        len(
                            json.dumps(result.model_dump(mode="json")).encode()
                        ),
                        512 * 1024 + delta,
                    )

    def test_per_module_and_package_resource_boundaries(self) -> None:
        def resources(start: int, count: int) -> list[dict[str, str]]:
            return [
                {"resource_id": f"fixture.resource{i}"}
                for i in range(start, start + count)
            ]

        for count in (255, 256):
            self.package([{"resources": resources(0, count)}])
        with self.assertRaises(ValueError):
            self.package([{"resources": resources(0, 257)}])
        self.package(
            [
                {"resources": resources(0, 256)},
                {"resources": resources(256, 256)},
            ]
        )
        with self.assertRaises(ValueError):
            self.package(
                [
                    {"resources": resources(0, 256)},
                    {"resources": resources(256, 256)},
                    {"resources": resources(512, 1)},
                ]
            )

    def test_malformed_and_duplicate_resources_are_rejected(self) -> None:
        for value in (None, "not-a-list", {"resource_id": "fixture.person"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.package([{"resources": value}])
        for value in (
            None,
            "not-an-object",
            {},
            {"resource_id": None},
            {"resource_id": 42},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.package([{"resources": [value]}])
        with self.assertRaises(ValueError):
            self.package(
                [
                    {"resources": [{"resource_id": "fixture.person"}]},
                    {"resources": [{"resource_id": "fixture.person"}]},
                ]
            )


if __name__ == "__main__":
    unittest.main()
