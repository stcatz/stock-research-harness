from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from us_equity_research.core.contracts import ContractError
from us_equity_research.ingest.futu_quotes import (
    FutuQuoteClient,
    collect,
    normalize,
    validate_plan,
    write_new_bundle,
)


def plan():
    return {
        "symbols": ["US.TEST", "US.SPY"],
        "benchmark": "US.SPY",
        "start": "2026-09-10",
        "end": "2026-09-11",
        "license_attestation": "authorized_for_local_research_snapshot",
    }


def row(day="2026-09-11"):
    return {
        "date": int(day.replace("-", "")),
        "time_key": int(datetime.fromisoformat(day + "T00:00:00-04:00").timestamp() * 1000),
        "time_zone": -240,
        "open": 10,
        "high": 12,
        "low": 9,
        "close": 11,
        "volume": 100,
    }


class FutuTests(unittest.TestCase):
    def test_endpoint_allowlist_excludes_accounts_and_writes(self):
        calls = []
        client = FutuQuoteClient(transport=lambda p, q: calls.append(p))
        for p in [
            "/api/v1.0/trade/accounts",
            "/api/v1.0/quote/HK.00700/history-kline",
            "https://evil.invalid",
        ]:
            with self.assertRaises(ContractError):
                client.get(p, {})
        self.assertFalse(calls)

    def test_no_raw_error_or_token_in_exception(self):
        c = FutuQuoteClient(transport=lambda p, q: {"ret_code": 2, "ret_msg": "Bearer SECRET"})
        with self.assertRaises(ContractError) as caught:
            c.history("US.TEST", "2026-09-10", "2026-09-11", 0)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_repeated_page_is_refused(self):
        c = FutuQuoteClient(
            transport=lambda p, q: {"ret_code": 0, "data": {"kline_list": [], "next_time": 123}}
        )
        with self.assertRaises(ContractError):
            c.history("US.TEST", "2026-09-10", "2026-09-11", 0)

    def test_cursor_forwarded_and_settings_explicit(self):
        calls = []

        def transport(p, q):
            calls.append(q)
            return {
                "ret_code": 0,
                "data": {"kline_list": [row()], "next_time": 123 if len(calls) == 1 else 0},
            }

        c = FutuQuoteClient(transport=transport)
        c.history("US.TEST", "2026-09-10", "2026-09-11", 0)
        self.assertEqual(calls[1]["end"], 123)
        self.assertEqual(calls[0]["ktype"], 2)
        self.assertEqual(calls[0]["autype"], 0)
        self.assertEqual(calls[0]["extended_time"], 0)

    def test_timezone_and_conflicting_duplicate_rejected(self):
        good = row()
        bad = copy.deepcopy(good)
        bad["time_zone"] = -300
        with self.assertRaises(ContractError):
            normalize([(bad, 0)], "US.TEST", "2026-09-10", "2026-09-11", "2026-09-13T00:00:00Z")
        bad = copy.deepcopy(good)
        bad["close"] = 10
        with self.assertRaises(ContractError):
            normalize(
                [(good, 0), (bad, 0)], "US.TEST", "2026-09-10", "2026-09-11", "2026-09-13T00:00:00Z"
            )

    def test_unknown_publication_and_volume_precision_preserved(self):
        bars = normalize(
            [(row(), 2)], "US.TEST", "2026-09-10", "2026-09-11", "2026-09-13T00:00:00Z"
        )
        self.assertEqual(bars[0]["volume"], 1)
        self.assertEqual(bars[0]["published_at"], "UNKNOWN")
        self.assertEqual(bars[0]["available_at"], "2026-09-13T00:00:00Z")
        self.assertIn("T00:00:00", bars[0]["bar_label_at"])

    def test_invalid_ohlc_or_other_security_rejected(self):
        for key, value in [("close", float("nan")), ("high", 8), ("code", "US.OTHER")]:
            bad = row()
            bad[key] = value
            with self.assertRaises(ContractError):
                normalize([(bad, 0)], "US.TEST", "2026-09-10", "2026-09-11", "2026-09-13T00:00:00Z")

    def test_plan_requires_license_and_frozen_benchmark(self):
        for key, value in [
            ("license_attestation", "UNKNOWN"),
            ("benchmark", "US.OTHER"),
            ("symbols", ["US.TEST", "US.TEST"]),
        ]:
            p = plan()
            p[key] = value
            with self.assertRaises(ContractError):
                validate_plan(p)

    def test_collection_is_normalized_staging_only(self):
        def transport(p, q):
            if p.endswith("trading-days"):
                return {
                    "ret_code": 0,
                    "data": {
                        "trading_days": [
                            {
                                "time": "2026-09-11",
                                "trade_second": 23400,
                                "trade_date_type": "WHOLE",
                            }
                        ]
                    },
                }
            return {
                "ret_code": 0,
                "data": {"kline_list": [{**row(), "secret_field": "MUST_NOT_PERSIST"}]},
            }

        bundle = collect(
            plan(),
            FutuQuoteClient(transport=transport),
            lambda: datetime.fromisoformat("2026-09-13T00:00:00+00:00"),
        )
        self.assertEqual(bundle["status"], "STAGED_NOT_FORMAL_EVIDENCE")
        self.assertNotIn("MUST_NOT_PERSIST", str(bundle))
        self.assertEqual(len(bundle["securities"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage.json"
            write_new_bundle(bundle, path)
            with self.assertRaises(FileExistsError):
                write_new_bundle(bundle, path)

    def test_calendar_flows_through_sec_collection(self):
        import json

        from test_ingest_sec import FakeSecTransport, _clock, _market_payload, _seed

        from us_equity_research.ingest.sec import SecClient, collect_sec_snapshot

        payload = _market_payload()
        evidence = {
            **payload["evidence"][0],
            "evidence_id": "EV-CALENDAR",
            "source_level": "official",
            "category": "regulatory",
        }
        cal = {
            "scope": "US_CORE_EQUITIES",
            "source_evidence_ref": "EV-CALENDAR",
            "coverage_start": "2026-08-17",
            "coverage_end": "2026-08-18",
            "complete": True,
            "sessions": [
                {
                    "date": "2026-08-17",
                    "open_at": "2026-08-17T09:30:00-04:00",
                    "close_at": "2026-08-17T16:00:00-04:00",
                },
                {
                    "date": "2026-08-18",
                    "open_at": "2026-08-18T09:30:00-04:00",
                    "close_at": "2026-08-18T16:00:00-04:00",
                },
            ],
        }
        payload["calendar_bundle"] = {"session_calendar": cal, "evidence": evidence}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed.json"
            market = root / "market.json"
            seed.write_text(json.dumps(_seed()))
            market.write_text(json.dumps(payload))
            result = collect_sec_snapshot(
                workspace=root,
                seed_path=seed,
                market_path=market,
                snapshot_id="test-calendar",
                client=SecClient(user_agent="test test@example.com", transport=FakeSecTransport()),
                clock=_clock,
            )
            snap = json.loads((root / result["relative_path"]).read_text())
            self.assertEqual(snap["session_calendar"], cal)
            self.assertTrue(any(e["evidence_id"] == "EV-CALENDAR" for e in snap["evidence"]))

    def test_observation_report_uses_complete_windows_and_excludes_today_from_volume_mean(self):
        from datetime import timedelta

        from us_equity_research.ingest.futu_quotes import observation_report

        start = datetime.fromisoformat("2026-06-01T00:00:00-04:00")
        dates = [
            (start + timedelta(days=i)).date().isoformat()
            for i in range(100)
            if (start + timedelta(days=i)).weekday() < 5
        ][:61]
        sessions = [
            {"date": d, "open_at": d + "T09:30:00-04:00", "close_at": d + "T16:00:00-04:00"}
            for d in dates
        ]
        cal = {
            "session_calendar": {
                "scope": "US_CORE_EQUITIES",
                "complete": True,
                "source_evidence_ref": "C",
                "coverage_start": dates[0],
                "coverage_end": dates[-1],
                "sessions": sessions,
            },
            "evidence": {
                "evidence_id": "C",
                "source_level": "official",
                "available_at": "2026-01-01T00:00:00Z",
            },
        }
        stamp = dates[-1] + "T17:00:00-04:00"
        bars = [
            {"date": d, "close": 100 + i, "volume": 200 if i == 60 else 100, "available_at": stamp}
            for i, d in enumerate(dates)
        ]
        bundle = {
            "schema_version": "futu-stage-1",
            "market": "US",
            "retrieved_at": stamp,
            "plan": {"benchmark": "US.SPY"},
            "securities": [
                {"symbol": "US.TEST", "split_adjusted": bars},
                {"symbol": "US.SPY", "split_adjusted": [{**b, "close": 100} for b in bars]},
            ],
        }
        report = observation_report(bundle, cal, stamp)
        self.assertIn("60.00%", report)
        self.assertIn("14.29%", report)  # 160 / 140 - 1
        self.assertIn("2.00 |", report)
        bundle["securities"][0]["split_adjusted"].pop(-10)
        report = observation_report(bundle, cal, stamp)
        line = next(l for l in report.splitlines() if l.startswith("| US.TEST"))
        self.assertEqual(line.count("UNKNOWN"), 4)  # 20d, 60d, relative, volume
        with self.assertRaises(ContractError):
            observation_report(bundle, cal, dates[-1] + "T15:00:00-04:00")


class FutuMCPAdapterTests(unittest.TestCase):
    def test_mcp_timezone_and_bounded_partition(self):
        from us_equity_research.ingest.futu_quotes import FutuMCPQuoteClient

        calls = []

        def rpc(name, args):
            calls.append((name, args))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ret_code": 0,
                                "data": {"kline_list": [{"time_zone": -4}], "volume_precision": 0},
                                "pagination": {"has_more": False},
                            }
                        ),
                    }
                ]
            }

        rows = FutuMCPQuoteClient(rpc).history("US.MSFT", "2025-09-01", "2026-09-02", 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["end"], "2026-06-27")
        self.assertEqual(calls[1][1]["start"], "2026-06-28")
        self.assertEqual(rows[0][0]["time_zone"], -240)
        self.assertTrue(all(n == "quote_history_kline" for n, _ in calls))

    def test_mcp_rejects_partial_error_and_other_paths(self):
        from us_equity_research.ingest.futu_quotes import FutuMCPQuoteClient

        for result in [
            {"isError": True, "content": []},
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ret_code": 0,
                                "data": {"kline_list": []},
                                "pagination": {"has_more": True},
                            }
                        ),
                    }
                ]
            },
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"ret_code": 0, "data": {"kline_list": []}}),
                    }
                ]
            },
        ]:
            with self.assertRaises(ContractError):
                FutuMCPQuoteClient(lambda *a, result=result: result).history(
                    "US.MSFT", "2026-09-01", "2026-09-02", 0
                )
        with self.assertRaises(ContractError):
            FutuMCPQuoteClient(lambda *a: self.fail("Must not call RPC")).get(
                "/api/v1.0/account", {}
            )
