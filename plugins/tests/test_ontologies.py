"""Packaged ontologies are immutable data; they never select tenant types."""

import json
import unittest
from importlib.resources import files

from galadril_plugins import PluginRegistry
from galadril_plugins.ontologies import OntologyPackage


class Packages(unittest.TestCase):
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
                self.assertIn("law.brief", resources)
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
                self.assertEqual(
                    "law.suspension_request" in resources,
                    branch == "administratif",
                )
                self.assertEqual(
                    "law.court.judicial" in resources, branch != "administratif"
                )
                if branch != "administratif":
                    courts = resources["law.court.judicial.kind"]["attributes"][
                        "allowed_values"
                    ]
                    self.assertIn("cour_cassation", courts)
                    self.assertEqual(
                        "cour_assises" in courts, branch == "penal"
                    )
                    self.assertEqual(
                        "conseil_de_prud_hommes" in courts,
                        branch == "judiciaire",
                    )
                self.assertEqual(
                    "recours_administratif"
                    in resources["law.deadline.kind"]["attributes"][
                        "allowed_values"
                    ],
                    branch == "administratif",
                )
                self.assertEqual(
                    "law.algorithm.dossier" in package["contracts"],
                    branch == "administratif",
                )
                for resource in resources.values():
                    self.assertTrue(
                        resources.keys() >= set(resource["references"])
                    )
                    self.assertTrue(
                        resources.keys()
                        >= set(resource["attributes"].get("extends", []))
                    )
                    if owner := resource.get("owner_id"):
                        self.assertIn(owner, resources)

    def test_law_package_links_source_evidence_to_disposition_and_execution(
        self,
    ) -> None:
        package = OntologyPackage.model_validate(
            {
                key: value
                for key, value in next(
                    item
                    for item in PluginRegistry.discover().ontology_packages()
                    if item["id"] == "law.contentieux"
                ).items()
                if key not in {"plugin_id", "plugin_version"}
            }
        )
        self.assertEqual(
            {module["id"] for module in package.ontologies},
            {
                "law.procedure",
                "law.ecritures",
                "law.evenements",
                "law.actes",
                "law.faits",
                "law.normes",
                "law.moyens",
                "law.preuves",
                "law.decisions",
                "law.delais",
                "law.execution",
                "law.administratif",
                "law.judiciaire",
                "law.penal",
            },
        )
        resources = {
            item["resource_id"]: item
            for module in package.ontologies
            for item in module["resources"]
        }
        for name, source, target, minimum, maximum in (
            ("law.filing.proceeding", "law.filing", "law.proceeding", 1, 1),
            (
                "law.proceeding.petitions",
                "law.proceeding",
                "law.petition",
                0,
                None,
            ),
            (
                "law.proceeding.challenged_acts",
                "law.proceeding",
                "law.administrative_act",
                0,
                None,
            ),
            (
                "law.act_documentation.document_version",
                "law.act_documentation",
                "law.document_version",
                1,
                1,
            ),
            (
                "law.evidence_relation.assertion",
                "law.evidence_relation",
                "law.assertion",
                1,
                1,
            ),
            (
                "law.evidence_relation.passage",
                "law.evidence_relation",
                "law.passage",
                1,
                1,
            ),
            (
                "law.qualification.assertions",
                "law.qualification",
                "law.assertion",
                1,
                None,
            ),
            (
                "law.qualification.rules",
                "law.qualification",
                "law.legal_rule",
                1,
                None,
            ),
            (
                "law.plea_version.qualifications",
                "law.plea_version",
                "law.qualification",
                0,
                None,
            ),
            (
                "law.objection_version.target",
                "law.objection_version",
                "law.position_version",
                1,
                1,
            ),
            (
                "law.plea_ruling.plea",
                "law.plea_ruling",
                "law.plea_version",
                1,
                1,
            ),
            (
                "law.disposition_item.effects",
                "law.disposition_item",
                "law.dispositive_effect",
                0,
                None,
            ),
            (
                "law.proceeding_closure.disposition",
                "law.proceeding_closure",
                "law.disposition",
                0,
                1,
            ),
            (
                "law.obligation.effect",
                "law.obligation",
                "law.dispositive_effect",
                1,
                1,
            ),
            (
                "law.review_challenge.challenged_decision",
                "law.review_challenge",
                "law.judicial_decision",
                1,
                1,
            ),
            (
                "law.suspension_effect.act",
                "law.suspension_effect",
                "law.administrative_act",
                1,
                1,
            ),
        ):
            with self.subTest(name=name):
                attributes = resources[name]["attributes"]
                self.assertEqual(
                    (
                        attributes["source_type"],
                        attributes["target_type"],
                        attributes["min_count"],
                        attributes.get("max_count"),
                    ),
                    (source, target, minimum, maximum),
                )
                self.assertEqual(
                    set(resources[name]["references"]), {source, target}
                )
        for item in resources.values():
            if item["kind"] != "link_type":
                continue
            attributes = item["attributes"]
            minimum = attributes["min_count"]
            maximum = attributes.get("max_count")
            endpoints = {attributes["source_type"], attributes["target_type"]}
            self.assertEqual(set(item["references"]), endpoints)
            for endpoint in endpoints:
                self.assertIn(
                    resources[endpoint]["kind"], {"object_type", "event_type"}
                )
            self.assertIs(type(minimum), int)
            self.assertGreaterEqual(minimum, 0)
            if maximum is not None:
                self.assertIs(type(maximum), int)
                self.assertGreaterEqual(maximum, minimum)
        for item in resources.values():
            for replacement in item["attributes"].get("superseded_by", []):
                self.assertIn(replacement, resources)
        self.assertEqual(
            resources["law.disposition.items"]["attributes"][
                "inverse_max_count"
            ],
            1,
        )
        self.assertEqual(
            resources["law.case.petition"]["attributes"]["max_count"], 1
        )
        self.assertNotIn(
            "law.judicial_decision",
            package.contracts["law.algorithm.dossier"].resources,
        )

    def test_law_branches_reuse_common_types_and_scope_administrative_skills(
        self,
    ) -> None:
        registry = PluginRegistry.discover()
        package = next(
            item
            for item in registry.ontology_packages()
            if item["id"] == "law.contentieux"
        )
        modules = {
            module["id"]: {
                item["resource_id"]: item for item in module["resources"]
            }
            for module in package["ontologies"]
        }
        for branch in ("administratif", "judiciaire", "penal"):
            module = modules[f"law.{branch}"]
            proceeding = module[f"law.{branch}.proceeding"]
            self.assertEqual(
                proceeding["attributes"]["extends"], ["law.proceeding"]
            )
            court = module[f"law.{branch}.proceeding.court"]
            self.assertEqual(
                court["attributes"]["target_type"],
                "law.administratif.court"
                if branch == "administratif"
                else "law.court.judicial",
            )
        self.assertEqual(
            modules["law.ecritures"]["law.brief"]["attributes"]["extends"],
            ["law.filing"],
        )
        self.assertEqual(package["name"], "French litigation")
        self.assertEqual(
            modules["law.ecritures"]["law.brief"]["display_name"], "Mémoire"
        )
        self.assertEqual(
            modules["law.decisions"]["law.disposition"]["display_name"],
            "Dispositif of the judicial decision",
        )
        self.assertIn(
            "tribunal_correctionnel",
            modules["law.penal"]["law.penal.proceeding.formation"][
                "attributes"
            ]["allowed_values"],
        )
        self.assertEqual(
            modules["law.administratif"][
                "law.administratif.proceeding.defendants"
            ]["attributes"]["min_count"],
            1,
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
            resources["law.case.petition"]["attributes"]["max_count"], 1
        )
        self.assertEqual(
            resources["law.case.proceedings"]["attributes"]["min_count"], 1
        )
        self.assertEqual(
            resources["law.proceeding.defendants"]["attributes"]["min_count"], 0
        )
        self.assertEqual(
            resources["law.proceeding.suspends"]["attributes"]["target_type"],
            "law.proceeding",
        )
        for section in ("subject", "facts", "discussion", "relief"):
            self.assertTrue(
                resources[f"law.petition.{section}"]["attributes"]["required"]
            )
        contract = ontology.contracts["law.algorithm.dossier"]
        self.assertEqual(contract.mode, "required")
        self.assertIn("law.proceeding.suspends", contract.resources)

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
