"""Checks invariants that the semantic convention schema cannot express."""

import json
import sys
import unittest
from pathlib import Path

import yaml


class ContractPolicyTest(unittest.TestCase):
    def test_authored_models_use_a_stable_definition_format(self) -> None:
        for model in MODELS:
            if "schema_url" not in model:
                self.assertEqual(model.get("file_format"), "definition/1")

    def test_published_error_codes_and_meanings_are_preserved(self) -> None:
        for code, contract in BASELINE.items():
            self.assertEqual(contract, CATALOG.get(code), code)

    def test_codes_are_unique_and_safe_for_each_transport(self) -> None:
        members = ATTRIBUTES["galadril.error.code"]["type"]["members"]
        self.assertEqual(len(members), len(CATALOG))
        self.assertEqual(
            len(members), len({member["id"] for member in members})
        )
        for code, contract in CATALOG.items():
            self.assertRegex(code, r"^[a-z][a-z0-9_]*$")
            self.assertIn(contract["http"], range(400, 600))
            self.assertEqual(contract["graphql"], code)
            self.assertRegex(contract["grpc"], r"^[A-Z_]+$")
            self.assertTrue(contract["category"])
            self.assertTrue(contract["meaning"])
            self.assertIn(
                contract["confidentiality"], {"protected_context", "code_only"}
            )

    def test_business_span_names_are_static(self) -> None:
        for span in (group for group in GROUPS if group["type"] == "span"):
            self.assertRegex(
                span["id"], r"^span\.galadril\.[a-z_]+\.[a-z_]+(?:\.[a-z_]+)?$"
            )
            self.assertNotIn("name", span)

    def test_metrics_accept_only_bounded_operational_values(self) -> None:
        for metric in (group for group in GROUPS if group["type"] == "metric"):
            for ref in metric["attributes"]:
                attribute = ATTRIBUTES[ref["ref"]]
                self.assertEqual(
                    attribute["annotations"]["galadril.cardinality"],
                    "bounded",
                )
                self.assertEqual(
                    attribute["annotations"]["galadril.classification"],
                    "operational",
                )
                enum = attribute["type"]["members"]
                self.assertLessEqual(len(enum), 32)


if __name__ == "__main__":
    catalog, baseline, registry = map(Path, sys.argv[1:])
    CATALOG = json.loads(catalog.read_text())
    BASELINE = json.loads(baseline.read_text())
    MODELS = [
        yaml.safe_load(model.read_text())
        for model in sorted(registry.glob("*.yaml"))
    ]
    GROUPS = [group for model in MODELS for group in model.get("groups", [])]
    ATTRIBUTES = {
        attribute["id"]: attribute
        for group in GROUPS
        if group["type"] == "attribute_group"
        for attribute in group.get("attributes", [])
        if "id" in attribute
    }
    unittest.main(argv=[sys.argv[0]])
