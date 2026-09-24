from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from us_equity_research.core.contracts import ContractError
from us_equity_research.core.research_ledger import append_record, read_record, select_expectation
from us_equity_research.ingest.exchange_calendar import build_calendar, parse_schedule
from us_equity_research.ingest.fundamentals import ttm


def financial(i, start, end, value, **extras):
    return {
        "fact_id": i,
        "concept": "Revenues",
        "unit": "USD",
        "period_start": start,
        "period_end": end,
        "value": value,
        "available_at": "2026-05-01T00:00:00Z",
        "retrieved_at": "2026-05-01T00:00:00Z",
        "published_at": "2026-04-30T00:00:00Z",
        **extras,
    }


class TTMTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            financial("FY", "2025-01-01", "2025-12-31", 100),
            financial("YTD", "2026-01-01", "2026-03-31", 35),
            financial("PRIOR", "2025-01-01", "2025-03-31", 20),
        ]

    def test_correct_bridge_and_all_source_ids(self):
        r = ttm(self.rows, ["Revenues"], "2026-05-02T00:00:00Z")
        self.assertEqual(r["value"], 115)
        self.assertEqual(r["input_fact_ids"], ["FY", "YTD", "PRIOR"])
        self.assertEqual(r["period_start"], "2025-04-01")

    def test_missing_comparable_period_never_uses_stale_fy(self):
        self.assertEqual(
            ttm(self.rows[:2], ["Revenues"], "2026-05-02T00:00:00Z")["status"], "UNKNOWN"
        )

    def test_quarter_is_not_ytd_and_concepts_cannot_mix(self):
        self.rows.append(financial("Q2", "2026-04-01", "2026-06-30", 50))
        self.assertEqual(ttm(self.rows, ["Revenues"], "2026-08-02T00:00:00Z")["status"], "UNKNOWN")
        self.rows = self.rows[:3]
        self.rows[-1]["concept"] = "SalesRevenueNet"
        self.assertEqual(ttm(self.rows, ["Revenues"], "2026-05-02T00:00:00Z")["status"], "UNKNOWN")

    def test_future_inputs_and_currency_are_excluded(self):
        self.rows[1]["available_at"] = "2026-08-01T00:00:00Z"
        self.rows[1]["retrieved_at"] = "2026-08-01T00:00:00Z"
        self.assertEqual(
            ttm(self.rows, ["Revenues"], "2026-05-02T00:00:00Z")["basis"], "FY_EQUALS_TTM"
        )
        self.rows[0]["unit"] = "EUR"
        self.assertEqual(ttm(self.rows, ["Revenues"], "2026-05-02T00:00:00Z")["status"], "UNKNOWN")


class CalendarTests(unittest.TestCase):
    def schedule(self):
        days = [
            "January 1",
            "January 19",
            "February 16",
            "April 3",
            "May 25",
            "June 19",
            "July 3",
            "September 7",
            "November 26",
            "December 25",
        ]
        html = (
            "<table><tr><th>Holiday</th><th>Date</th><th>Market Status</th></tr>"
            + "".join(f"<tr><td>Holiday</td><td>{d}, 2026</td><td>Closed</td></tr>" for d in days)
            + "<tr><td>Early Close</td><td>November 27, 2026</td><td>1:00 p.m.</td></tr></table><table><tr><td>The Nasdaq Stock Market</td><td>9:30 am – 4:00 pm (ET)</td></tr></table>"
        )
        return parse_schedule(html, 2026, "2026-09-13T12:00:00Z")

    def test_dst_early_close_and_holiday(self):
        c = build_calendar(self.schedule(), "2026-03-01", "2026-11-30", "2026-09-13T13:00:00Z")[
            "session_calendar"
        ]
        rows = {r["date"]: r for r in c["sessions"]}
        self.assertTrue(rows["2026-03-06"]["open_at"].endswith("-05:00"))
        self.assertTrue(rows["2026-03-09"]["open_at"].endswith("-04:00"))
        self.assertIn("T13:00:", rows["2026-11-27"]["close_at"])
        self.assertNotIn("2026-09-07", rows)

    def test_layout_expiry_and_supplier_conflict(self):
        with self.assertRaises(ContractError):
            parse_schedule("<html>unavailable</html>", 2026, "2026-09-13T12:00:00Z")
        with self.assertRaises(ContractError):
            build_calendar(self.schedule(), "2026-03-01", "2026-11-30", "2026-10-01T00:00:00Z")
        with self.assertRaises(ContractError):
            build_calendar(
                self.schedule(),
                "2026-03-01",
                "2026-11-30",
                "2026-09-13T13:00:00Z",
                {"start": "2026-09-11", "end": "2026-09-11", "days": []},
            )


