from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from importlib.resources import files

from us_equity_research.core.calculations import build_calculation_bundle
from us_equity_research.core.snapshot import validate_snapshot

DECISION_AT = datetime.fromisoformat("2026-08-16T08:30:00-04:00")


def _load_demo_snapshot() -> dict[str, object]:
    resource = files("us_equity_research.fixtures").joinpath("demo_snapshot.json")
    with resource.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _demoa_candidate(snapshot: dict[str, object]) -> dict[str, object]:
    return snapshot["themes"][0]["candidates"][0]  # type: ignore[index]


class CalculationTests(unittest.TestCase):
    def test_demoa_calculations_are_deterministic_and_traceable(self) -> None:
        snapshot = validate_snapshot(_load_demo_snapshot())
        bundle = build_calculation_bundle(snapshot, _demoa_candidate(snapshot.data), DECISION_AT)

        self.assertEqual(
            [fact["fact_id"] for fact in bundle["facts"]],
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

        calculations = {item["metric"]: item for item in bundle["calculations"]}
        self.assertEqual(
            set(calculations),
            {
                "revenue_growth",
                "operating_margin",
                "free_cash_flow_margin",
                "market_cap",
                "net_cash",
                "enterprise_value",
                "ev_to_revenue",
                "free_cash_flow_yield",
            },
        )

        for metric, calculation in calculations.items():
            with self.subTest(metric=metric):
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

        self.assertAlmostEqual(calculations["revenue_growth"]["value"], 0.3333333333333333)
        self.assertEqual(calculations["revenue_growth"]["status"], "OK")
        self.assertEqual(
            calculations["revenue_growth"]["formula"],
            "(revenue_ttm_current - revenue_ttm_prior) / revenue_ttm_prior",
        )
        self.assertEqual(
            calculations["revenue_growth"]["input_fact_ids"],
            ["FACT-DEMOA-REV-TTM", "FACT-DEMOA-REV-TTM-PRIOR"],
        )
        self.assertEqual(calculations["revenue_growth"]["unit"], "ratio")
        self.assertEqual(calculations["revenue_growth"]["period_end"], "2026-06-30")
        self.assertIsNone(calculations["revenue_growth"]["price_as_of"])
        self.assertTrue(calculations["revenue_growth"]["available_at_cutoff_passed"])
        self.assertEqual(calculations["revenue_growth"]["evidence_refs"], ["EV-SEC-001"])

        self.assertAlmostEqual(calculations["operating_margin"]["value"], 0.175)
        self.assertEqual(
            calculations["operating_margin"]["formula"], "operating_income_ttm / revenue_ttm"
        )
        self.assertEqual(calculations["operating_margin"]["period_end"], "2026-06-30")
        self.assertEqual(calculations["operating_margin"]["evidence_refs"], ["EV-SEC-001"])

        self.assertAlmostEqual(calculations["free_cash_flow_margin"]["value"], 0.125)
        self.assertEqual(
            calculations["free_cash_flow_margin"]["formula"],
            "free_cash_flow_ttm / revenue_ttm",
        )
        self.assertEqual(calculations["free_cash_flow_margin"]["period_end"], "2026-06-30")
        self.assertEqual(calculations["free_cash_flow_margin"]["evidence_refs"], ["EV-SEC-001"])

        self.assertEqual(calculations["market_cap"]["value"], 222000000.0)
        self.assertEqual(calculations["market_cap"]["formula"], "diluted_shares * close_price")
        self.assertEqual(calculations["market_cap"]["unit"], "USD")
        self.assertEqual(
            calculations["market_cap"]["input_fact_ids"],
            ["FACT-DEMOA-SHARES", "FACT-DEMOA-CLOSE"],
        )
        self.assertEqual(calculations["market_cap"]["period_end"], "2026-06-30")
        self.assertEqual(calculations["market_cap"]["price_as_of"], "2026-08-15")
        self.assertEqual(
            calculations["market_cap"]["evidence_refs"],
            ["EV-SEC-001", "EV-MARKET-001"],
        )

        self.assertEqual(calculations["net_cash"]["value"], 65000000.0)
        self.assertEqual(
            calculations["net_cash"]["input_fact_ids"],
            ["FACT-DEMOA-CASH", "FACT-DEMOA-DEBT"],
        )
        self.assertEqual(calculations["net_cash"]["price_as_of"], None)

        self.assertEqual(calculations["enterprise_value"]["value"], 157000000.0)
        self.assertEqual(
            calculations["enterprise_value"]["formula"],
            "(diluted_shares * close_price) + total_debt - cash_and_equivalents",
        )
        self.assertEqual(
            calculations["enterprise_value"]["input_fact_ids"],
            [
                "FACT-DEMOA-CASH",
                "FACT-DEMOA-DEBT",
                "FACT-DEMOA-SHARES",
                "FACT-DEMOA-CLOSE",
            ],
        )
        self.assertEqual(calculations["enterprise_value"]["price_as_of"], "2026-08-15")

        self.assertAlmostEqual(calculations["ev_to_revenue"]["value"], 0.6541666666666667)
        self.assertEqual(
            calculations["ev_to_revenue"]["formula"],
            "enterprise_value / revenue_ttm",
        )
        self.assertEqual(
            calculations["ev_to_revenue"]["input_fact_ids"],
            [
                "FACT-DEMOA-REV-TTM",
                "FACT-DEMOA-CASH",
                "FACT-DEMOA-DEBT",
                "FACT-DEMOA-SHARES",
                "FACT-DEMOA-CLOSE",
            ],
        )
        self.assertEqual(calculations["ev_to_revenue"]["unit"], "x")
        self.assertEqual(calculations["ev_to_revenue"]["price_as_of"], "2026-08-15")

        self.assertAlmostEqual(calculations["free_cash_flow_yield"]["value"], 0.13513513513513514)
        self.assertEqual(
            calculations["free_cash_flow_yield"]["formula"],
            "free_cash_flow_ttm / market_cap",
        )
        self.assertEqual(
            calculations["free_cash_flow_yield"]["input_fact_ids"],
            ["FACT-DEMOA-FCF-TTM", "FACT-DEMOA-SHARES", "FACT-DEMOA-CLOSE"],
        )
        self.assertEqual(calculations["free_cash_flow_yield"]["unit"], "ratio")
        self.assertEqual(calculations["free_cash_flow_yield"]["price_as_of"], "2026-08-15")

    def test_future_candidate_fact_is_excluded_and_incomplete_metrics_become_unknown(self) -> None:
        raw = _load_demo_snapshot()
        candidate = _demoa_candidate(raw)
        candidate["financial_fact_refs"].remove("FACT-DEMOA-REV-TTM-PRIOR")
        raw["financial_facts"].append(  # type: ignore[index]
            {
                "fact_id": "FACT-DEMOA-REV-TTM-PRIOR-FUTURE",
                "security_id": "US.DEMOA",
                "metric": "revenue_ttm",
                "value": 175000000.0,
                "unit": "USD",
                "period_end": "2025-06-30",
                "available_at": "2026-08-16T09:45:00-04:00",
                "evidence_ref": "EV-SEC-001",
            }
        )
        candidate["financial_fact_refs"].append("FACT-DEMOA-REV-TTM-PRIOR-FUTURE")

        snapshot = validate_snapshot(raw)
        bundle = build_calculation_bundle(snapshot, _demoa_candidate(snapshot.data), DECISION_AT)
        calculations = {item["metric"]: item for item in bundle["calculations"]}

        self.assertNotIn(
            "FACT-DEMOA-REV-TTM-PRIOR-FUTURE",
            [fact["fact_id"] for fact in bundle["facts"]],
        )
        self.assertIn("FACT-DEMOA-REV-TTM-PRIOR-FUTURE", bundle["cutoff_excluded_fact_ids"])
        self.assertEqual(calculations["revenue_growth"]["status"], "UNKNOWN")
        self.assertIsNone(calculations["revenue_growth"]["value"])
        self.assertEqual(calculations["revenue_growth"]["period_end"], "2026-06-30")
        self.assertFalse(calculations["revenue_growth"]["available_at_cutoff_passed"])
        self.assertEqual(calculations["revenue_growth"]["input_fact_ids"], ["FACT-DEMOA-REV-TTM"])
        self.assertEqual(calculations["revenue_growth"]["evidence_refs"], ["EV-SEC-001"])

    def test_zero_denominators_and_nonpositive_market_cap_are_not_meaningful(self) -> None:
        raw = copy.deepcopy(_load_demo_snapshot())
        facts = {fact["fact_id"]: fact for fact in raw["financial_facts"]}  # type: ignore[index]
        facts["FACT-DEMOA-REV-TTM"]["value"] = 0.0
        facts["FACT-DEMOA-CLOSE"]["value"] = 0.0

        snapshot = validate_snapshot(raw)
        bundle = build_calculation_bundle(snapshot, _demoa_candidate(snapshot.data), DECISION_AT)
        calculations = {item["metric"]: item for item in bundle["calculations"]}

        for metric in (
            "operating_margin",
            "free_cash_flow_margin",
            "market_cap",
            "enterprise_value",
            "ev_to_revenue",
            "free_cash_flow_yield",
        ):
            with self.subTest(metric=metric):
                self.assertEqual(calculations[metric]["status"], "NOT_MEANINGFUL")
                self.assertIsNone(calculations[metric]["value"])
                self.assertTrue(calculations[metric]["available_at_cutoff_passed"])

    def test_latest_eligible_facts_use_period_end_then_available_at_revision_tiebreak(self) -> None:
        raw = copy.deepcopy(_load_demo_snapshot())
        raw["financial_facts"].extend(  # type: ignore[index]
            [
                {
                    "fact_id": "FACT-DEMOA-REV-TTM-CURRENT-REV",
                    "security_id": "US.DEMOA",
                    "metric": "revenue_ttm",
                    "value": 250000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:00:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-REV-TTM-INTERIM",
                    "security_id": "US.DEMOA",
                    "metric": "revenue_ttm",
                    "value": 210000000.0,
                    "unit": "USD",
                    "period_end": "2025-12-31",
                    "available_at": "2026-02-05T08:00:00-05:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-REV-TTM-INTERIM-REV",
                    "security_id": "US.DEMOA",
                    "metric": "revenue_ttm",
                    "value": 220000000.0,
                    "unit": "USD",
                    "period_end": "2025-12-31",
                    "available_at": "2026-02-07T08:00:00-05:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-FCF-TTM-PRIOR",
                    "security_id": "US.DEMOA",
                    "metric": "free_cash_flow_ttm",
                    "value": 28000000.0,
                    "unit": "USD",
                    "period_end": "2026-03-31",
                    "available_at": "2026-05-10T08:00:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-FCF-TTM-REV",
                    "security_id": "US.DEMOA",
                    "metric": "free_cash_flow_ttm",
                    "value": 32000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:05:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-CASH-PRIOR",
                    "security_id": "US.DEMOA",
                    "metric": "cash_and_equivalents",
                    "value": 70000000.0,
                    "unit": "USD",
                    "period_end": "2026-03-31",
                    "available_at": "2026-05-10T08:00:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-CASH-REV",
                    "security_id": "US.DEMOA",
                    "metric": "cash_and_equivalents",
                    "value": 95000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:10:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-DEBT-PRIOR",
                    "security_id": "US.DEMOA",
                    "metric": "total_debt",
                    "value": 28000000.0,
                    "unit": "USD",
                    "period_end": "2026-03-31",
                    "available_at": "2026-05-10T08:00:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-DEBT-REV",
                    "security_id": "US.DEMOA",
                    "metric": "total_debt",
                    "value": 24000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:15:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-SHARES-PRIOR",
                    "security_id": "US.DEMOA",
                    "metric": "diluted_shares",
                    "value": 11000000.0,
                    "unit": "shares",
                    "period_end": "2026-03-31",
                    "available_at": "2026-05-10T08:00:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-SHARES-REV",
                    "security_id": "US.DEMOA",
                    "metric": "diluted_shares",
                    "value": 12500000.0,
                    "unit": "shares",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:20:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-CLOSE-PRIOR",
                    "security_id": "US.DEMOA",
                    "metric": "close_price",
                    "value": 17.5,
                    "unit": "USD/share",
                    "period_end": "2026-08-14",
                    "available_at": "2026-08-14T16:05:00-04:00",
                    "evidence_ref": "EV-MARKET-001",
                },
                {
                    "fact_id": "FACT-DEMOA-CLOSE-REV",
                    "security_id": "US.DEMOA",
                    "metric": "close_price",
                    "value": 18.8,
                    "unit": "USD/share",
                    "period_end": "2026-08-15",
                    "available_at": "2026-08-15T16:10:00-04:00",
                    "evidence_ref": "EV-MARKET-001",
                },
            ]
        )
        candidate = _demoa_candidate(raw)
        candidate["financial_fact_refs"].extend(
            [
                "FACT-DEMOA-REV-TTM-CURRENT-REV",
                "FACT-DEMOA-REV-TTM-INTERIM",
                "FACT-DEMOA-REV-TTM-INTERIM-REV",
                "FACT-DEMOA-FCF-TTM-PRIOR",
                "FACT-DEMOA-FCF-TTM-REV",
                "FACT-DEMOA-CASH-PRIOR",
                "FACT-DEMOA-CASH-REV",
                "FACT-DEMOA-DEBT-PRIOR",
                "FACT-DEMOA-DEBT-REV",
                "FACT-DEMOA-SHARES-PRIOR",
                "FACT-DEMOA-SHARES-REV",
                "FACT-DEMOA-CLOSE-PRIOR",
                "FACT-DEMOA-CLOSE-REV",
            ]
        )

        snapshot = validate_snapshot(raw)
        bundle = build_calculation_bundle(snapshot, _demoa_candidate(snapshot.data), DECISION_AT)
        calculations = {item["metric"]: item for item in bundle["calculations"]}

        self.assertEqual(
            calculations["revenue_growth"]["input_fact_ids"],
            ["FACT-DEMOA-REV-TTM-CURRENT-REV", "FACT-DEMOA-REV-TTM-INTERIM-REV"],
        )
        self.assertAlmostEqual(calculations["revenue_growth"]["value"], 0.13636363636363635)
        self.assertEqual(calculations["revenue_growth"]["period_end"], "2026-06-30")

        self.assertEqual(
            calculations["net_cash"]["input_fact_ids"],
            ["FACT-DEMOA-CASH-REV", "FACT-DEMOA-DEBT-REV"],
        )
        self.assertEqual(calculations["net_cash"]["value"], 71000000.0)

        self.assertEqual(
            calculations["market_cap"]["input_fact_ids"],
            ["FACT-DEMOA-SHARES-REV", "FACT-DEMOA-CLOSE-REV"],
        )
        self.assertEqual(calculations["market_cap"]["price_as_of"], "2026-08-15")
        self.assertEqual(calculations["market_cap"]["value"], 235000000.0)

        self.assertEqual(
            calculations["free_cash_flow_margin"]["input_fact_ids"],
            ["FACT-DEMOA-FCF-TTM-REV", "FACT-DEMOA-REV-TTM-CURRENT-REV"],
        )
        self.assertAlmostEqual(calculations["free_cash_flow_margin"]["value"], 0.128)

    def test_calculation_outputs_do_not_depend_on_input_fact_order(self) -> None:
        raw = copy.deepcopy(_load_demo_snapshot())
        raw["financial_facts"].extend(  # type: ignore[index]
            [
                {
                    "fact_id": "FACT-DEMOA-CASH-REV",
                    "security_id": "US.DEMOA",
                    "metric": "cash_and_equivalents",
                    "value": 95000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:10:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-DEBT-REV",
                    "security_id": "US.DEMOA",
                    "metric": "total_debt",
                    "value": 24000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:15:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-SHARES-REV",
                    "security_id": "US.DEMOA",
                    "metric": "diluted_shares",
                    "value": 12500000.0,
                    "unit": "shares",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:20:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
                {
                    "fact_id": "FACT-DEMOA-FCF-TTM-REV",
                    "security_id": "US.DEMOA",
                    "metric": "free_cash_flow_ttm",
                    "value": 32000000.0,
                    "unit": "USD",
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-15T10:05:00-04:00",
                    "evidence_ref": "EV-SEC-001",
                },
            ]
        )
        refs = _demoa_candidate(raw)["financial_fact_refs"]
        refs.extend(
            [
                "FACT-DEMOA-CASH-REV",
                "FACT-DEMOA-DEBT-REV",
                "FACT-DEMOA-SHARES-REV",
                "FACT-DEMOA-FCF-TTM-REV",
            ]
        )
        reordered = copy.deepcopy(raw)
        reordered["financial_facts"].reverse()  # type: ignore[index]
        reordered_candidate_refs = _demoa_candidate(reordered)["financial_fact_refs"]
        reordered_candidate_refs[:] = list(reversed(reordered_candidate_refs))

        first_snapshot = validate_snapshot(raw)
        second_snapshot = validate_snapshot(reordered)
        first_bundle = build_calculation_bundle(
            first_snapshot,
            _demoa_candidate(first_snapshot.data),
            DECISION_AT,
        )
        second_bundle = build_calculation_bundle(
            second_snapshot,
            _demoa_candidate(second_snapshot.data),
            DECISION_AT,
        )

        self.assertEqual(first_bundle["calculations"], second_bundle["calculations"])


if __name__ == "__main__":
    unittest.main()
