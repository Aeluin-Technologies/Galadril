"""Checks invariants that the semantic convention schema cannot express."""

import json
import sys
import unittest
from pathlib import Path

import yaml


class ContractPolicyTest(unittest.TestCase):
    def test_published_error_codes_and_meanings_are_preserved(self) -> None:
        for code, contract in BASELINE.items():
            self.assertEqual(contract, CATALOG.get(code), code)

    def test_codes_are_unique_and_safe_for_each_transport(self) -> None:
        error_code = next(
            attribute
            for doc in MODELS
            for attribute in doc.get("attributes", [])
            if attribute["key"] == "galadril.error.code"
        )
        members = error_code["type"]["members"]
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
        for doc in MODELS:
            for span in doc.get("spans", []):
                self.assertRegex(
                    span["type"], r"^galadril\.[a-z_]+\.[a-z_]+(?:\.[a-z_]+)?$"
                )
                for name in span["name"].get("templates", []):
                    self.assertEqual(span["type"], name)

    def test_metrics_accept_only_bounded_operational_values(self) -> None:
        attributes = {
            attr["key"]: attr
            for doc in MODELS
            for attr in doc.get("attributes", [])
        }
        for doc in MODELS:
            for metric in doc.get("metrics", []):
                for ref in metric["attributes"]:
                    attribute = attributes[ref["ref"]]
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
    unittest.main(argv=[sys.argv[0]])
