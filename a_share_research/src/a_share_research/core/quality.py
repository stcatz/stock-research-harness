from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .utils import sha256_value

VALUATION_METRICS = ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm")
HARD_GATE_WEIGHTS = {
    "official_event": 18,
    "structured_market": 18,
    "transmission_chain": 12,
    "stage_not_declining": 12,
}
SOFT_GATE_WEIGHTS = {
    "counter_thesis": 8,
    "invalidation_conditions": 8,
    "next_catalyst": 8,
    "time_boundary": 8,
    "company_disclosure": 8,
}
ROLE_BONUS = {"core": 5, "midcap": 4, "elastic": 2, "follower": 0}
STAGE_BONUS = {
    "organizing": 3,
    "warming": 5,
    "expanding": 5,
    "climax": -8,
    "diverging": -5,
    "declining": -20,
}
HARD_RISK_FLAGS = {"ST", "SUSPENDED", "REGULATORY_MAJOR", "LIQUIDITY_INSUFFICIENT"}


def decorate_quality(decisions: list[dict[str, Any]], decision_at: str) -> None:
    """Add deterministic quality, valuation, gap and evaluation contracts in place."""

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
    if decision["stage"] in {"climax", "diverging"} or HARD_RISK_FLAGS.intersection(
        decision["risk_flags"]
    ):
        return "conflicted"
    if any(not passed for passed in decision["gates"].values()) or decision["data_gaps"]:
        return "incomplete"
    return "sufficient"


def _research_priority(decision: dict[str, Any]) -> int:
    gates = decision["gates"]
    hard_score = sum(weight for gate, weight in HARD_GATE_WEIGHTS.items() if gates.get(gate))
    soft_score = sum(weight for gate, weight in SOFT_GATE_WEIGHTS.items() if gates.get(gate))
    score = hard_score + soft_score
    score += ROLE_BONUS.get(decision["role"], 0)
    score += STAGE_BONUS.get(decision["stage"], 0)
    score -= min(15, 3 * len(decision["data_gaps"]))
    score -= min(8, 2 * len(decision["manual_review_items"]))
    hard_risks = HARD_RISK_FLAGS.intersection(decision["risk_flags"])
    if hard_risks:
        score -= min(20, 5 * len(hard_risks))
    if decision["decision"] == "exclude":
        score = min(score, 39)
    elif decision["decision"] == "continue_research":
        score = min(score, 79)
    return max(0, min(100, score))


def _research_gaps(decision: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    gate_specs = {
        "official_event": (
            "missing_from_snapshot",
            "collector",
            "补充研究时点前可用的官方事件或监管来源",
        ),
        "structured_market": (
            "missing_from_snapshot",
            "market_provider",
            "补充与 decision_at 同期的结构化行情证据",
        ),
        "transmission_chain": (
            "manual_review",
            "industry_research",
            "形成至少三段且可证伪的受益传导链",
        ),
        "counter_thesis": (
            "manual_review",
            "independent_bear",
            "由看不到多方结论的独立反方给出证据化反例",
        ),
        "invalidation_conditions": (
            "manual_review",
            "risk_reviewer",
            "给出可观察、可到期的证伪条件",
        ),
        "next_catalyst": (
            "not_yet_public",
            "event_monitor",
            "确认研究时点之后的下一项正式催化或观察日期",
        ),
        "time_boundary": (
            "conflicted",
            "pit_reviewer",
            "移除所有 decision_at 之后才可用的信息并重新运行",
        ),
        "stage_not_declining": (
            "conflicted",
            "market_reviewer",
            "用同期市场数据确认题材不再处于退潮阶段",
        ),
        "company_disclosure": (
            "missing_from_snapshot",
            "issuer_collector",
            "补充候选公司自身正式披露以验证业务纯度",
        ),
    }
    for gate, passed in decision["gates"].items():
        if passed or gate not in gate_specs:
            continue
        state, resolver, transition = gate_specs[gate]
        gaps.append(_gap_card(decision, f"gate:{gate}", state, resolver, transition, True))
    for raw_gap in decision["data_gaps"]:
        lowered = raw_gap.casefold()
        if "unknown" in lowered or "未知" in raw_gap or "provider" in lowered:
            state = "provider_unknown"
            resolver = "data_provider"
        else:
            state = "manual_review"
            resolver = "researcher"
        gaps.append(
            _gap_card(
                decision,
                f"data:{raw_gap}",
                state,
                resolver,
                f"取得并核验缺失项：{raw_gap}",
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
                f"完成人工复核：{item}",
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
                "明确继续研究的阻塞事实、责任人和转换条件",
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
        "gap_id": f"cn-gap-{identity}",
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
        "benchmark": {"symbol": "000906.SH", "name": "中证800"},
        "horizons_trading_days": [5, 20],
        "state_order": ["observe", "continue_research", "exclude"],
        "evaluation_rule": (
            "分别统计各研究状态的超额收益、命中率和样本数；只检验状态排序的区分度，"
            "不把研究状态解释为交易指令。"
        ),
        "status": "pending_outcome_observation",
    }


def _valuation_profile(decision: dict[str, Any]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for evidence in decision["evidence"]:
        for fact in evidence.get("facts", []):
            metric = fact.get("metric")
            if metric not in VALUATION_METRICS or metric in latest:
                continue
            entry: dict[str, Any] = {
                "metric": metric,
                "status": fact.get("status", "unknown"),
                "value": None,
                "unit": fact.get("unit", "x"),
                "as_of": fact.get("as_of"),
                "fact_id": fact.get("fact_id"),
                "snapshot_candidate_percentile": None,
                "sample_size": 0,
            }
            if fact.get("status") == "observed":
                try:
                    value = float(fact["value"])
                except (KeyError, TypeError, ValueError):
                    entry["status"] = "unknown"
                else:
                    if metric.startswith("pe_") and value <= 0:
                        entry["status"] = "not_meaningful"
                    else:
                        entry["value"] = value
            latest[metric] = entry
    metrics = [
        latest.get(
            metric,
            {
                "metric": metric,
                "status": "unknown",
                "value": None,
                "unit": "x",
                "as_of": None,
                "fact_id": None,
                "snapshot_candidate_percentile": None,
                "sample_size": 0,
            },
        )
        for metric in VALUATION_METRICS
    ]
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
            if entry["metric"] == metric and entry["status"] == "observed"
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
