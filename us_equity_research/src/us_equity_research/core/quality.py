from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .utils import sha256_value

VALUATION_METRICS = ("ev_to_revenue", "free_cash_flow_yield")
HARD_GATE_WEIGHTS = {
    "official_primary": 16,
    "structured_market": 16,
    "transmission_chain": 10,
    "stage_not_fading": 10,
}
SOFT_GATE_WEIGHTS = {
    "three_viewpoints": 8,
    "invalidation": 7,
    "future_catalyst": 7,
    "no_time_leaks": 8,
    "calculations_complete": 8,
    "issuer_specific_official": 8,
}
ROLE_BONUS = {"leader": 5, "platform": 4, "beneficiary": 2, "speculative": 0}
STAGE_BONUS = {
    "discovery": 2,
    "confirming": 5,
    "expanding": 5,
    "crowded": -6,
    "diverging": -7,
    "fading": -20,
}
HARD_RISKS = {
    "going_concern",
    "restatement",
    "late_filing",
    "sanctions",
    "liquidity_constraint",
}


def decorate_quality(decisions: list[dict[str, Any]], decision_at: str) -> None:
    for decision in decisions:
        decision["evidence_state"] = _evidence_state(decision)
        decision["research_priority"] = _research_priority(decision)
        decision["research_gaps"] = _research_gaps(decision)
        decision["falsification_contract"] = _falsification_contract(decision)
        decision["evaluation_contract"] = _evaluation_contract(decision, decision_at)
        decision["valuation_profile"] = _valuation_profile(decision)
    _apply_cross_sectional_percentiles(decisions)


