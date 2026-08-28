from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from a_share_research.core.drift import audit_snapshot_drift
from a_share_research.core.contracts import ContractError, parse_datetime
from a_share_research.core.outcomes import (
    record_outcome,
    summarize_outcome_history,
    summarize_outcomes,
)
from a_share_research.core.opportunity import assess_opportunity
from a_share_research.core.pipeline import read_artifact, run_research
from a_share_research.core.research_history import summarize_research_history


class QualityLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.request = {
            "schema_version": "0.1",
            "market": "CN",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00+08:00",
            "snapshot": {"selector": "demo"},
            "top_n": 5,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_packet_has_actionable_quality_contracts_and_thesis_blind_facts(self) -> None:
        result = run_research(self.request, self.workspace)
        run_dir = self.workspace / "artifacts" / "runs" / result["run_id"]
        packet = json.loads((run_dir / "research_packet.json").read_text(encoding="utf-8"))

        for decision in packet["all_decisions"]:
            self.assertIn(decision["evidence_state"], {"sufficient", "incomplete", "conflicted"})
            self.assertGreaterEqual(decision["research_priority"], 0)
            self.assertLessEqual(decision["research_priority"], 100)
            self.assertEqual(
                decision["falsification_contract"]["bull_thesis_visible"],
                False,
            )
            if decision["decision"] == "continue_research":
                self.assertTrue(decision["research_gaps"])
                self.assertTrue(any(gap["transition_condition"] for gap in decision["research_gaps"]))

        primary = next(item for item in packet["all_decisions"] if item["symbol"] == "DEMO001")
        legacy = next(item for item in packet["all_decisions"] if item["symbol"] == "DEMO002")
        self.assertEqual(primary["opportunity_profile"]["contract_version"], "2.0")
        self.assertEqual(primary["opportunity_view"], "positive")
        self.assertEqual(primary["attention_bucket"], "deep_dive")
        self.assertGreater(primary["attention_score"], legacy["attention_score"])
        self.assertEqual(legacy["opportunity_view"], "unclear")
        generic_gap = next(
            gap
            for gap in legacy["research_gaps"]
            if gap["transition_condition"].startswith("取得并核验缺失项")
        )
        self.assertFalse(generic_gap["blocking"])

        facts_text = (run_dir / "fact_packet.json").read_text(encoding="utf-8")
        facts = json.loads(facts_text)
        self.assertIn("fact_packet_hash", facts)
        self.assertNotIn('"thesis"', facts_text)
        self.assertNotIn('"counter_thesis"', facts_text)
        self.assertIn("DEMO-COMPANY-ANNOUNCEMENT-001", facts_text)
        self.assertIn("d867629402f338f5c31044ae1c9532181ec100121b7180f47b3dbd55d61b1f28", facts_text)

