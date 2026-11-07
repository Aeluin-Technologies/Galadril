"""Packaged ontologies are immutable data; they never select tenant types."""

import json
import unittest
from importlib.resources import files

from galadril_plugins import PluginRegistry
from galadril_plugins.ontologies import OntologyPackage


class Packages(unittest.TestCase):
    def test_package_imports_deduplicate_transitive_layers_and_reject_cycles(
        self,
    ) -> None:
        from galadril_plugins.ontologies import resolve_ontology_packages

        def package(
            name: str, imports: tuple[str, ...] = ()
        ) -> OntologyPackage:
            return OntologyPackage.model_validate(
                {
                    "id": name,
                    "name": name,
                    "version": "1",
                    "imports": [
                        {"id": item, "version": "1"} for item in imports
                    ],
                    "ontologies": [
                        {
                            "id": name,
                            "version": "1",
                            "resources": [
                                {
                                    "resource_id": name + ".entity",
                                    "kind": "object_type",
                                    "display_name": name,
                                    "references": [],
                                    "attributes": {},
                                }
                            ],
                        }
                    ],
                }
            )

        core = package("sample.core")
        writing = package("sample.writing", (core.id,))
        reasoning = package("sample.reasoning", (core.id, writing.id))
        result = resolve_ontology_packages((core, writing, reasoning))
        self.assertEqual(
            [module["id"] for module in result[-1].ontologies],
            [core.id, writing.id, reasoning.id],
        )
        self.assertEqual(result[-1].ontologies[0]["package_id"], core.id)
        for invalid in (
            (package("sample.a", ("sample.missing",)),),
            (
                package("sample.a", ("sample.b",)),
                package("sample.b", ("sample.a",)),
            ),
            (package("sample.a", ("foreign.core",)), package("foreign.core")),
            (core.model_copy(update={"version": "2"}), writing),
        ):
            with self.subTest(invalid=[item.id for item in invalid]):
                with self.assertRaises(ValueError):
                    resolve_ontology_packages(invalid)

    def test_law_core_is_shared_without_domain_version_objects(self) -> None:
        root = files("galadril_plugins.law")
        common = json.loads(
            root.joinpath("contentieux-ontology.json").read_text()
        )
        core = {
            resource["resource_id"]
            for module in next(
                item
                for item in PluginRegistry.discover().ontology_packages()
                if item["id"] == "law.contentieux"
            )["ontologies"]
            for resource in module["resources"]
        }
        self.assertNotIn("law.objection_version", core)
        self.assertNotIn("law.plea_version", core)
        self.assertNotIn("law.provision_version", core)
        self.assertNotIn("law.deadline_adjustment", core)
        self.assertLess(len(core), 160)
        self.assertIn("law.party.name", core)
        self.assertIn("law.party.public_postal_address", core)
        self.assertIn("law.party.representative", core)
        for branch in ("administratif", "judiciaire", "penal"):
            package = json.loads(
                root.joinpath(branch + "-ontology.json").read_text()
            )
            self.assertEqual(
                package["imports"],
                [{"id": "law.contentieux", "version": common["version"]}],
            )
            own = {
                resource["resource_id"]
                for module in package["ontologies"]
                for resource in module["resources"]
            }
            self.assertFalse(core.intersection(own))

    def test_law_views_exclude_other_branches_and_dangling_resources(
        self,
    ) -> None:
        packages = {
            item["id"]: item
            for item in PluginRegistry.discover().ontology_packages()
        }
        branches = {"administratif", "judiciaire", "penal"}
        for branch in branches:
            with self.subTest(branch=branch):
                package = packages[f"law.{branch}"]
                resources = {
                    item["resource_id"]: item
                    for module in package["ontologies"]
                    for item in module["resources"]
                }
                self.assertIn("law.filing", resources)
                self.assertNotIn("law.petition", resources)
                self.assertNotIn("law.brief", resources)
                self.assertIn(f"law.{branch}.proceeding", resources)
                for other in branches - {branch}:
                    self.assertFalse(
                        any(
                            key.startswith(f"law.{other}.") for key in resources
                        )
                    )
                self.assertEqual(
                    "law.administrative_act" in resources,
                    branch == "administratif",
                )
                courts = resources[f"law.{branch}.court.kind"]["attributes"][
                    "allowed_values"
                ]
                self.assertEqual(
                    "conseil_etat" in courts, branch == "administratif"
                )
                self.assertEqual("cour_assises" in courts, branch == "penal")
                self.assertEqual(
                    "conseil_prudhommes" in courts, branch == "judiciaire"
                )
                self.assertEqual(
                    "law.algorithm.dossier" in package["contracts"],
                    branch == "administratif",
                )
                for resource in resources.values():
                    self.assertGreaterEqual(
                        resources.keys(), set(resource["references"])
                    )
                    self.assertGreaterEqual(
                        resources.keys(),
                        set(resource["attributes"].get("extends", [])),
                    )
                    if owner := resource.get("owner_id"):
                        self.assertIn(owner, resources)

    def test_law_layers_link_writings_grounds_citations_and_sources(
        self,
    ) -> None:
        package = next(
            item
            for item in PluginRegistry.discover().ontology_packages()
            if item["id"] == "law.contentieux"
        )
        self.assertEqual(
            {module["id"] for module in package["ontologies"]},
            {"law.socle", "law.ecritures", "law.moyens", "law.preuves"},
        )
        resources = {
            item["resource_id"]: item
            for module in package["ontologies"]
            for item in module["resources"]
        }
        for key, source, target in (
            ("law.case.proceedings", "law.case", "law.proceeding"),
            ("law.proceeding.court", "law.proceeding", "law.court"),
            ("law.filing.proceeding", "law.filing", "law.proceeding"),
            ("law.filing.author", "law.filing", "law.party"),
            ("law.filing.pleas", "law.filing", "law.plea"),
            ("law.plea.provisions", "law.plea", "law.provision"),
            ("law.plea.jurisprudence", "law.plea", "law.judicial_decision"),
            ("law.plea.exhibits", "law.plea", "law.exhibit"),
            ("law.exhibit.document", "law.exhibit", "law.document"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    resources[key]["attributes"]["source_type"], source
                )
                self.assertEqual(
                    resources[key]["attributes"]["target_type"], target
                )
        self.assertEqual(
            resources["law.judicial_decision.disposition"]["owner_id"],
            "law.judicial_decision",
        )
        self.assertFalse(
            any("_version" in key or "temporal" in key for key in resources)
        )
        self.assertTrue(
            all(
                module["package_id"] == module["id"]
                for module in package["ontologies"]
            )
        )

    def test_law_branches_reuse_common_types_and_scope_administrative_skills(
        self,
    ) -> None:
        registry = PluginRegistry.discover()
        packages = {item["id"]: item for item in registry.ontology_packages()}
        for branch in ("administratif", "judiciaire", "penal"):
            resources = {
                item["resource_id"]: item
                for module in packages[f"law.{branch}"]["ontologies"]
                for item in module["resources"]
            }
            self.assertEqual(
                resources[f"law.{branch}.proceeding"]["attributes"]["extends"],
                ["law.proceeding"],
            )
            self.assertEqual(
                resources[f"law.{branch}.proceeding.court"]["attributes"][
                    "target_type"
                ],
                f"law.{branch}.court",
            )
        manifest = json.loads(
            files("galadril_plugins.law")
            .joinpath("plugin.json")
            .read_text(encoding="utf-8")
        )
        skills = [
            skill
            for skill in registry.skills()
            if skill.capability.plugin_id == "law"
        ]
        self.assertEqual(len(skills), 9)
        for skill in skills:
            self.assertTrue(
                manifest["bindings"][skill.capability.id]["skill"].startswith(
                    "skills/administratif/"
                )
            )

    def test_law_package_declares_dossier_constraints_and_binding(self) -> None:
        registry = PluginRegistry.discover()
        package = next(
            p
            for p in registry.ontology_packages()
            if p["id"] == "law.contentieux"
        )
        ontology = OntologyPackage.model_validate(
            {
                key: value
                for key, value in package.items()
                if key not in {"plugin_id", "plugin_version"}
            }
        )
        resources = {
            resource["resource_id"]: resource
            for module in ontology.ontologies
            for resource in module["resources"]
        }
        self.assertEqual(
            resources["law.filing.proceeding"]["attributes"]["max_count"], 1
        )
        self.assertEqual(
            resources["law.proceeding.court"]["attributes"]["min_count"], 1
        )
        for section in ("subject", "facts", "discussion", "conclusions"):
            self.assertIn(f"law.filing.{section}", resources)
        contract = ontology.contracts["law.algorithm.dossier"]
        self.assertEqual(contract.mode, "required")
        self.assertIn("law.plea.jurisprudence", contract.resources)

    def test_law_dossier_accepts_one_petition_and_rejects_inconsistent_evidence(
        self,
    ) -> None:
        from copy import deepcopy

        from galadril_plugins.law.dossier import Dossier
        from galadril_plugins.law.writings import render_filing
        from pydantic import ValidationError

        dossier = {
            "id": "case",
            "parties": [
                {"id": "administration", "name": "Administration d’exemple"}
            ],
            "proceedings": [
                {
                    "id": "merits",
                    "court": "Tribunal administratif",
                    "regime": "fond",
                    "defendant_ids": ["administration"],
                },
                {
                    "id": "urgent",
                    "court": "Tribunal administratif",
                    "regime": "refere_suspension",
                    "defendant_ids": ["administration"],
                },
            ],
            "links": [
                {
                    "source_id": "urgent",
                    "target_id": "merits",
                    "kind": "suspends",
                }
            ],
            "petition": {
                "id": "request",
                "proceeding_id": "merits",
                "subject": "Acte contesté",
                "facts": "Faits à 50 %",
                "discussion": "Moyen vérifié",
                "relief": "Annulation demandée",
            },
            "briefs": [
                {
                    "id": "defence",
                    "proceeding_id": "merits",
                    "kind": "defense",
                    "body": "Argument en défense",
                },
                {
                    "id": "response",
                    "proceeding_id": "merits",
                    "kind": "replique",
                    "body": "Réponse",
                    "responds_to": "defence",
                    "exhibit_ids": ["notice"],
                },
            ],
            "exhibits": [
                {
                    "id": "notice",
                    "title": "Notification",
                    "source_ref": "notice-1",
                }
            ],
            "events": [
                {
                    "id": "received",
                    "proceeding_id": "merits",
                    "kind": "notification",
                    "exhibit_ids": ["notice"],
                }
            ],
        }
        parsed = Dossier.model_validate(dossier)
        self.assertEqual(len(parsed.briefs), 2)
        self.assertEqual(parsed.links[0].target_id, "merits")
        rendered = render_filing(parsed, "request")
        self.assertIn(r"Faits à 50 \%", rendered)
        self.assertIn("Administration d'exemple", rendered)
        self.assertIn(r"\ParCesMotifs", rendered)
        self.assertIn(
            r"\TypeMemoire{replique}", render_filing(parsed, "response")
        )
        self.assertIn(
            r"\DeclarerPiece{1}{Notification}",
            render_filing(parsed, "response"),
        )
        for section, replacement in (
            ("proceedings", []),
            ("parties", []),
            ("exhibits", []),
            ("petition", [dossier["petition"], dossier["petition"]]),
            (
                "links",
                [
                    {
                        "source_id": "merits",
                        "target_id": "urgent",
                        "kind": "suspends",
                    }
                ],
            ),
        ):
            with (
                self.subTest(section=section),
                self.assertRaises(ValidationError),
            ):
                Dossier.model_validate({**dossier, section: replacement})
        incomplete = deepcopy(dossier)
        incomplete["petition"]["relief"] = " "
        with self.assertRaises(ValidationError):
            Dossier.model_validate(incomplete)

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
