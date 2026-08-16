from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from importlib.resources import files

from us_equity_research.core.contracts import RunRequest
from us_equity_research.core.engine import METHOD_ID, build_research_packet
from us_equity_research.core.snapshot import validate_snapshot

DECISION_AT = "2026-08-16T08:30:00-04:00"


def _load_demo_snapshot() -> dict[str, object]:
    resource = files("us_equity_research.fixtures").joinpath("demo_snapshot.json")
    with resource.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _request(workflow: str, **extra: object) -> RunRequest:
    payload: dict[str, object] = {
        "schema_version": "0.1",
        "workflow": workflow,
        "decision_at": DECISION_AT,
        "snapshot": {"selector": "demo"},
    }
    payload.update(extra)
    return RunRequest.from_dict(payload)


def _append_second_theme(raw: dict[str, object]) -> None:
    second_theme = copy.deepcopy(raw["themes"][0])  # type: ignore[index]
    second_theme["theme_id"] = "theme-us-automation-services"
    second_theme["name"] = "Synthetic automation service follow-through"
    second_theme["event_type"] = "service_follow_through"
    second_theme["evidence_refs"] = [
        "EV-MARKET-001",
        "EV-MACRO-001",
        "EV-INDUSTRY-001",
        "EV-SECONDARY-001",
    ]
    second_theme["candidates"] = [copy.deepcopy(raw["themes"][0]["candidates"][1])]  # type: ignore[index]
    second_theme["candidates"][0]["security_id"] = "US.DEMOD"  # type: ignore[index]
    second_theme["candidates"][0]["symbol"] = "DEMOD"  # type: ignore[index]
    second_theme["candidates"][0]["name"] = "Demo Service Follow-through D"  # type: ignore[index]
    second_theme["candidates"][0]["thesis"] = (  # type: ignore[index]
        "Synthetic service follow-through remains informational only until official issuer evidence improves."
    )
    raw["themes"].append(second_theme)  # type: ignore[index]


def _reverse_equivalent_order_lists(raw: dict[str, object]) -> None:
    raw["themes"].reverse()  # type: ignore[index]
    raw["evidence"].reverse()  # type: ignore[index]
    raw["market_context"]["evidence_refs"].reverse()  # type: ignore[index]
    for theme in raw["themes"]:  # type: ignore[index]
        theme["evidence_refs"].reverse()
        theme["candidates"].reverse()
        for dimension in theme["dimensions"].values():
            dimension["evidence_refs"].reverse()
        for candidate in theme["candidates"]:
            candidate["evidence_refs"].reverse()
            candidate["market_evidence_refs"].reverse()
            candidate["financial_fact_refs"].reverse()
            candidate["bull_case"]["evidence_refs"].reverse()
            candidate["bear_case"]["evidence_refs"].reverse()
            candidate["risk_verdict"]["evidence_refs"].reverse()


