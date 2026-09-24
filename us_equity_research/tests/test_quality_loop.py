from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from us_equity_research.core.contracts import ContractError
from us_equity_research.core.drift import audit_snapshot_drift
from us_equity_research.core.outcomes import (
    record_outcome,
    summarize_outcome_history,
    summarize_outcomes,
)
from us_equity_research.core.pipeline import read_artifact, run_research


class QualityLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.request = {
            "schema_version": "0.1",
            "market": "US",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00-04:00",
            "snapshot": {"selector": "demo"},
            "top_n": 5,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quality_queue_and_fact_packet_are_replay_safe(self) -> None:
        result = run_research(self.request, self.workspace)
        pages: list[str] = []
        cursor = 0
        while True:
            fact_page = read_artifact(
                {
                    "artifact_id": result["artifact_id"],
                    "section": "facts",
                    "max_chars": 20000,
                    "cursor": cursor,
                },
                self.workspace,
            )
            pages.append(fact_page["content"])
            if fact_page["next_cursor"] is None:
                break
            cursor = fact_page["next_cursor"]
        fact_text = "".join(pages)
        self.assertNotIn('"thesis"', fact_text)
        self.assertIn('"fact_packet_hash"', fact_text)

    def test_us_outcome_sidecar_obeys_availability_cutoff(self) -> None:
        result = run_research(self.request, self.workspace)
        observation = {
            "schema_version": "0.1",
            "market": "US",
            "run_id": result["run_id"],
            "symbol": "DEMOA",
            "horizon_trading_days": 5,
            "observed_at": "2026-08-24T16:00:00-04:00",
            "available_at": "2026-08-24T16:05:00-04:00",
            "candidate_return": 0.06,
            "benchmark_return": 0.02,
            "benchmark_symbol": "SPY",
            "source_url": "https://data.example.com/us-close",
            "source_document_id": "licensed-close-20260824",
        }
        recorded = record_outcome(observation, self.workspace)
        self.assertAlmostEqual(recorded["excess_return"], 0.04)
        summary = summarize_outcomes(
            {
                "schema_version": "0.1",
                "market": "US",
                "run_id": result["run_id"],
                "evaluation_at": "2026-08-24T20:06:00+00:00",
            },
            self.workspace,
        )
        self.assertEqual(summary["observation_count"], 1)
        self.assertEqual(summary["status"], "incomplete")
        history = summarize_outcome_history(
            {
                "schema_version": "0.1",
                "market": "US",
                "evaluation_at": "2026-08-24T20:06:00+00:00",
                "limit": 5,
            },
            self.workspace,
        )
        self.assertEqual(history["run_count"], 1)

    def test_us_outcome_record_rejects_an_ambiguous_symbol(self) -> None:
        result = run_research(self.request, self.workspace)
        database = self.workspace / "data" / "us_stock_research.sqlite3"
        with sqlite3.connect(database) as connection:
            original_id = connection.execute(
                """
                SELECT candidate_id FROM candidate_decisions
                WHERE run_id = ? AND symbol = 'DEMOA'
                ORDER BY candidate_id LIMIT 1
                """,
                (result["run_id"],),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    run_id, candidate_id, market, theme_id, security_id, symbol,
                    name, decision, reasons_json, evidence_refs_json,
                    data_gaps_json, decision_json
                )
                SELECT run_id, candidate_id || ':duplicate', market,
                       theme_id || '-duplicate', security_id || '-duplicate', symbol,
                       name, decision, reasons_json, evidence_refs_json,
                       data_gaps_json, decision_json
                FROM candidate_decisions
                WHERE run_id = ? AND candidate_id = ?
                """,
                (result["run_id"], original_id),
            )

        observation = {
            "schema_version": "0.1",
            "market": "US",
            "run_id": result["run_id"],
            "symbol": "DEMOA",
            "horizon_trading_days": 5,
            "observed_at": "2026-08-24T16:00:00-04:00",
            "available_at": "2026-08-24T16:05:00-04:00",
            "candidate_return": 0.06,
            "benchmark_return": 0.02,
            "benchmark_symbol": "SPY",
            "source_url": "https://data.example.com/us-close",
            "source_document_id": "licensed-close-20260824",
        }
        with self.assertRaisesRegex(ContractError, "candidate_id is required"):
            record_outcome(observation, self.workspace)

        recorded = record_outcome(
            {**observation, "candidate_id": original_id},
            self.workspace,
        )
        self.assertEqual(recorded["candidate_id"], original_id)

    def test_us_drift_detects_a_disappearing_historical_fact(self) -> None:
        raw = json.loads(
            files("us_equity_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        before = copy.deepcopy(raw)
        after = copy.deepcopy(raw)
        before["snapshot_id"] = "us-before-drift"
        after["snapshot_id"] = "us-after-drift"
        after["retrieved_at"] = "2026-08-16T11:30:00-04:00"
        removed = after["financial_facts"].pop(0)
        next(
            fact
            for fact in after["financial_facts"]
            if fact["fact_id"] == "FACT-DEMOA-REV-TTM-PRIOR"
        )["available_at"] = "2026-08-14T17:29:00-04:00"
        candidate = after["themes"][0]["candidates"][0]
        candidate["financial_fact_refs"].remove(removed["fact_id"])
        for payload in (before, after):
            path = (
                self.workspace
                / "data"
                / "normalized"
                / "us"
                / payload["snapshot_id"]
                / "snapshot.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        receipt = audit_snapshot_drift(
            {
                "schema_version": "0.1",
                "market": "US",
                "before_snapshot_id": before["snapshot_id"],
                "after_snapshot_id": after["snapshot_id"],
            },
            self.workspace,
        )
        self.assertEqual(receipt["status"], "alert")
        self.assertIn(
            "historical_fact_disappeared",
            {item["change_type"] for item in receipt["changes"]},
        )
        self.assertIn(
            "historical_time_semantics_changed",
            {item["change_type"] for item in receipt["changes"]},
        )


if __name__ == "__main__":
    unittest.main()