class LedgerTests(unittest.TestCase):
    def record(self):
        stamp = "2026-09-01T00:00:00Z"
        return {
            "kind": "expectation",
            "symbol": "MSFT",
            "metric": "eps",
            "period_end": "2026-09-30",
            "value": 4,
            "unit": "USD/share",
            "source_url": "https://example.org/estimates",
            "source_level": "structured_market",
            "period_type": "quarterly",
            "accounting_basis": "GAAP",
            "published_at": stamp,
            "effective_at": stamp,
            "available_at": stamp,
            "retrieved_at": stamp,
            "as_of": stamp,
            "license_attestation": "authorized_for_local_research_snapshot",
        }

    def test_late_import_cannot_be_replayed_before_first_observation(self):
        with tempfile.TemporaryDirectory() as d:
            r = append_record(d, self.record(), clock=lambda: datetime(2026, 9, 13, tzinfo=UTC))
            self.assertIsNone(
                select_expectation(
                    d,
                    "MSFT",
                    "eps",
                    "2026-09-30",
                    "2026-09-10T00:00:00Z",
                    period_type="quarterly",
                    accounting_basis="GAAP",
                )
            )
            self.assertEqual(
                select_expectation(
                    d,
                    "MSFT",
                    "eps",
                    "2026-09-30",
                    "2026-09-14T00:00:00Z",
                    period_type="quarterly",
                    accounting_basis="GAAP",
                )["record"]["value"],
                4,
            )
            with self.assertRaises(FileExistsError):
                append_record(d, self.record(), clock=lambda: datetime(2026, 9, 13, tzinfo=UTC))
            (Path(d) / (r["record_id"] + ".json")).write_text("{}")
            with self.assertRaises((ContractError, KeyError)):
                read_record(d, r["record_id"])

    def test_revision_preserves_history_and_unit_check(self):
        with tempfile.TemporaryDirectory() as d:
            r = append_record(d, self.record(), clock=lambda: datetime(2026, 9, 13, tzinfo=UTC))
            rev = {**self.record(), "value": 5, "supersedes": r["record_id"]}
            append_record(d, rev, clock=lambda: datetime(2026, 9, 14, tzinfo=UTC))
            self.assertEqual(
                select_expectation(
                    d,
                    "MSFT",
                    "eps",
                    "2026-09-30",
                    "2026-09-13T23:00:00Z",
                    period_type="quarterly",
                    accounting_basis="GAAP",
                )["record"]["value"],
                4,
            )
            self.assertEqual(
                select_expectation(
                    d,
                    "MSFT",
                    "eps",
                    "2026-09-30",
                    "2026-09-15T00:00:00Z",
                    period_type="quarterly",
                    accounting_basis="GAAP",
                )["record"]["value"],
                5,
            )
            bad = copy.deepcopy(self.record())
            bad["unit"] = "USD"
            with self.assertRaises(ContractError):
                append_record(d, bad)

    def test_plan_outcome_and_estimate_basis_do_not_mix(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.record()
            p.update(kind="plan", source_url="UNKNOWN", artifact_ref="a" * 64)
            plan = append_record(d, p, clock=lambda: datetime(2026, 9, 13, tzinfo=UTC))
            outcome = {**p, "kind": "outcome", "plan_ref": plan["record_id"], "result": "unknown"}
            append_record(d, outcome, clock=lambda: datetime(2026, 9, 14, tzinfo=UTC))
            estimate = self.record()
            estimate["accounting_basis"] = "adjusted"
            append_record(d, estimate, clock=lambda: datetime(2026, 9, 13, tzinfo=UTC))
            self.assertIsNone(
                select_expectation(
                    d,
                    "MSFT",
                    "eps",
                    "2026-09-30",
                    "2026-09-15T00:00:00Z",
                    period_type="quarterly",
                    accounting_basis="GAAP",
                )
            )