    def test_economic_score_requires_frozen_content_on_company_disclosure(self) -> None:
        snapshot = json.loads(
            files("a_share_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        company = next(
            item for item in snapshot["evidence"] if item["evidence_id"] == "EV-COMPANY-001"
        )
        industry = next(
            item for item in snapshot["evidence"] if item["evidence_id"] == "EV-INDUSTRY-001"
        )
        industry["document"] = company.pop("document")
        theme = snapshot["themes"][0]
        candidate = theme["candidates"][0]
        evidence_by_id = {item["evidence_id"]: item for item in snapshot["evidence"]}
        usable_refs = [
            item["evidence_id"]
            for item in snapshot["evidence"]
            if item["evidence_id"] != "EV-FUTURE-001"
        ]

        result = assess_opportunity(
            theme,
            candidate,
            evidence_by_id,
            usable_refs,
            parse_datetime(self.request["decision_at"], "decision_at"),
        )

        self.assertFalse(result["economic_impact"]["source_qualified"])
        self.assertFalse(result["economic_impact"]["frozen_content_qualified"])
        self.assertEqual(result["economic_impact"]["points"], 0)

    def test_unqualified_negative_editorial_label_cannot_force_exclusion(self) -> None:
        snapshot = json.loads(
            files("a_share_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        theme = snapshot["themes"][0]
        candidate = theme["candidates"][0]
        candidate["opportunity_profile"]["expectation_gap"].update(
            assessment="negative",
            evidence_refs=["EV-SECONDARY-001"],
        )
        evidence_by_id = {item["evidence_id"]: item for item in snapshot["evidence"]}

        result = assess_opportunity(
            theme,
            candidate,
            evidence_by_id,
            [item["evidence_id"] for item in snapshot["evidence"][:-1]],
            parse_datetime(self.request["decision_at"], "decision_at"),
        )

        self.assertFalse(result["expectation_gap"]["evidence_qualified"])
        self.assertNotEqual(result["opportunity_view"], "negative")

    def test_paginated_read_reconstructs_exact_verified_report(self) -> None:
        result = run_research(self.request, self.workspace)
        pages: list[str] = []
        cursor = 0
        expected_hash = None
        while True:
            page = read_artifact(
                {
                    "artifact_id": result["artifact_id"],
                    "section": "report",
                    "max_chars": 500,
                    "cursor": cursor,
                },
                self.workspace,
            )
            self.assertEqual(page["cursor"], cursor)
            expected_hash = expected_hash or page["content_sha256"]
            self.assertEqual(page["content_sha256"], expected_hash)
            pages.append(page["content"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]

        report_path = (
            self.workspace / "artifacts" / "runs" / result["run_id"] / "report.md"
        )
        self.assertEqual("".join(pages), report_path.read_text(encoding="utf-8"))
        self.assertEqual(sum(len(page) for page in pages), page["total_chars"])

    def test_outcomes_are_immutable_and_filtered_by_available_at(self) -> None:
        result = run_research(self.request, self.workspace)
        observation = {
            "schema_version": "0.1",
            "market": "CN",
            "run_id": result["run_id"],
            "symbol": "DEMO001",
            "horizon_trading_days": 5,
            "observed_at": "2026-08-24T15:00:00+08:00",
            "available_at": "2026-08-24T15:05:00+08:00",
            "candidate_return": 0.08,
            "benchmark_return": 0.03,
            "benchmark_symbol": "000906.SH",
            "source_url": "https://data.example.com/cn-close",
            "source_document_id": "licensed-close-20260824",
        }
        first = record_outcome(observation, self.workspace)
        second = record_outcome(observation, self.workspace)
        self.assertEqual(first["observation_hash"], second["observation_hash"])
        self.assertAlmostEqual(first["excess_return"], 0.05)

        before = summarize_outcomes(
            {
                "schema_version": "0.1",
                "market": "CN",
                "run_id": result["run_id"],
                "evaluation_at": "2026-08-24T07:04:00+00:00",
            },
            self.workspace,
        )
        after = summarize_outcomes(
            {
                "schema_version": "0.1",
                "market": "CN",
                "run_id": result["run_id"],
                "evaluation_at": "2026-08-24T07:06:00+00:00",
            },
            self.workspace,
        )
        self.assertEqual(before["observation_count"], 0)
        self.assertEqual(after["observation_count"], 1)
        history = summarize_outcome_history(
            {
                "schema_version": "0.1",
                "market": "CN",
                "evaluation_at": "2026-08-24T07:06:00+00:00",
                "limit": 5,
            },
            self.workspace,
        )
        self.assertEqual(history["run_count"], 1)
        self.assertEqual(history["runs"][0]["run_id"], result["run_id"])

        changed = {**observation, "candidate_return": 0.09}
        with self.assertRaisesRegex(RuntimeError, "different immutable observation"):
            record_outcome(changed, self.workspace)

    def test_outcome_record_requires_candidate_id_for_a_cross_theme_symbol(self) -> None:
        result = run_research(self.request, self.workspace)
        database = self.workspace / "data" / "stock_research.sqlite3"
        with sqlite3.connect(database) as connection:
            original_id = connection.execute(
                """
                SELECT candidate_id FROM candidate_decisions
                WHERE run_id = ? AND symbol = 'DEMO001'
                ORDER BY candidate_id LIMIT 1
                """,
                (result["run_id"],),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    run_id, candidate_id, symbol, name, theme_id, decision,
                    reasons_json, evidence_refs_json, data_gaps_json
                )
                SELECT run_id, candidate_id || ':duplicate', symbol, name,
                       theme_id || '-duplicate', decision, reasons_json,
                       evidence_refs_json, data_gaps_json
                FROM candidate_decisions
                WHERE run_id = ? AND candidate_id = ?
                """,
                (result["run_id"], original_id),
            )

        observation = {
            "schema_version": "0.1",
            "market": "CN",
            "run_id": result["run_id"],
            "symbol": "DEMO001",
            "horizon_trading_days": 5,
            "observed_at": "2026-08-24T15:00:00+08:00",
            "available_at": "2026-08-24T15:05:00+08:00",
            "candidate_return": 0.08,
            "benchmark_return": 0.03,
            "benchmark_symbol": "000906.SH",
            "source_url": "https://data.example.com/cn-close",
            "source_document_id": "licensed-close-20260824",
        }
        with self.assertRaisesRegex(ContractError, "candidate_id is required"):
            record_outcome(observation, self.workspace)

        recorded = record_outcome(
            {**observation, "candidate_id": original_id},
            self.workspace,
        )
        self.assertEqual(recorded["candidate_id"], original_id)

    def test_snapshot_drift_detects_historical_fact_revision(self) -> None:
        raw = json.loads(
            files("a_share_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        before = copy.deepcopy(raw)
        after = copy.deepcopy(raw)
        before["snapshot_id"] = "cn-before-drift"
        after["snapshot_id"] = "cn-after-drift"
        after["retrieved_at"] = "2026-08-16T12:05:00+08:00"
        fact = {
            "fact_id": "FACT-DRIFT-PE",
            "metric": "pe_ttm",
            "value": "20",
            "unit": "x",
            "status": "observed",
            "as_of": "2026-08-15T15:00:00+08:00",
            "effective_at": "2026-08-15T15:00:00+08:00",
            "available_at": before["evidence"][0]["available_at"],
            "source_field": "pe_ttm",
        }
        before["evidence"][0]["facts"] = [copy.deepcopy(fact)]
        disappearing = {
            **fact,
            "fact_id": "FACT-DRIFT-PB",
            "metric": "pb_mrq",
            "value": "3",
            "source_field": "pb_mrq",
        }
        before["evidence"][0]["facts"].append(disappearing)
        retimed = {
            **fact,
            "fact_id": "FACT-DRIFT-PS",
            "metric": "ps_ttm",
            "value": "4",
            "available_at": "2026-08-15T15:10:00+08:00",
            "source_field": "ps_ttm",
        }
        before["evidence"][0]["facts"].append(retimed)
        after["evidence"][0]["facts"] = [
            {**fact, "value": "22"},
            {**retimed, "available_at": "2026-08-15T15:11:00+08:00"},
        ]
        for payload in (before, after):
            path = (
                self.workspace
                / "data"
                / "normalized"
                / payload["snapshot_id"]
                / "snapshot.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        receipt = audit_snapshot_drift(
            {
                "schema_version": "0.1",
                "market": "CN",
                "before_snapshot_id": before["snapshot_id"],
                "after_snapshot_id": after["snapshot_id"],
            },
            self.workspace,
        )
        self.assertEqual(receipt["status"], "alert")
        change_types = {item["change_type"] for item in receipt["changes"]}
        self.assertEqual(
            change_types,
            {
                "historical_value_changed",
                "historical_fact_disappeared",
                "historical_availability_changed",
            },
        )
        self.assertTrue((self.workspace / receipt["relative_path"]).is_file())

    def test_research_history_ages_stagnant_continue_state_without_rewriting_runs(self) -> None:
        for decision_at in (
            "2026-08-16T08:30:00+08:00",
            "2026-08-21T08:30:00+08:00",
            "2026-09-25T08:30:00+08:00",
        ):
            run_research({**self.request, "decision_at": decision_at}, self.workspace)

        history = summarize_research_history(
            {
                "schema_version": "0.1",
                "market": "CN",
                "evaluation_at": "2026-09-25T08:30:00+08:00",
                "limit": 100,
                "candidate_ids": ["theme-demo-compute:CN.DEMO.001"],
            },
            self.workspace,
        )
        candidate = next(item for item in history["candidates"] if item["symbol"] == "DEMO001")
        self.assertEqual(history["candidate_count"], 1)
        self.assertEqual(candidate["latest_decision"], "continue_research")
        self.assertGreaterEqual(candidate["stale_days"], 30)
        self.assertEqual(candidate["aging_action"], "close_review")
        self.assertIn("never rewrites", history["policy"]["meaning"])

    def test_non_pit_retrieval_timestamp_drift_is_info_unless_it_crosses_cutoff(self) -> None:
        raw = json.loads(
            files("a_share_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        before = copy.deepcopy(raw)
        after = copy.deepcopy(raw)
        before.update(
            snapshot_id="cn-before-retrieval-drift",
            data_mode="snapshot",
            pit_quality="RECONSTRUCTED_NON_PIT",
        )
        after.update(
            snapshot_id="cn-after-retrieval-drift",
            data_mode="snapshot",
            pit_quality="RECONSTRUCTED_NON_PIT",
            retrieved_at="2026-08-16T12:05:00+08:00",
        )
        fact = {
            "fact_id": "FACT-RETRIEVAL-ONLY",
            "metric": "pe_ttm",
            "value": "20",
            "unit": "x",
            "status": "observed",
            "as_of": "2026-08-15T15:00:00+08:00",
            "effective_at": "2026-08-15T15:00:00+08:00",
            "available_at": "2026-08-16T07:20:00+08:00",
            "source_field": "pe_ttm",
        }
        before["evidence"][0]["facts"] = [copy.deepcopy(fact)]
        after["evidence"][0]["facts"] = [
            {**fact, "available_at": "2026-08-16T07:25:00+08:00"}
        ]
        for payload in (before, after):
            payload["evidence"][0]["available_at"] = payload["evidence"][0]["facts"][0][
                "available_at"
            ]
            payload["evidence"][0]["retrieved_at"] = payload["retrieved_at"]
            path = (
                self.workspace
                / "data"
                / "normalized"
                / payload["snapshot_id"]
                / "snapshot.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        info = audit_snapshot_drift(
            {
                "schema_version": "0.1",
                "market": "CN",
                "before_snapshot_id": before["snapshot_id"],
                "after_snapshot_id": after["snapshot_id"],
                "decision_at": "2026-08-16T08:30:00+08:00",
            },
            self.workspace,
        )
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["material_change_count"], 0)
        self.assertEqual(info["changes"][0]["change_type"], "retrieval_timestamp_changed")

        crossing = audit_snapshot_drift(
            {
                "schema_version": "0.1",
                "market": "CN",
                "before_snapshot_id": before["snapshot_id"],
                "after_snapshot_id": after["snapshot_id"],
                "decision_at": "2026-08-16T07:22:00+08:00",
            },
            self.workspace,
        )
        self.assertEqual(crossing["status"], "alert")
        self.assertEqual(crossing["changes"][0]["change_type"], "pit_eligibility_changed")


if __name__ == "__main__":
    unittest.main()
