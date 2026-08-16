from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from a_share_research.core.contracts import DISCLAIMER
from a_share_research.core.pipeline import read_artifact, run_research


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.request = {
            "schema_version": "0.1",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00+08:00",
            "snapshot": {"selector": "demo"},
            "top_n": 5,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_demo_run_enforces_gates_and_writes_artifacts(self) -> None:
        result = run_research(self.request, self.workspace)

        self.assertEqual(result["market"], "CN")
        self.assertEqual(result["data_mode"], "fixture")
        self.assertEqual(
            result["counts"],
            {"observe": 1, "continue_research": 1, "exclude": 1},
        )
        self.assertEqual([item["symbol"] for item in result["focus"]], ["DEMO001", "DEMO002"])
        self.assertTrue(any("研究时点之后" in warning for warning in result["warnings"]))

        run_dir = self.workspace / "artifacts" / "runs" / result["run_id"]
        for filename in (
            "request.json",
            "snapshot.json",
            "research_packet.json",
            "report.md",
            "summary.json",
            "manifest.json",
        ):
            self.assertTrue((run_dir / filename).is_file(), filename)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("安装测试数据", report)
        self.assertIn(DISCLAIMER, report)
        self.assertNotIn("建议买入", report)
        self.assertNotIn("目标价", report)

        packet = json.loads((run_dir / "research_packet.json").read_text(encoding="utf-8"))
        excluded = next(item for item in packet["excluded"] if item["symbol"] == "DEMO003")
        self.assertIn("EV-FUTURE-001", excluded["time_leak_evidence_refs"])
        self.assertNotIn("EV-FUTURE-001", excluded["usable_evidence_refs"])

    def test_same_request_reuses_immutable_run(self) -> None:
        first = run_research(self.request, self.workspace)
        second = run_research(self.request, self.workspace)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        self.assertEqual(first["analysis_hash"], second["analysis_hash"])
        self.assertTrue(second["reused"])

        with tempfile.TemporaryDirectory() as other_directory:
            independent = run_research(self.request, Path(other_directory))
        self.assertEqual(first["manifest_hash"], independent["manifest_hash"])
        self.assertEqual(first["analysis_hash"], independent["analysis_hash"])

    def test_concurrent_same_request_is_serialized(self) -> None:
        script = """
import json
import sys
from pathlib import Path
from a_share_research.core.pipeline import run_research
print(json.dumps(run_research(json.loads(sys.argv[2]), Path(sys.argv[1]))))
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.workspace), json.dumps(self.request)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(results[0]["run_id"], results[1]["run_id"])
        self.assertTrue(any(result.get("reused") for result in results))

    def test_concurrent_different_requests_share_database_safely(self) -> None:
        script = """
import json
import sys
from pathlib import Path
from a_share_research.core.pipeline import run_research
print(json.dumps(run_research(json.loads(sys.argv[2]), Path(sys.argv[1]))))
"""
        requests = [
            self.request,
            {
                **self.request,
                "workflow": "theme_research",
                "subject": "示例算力",
            },
        ]
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.workspace), json.dumps(request)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for request in requests
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertNotEqual(results[0]["run_id"], results[1]["run_id"])
        with sqlite3.connect(self.workspace / "data" / "stock_research.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 2)

    def test_artifact_read_is_bounded_and_relative(self) -> None:
        result = run_research(self.request, self.workspace)
        artifact = read_artifact(
            {
                "artifact_id": result["artifact_id"],
                "section": "report",
                "max_chars": 500,
            },
            self.workspace,
        )
        self.assertTrue(artifact["truncated"])
        self.assertLessEqual(len(artifact["content"]), 500)
        self.assertFalse(Path(artifact["relative_path"]).is_absolute())

    def test_immutable_run_refuses_tampered_artifacts(self) -> None:
        result = run_research(self.request, self.workspace)
        report = self.workspace / "artifacts" / "runs" / result["run_id"] / "report.md"
        report.write_text(report.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "immutable artifact hash mismatch"):
            read_artifact(
                {
                    "artifact_id": result["artifact_id"],
                    "section": "report",
                    "max_chars": 500,
                },
                self.workspace,
            )
        with self.assertRaisesRegex(RuntimeError, "immutable artifact hash mismatch"):
            run_research(self.request, self.workspace)

    def test_sqlite_records_every_candidate_including_exclusions(self) -> None:
        result = run_research(self.request, self.workspace)
        database = self.workspace / "data" / "stock_research.sqlite3"
        with sqlite3.connect(database) as connection:
            run_count = connection.execute("SELECT count(*) FROM runs").fetchone()[0]
            candidate_count = connection.execute(
                "SELECT count(*) FROM candidate_decisions WHERE run_id = ?",
                (result["run_id"],),
            ).fetchone()[0]
            excluded_count = connection.execute(
                "SELECT count(*) FROM candidate_decisions WHERE run_id = ? AND decision = 'exclude'",
                (result["run_id"],),
            ).fetchone()[0]
        self.assertEqual(run_count, 1)
        self.assertEqual(candidate_count, 3)
        self.assertEqual(excluded_count, 1)

    def test_stock_and_theme_filters_do_not_cross_subjects(self) -> None:
        stock = run_research(
            {
                **self.request,
                "workflow": "stock_research",
                "symbol": "DEMO002",
            },
            self.workspace,
        )
        self.assertEqual([item["symbol"] for item in stock["focus"]], ["DEMO002"])

        theme = run_research(
            {
                **self.request,
                "workflow": "theme_research",
                "subject": "概念映射",
            },
            self.workspace,
        )
        self.assertEqual(theme["counts"]["exclude"], 1)
        self.assertEqual(theme["focus"], [])


if __name__ == "__main__":
    unittest.main()
