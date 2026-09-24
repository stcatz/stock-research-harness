from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from us_equity_research.core.contracts import ContractError
from us_equity_research.core.daily_review import analyze, verify_review, write_review
from us_equity_research.ingest.futu_mcp import FutuMCPTransport


def inputs():
    start = datetime.fromisoformat("2026-06-01T00:00:00-04:00")
    dates = [
        (start + timedelta(days=i)).date().isoformat()
        for i in range(90)
        if (start + timedelta(days=i)).weekday() < 5
    ][:61]
    stamp = dates[-1] + "T17:00:00-04:00"
    cal = {
        "session_calendar": {
            "scope": "US_CORE_EQUITIES",
            "complete": True,
            "source_evidence_ref": "C",
            "coverage_start": dates[0],
            "coverage_end": dates[-1],
            "sessions": [
                {"date": d, "open_at": d + "T09:30:00-04:00", "close_at": d + "T16:00:00-04:00"}
                for d in dates
            ],
        },
        "evidence": {
            "evidence_id": "C",
            "source_level": "official",
            "source_url": "https://www.nyse.com/trade/hours-calendars",
            "available_at": stamp,
        },
    }
    bars = [
        {"date": d, "close": 100 + i, "volume": 100, "available_at": stamp, "retrieved_at": stamp}
        for i, d in enumerate(dates)
    ]
    bundle = {
        "schema_version": "futu-stage-1",
        "market": "US",
        "provider": "futu_mcp",
        "retrieved_at": stamp,
        "plan": {
            "symbols": ["US.MSFT", "US.SPY"],
            "benchmark": "US.SPY",
            "start": dates[0],
            "end": dates[-1],
            "license_attestation": "authorized_for_local_research_snapshot",
        },
        "securities": [
            {
                "symbol": s,
                "split_adjusted": copy.deepcopy(bars),
                "source_url": "https://mcp.futunn.com/mcp",
            }
            for s in ["US.MSFT", "US.SPY"]
        ],
    }
    return bundle, cal, None, stamp


class DailyReviewTests(unittest.TestCase):
    def test_complete_windows_and_no_false_formal_readiness(self):
        p = analyze(*inputs())
        self.assertAlmostEqual(p["metrics"]["US.MSFT"]["returns"]["60"], 0.6)
        self.assertFalse(p["formal_market_eligible"])
        self.assertIsNone(p["sector_positive"])
        self.assertEqual(p["sec"]["status"], "MISSING")

    def test_missing_latest_or_internal_bar_degrades(self):
        b, c, s, t = inputs()
        b["securities"][0]["split_adjusted"].pop(-5)
        p = analyze(b, c, s, t)
        self.assertIsNone(p["metrics"]["US.MSFT"]["returns"]["20"])
        self.assertIsNotNone(p["metrics"]["US.MSFT"]["returns"]["1"])
        b["securities"][0]["split_adjusted"].pop()
        p = analyze(b, c, s, t)
        self.assertIsNone(p["metrics"]["US.MSFT"]["returns"]["1"])

    def test_future_duplicate_and_expired_calendar_rejected(self):
        b, c, s, t = inputs()
        b["securities"][0]["split_adjusted"].append(b["securities"][0]["split_adjusted"][-1])
        with self.assertRaises(ContractError):
            analyze(b, c, s, t)
        b, c, s, t = inputs()
        b["securities"][0]["split_adjusted"][0]["retrieved_at"] = "2030-01-01T00:00:00Z"
        with self.assertRaises(ContractError):
            analyze(b, c, s, t)
        b, c, s, t = inputs()
        with self.assertRaises(ContractError):
            analyze(b, c, s, "2027-01-01T00:00:00Z")

    def test_artifact_recomputes_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "run"
            write_review(*inputs(), target)
            self.assertEqual(verify_review(target)["status"], "VERIFIED")
            with self.assertRaises(FileExistsError):
                write_review(*inputs(), target)
            (target / "report.md").write_text("modified")
            with self.assertRaises(ContractError):
                verify_review(target)


class MCPTransportTests(unittest.TestCase):
    def client(self, outcomes):
        c = object.__new__(FutuMCPTransport)
        c._interval = 0
        c.waits = []
        c._sleep = c.waits.append

        def rpc(*args):
            return {
                "content": [{"type": "text", "text": json.dumps({"ret_code": outcomes.pop(0)})}]
            }

        c._rpc = rpc
        return c

    def test_quote_allowlist_and_bounded_retry(self):
        c = self.client([-11, 0])
        c("quote_trading_days", {"market": "US"})
        self.assertEqual(c.waits, [0, 30])
        c = self.client([-11, -11, -11])
        with self.assertRaises(ContractError):
            c("quote_trading_days", {"market": "US"})
        self.assertEqual(c.waits, [0, 30, 60])
        for tool, args in [
            ("trading_order_place", {}),
            ("account_info", {}),
            ("quote_trading_days", {"market": "HK"}),
        ]:
            with self.assertRaises(ContractError):
                self.client([])(tool, args)


class DailyCalendarGuardTests(unittest.TestCase):
    def test_old_calendar_and_half_session_volume(self):
        b, c, s, t = inputs()
        c["evidence"]["retrieved_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(ContractError):
            analyze(b, c, s, t)
        b, c, s, t = inputs()
        row = c["session_calendar"]["sessions"][-2]
        row["close_at"] = row["date"] + "T13:00:00-04:00"
        result = analyze(b, c, s, t)
        self.assertIsNone(result["metrics"]["US.MSFT"]["volume_ratio"])
        self.assertIsNotNone(result["metrics"]["US.MSFT"]["returns"]["20"])
