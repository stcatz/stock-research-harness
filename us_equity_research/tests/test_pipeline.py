from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from us_equity_research.core import pipeline as pipeline_module
from us_equity_research.core.pipeline import read_artifact, run_research
from us_equity_research.core.reporting import (
    DISCLAIMER,
    FIXTURE_BANNER,
    PROHIBITED_REPORT_PHRASES,
    render_report,
)
from us_equity_research.core.utils import sha256_file, sha256_value

DECISION_AT = "2026-08-16T08:30:00-04:00"
SUMMARY_FIELDS = {
    "schema_version",
    "market",
    "run_id",
    "artifact_id",
    "status",
    "writer_mode",
    "data_mode",
    "pit_quality",
    "workflow",
    "decision_at",
    "snapshot_id",
    "analysis_hash",
    "counts",
    "focus",
    "warnings",
    "gaps",
    "available_sections",
    "manifest_hash",
}
ARTIFACT_RESULT_FIELDS = {
    "schema_version",
    "market",
    "artifact_id",
    "section",
    "content_type",
    "content",
    "truncated",
    "relative_path",
}
IMMUTABLE_FILENAMES = {
    "request.json",
    "snapshot.json",
    "research_packet.json",
    "report.md",
    "summary.json",
    "manifest.json",
}