def build_research_queue(decisions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    queue = [
        {
            "candidate_id": decision["candidate_id"],
            "symbol": decision["symbol"],
            "name": decision["name"],
            "decision": decision["decision"],
            "research_priority": decision["research_priority"],
            "gaps": decision["research_gaps"],
            "falsification_contract": decision["falsification_contract"],
        }
        for decision in decisions
        if decision["decision"] == "continue_research" or decision["research_gaps"]
    ]
    return sorted(queue, key=lambda item: (-item["research_priority"], item["symbol"]))


def _evidence_state(decision: dict[str, Any]) -> str:
    if decision["stage"] in {"crowded", "diverging"} or HARD_RISKS.intersection(
        decision["risk_flags"]
    ):
        return "conflicted"
    if any(not passed for passed in decision["gates"].values()) or decision["data_gaps"]:
        return "incomplete"
    return "sufficient"


def _research_priority(decision: dict[str, Any]) -> int:
    gates = decision["gates"]
    score = sum(weight for gate, weight in HARD_GATE_WEIGHTS.items() if gates.get(gate))
    score += sum(weight for gate, weight in SOFT_GATE_WEIGHTS.items() if gates.get(gate))
    score += ROLE_BONUS.get(decision["role"], 0)
    score += STAGE_BONUS.get(decision["stage"], 0)
    score -= min(15, 3 * len(decision["data_gaps"]))
    score -= min(8, 2 * len(decision["manual_review_items"]))
    score -= min(20, 5 * len(HARD_RISKS.intersection(decision["risk_flags"])))
    if decision["decision"] == "exclude":
        score = min(score, 39)
    elif decision["decision"] == "continue_research":
        score = min(score, 79)
    return max(0, min(100, score))


def _research_gaps(decision: dict[str, Any]) -> list[dict[str, Any]]:
    specs = {
        "official_primary": (
            "missing_from_snapshot",
            "official_collector",
            "Add usable official primary evidence available by decision_at.",
        ),
        "structured_market": (
            "missing_from_snapshot",
            "market_provider",
            "Add licensed structured market evidence aligned with decision_at.",
        ),
        "transmission_chain": (
            "manual_review",
            "industry_research",
            "Build a falsifiable transmission chain with at least three links.",
        ),
        "three_viewpoints": (
            "manual_review",
            "independent_bear",
            "Support bull, bear and risk views with usable independent evidence.",
        ),
        "invalidation": (
            "manual_review",
            "risk_reviewer",
            "Define observable and time-bounded invalidation conditions.",
        ),
        "future_catalyst": (
            "not_yet_public",
            "event_monitor",
            "Confirm a formal future catalyst or review date.",
        ),
        "no_time_leaks": (
            "conflicted",
            "pit_reviewer",
            "Remove facts unavailable at decision_at and rerun.",
        ),
        "stage_not_fading": (
            "conflicted",
            "market_reviewer",
            "Confirm with market evidence that the theme is no longer fading.",
        ),
        "calculations_complete": (
            "provider_unknown",
            "financial_collector",
            "Supply the point-in-time inputs required by deterministic calculations.",
        ),
        "issuer_specific_official": (
            "missing_from_snapshot",
            "issuer_collector",
            "Add issuer-specific SEC or IR evidence available by decision_at.",
        ),
    }
    gaps: list[dict[str, Any]] = []
    for gate, passed in decision["gates"].items():
        if passed or gate not in specs:
            continue
        state, resolver, transition = specs[gate]
        gaps.append(_gap_card(decision, f"gate:{gate}", state, resolver, transition, True))
    for raw_gap in decision["data_gaps"]:
        lowered = raw_gap.casefold()
        state = "provider_unknown" if "unknown" in lowered or "missing" in lowered else "manual_review"
        resolver = "data_provider" if state == "provider_unknown" else "researcher"
        gaps.append(
            _gap_card(
                decision,
                f"data:{raw_gap}",
                state,
                resolver,
                f"Resolve and verify: {raw_gap}",
                True,
            )
        )
    for item in decision["manual_review_items"]:
        gaps.append(
            _gap_card(
                decision,
                f"review:{item}",
                "manual_review",
                "researcher",
                f"Complete manual review: {item}",
                False,
            )
        )
    if decision["decision"] == "continue_research" and not gaps:
        gaps.append(
            _gap_card(
                decision,
                "state:continue_research",
                "manual_review",
                "researcher",
                "Name the blocking fact, owner and transition condition.",
                True,
            )
        )
    return gaps


def _gap_card(
    decision: dict[str, Any],
    seed: str,
    state: str,
    resolver: str,
    transition_condition: str,
    blocking: bool,
) -> dict[str, Any]:
    identity = sha256_value({"candidate_id": decision["candidate_id"], "seed": seed})[:12]
    return {
        "gap_id": f"us-gap-{identity}",
        "state": state,
        "resolver": resolver,
        "blocking": blocking,
        "transition_condition": transition_condition,
        "expires_at": decision["next_catalyst_at"],
    }


def _falsification_contract(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": "0.1",
        "status": "pending_independent_review",
        "review_mode": "independent_bear",
        "bull_thesis_visible": False,
        "input_scope": "usable_fact_layer_only",
        "claim_to_falsify": decision["thesis"],
        "usable_evidence_refs": list(decision["usable_evidence_refs"]),
        "invalidation_conditions": list(decision["invalidation_conditions"]),
        "required_output": [
            "contradicting_evidence",
            "alternative_causal_explanation",
            "invalidation_test",
            "confidence_and_unknowns",
        ],
    }


def _evaluation_contract(decision: dict[str, Any], decision_at: str) -> dict[str, Any]:
    return {
        "contract_version": "0.1",
        "decision_at": decision_at,
        "candidate_id": decision["candidate_id"],
        "outcome_metric": "benchmark_relative_total_return",
        "benchmark": {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust"},
        "horizons_trading_days": [5, 20],
        "state_order": ["observe", "continue_research", "exclude"],
        "evaluation_rule": (
            "Report excess return, hit rate and sample count by research state and test only "
            "whether state ordering is discriminative; research states are not trade signals."
        ),
        "status": "pending_outcome_observation",
    }


def _valuation_profile(decision: dict[str, Any]) -> dict[str, Any]:
    by_metric = {item["metric"]: item for item in decision["calculations"]}
    metrics: list[dict[str, Any]] = []
    for metric in VALUATION_METRICS:
        calculation = by_metric.get(metric, {})
        status = str(calculation.get("status", "UNKNOWN")).lower()
        value = calculation.get("value") if status == "ok" else None
        metrics.append(
            {
                "metric": metric,
                "status": status,
                "value": value,
                "unit": calculation.get("unit", "UNKNOWN"),
                "period_end": calculation.get("period_end"),
                "price_as_of": calculation.get("price_as_of"),
                "input_fact_ids": list(calculation.get("input_fact_ids", [])),
                "snapshot_candidate_percentile": None,
                "sample_size": 0,
            }
        )
    return {
        "scope": "snapshot_candidate_set_not_industry_peers",
        "percentile_formula": "100 * (count(lower) + 0.5 * count(equal)) / sample_size",
        "industry_percentile": "UNKNOWN",
        "historical_percentile": "UNKNOWN",
        "metrics": metrics,
    }


def _apply_cross_sectional_percentiles(decisions: list[dict[str, Any]]) -> None:
    for metric in VALUATION_METRICS:
        entries = [
            entry
            for decision in decisions
            for entry in decision["valuation_profile"]["metrics"]
            if entry["metric"] == metric and entry["status"] == "ok"
        ]
        values = [entry["value"] for entry in entries]
        for entry in entries:
            value = entry["value"]
            lower = sum(peer < value for peer in values)
            equal = sum(peer == value for peer in values)
            entry["sample_size"] = len(values)
            entry["snapshot_candidate_percentile"] = round(
                100 * (lower + 0.5 * equal) / len(values), 2
            )
