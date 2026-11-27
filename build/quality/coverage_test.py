"""Prove coverage identity, provenance and classification independently of CI."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coverage_records import read_lcov
from evidence import capture, open_evidence, source_fingerprint
from metrics import CoverageStatus, measure, summarize


class CoverageContractTest(unittest.TestCase):
    def test_explicit_executor_root_does_not_match_another_checkout(
        self,
    ) -> None:
        text = (
            "SF:/buildbuddy-execroot/src/rule.py\nDA:2,1\nend_of_record\n"
            "SF:/unrelated/src/rule.py\nDA:2,0\nend_of_record\n"
        )
        sources = read_lcov(
            text, {"src/rule.py": 3}, (Path("/buildbuddy-execroot"),)
        )
        self.assertEqual(sources["src/rule.py"].lines, {2: True})

    def test_python_entry_arc_is_not_a_source_line(self) -> None:
        text = "SF:src/rule.py\nBRDA:0,1,1,1\nDA:2,0\nend_of_record\n"
        records = read_lcov(text, {"src/rule.py": 2}, ())
        self.assertEqual(records["src/rule.py"].percent(1, 2), 0.0)

    def test_merge_normalized_copies_by_branch_identity(self) -> None:
        text = (
            "SF:/buildbuddy-execroot/src/rule.py\nDA:2,0\n"
            "BRDA:2,0,0,-\nBRDA:2,0,1,1\nend_of_record\n"
            "SF:src/rule.py\nDA:2,1\n"
            "BRDA:2,0,0,1\nBRDA:2,0,1,0\nend_of_record\n"
        )
        records = read_lcov(
            text, {"src/rule.py": 3}, (Path("/buildbuddy-execroot"),)
        )
        self.assertEqual(records["src/rule.py"].percent(2, 2), 100.0)
        self.assertEqual(len(records["src/rule.py"].branches), 2)

    def test_reject_stale_line_numbers_and_incomplete_records(self) -> None:
        for record in (
            "SF:src/rule.py\nDA:9,1\nend_of_record\n",
            "SF:src/rule.py\nDA:2,1\n",
            "SF:src/rule.py\nBRDA:2,0,0,bad\nend_of_record\n",
        ):
            with self.subTest(record=record), self.assertRaises(ValueError):
                read_lcov(record, {"src/rule.py": 3}, ())

    def test_zero_missing_and_declaration_are_distinct(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.ts").write_text(
                "export declare function wire(): number;\n"
                "export function zero(): number {\n  return 0;\n}\n"
                "export function missing(): number {\n  return 1;\n}\n",
                encoding="utf-8",
            )
            results = measure(
                root, ["app.ts"], "SF:app.ts\nDA:3,0\nend_of_record\n", ()
            )
        self.assertEqual(
            {row.name: row.status for row in results},
            {
                "wire": CoverageStatus.NOT_APPLICABLE,
                "zero": CoverageStatus.ZERO,
                "missing": CoverageStatus.MISSING,
            },
        )

    def test_summary_separates_plugin_code_and_services(self) -> None:
        files = {
            "plugins/src/galadril_plugins/law/actions.py": "law",
            "plugins/src/galadril_plugins/utils/query.py": "utils",
            "plugins/utils/tracker/src/tracker.py": "utils/tracker",
            "platform/ontology/src/query.py": "ontology",
            "services/gateway/src/query.py": "gateway",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in files:
                source = root / path
                source.parent.mkdir(parents=True)
                source.write_text(
                    "def run() -> bool:\n    return True\n", encoding="utf-8"
                )
            rows = measure(root, list(files), "", ())
        self.assertEqual({row.path: row.component for row in rows}, files)

    def test_vue_top_level_hits_cannot_cover_an_unexecuted_function(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Panel.vue").write_text(
                '<script setup lang="ts">\n'
                "function read(): number { return 1; }\n"
                "</script>\n",
                encoding="utf-8",
            )
            results = measure(
                root,
                ["Panel.vue"],
                "SF:Panel.vue\nFN:2,read\nFNDA:0,read\nDA:2,1\nend_of_record\n",
                (),
            )
        [row] = results
        self.assertEqual(row.status, CoverageStatus.ZERO)
        self.assertEqual(row.score, 2.0)

    def test_capture_rejects_non_bazel_and_detects_stale_sources(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rule.py"
            path.write_text("pass\n", encoding="utf-8")
            source_id = source_fingerprint(root, ["rule.py"])
            events = (
                '{"started":{"uuid":"00000000-0000-4000-8000-000000000001",'
                '"command":"coverage","buildToolVersion":"9.3.0"}}\n'
                '{"finished":{"exitCode":{"code":3}},"lastMessage":true}\n'
            )
            report = root / "raw.dat"
            report.write_text(
                "SF:rule.py\nDA:1,1\nend_of_record\n", encoding="utf-8"
            )
            artifact = root / "artifact"

            with self.assertRaises(ValueError):
                capture(
                    root,
                    ["rule.py"],
                    report,
                    events.replace('"coverage"', '"test"'),
                    source_id,
                    artifact,
                )
            capture(root, ["rule.py"], report, events, source_id, artifact)
            self.assertEqual(
                open_evidence(root, ["rule.py"], artifact)[0].exit_code, 3
            )

            bundled = artifact / "coverage.lcov"
            bundled.write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                open_evidence(root, ["rule.py"], artifact)
            bundled.write_bytes(report.read_bytes())

            path.write_text("return 3\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                open_evidence(root, ["rule.py"], artifact)

    def test_vue_diagnostics_use_sfc_line_numbers(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Panel.vue").write_text(
                '<template>app</template>\n<script setup lang="ts">\n'
                "function read(flag: boolean): number {\n"
                "  if (flag) return 1;\n  return 0;\n}\n</script>\n",
                encoding="utf-8",
            )
            results = measure(
                root,
                ["Panel.vue"],
                "SF:Panel.vue\nFN:3,read\nFNDA:1,read\n"
                "DA:4,1\nDA:5,0\nBRDA:4,0,0,1\nBRDA:4,0,1,0\n"
                "end_of_record\n",
                (),
            )
        self.assertEqual(
            [
                (
                    row.language,
                    row.name,
                    row.line,
                    row.cyclomatic_complexity,
                    row.coverage_percent,
                    row.score,
                )
                for row in results
            ],
            [("vue", "read", 3, 2, 50.0, 2.5)],
        )
        self.assertEqual(
            summarize(results),
            [
                {
                    "language": "vue",
                    "domain": "repository",
                    "component": "repository",
                    "function_count": 1,
                    "measured_count": 1,
                    "coverage_status": {"partial": 1},
                    "complexity_total": 2,
                    "complexity_maximum": 2,
                    "coverage_mean_percent": 50.0,
                    "crap_mean": 2.5,
                    "crap_maximum": 2.5,
                    "crap_at_target": 1,
                    "crap_needs_review": 0,
                    "crap_blocking": 0,
                }
            ],
        )