class EngineTests(unittest.TestCase):
    def test_daily_report_packet_is_canonical_and_stable(self) -> None:
        snapshot = validate_snapshot(_load_demo_snapshot())
        request = _request("daily_report")

        first = build_research_packet(
            snapshot,
            request,
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        second = build_research_packet(
            snapshot,
            request,
            generated_at=datetime.fromisoformat("2026-08-16T11:35:00-04:00"),
        )

        self.assertEqual(
            set(first),
            {
                "schema_version",
                "market",
                "writer_mode",
                "method_id",
                "run_id",
                "artifact_id",
                "workflow",
                "decision_at",
                "generated_at",
                "snapshot_id",
                "snapshot_hash",
                "data_mode",
                "pit_quality",
                "data_status",
                "market_context",
                "themes",
                "focus",
                "excluded",
                "all_decisions",
                "warnings",
                "data_gaps",
                "analysis_hash",
            },
        )
        self.assertEqual(first["schema_version"], "0.1")
        self.assertEqual(first["market"], "US")
        self.assertEqual(first["writer_mode"], "engine")
        self.assertEqual(first["method_id"], METHOD_ID)
        self.assertEqual(first["workflow"], "daily_report")
        self.assertEqual(first["decision_at"], DECISION_AT)
        self.assertEqual(first["data_mode"], "fixture")
        self.assertEqual(first["pit_quality"], "FIXTURE")
        self.assertEqual(first["snapshot_id"], "us-demo-20260816")
        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(first["analysis_hash"], second["analysis_hash"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["data_status"]["status"], "completed")
        self.assertIn("EV-FUTURE-001", "\n".join(first["warnings"]))

        counts = {
            decision: sum(1 for item in first["all_decisions"] if item["decision"] == decision)
            for decision in ("observe", "continue_research", "exclude")
        }
        self.assertEqual(counts, {"observe": 1, "continue_research": 1, "exclude": 1})
        self.assertEqual(len(first["focus"]), 2)
        self.assertEqual(len(first["excluded"]), 1)
        self.assertEqual(
            set(first["themes"][0]["dimensions"]),
            {"materiality", "novelty", "breadth", "durability", "timeliness"},
        )

        decisions = {item["symbol"]: item for item in first["all_decisions"]}
        self.assertEqual(decisions["DEMOA"]["decision"], "observe")
        self.assertEqual(decisions["DEMOB"]["decision"], "continue_research")
        self.assertEqual(decisions["DEMOC"]["decision"], "exclude")

        demoa = decisions["DEMOA"]
        self.assertEqual(
            set(demoa),
            {
                "candidate_id",
                "theme_id",
                "theme_name",
                "security_id",
                "symbol",
                "name",
                "role",
                "decision",
                "decision_label",
                "thesis",
                "bull_case",
                "bear_case",
                "risk_verdict",
                "transmission_chain",
                "next_catalyst_at",
                "invalidation_conditions",
                "gates",
                "reasons",
                "risk_flags",
                "data_gaps",
                "manual_review_items",
                "usable_evidence_refs",
                "time_leak_evidence_refs",
                "evidence",
                "facts",
                "calculations",
            },
        )
        self.assertEqual(demoa["decision_label"], "Observe")
        self.assertEqual(set(demoa["bull_case"]), {"text", "evidence_refs"})
        self.assertEqual(set(demoa["bear_case"]), {"text", "evidence_refs"})
        self.assertEqual(set(demoa["risk_verdict"]), {"text", "evidence_refs"})
        self.assertTrue(demoa["gates"]["official_primary"])
        self.assertTrue(demoa["gates"]["structured_market"])
        self.assertTrue(demoa["gates"]["transmission_chain"])
        self.assertTrue(demoa["gates"]["three_viewpoints"])
        self.assertTrue(demoa["gates"]["invalidation"])
        self.assertTrue(demoa["gates"]["future_catalyst"])
        self.assertTrue(demoa["gates"]["no_time_leaks"])
        self.assertTrue(demoa["gates"]["stage_not_fading"])
        self.assertTrue(demoa["gates"]["calculations_complete"])
        self.assertTrue(demoa["gates"]["issuer_specific_official"])

        for evidence in demoa["evidence"]:
            self.assertEqual(
                set(evidence),
                {
                    "evidence_id",
                    "category",
                    "source_level",
                    "title",
                    "source_url",
                    "source_document_id",
                    "published_at",
                    "effective_at",
                    "available_at",
                    "retrieved_at",
                    "as_of",
                    "summary",
                },
            )
        for fact in demoa["facts"]:
            self.assertEqual(
                set(fact),
                {
                    "fact_id",
                    "metric",
                    "value",
                    "unit",
                    "period_end",
                    "available_at",
                    "evidence_ref",
                },
            )
        for calculation in demoa["calculations"]:
            self.assertEqual(
                set(calculation),
                {
                    "metric",
                    "status",
                    "formula",
                    "value",
                    "unit",
                    "input_fact_ids",
                    "period_end",
                    "price_as_of",
                    "available_at_cutoff_passed",
                    "evidence_refs",
                },
            )

        demob = decisions["DEMOB"]
        self.assertFalse(demob["gates"]["issuer_specific_official"])
        self.assertFalse(demob["gates"]["calculations_complete"])
        self.assertIn("Missing issuer-specific official disclosure.", demob["reasons"])

        democ = decisions["DEMOC"]
        self.assertEqual(democ["usable_evidence_refs"], ["EV-SECONDARY-001"])
        self.assertEqual(democ["time_leak_evidence_refs"], ["EV-FUTURE-001"])
        self.assertFalse(democ["gates"]["official_primary"])
        self.assertFalse(democ["gates"]["structured_market"])
        self.assertFalse(democ["gates"]["no_time_leaks"])

    def test_theme_and_stock_workflows_filter_without_fallback(self) -> None:
        snapshot = validate_snapshot(_load_demo_snapshot())

        theme_packet = build_research_packet(
            snapshot,
            _request("theme_research", subject="AUTOMATION PLATFORM"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        self.assertEqual(theme_packet["subject"], "AUTOMATION PLATFORM")
        self.assertEqual(len(theme_packet["themes"]), 1)
        self.assertEqual(theme_packet["themes"][0]["theme_id"], "theme-us-automation-platforms")
        self.assertEqual(len(theme_packet["all_decisions"]), 3)

        stock_packet = build_research_packet(
            snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        self.assertEqual(stock_packet["symbol"], "DEMOA")
        self.assertEqual(len(stock_packet["themes"]), 1)
        self.assertEqual([item["symbol"] for item in stock_packet["all_decisions"]], ["DEMOA"])

        no_match = build_research_packet(
            snapshot,
            _request("theme_research", subject="semiconductors"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        self.assertEqual(no_match["data_status"]["status"], "completed")
        self.assertEqual(no_match["themes"], [])
        self.assertEqual(no_match["all_decisions"], [])
        self.assertTrue(any("No themes matched" in warning for warning in no_match["warnings"]))

    def test_hard_risks_and_fading_stage_force_exclude(self) -> None:
        hard_risk_raw = copy.deepcopy(_load_demo_snapshot())
        hard_risk_raw["themes"][0]["candidates"][0]["risk_flags"].append("going_concern")  # type: ignore[index]
        hard_risk_snapshot = validate_snapshot(hard_risk_raw)
        hard_risk_packet = build_research_packet(
            hard_risk_snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        self.assertEqual(hard_risk_packet["all_decisions"][0]["decision"], "exclude")

        fading_raw = copy.deepcopy(_load_demo_snapshot())
        fading_raw["themes"][0]["stage"] = "fading"  # type: ignore[index]
        fading_snapshot = validate_snapshot(fading_raw)
        fading_packet = build_research_packet(
            fading_snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        self.assertEqual(fading_packet["all_decisions"][0]["decision"], "exclude")

    def test_missing_viewpoint_support_blocks_observe_and_sets_reason(self) -> None:
        raw = copy.deepcopy(_load_demo_snapshot())
        raw["themes"][0]["candidates"][0]["bull_case"]["evidence_refs"] = []  # type: ignore[index]
        snapshot = validate_snapshot(raw)

        packet = build_research_packet(
            snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        decision = packet["all_decisions"][0]

        self.assertNotEqual(decision["decision"], "observe")
        self.assertEqual(decision["decision"], "continue_research")
        self.assertFalse(decision["gates"]["three_viewpoints"])
        self.assertIn(
            "Bull, bear, and risk viewpoints need usable supporting evidence.",
            decision["reasons"],
        )

    def test_post_cutoff_candidate_fact_is_removed_and_warned(self) -> None:
        raw = copy.deepcopy(_load_demo_snapshot())
        raw["financial_facts"].append(  # type: ignore[index]
            {
                "fact_id": "FACT-DEMOA-FCF-TTM-FUTURE",
                "security_id": "US.DEMOA",
                "metric": "free_cash_flow_ttm",
                "value": 33000000.0,
                "unit": "USD",
                "period_end": "2026-06-30",
                "available_at": "2026-08-16T09:45:00-04:00",
                "evidence_ref": "EV-SEC-001",
            }
        )
        raw["themes"][0]["candidates"][0]["financial_fact_refs"].remove("FACT-DEMOA-FCF-TTM")  # type: ignore[index]
        raw["themes"][0]["candidates"][0]["financial_fact_refs"].append("FACT-DEMOA-FCF-TTM-FUTURE")  # type: ignore[index]
        snapshot = validate_snapshot(raw)

        packet = build_research_packet(
            snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        decision = packet["all_decisions"][0]
        calculations = {item["metric"]: item for item in decision["calculations"]}

        self.assertEqual(calculations["free_cash_flow_margin"]["status"], "UNKNOWN")
        self.assertEqual(calculations["free_cash_flow_yield"]["status"], "UNKNOWN")
        self.assertTrue(
            any("FACT-DEMOA-FCF-TTM-FUTURE" in warning for warning in packet["warnings"])
        )

    def test_packet_never_introduces_forecast_or_target_price_fields(self) -> None:
        snapshot = validate_snapshot(_load_demo_snapshot())
        packet = build_research_packet(
            snapshot,
            _request("daily_report"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        serialized = json.dumps(packet, sort_keys=True)
        self.assertNotIn("target_price", serialized)
        self.assertNotIn("forecast", serialized)

    def test_data_status_uses_chronological_datetimes_across_mixed_offsets(self) -> None:
        raw = copy.deepcopy(_load_demo_snapshot())
        raw["evidence"].extend(  # type: ignore[index]
            [
                {
                    "evidence_id": "EV-OFFSET-OFFICIAL-EARLY",
                    "source_level": "official",
                    "category": "issuer_ir",
                    "title": "Earlier mixed-offset official evidence",
                    "summary": "Lexically later timestamp but chronologically earlier than existing macro evidence.",
                    "source_url": "https://ir.example.invalid/us/demoa/offset-official",
                    "source_document_id": "doc-ir-demoa-offset-official",
                    "published_at": "2026-08-15T15:30:00+09:00",
                    "effective_at": "2026-08-15T15:30:00+09:00",
                    "available_at": "2026-08-15T16:00:00+09:00",
                    "retrieved_at": "2026-08-16T07:45:00-04:00",
                    "as_of": "2026-08-15T16:00:00+09:00",
                },
                {
                    "evidence_id": "EV-OFFSET-MARKET-EARLY",
                    "source_level": "structured_market",
                    "category": "market_price",
                    "title": "Earlier mixed-offset market evidence",
                    "summary": "Lexically later timestamp but chronologically earlier than the existing close evidence.",
                    "source_url": "https://market.example.invalid/us/demoa/offset-close",
                    "source_document_id": "doc-market-demoa-offset-close",
                    "published_at": "2026-08-16T00:30:00+09:00",
                    "effective_at": "2026-08-16T00:30:00+09:00",
                    "available_at": "2026-08-16T00:30:00+09:00",
                    "retrieved_at": "2026-08-16T07:46:00-04:00",
                    "as_of": "2026-08-16T00:30:00+09:00",
                },
            ]
        )
        raw["themes"][0]["candidates"][0]["evidence_refs"].append("EV-OFFSET-OFFICIAL-EARLY")  # type: ignore[index]
        raw["themes"][0]["candidates"][0]["market_evidence_refs"].append("EV-OFFSET-MARKET-EARLY")  # type: ignore[index]
        snapshot = validate_snapshot(raw)

        packet = build_research_packet(
            snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )

        self.assertEqual(
            packet["data_status"]["official_primary_latest_available_at"],
            "2026-08-15T09:00:00-04:00",
        )
        self.assertEqual(packet["data_status"]["market_latest_as_of"], "2026-08-15T16:00:00-04:00")

    def test_packet_facts_and_analysis_hash_are_stable_under_reversed_fact_inputs(self) -> None:
        base_raw = copy.deepcopy(_load_demo_snapshot())
        reversed_raw = copy.deepcopy(base_raw)
        reversed_raw["financial_facts"].reverse()  # type: ignore[index]
        reversed_refs = reversed_raw["themes"][0]["candidates"][0]["financial_fact_refs"]  # type: ignore[index]
        reversed_refs[:] = list(reversed(reversed_refs))

        base_snapshot = validate_snapshot(base_raw)
        reversed_snapshot = validate_snapshot(reversed_raw)
        base_packet = build_research_packet(
            base_snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        reversed_packet = build_research_packet(
            reversed_snapshot,
            _request("stock_research", symbol="DEMOA"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )

        base_decision = base_packet["all_decisions"][0]
        reversed_decision = reversed_packet["all_decisions"][0]

        self.assertEqual(
            [fact["fact_id"] for fact in base_decision["facts"]],
            [
                "FACT-DEMOA-REV-TTM",
                "FACT-DEMOA-REV-TTM-PRIOR",
                "FACT-DEMOA-OPINC-TTM",
                "FACT-DEMOA-FCF-TTM",
                "FACT-DEMOA-CASH",
                "FACT-DEMOA-DEBT",
                "FACT-DEMOA-SHARES",
                "FACT-DEMOA-CLOSE",
            ],
        )
        self.assertEqual(base_decision["facts"], reversed_decision["facts"])
        self.assertEqual(base_packet["analysis_hash"], reversed_packet["analysis_hash"])
        self.assertEqual(base_decision["calculations"], reversed_decision["calculations"])

    def test_packet_is_stable_under_equivalent_theme_candidate_and_ref_reordering(self) -> None:
        base_raw = copy.deepcopy(_load_demo_snapshot())
        _append_second_theme(base_raw)
        reordered_raw = copy.deepcopy(base_raw)
        _reverse_equivalent_order_lists(reordered_raw)

        base_snapshot = validate_snapshot(base_raw)
        reordered_snapshot = validate_snapshot(reordered_raw)
        base_packet = build_research_packet(
            base_snapshot,
            _request("daily_report"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )
        reordered_packet = build_research_packet(
            reordered_snapshot,
            _request("daily_report"),
            generated_at=datetime.fromisoformat("2026-08-16T10:35:00-04:00"),
        )

        self.assertEqual(base_packet["themes"], reordered_packet["themes"])
        self.assertEqual(base_packet["all_decisions"], reordered_packet["all_decisions"])
        self.assertEqual(base_packet["analysis_hash"], reordered_packet["analysis_hash"])
        self.assertEqual(
            [theme["theme_id"] for theme in base_packet["themes"]],
            ["theme-us-automation-platforms", "theme-us-automation-services"],
        )
        self.assertEqual(
            base_packet["themes"][0]["candidate_ids"],
            ["US.DEMOA", "US.DEMOB", "US.DEMOC"],
        )
        self.assertEqual(base_packet["themes"][1]["candidate_ids"], ["US.DEMOD"])


if __name__ == "__main__":
    unittest.main()
