from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_engine import _load_demo_snapshot, _request

from us_equity_research.core.engine import build_research_packet
from us_equity_research.core.pipeline import read_artifact, run_research
from us_equity_research.core.snapshot import validate_snapshot


class UpgradeTests(unittest.TestCase):
    def test_engine_version_changes_identity(self):
        from us_equity_research.core import engine

        raw = _load_demo_snapshot()
        request = _request("daily_report")
        first = build_research_packet(
            validate_snapshot(raw), request, generated_at=request.decision_at
        )
        with patch.object(engine, "ENGINE_VERSION", "test-new-version"):
            second = build_research_packet(
                validate_snapshot(raw), request, generated_at=request.decision_at
            )
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])

    def test_future_context_cannot_leak_through_narrative(self):
        raw = _load_demo_snapshot()
        request = _request("daily_report")
        raw["market_context"]["evidence_refs"] = ["EV-FUTURE-001"]
        raw["market_context"]["regime"] = "FUTURE LABEL MUST NOT APPEAR"
        p = build_research_packet(validate_snapshot(raw), request, generated_at=request.decision_at)
        self.assertNotIn("FUTURE LABEL", str(p["market_context"]))

    def test_stale_real_prices_cannot_reach_observe(self):
        raw = _load_demo_snapshot()
        raw.update(data_mode="snapshot", pit_quality="P2")
        request = _request("daily_report", decision_at="2026-09-12T12:00:00-04:00")
        p = build_research_packet(validate_snapshot(raw), request, generated_at=request.decision_at)
        self.assertFalse(any(c["decision"] == "observe" for c in p["all_decisions"]))
        self.assertEqual(
            p["research_diagnostics"]["candidate_readiness"][0]["price_status"], "STALE"
        )

    def test_exclusions_remain_in_remediation_cards_not_priority_observations(self):
        raw = _load_demo_snapshot()
        request = _request("daily_report")
        p = build_research_packet(validate_snapshot(raw), request, generated_at=request.decision_at)
        cards = p["research_diagnostics"]["plan_cards"]
        excluded = {c["candidate_id"] for c in p["excluded"]}
        self.assertTrue(excluded)
        self.assertEqual(
            {c["candidate_id"] for c in cards}, {c["candidate_id"] for c in p["all_decisions"]}
        )
        for c in cards:
            if c["candidate_id"] in excluded:
                self.assertEqual(c["purpose"], "EVIDENCE_REPAIR")
            self.assertEqual(c["observed_result"], "PENDING_MANUAL_REVIEW")

    def test_unicode_pages_reassemble_exact_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = run_research(_request("daily_report").to_dict(), root)
            expected = (root / "artifacts/us/runs" / r["run_id"] / "report.md").read_text()
            parts = []
            cursor = 0
            while True:
                p = read_artifact(
                    {
                        "artifact_id": r["artifact_id"],
                        "section": "report",
                        "cursor": cursor,
                        "max_chars": 500,
                    },
                    root,
                )
                parts.append(p["content"])
                if not p["truncated"]:
                    break
                self.assertGreater(p["next_cursor"], cursor)
                cursor = p["next_cursor"]
            self.assertEqual("".join(parts), expected)

    def test_future_only_cases_do_not_display_post_cutoff_claim(self):
        raw = _load_demo_snapshot()
        request = _request("daily_report")
        c = raw["themes"][0]["candidates"][0]
        c["bull_case"] = {"text": "FUTURE BUSINESS CLAIM", "evidence_refs": ["EV-FUTURE-001"]}
        p = build_research_packet(validate_snapshot(raw), request, generated_at=request.decision_at)
        self.assertNotIn("FUTURE BUSINESS CLAIM", str(p["all_decisions"][0]["bull_case"]))

    def test_future_source_cannot_enter_calculations_or_theme_reason(self):
        raw = _load_demo_snapshot()
        request = _request("daily_report")
        fact = raw["financial_facts"][0]
        fact["evidence_ref"] = "EV-FUTURE-001"
        dimension = next(iter(raw["themes"][0]["dimensions"].values()))
        dimension.update(reason="FUTURE DIMENSION", evidence_refs=["EV-FUTURE-001"])
        p = build_research_packet(validate_snapshot(raw), request, generated_at=request.decision_at)
        self.assertNotIn("FUTURE DIMENSION", str(p["themes"]))
        self.assertFalse(
            any(f["fact_id"] == fact["fact_id"] for c in p["all_decisions"] for f in c["facts"])
        )

    def test_price_calendar_holiday_early_close_and_extended_hours(self):
        from types import SimpleNamespace

        from us_equity_research.core.research_diagnostics import (
            price_readiness,
            validate_session_calendar,
        )

        # Synthetic calendar for testing, not real source coverage.
        source = {"source_level": "official", "available_at": "2026-01-01T00:00:00Z"}
        market = {
            "evidence_id": "M",
            "source_level": "structured_market",
            "category": "market_price",
            "available_at": "2026-11-27T13:05:00-05:00",
            "as_of": "2026-11-27T13:00:00-05:00",
        }
        fact = {
            "fact_id": "F",
            "security_id": "US.TEST",
            "metric": "close_price",
            "unit": "USD/share",
            "value": 10,
            "period_end": "2026-11-27",
            "available_at": market["available_at"],
            "evidence_ref": "M",
        }
        data = {
            "session_calendar": {
                "scope": "US_CORE_EQUITIES",
                "complete": True,
                "source_evidence_ref": "C",
                "coverage_start": "2026-11-25",
                "coverage_end": "2026-11-30",
                "sessions": [
                    {
                        "date": "2026-11-25",
                        "open_at": "2026-11-25T09:30:00-05:00",
                        "close_at": "2026-11-25T16:00:00-05:00",
                    },
                    {
                        "date": "2026-11-27",
                        "open_at": "2026-11-27T09:30:00-05:00",
                        "close_at": "2026-11-27T13:00:00-05:00",
                    },
                    {
                        "date": "2026-11-30",
                        "open_at": "2026-11-30T09:30:00-05:00",
                        "close_at": "2026-11-30T16:00:00-05:00",
                    },
                ],
            }
        }
        snap = SimpleNamespace(
            data=data,
            data_mode="snapshot",
            evidence_by_id={"C": source, "M": market},
            financial_facts_by_id={"F": fact},
        )
        candidate = {"security_id": "US.TEST", "financial_fact_refs": ["F"]}
        cutoff = datetime.fromisoformat("2026-11-28T10:00:00-05:00")
        validate_session_calendar(data, snap.evidence_by_id)
        self.assertEqual(price_readiness(snap, candidate, cutoff)["status"], "CURRENT_CORE_SESSION")
        market["as_of"] = "2026-11-27T17:00:00-05:00"
        self.assertEqual(price_readiness(snap, candidate, cutoff)["status"], "SESSION_MISMATCH")
        market["as_of"] = "2026-11-27T13:00:00-05:00"
        source["available_at"] = "2026-12-01T00:00:00Z"
        self.assertEqual(price_readiness(snap, candidate, cutoff)["status"], "UNKNOWN_CALENDAR")
        data.clear()
        self.assertEqual(price_readiness(snap, candidate, cutoff)["status"], "UNKNOWN_CALENDAR")
        fact["unit"] = "USD"
        self.assertEqual(price_readiness(snap, candidate, cutoff)["status"], "UNKNOWN_NO_PRICE")

    def test_timezone_conversion_respects_daylight_savings(self):
        from us_equity_research.core.research_diagnostics import NEW_YORK, SHANGHAI

        for stamp, expected_hour in [
            ("2026-07-01T09:30:00-04:00", 21),
            ("2026-12-01T09:30:00-05:00", 22),
        ]:
            ny = datetime.fromisoformat(stamp).astimezone(NEW_YORK)
            self.assertEqual(ny.astimezone(SHANGHAI).hour, expected_hour)

    def test_complete_cli_returns_verified_whole_report(self):
        import io
        import json

        from us_equity_research.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = run_research(_request("daily_report").to_dict(), root)
            req = root / "read.json"
            req.write_text(
                json.dumps({"artifact_id": r["artifact_id"], "section": "report", "max_chars": 500})
            )
            out = io.StringIO()
            with patch(
                "us_equity_research.cli._write_json", side_effect=lambda p: out.write(json.dumps(p))
            ):
                code = main(
                    ["--workspace", tmp, "artifact-read", "--complete", "--request-json", str(req)]
                )
            result = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(result["full_report_verified"])
            self.assertEqual(
                result["content"],
                (root / "artifacts/us/runs" / r["run_id"] / "report.md").read_text(),
            )
            with self.assertRaises(ValueError):
                read_artifact(
                    {"artifact_id": r["artifact_id"], "cursor": result["total_chars"] + 100000},
                    root,
                )

    def test_legacy_report_remains_readable_under_its_frozen_engine(self):
        from us_equity_research.core.legacy_v01.engine import (
            build_research_packet as legacy_builder,
        )
        from us_equity_research.core.legacy_v01.reporting import render_report as legacy_renderer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("us_equity_research.core.pipeline.build_research_packet", legacy_builder),
                patch("us_equity_research.core.pipeline.render_report", legacy_renderer),
            ):
                old = run_research(_request("daily_report").to_dict(), root)
            read = read_artifact({"artifact_id": old["artifact_id"], "section": "report"}, root)
            self.assertIn("美股投研报告", read["content"])
            new = run_research(_request("daily_report").to_dict(), root)
            self.assertNotEqual(old["artifact_id"], new["artifact_id"])
