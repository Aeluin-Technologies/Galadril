"""Prove the quality gate rejects violations and accepts valid controls."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from check import architecture_violations
from metrics import crap_violations
from tools import tool_command


class EngineeringGateTest(unittest.TestCase):
    """Use actual parsers and LCOV records to exercise rejection paths."""

    def test_python_analyzers_reject_explicit_and_inferred_any(self) -> None:
        runfiles = Path(os.environ.get("TEST_SRCDIR", "."))
        root = runfiles / os.environ.get("TEST_WORKSPACE", "")
        config = root / "pyproject.toml"
        source = root / "build/quality/typing_violation.py.txt"
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "violation.py"
            fixture.write_bytes(source.read_bytes())
            for module, args, expected in (
                (
                    "ruff",
                    ["format", "--check", "--config", str(config)],
                    ("File would be reformatted",),
                ),
                (
                    "ruff",
                    ["check", "--no-cache", "--config", str(config)],
                    ("I001",),
                ),
                (
                    "basedpyright",
                    ["--project", str(config)],
                    (
                        "reportExplicitAny",
                        "reportAny",
                        "reportUnknownParameterType",
                    ),
                ),
            ):
                with self.subTest(module=module, args=args):
                    result = subprocess.run(
                        [*tool_command(module), *args, str(fixture)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 1, output)
                    for diagnostic in expected:
                        self.assertIn(diagnostic, output)

    def test_rust_grouped_import_cannot_hide_adapter(self) -> None:
        self.assertTrue(
            architecture_violations(
                "services/example/src/domain/entity.rs",
                "use crate::{domain::Id, adapters::sql};",
            )
        )

    def test_ui_dynamic_import_cannot_reach_dashboard(self) -> None:
        self.assertTrue(
            architecture_violations(
                "front/ui/src/catalog.ts",
                "export const load = () => import('../../dashboard/app/catalog');",
            )
        )

    def test_domain_importing_adapter_is_rejected(self) -> None:
        path = "platform/example/src/example/domain/entity.py"
        self.assertEqual(
            len(architecture_violations(path, "from ..adapters import sql\n")),
            1,
        )
        self.assertEqual(
            architecture_violations(path, "from ..ports import Clock\n"), []
        )

    def test_type_suppression_is_rejected(self) -> None:
        self.assertEqual(
            len(
                architecture_violations(
                    "platform/example/src/example.py",
                    "from api import load  # pyright: ignore[reportAny]\n",
                )
            ),
            1,
        )

    def test_vue_template_suppression_is_rejected(self) -> None:
        self.assertEqual(
            len(
                architecture_violations(
                    "front/dashboard/app/Panel.vue",
                    "<template><!-- @vue-expect-error --><Panel /></template>",
                )
            ),
            1,
        )

    def test_comment_like_string_is_allowed(self) -> None:
        self.assertEqual(
            architecture_violations(
                "platform/example/src/example.py",
                'VALUE = "# pyright: ignore[reportAny]"\n',
            ),
            [],
        )

    def test_crap_uses_real_coverage_and_detects_missing_records(self) -> None:
        path = "rule.py"
        cases = "\n".join(f"    if value == {n}: return {n}" for n in range(7))
        source = f"def classify(value: int) -> int:\n{cases}\n    return -1\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / path).write_text(source, encoding="utf-8")
            self.assertEqual(
                [finding.rule for finding in crap_violations(root, [path], "")],
                ["coverage-missing"],
            )
            for hits, expected in [(0, ["crap-block"]), (1, [])]:
                with self.subTest(hits=hits):
                    records = "".join(
                        f"DA:{line},{hits}\n" for line in range(2, 10)
                    )
                    lcov = f"SF:{path}\n{records}end_of_record\n"
                    self.assertEqual(
                        [
                            finding.rule
                            for finding in crap_violations(root, [path], lcov)
                        ],
                        expected,
                    )

    def test_score_thirty_requires_review(self) -> None:
        path = "rule.py"
        cases = "\n".join(f"    if value == {n}: return {n}" for n in range(4))
        source = f"def classify(value: int) -> int:\n{cases}\n    return -1\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / path).write_text(source, encoding="utf-8")
            findings = crap_violations(
                root, [path], f"SF:{path}\nDA:2,0\nend_of_record\n"
            )
            self.assertEqual(
                [finding.rule for finding in findings], ["crap-target"]
            )
            self.assertTrue(
                any("CRAP=30.0000" in finding.detail for finding in findings)
            )

    def test_coverage_from_a_different_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/rule.py").write_text(
                "def one() -> int:\n    return 1\n", encoding="utf-8"
            )
            findings = crap_violations(
                root,
                ["src/rule.py"],
                "SF:other/src/rule.py\nDA:2,1\nend_of_record\n",
            )
            self.assertEqual(
                [finding.rule for finding in findings], ["coverage-missing"]
            )


if __name__ == "__main__":
    unittest.main()