def _request(workflow: str = "daily_report", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "0.1",
        "market": "US",
        "workflow": workflow,
        "decision_at": DECISION_AT,
        "snapshot": {"selector": "demo"},
        "top_n": 5,
    }
    payload.update(extra)
    return payload


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_daily_fixture_writes_exact_us_layout_and_canonical_summary(self) -> None:
        result = run_research(_request(), self.workspace)

        self.assertEqual(set(result), SUMMARY_FIELDS)
        self.assertEqual(result["schema_version"], "0.1")
        self.assertEqual(result["market"], "US")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["writer_mode"], "engine")
        self.assertEqual(result["data_mode"], "fixture")
        self.assertEqual(result["pit_quality"], "FIXTURE")
        self.assertEqual(result["workflow"], "daily_report")
        self.assertEqual(result["decision_at"], DECISION_AT)
        self.assertEqual(result["snapshot_id"], "us-demo-20260816")
        self.assertEqual(
            result["counts"],
            {"observe": 1, "continue_research": 1, "exclude": 1},
        )
        self.assertEqual([item["symbol"] for item in result["focus"]], ["DEMOA", "DEMOB"])
        self.assertEqual(
            result["available_sections"],
            ["summary", "report", "manifest", "packet"],
        )

        run_dir = self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"])
        self.assertEqual({path.name for path in run_dir.iterdir()}, IMMUTABLE_FILENAMES)
        self.assertFalse((self.workspace / "artifacts" / "runs").exists())
        self.assertTrue((self.workspace / "data" / "us_stock_research.sqlite3").is_file())
        daily_reports = list((self.workspace / "reports" / "us" / "daily").glob("*.md"))
        self.assertEqual(len(daily_reports), 1)

        stored_summary = _read_json(run_dir / "summary.json")
        self.assertEqual(stored_summary, result)
        packet = _read_json(run_dir / "research_packet.json")
        self.assertEqual(packet["market"], "US")
        self.assertEqual(len(packet["all_decisions"]), 3)

    def test_report_is_financial_worker_oriented_and_enforces_policy(self) -> None:
        result = run_research(_request(), self.workspace)
        run_dir = self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"])
        report = (run_dir / "report.md").read_text(encoding="utf-8")

        self.assertIn(FIXTURE_BANNER, report)
        self.assertTrue(report.rstrip().endswith(DISCLAIMER))
        for heading in (
            "## 数据与 PIT 状态",
            "## 宏观与市场背景",
            "## 主题与催化剂",
            "## 重点研究标的",
            "#### 事实（输入）",
            "#### 确定性计算（规则输出）",
            "#### 多空与风险推断",
            "#### 催化剂与证伪条件",
            "## 排除的候选",
            "## 数据缺口与人工复核",
        ):
            self.assertIn(heading, report)
        self.assertIn(
            "| metric | value | unit | period_end | available_at | evidence_ref |",
            report,
        )
        self.assertIn(
            "| metric | status | value | unit | period_end | price_as_of | formula | input_fact_ids | evidence_refs |",
            report,
        )
        for phrase in PROHIBITED_REPORT_PHRASES:
            self.assertNotIn(phrase, report)
        convenience_reports = list((self.workspace / "reports" / "us" / "daily").glob("*.md"))
        self.assertEqual(convenience_reports[0].read_text(encoding="utf-8"), report)

    def test_report_never_renders_post_cutoff_numeric_value(self) -> None:
        result = run_research(_request(), self.workspace)
        run_dir = self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"])
        packet = _read_json(run_dir / "research_packet.json")
        calculation = packet["focus"][0]["calculations"][0]
        calculation["available_at_cutoff_passed"] = False
        calculation["status"] = "OK"
        calculation["value"] = 987654321.123456

        report = render_report(packet)

        self.assertNotIn("987654321.123456", report)
        matching_row = next(
            line for line in report.splitlines() if line.startswith(f"| {calculation['metric']} |")
        )
        self.assertIn("| UNKNOWN | UNKNOWN |", matching_row)

    def test_manifest_separates_stable_identity_from_file_integrity(self) -> None:
        result = run_research(_request(), self.workspace)
        run_dir = self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"])
        manifest = _read_json(run_dir / "manifest.json")
        stable_fields = {
            key: manifest[key]
            for key in (
                "schema_version",
                "run_id",
                "artifact_id",
                "request_hash",
                "snapshot_hash",
                "analysis_hash",
                "writer_mode",
                "method_id",
            )
        }

        self.assertEqual(manifest["manifest_hash"], sha256_value(stable_fields))
        self.assertEqual(
            manifest["integrity_hash"],
            sha256_value(
                {
                    **stable_fields,
                    "files": manifest["files"],
                    "paths": manifest["paths"],
                }
            ),
        )
        self.assertEqual(
            set(manifest["files"]),
            IMMUTABLE_FILENAMES - {"manifest.json"},
        )
        self.assertEqual(
            set(manifest["paths"]),
            {"run_dir", "request", "snapshot", "packet", "report", "summary", "manifest"},
        )
        for filename, expected_hash in manifest["files"].items():
            self.assertEqual(expected_hash, sha256_file(run_dir / filename))
        for relative_path in manifest["paths"].values():
            self.assertFalse(Path(relative_path).is_absolute())
            self.assertNotIn("..", Path(relative_path).parts)
        self.assertNotIn("generated_at", stable_fields)
        self.assertNotIn("files", stable_fields)
        self.assertNotIn("paths", stable_fields)

    def test_same_workspace_reuses_byte_identical_immutable_artifacts(self) -> None:
        first = run_research(_request(), self.workspace)
        run_dir = self.workspace / "artifacts" / "us" / "runs" / str(first["run_id"])
        before = {path.name: path.read_bytes() for path in run_dir.iterdir()}
        before_mtimes = {path.name: path.stat().st_mtime_ns for path in run_dir.iterdir()}

        second = run_research(_request(), self.workspace)

        after = {path.name: path.read_bytes() for path in run_dir.iterdir()}
        after_mtimes = {path.name: path.stat().st_mtime_ns for path in run_dir.iterdir()}
        self.assertEqual(second, first)
        self.assertEqual(after, before)
        self.assertEqual(after_mtimes, before_mtimes)
        self.assertEqual(
            list((self.workspace / "artifacts" / "us" / "runs").glob(".*.stale-*")),
            [],
        )

    def test_preexisting_partial_final_run_is_quarantined_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as reference_directory:
            reference = run_research(_request(), Path(reference_directory))
        run_id = str(reference["run_id"])
        artifact_store = self.workspace / "artifacts" / "us" / "runs"
        partial_run = artifact_store / run_id
        partial_run.mkdir(parents=True)
        partial_content = b"interrupted-before-manifest\n"
        (partial_run / "request.json").write_bytes(partial_content)

        result = run_research(_request(), self.workspace)

        self.assertEqual(result["run_id"], run_id)
        self.assertEqual({path.name for path in partial_run.iterdir()}, IMMUTABLE_FILENAMES)
        quarantined = list(artifact_store.glob(f".{run_id}.stale-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual((quarantined[0] / "request.json").read_bytes(), partial_content)

    def test_interrupted_staging_write_does_not_brick_retry(self) -> None:
        with tempfile.TemporaryDirectory() as reference_directory:
            reference = run_research(_request(), Path(reference_directory))
        run_id = str(reference["run_id"])
        original_write = pipeline_module.write_json_atomic
        call_count = 0

        def interrupted_write(path: Path, value: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("simulated interruption")
            original_write(path, value)

        with (
            patch.object(
                pipeline_module,
                "write_json_atomic",
                side_effect=interrupted_write,
            ),
            self.assertRaisesRegex(OSError, "simulated interruption"),
        ):
            run_research(_request(), self.workspace)

        artifact_store = self.workspace / "artifacts" / "us" / "runs"
        self.assertFalse(os.path.lexists(artifact_store / run_id))
        interrupted_stages = list(artifact_store.glob(f".{run_id}.tmp-*"))
        self.assertEqual(len(interrupted_stages), 1)
        self.assertTrue((interrupted_stages[0] / "request.json").is_file())

        retried = run_research(_request(), self.workspace)

        self.assertEqual(retried["run_id"], run_id)
        final_run = artifact_store / run_id
        self.assertEqual({path.name for path in final_run.iterdir()}, IMMUTABLE_FILENAMES)

    def test_preexisting_run_symlink_is_moved_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as reference_directory:
            reference = run_research(_request(), Path(reference_directory))
        run_id = str(reference["run_id"])
        artifact_store = self.workspace / "artifacts" / "us" / "runs"
        artifact_store.mkdir(parents=True)
        outside_target = self.workspace / "outside-target"
        outside_target.mkdir()
        sentinel = outside_target / "sentinel.txt"
        sentinel.write_text("preserve me", encoding="utf-8")
        final_run = artifact_store / run_id
        final_run.symlink_to(outside_target, target_is_directory=True)

        result = run_research(_request(), self.workspace)

        self.assertEqual(result["run_id"], run_id)
        self.assertFalse(final_run.is_symlink())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me")
        quarantined = list(artifact_store.glob(f".{run_id}.stale-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0].is_symlink())

    def test_independent_workspace_replay_preserves_all_stable_analysis(self) -> None:
        first = run_research(_request(), self.workspace)
        first_packet_path = (
            self.workspace
            / "artifacts"
            / "us"
            / "runs"
            / str(first["run_id"])
            / "research_packet.json"
        )
        first_packet = _read_json(first_packet_path)

        with tempfile.TemporaryDirectory() as other_directory:
            other_workspace = Path(other_directory)
            second = run_research(_request(), other_workspace)
            second_packet = _read_json(
                other_workspace
                / "artifacts"
                / "us"
                / "runs"
                / str(second["run_id"])
                / "research_packet.json"
            )

        for field in ("run_id", "artifact_id", "analysis_hash", "manifest_hash"):
            self.assertEqual(first[field], second[field])
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(first_packet["all_decisions"], second_packet["all_decisions"])
        self.assertEqual(
            [item["calculations"] for item in first_packet["all_decisions"]],
            [item["calculations"] for item in second_packet["all_decisions"]],
        )

    def test_all_workflows_run_end_to_end_without_fallback(self) -> None:
        daily = run_research(_request(), self.workspace)
        theme = run_research(
            _request("theme_research", subject="AUTOMATION PLATFORM"),
            self.workspace,
        )
        stock = run_research(_request("stock_research", symbol="DEMOB"), self.workspace)
        no_match = run_research(
            _request("theme_research", subject="semiconductors"),
            self.workspace,
        )

        self.assertEqual(daily["counts"], {"observe": 1, "continue_research": 1, "exclude": 1})
        self.assertEqual(theme["counts"], daily["counts"])
        self.assertEqual(stock["counts"], {"observe": 0, "continue_research": 1, "exclude": 0})
        self.assertEqual([item["symbol"] for item in stock["focus"]], ["DEMOB"])
        self.assertEqual(no_match["counts"], {"observe": 0, "continue_research": 0, "exclude": 0})
        self.assertEqual(no_match["focus"], [])
        self.assertTrue(any("No themes matched" in warning for warning in no_match["warnings"]))
        self.assertEqual(
            len({daily["run_id"], theme["run_id"], stock["run_id"], no_match["run_id"]}), 4
        )

    def test_artifact_read_has_exact_bounded_relative_envelope(self) -> None:
        result = run_research(_request(), self.workspace)
        expected_files = {
            "summary": "summary.json",
            "report": "report.md",
            "manifest": "manifest.json",
            "packet": "research_packet.json",
        }
        expected_content_types = {
            "summary": "application/json",
            "report": "text/markdown",
            "manifest": "application/json",
            "packet": "application/json",
        }
        for section, filename in expected_files.items():
            with self.subTest(section=section):
                artifact = read_artifact(
                    {
                        "artifact_id": result["artifact_id"],
                        "section": section,
                        "max_chars": 500,
                    },
                    self.workspace,
                )
                self.assertEqual(set(artifact), ARTIFACT_RESULT_FIELDS)
                self.assertEqual(artifact["schema_version"], "0.1")
                self.assertEqual(artifact["market"], "US")
                self.assertEqual(artifact["artifact_id"], result["artifact_id"])
                self.assertEqual(artifact["section"], section)
                self.assertEqual(artifact["content_type"], expected_content_types[section])
                self.assertLessEqual(len(artifact["content"]), 500)
                self.assertFalse(Path(artifact["relative_path"]).is_absolute())
                self.assertEqual(Path(artifact["relative_path"]).name, filename)
                self.assertTrue(str(artifact["relative_path"]).startswith("artifacts/us/runs/"))

    def test_tampering_is_refused_for_reads_and_run_reuse(self) -> None:
        result = run_research(_request(), self.workspace)
        report_path = (
            self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"]) / "report.md"
        )
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8"
        )

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
            run_research(_request(), self.workspace)

    def test_artifact_read_returns_verified_bytes_without_second_reopen(self) -> None:
        result = run_research(_request(), self.workspace)
        summary_path = (
            self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"]) / "summary.json"
        )
        expected = summary_path.read_text(encoding="utf-8")
        original_verify = pipeline_module._verify_complete_run

        def mutate_after_verification(*args: object, **kwargs: object) -> object:
            verified = original_verify(*args, **kwargs)
            summary_path.write_text('{"unverified":"replacement"}\n', encoding="utf-8")
            return verified

        with patch.object(
            pipeline_module,
            "_verify_complete_run",
            side_effect=mutate_after_verification,
        ):
            artifact = read_artifact(
                {
                    "artifact_id": result["artifact_id"],
                    "section": "summary",
                    "max_chars": 20000,
                },
                self.workspace,
            )

        self.assertEqual(artifact["content"], expected)
        self.assertNotIn("unverified", artifact["content"])

    def test_database_path_escape_is_refused(self) -> None:
        result = run_research(_request(), self.workspace)
        database = self.workspace / "data" / "us_stock_research.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE artifacts SET run_dir = ? WHERE artifact_id = ?",
                ("../../outside", result["artifact_id"]),
            )

        with self.assertRaisesRegex(RuntimeError, "escaped the immutable US artifact store"):
            read_artifact(
                {
                    "artifact_id": result["artifact_id"],
                    "section": "summary",
                    "max_chars": 500,
                },
                self.workspace,
            )

    def test_sqlite_records_every_candidate_including_exclusions(self) -> None:
        result = run_research(_request(), self.workspace)
        database = self.workspace / "data" / "us_stock_research.sqlite3"
        with sqlite3.connect(database) as connection:
            run = connection.execute("SELECT market, run_id, artifact_id FROM runs").fetchone()
            decisions = connection.execute(
                "SELECT symbol, decision FROM candidate_decisions WHERE run_id = ? ORDER BY symbol",
                (result["run_id"],),
            ).fetchall()
        self.assertEqual(run, ("US", result["run_id"], result["artifact_id"]))
        self.assertEqual(
            decisions,
            [("DEMOA", "observe"), ("DEMOB", "continue_research"), ("DEMOC", "exclude")],
        )

    def test_concurrent_same_request_serializes_one_immutable_run(self) -> None:
        results = self._run_concurrently([_request(), _request()])
        self.assertEqual(results[0], results[1])
        run_dir = self.workspace / "artifacts" / "us" / "runs" / str(results[0]["run_id"])
        self.assertEqual({path.name for path in run_dir.iterdir()}, IMMUTABLE_FILENAMES)
        with sqlite3.connect(self.workspace / "data" / "us_stock_research.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM candidate_decisions").fetchone()[0],
                3,
            )

    def test_concurrent_different_requests_share_us_database_safely(self) -> None:
        results = self._run_concurrently([_request(), _request("stock_research", symbol="DEMOA")])
        self.assertNotEqual(results[0]["run_id"], results[1]["run_id"])
        with sqlite3.connect(self.workspace / "data" / "us_stock_research.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM artifacts").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM candidate_decisions").fetchone()[0],
                4,
            )

    def test_real_snapshot_is_frozen_into_artifact_without_mutating_source(self) -> None:
        raw = json.loads(
            files("us_equity_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        raw = copy.deepcopy(raw)
        raw["snapshot_id"] = "us-snapshot-copy-20260816"
        raw["data_mode"] = "snapshot"
        raw["pit_quality"] = "P1"
        raw.pop("fixture_notice")
        source = (
            self.workspace / "data" / "normalized" / "us" / raw["snapshot_id"] / "snapshot.json"
        )
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before = source.read_bytes()

        result = run_research(
            _request(snapshot={"selector": "id", "snapshot_id": raw["snapshot_id"]}),
            self.workspace,
        )

        self.assertEqual(result["data_mode"], "snapshot")
        self.assertEqual(source.read_bytes(), before)
        frozen = _read_json(
            self.workspace / "artifacts" / "us" / "runs" / str(result["run_id"]) / "snapshot.json"
        )
        self.assertEqual(frozen["snapshot_id"], raw["snapshot_id"])

    def _run_concurrently(self, requests: list[dict[str, object]]) -> list[dict[str, object]]:
        script = """
import json
import sys
from pathlib import Path
from us_equity_research.core.pipeline import run_research
print(json.dumps(run_research(json.loads(sys.argv[2]), Path(sys.argv[1]))))
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.workspace), json.dumps(request)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for request in requests
        ]
        results: list[dict[str, object]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        return results


if __name__ == "__main__":
    unittest.main()
