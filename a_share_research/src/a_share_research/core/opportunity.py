from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import DIMENSIONS, parse_datetime

_STRENGTH_POINTS = {
    "strong": 1.0,
    "medium": 0.6,
    "weak": 0.15,
    "unknown": 0.0,
}
_DIMENSION_WEIGHTS = {
    "big": 6,
    "new": 6,
    "many": 4,
    "durable": 5,
    "timely": 4,
}
_FACTOR_WEIGHTS = {
    "new_information": 15,
    "economic_impact": 20,
}
_EXPECTATION_POINTS = {
    "positive": 15,
    "neutral": 5,
    "negative": -12,
    "unknown": 0,
}
_PRICING_POINTS = {
    "underreacted": 10,
    "confirmed": 7,
    "overreacted": -6,
    "contradicted": -12,
    "unknown": 0,
}
_HARD_RISK_FLAGS = {"ST", "SUSPENDED", "REGULATORY_MAJOR", "LIQUIDITY_INSUFFICIENT"}


def assess_opportunity(
    theme: dict[str, Any],
    candidate: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    usable_refs: list[str],
    decision_at: datetime,
    benchmark_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Build an evidence-qualified, deterministic opportunity assessment.

    Editorial/LLM assessments are inputs, never scores by themselves.  A factor only earns
    points when its cited evidence exists in the frozen snapshot, was usable at decision_at and
    has an appropriate source class.  Missing V2 fields remain explicit UNKNOWN so old snapshots
    stay replayable without receiving unearned points.
    """

    usable = set(usable_refs)
    dimensions: dict[str, dict[str, Any]] = {}
    dimension_score = 0.0
    for name in DIMENSIONS:
        raw = theme["dimensions"][name]
        refs = list(raw.get("evidence_refs", []))
        qualified = _refs_qualified(refs, usable, evidence_by_id, _dimension_levels(name))
        points = (
            _DIMENSION_WEIGHTS[name] * _STRENGTH_POINTS.get(raw["assessment"], 0.0)
            if qualified
            else 0.0
        )
        dimension_score += points
        dimensions[name] = {
            "assessment": raw["assessment"],
            "reason": raw["reason"],
            "evidence_refs": refs,
            "evidence_qualified": qualified,
            "points": round(points, 2),
            "max_points": _DIMENSION_WEIGHTS[name],
        }

    raw_profile = candidate.get("opportunity_profile")
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    new_information = _strength_factor(
        profile.get("new_information"),
        "new_information",
        usable,
        evidence_by_id,
        {"official"},
    )
    economic_impact = _strength_factor(
        profile.get("economic_impact"),
        "economic_impact",
        usable,
        evidence_by_id,
        {"official", "industry"},
        required_categories={"company_disclosure"},
        magnitude_required=True,
        frozen_content_required=True,
    )
    expectation_gap = _categorical_factor(
        profile.get("expectation_gap"),
        _EXPECTATION_POINTS,
        usable,
        evidence_by_id,
        {"official", "industry", "structured_market"},
        extra_fields=("baseline",),
    )
    impact_chain = _impact_chain(candidate, theme, usable, evidence_by_id)
    catalyst = _catalyst(profile.get("next_catalyst"), usable, evidence_by_id, decision_at)
    # The engine pre-filters benchmark_refs by decision_at. They are market context rather than
    # candidate citations, so they are not duplicated into usable_refs/evidence cards.
    benchmark_ref_set = set(benchmark_refs or [])
    market_usable = usable.union(benchmark_ref_set)
    market_signal = _market_signal(
        evidence_by_id,
        market_usable,
        set(candidate.get("market_evidence_refs", [])),
        benchmark_ref_set,
    )
    market_pricing = _market_pricing_factor(
        profile.get("market_pricing"),
        market_usable,
        evidence_by_id,
        market_signal,
        positive_fundamental_context=(
            new_information["assessment"] in {"strong", "medium"}
            and new_information["evidence_qualified"]
            and economic_impact["assessment"] in {"strong", "medium"}
            and economic_impact["evidence_qualified"]
            and expectation_gap["assessment"] == "positive"
            and expectation_gap["evidence_qualified"]
        ),
    )

    positive_points = (
        dimension_score
        + new_information["points"]
        + economic_impact["points"]
        + expectation_gap["points"]
        + market_pricing["points"]
        + impact_chain["points"]
        + catalyst["points"]
    )
    stage_penalty = {
        "climax": 8,
        "diverging": 6,
        "declining": 20,
    }.get(theme["stage"], 0)
    hard_risks = sorted(_HARD_RISK_FLAGS.intersection(candidate.get("risk_flags", [])))
    risk_penalty = min(20, 5 * len(hard_risks))
    attention_score = max(0, min(100, round(positive_points - stage_penalty - risk_penalty)))

    factor_coverage = sum(
        bool(item["evidence_qualified"])
        for item in (new_information, economic_impact, expectation_gap, market_pricing)
    )
    negative = (
        (
            expectation_gap["assessment"] == "negative"
            and expectation_gap["evidence_qualified"]
        )
        or (
            market_pricing["assessment"] == "contradicted"
            and market_pricing["evidence_qualified"]
        )
        or theme["stage"] == "declining"
        or "CLIMAX" in candidate.get("risk_flags", [])
        or bool(hard_risks)
    )
    positive = (
        attention_score >= 50
        and new_information["assessment"] in {"strong", "medium"}
        and new_information["evidence_qualified"]
        and economic_impact["assessment"] in {"strong", "medium"}
        and economic_impact["evidence_qualified"]
        and expectation_gap["assessment"] == "positive"
        and expectation_gap["evidence_qualified"]
        and market_pricing["assessment"] not in {"overreacted", "contradicted"}
        and impact_chain["coverage_ratio"] >= 2 / 3
    )
    if negative:
        view = "negative"
    elif positive:
        view = "positive"
    elif attention_score >= 30 and factor_coverage >= 2:
        view = "neutral"
    else:
        view = "unclear"

    missing_factors = [
        name
        for name, item in (
            ("new_information", new_information),
            ("economic_impact", economic_impact),
            ("expectation_gap", expectation_gap),
            ("market_pricing", market_pricing),
        )
        if not item["evidence_qualified"]
    ]
    if impact_chain["coverage_ratio"] < 2 / 3:
        missing_factors.append("candidate_impact_chain")
    if not catalyst["evidence_qualified"]:
        missing_factors.append("verified_next_catalyst")

    return {
        "contract_version": "2.0",
        "method_dimensions": dimensions,
        "dimension_score": round(dimension_score, 2),
        "new_information": new_information,
        "economic_impact": economic_impact,
        "expectation_gap": expectation_gap,
        "market_pricing": market_pricing,
        "impact_chain": impact_chain,
        "next_catalyst": catalyst,
        "market_signal": market_signal,
        "opportunity_view": view,
        "attention_score": attention_score,
        "score_explanation": (
            "Evidence-qualified theme dimensions 25 + new information 15 + economic impact 20 "
            "+ expectation gap 15 + market pricing 10 + candidate impact chain 10 + catalyst 5; "
            "stage and hard-risk penalties apply. This is research-attention priority, not an "
            "expected-return forecast or trading signal."
        ),
        "missing_opportunity_factors": missing_factors,
    }


def _dimension_levels(name: str) -> set[str]:
    if name in {"many", "timely"}:
        return {"structured_market", "official", "industry"}
    return {"official", "industry"}


def _strength_factor(
    raw: Any,
    name: str,
    usable: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
    source_levels: set[str],
    *,
    required_categories: set[str] | None = None,
    magnitude_required: bool = False,
    frozen_content_required: bool = False,
) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    assessment = item.get("assessment", "unknown")
    if assessment not in _STRENGTH_POINTS:
        assessment = "unknown"
    refs = (
        list(item.get("evidence_refs", []))
        if isinstance(item.get("evidence_refs", []), list)
        else []
    )
    eligible_refs = [
        ref
        for ref in refs
        if ref in usable and evidence_by_id[ref].get("source_level") in source_levels
    ]
    if required_categories is not None:
        eligible_refs = [
            ref
            for ref in eligible_refs
            if evidence_by_id[ref].get("category") in required_categories
        ]
    content_qualified = not frozen_content_required or any(
        _has_frozen_primary_content(evidence_by_id[ref]) for ref in eligible_refs
    )
    source_qualified = (
        bool(refs)
        and all(ref in usable for ref in refs)
        and bool(eligible_refs)
        and content_qualified
    )
    magnitude = _normalized_magnitude(item.get("magnitude")) if magnitude_required else None
    magnitude_multiplier = (
        {
            "quantified": 1.0,
            "partial": 0.5,
            "unknown": 0.0,
        }.get(magnitude["status"], 0.0)
        if magnitude is not None
        else 1.0
    )
    qualified = source_qualified and magnitude_multiplier > 0
    points = (
        _FACTOR_WEIGHTS[name]
        * _STRENGTH_POINTS[assessment]
        * magnitude_multiplier
        if source_qualified
        else 0.0
    )
    result = {
        "assessment": assessment,
        "reason": item.get("reason", "UNKNOWN"),
        "evidence_refs": refs,
        "evidence_qualified": qualified,
        "source_qualified": source_qualified,
        "frozen_content_qualified": content_qualified,
        "points": round(points, 2),
        "max_points": _FACTOR_WEIGHTS[name],
    }
    if magnitude is not None:
        result["magnitude"] = magnitude
        result["quantification_multiplier"] = magnitude_multiplier
    return result


def _categorical_factor(
    raw: Any,
    point_map: dict[str, int],
    usable: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
    source_levels: set[str],
    *,
    extra_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    assessment = item.get("assessment", "unknown")
    if assessment not in point_map:
        assessment = "unknown"
    refs = (
        list(item.get("evidence_refs", []))
        if isinstance(item.get("evidence_refs", []), list)
        else []
    )
    qualified = _refs_qualified(refs, usable, evidence_by_id, source_levels)
    result = {
        "assessment": assessment,
        "reason": item.get("reason", "UNKNOWN"),
        "evidence_refs": refs,
        "evidence_qualified": qualified,
        "points": point_map[assessment] if qualified else 0,
        "max_points": max(point_map.values()),
    }
    for field in extra_fields:
        result[field] = item.get(field, "none")
    return result


def _normalized_magnitude(raw: Any) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    status = item.get("status", "unknown")
    if status not in {"quantified", "partial", "unknown"}:
        status = "unknown"
    return {
        "status": status,
        "basis": item.get("basis", "unknown"),
        "numerator": item.get("numerator"),
        "denominator": item.get("denominator"),
        "ratio": item.get("ratio"),
        "formula": item.get("formula", "UNKNOWN"),
    }


def _has_frozen_primary_content(evidence: dict[str, Any]) -> bool:
    document = evidence.get("document")
    if isinstance(document, dict) and document.get("text") and document.get("text_sha256"):
        return True
    return any(
        isinstance(fact, dict)
        and fact.get("status") == "observed"
        and fact.get("value") is not None
        for fact in evidence.get("facts", [])
    )


def _market_pricing_factor(
    raw: Any,
    usable: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
    market_signal: dict[str, Any],
    *,
    positive_fundamental_context: bool,
) -> dict[str, Any]:
    declared = _categorical_factor(
        raw,
        _PRICING_POINTS,
        usable,
        evidence_by_id,
        {"structured_market"},
    )
    if declared["assessment"] != "unknown" and declared["evidence_qualified"]:
        return {**declared, "origin": "declared_snapshot_assessment"}

    candidate = market_signal.get("candidate")
    benchmark = market_signal.get("csi800_benchmark")
    if not isinstance(candidate, dict) or not isinstance(benchmark, dict):
        return {
            **declared,
            "assessment": "unknown",
            "reason": "缺少候选与中证800同期十日数据，市场定价保持 UNKNOWN。",
            "evidence_refs": [],
            "evidence_qualified": False,
            "points": 0,
            "origin": "deterministic_market_signal",
        }

    direction = market_signal.get("direction")
    relative = market_signal.get("ten_session_relative_return_pct")
    if relative is None:
        assessment = "unknown"
    elif direction in {"positive", "strong_positive"}:
        assessment = "confirmed"
    elif direction in {"negative", "strong_negative"}:
        assessment = "contradicted"
    elif direction == "flat" and positive_fundamental_context:
        assessment = "underreacted"
    else:
        assessment = "unknown"
    refs = [candidate["evidence_ref"], benchmark["evidence_ref"]]
    qualified = assessment != "unknown" and _refs_qualified(
        refs,
        usable,
        evidence_by_id,
        {"structured_market"},
    )
    return {
        "assessment": assessment,
        "reason": (
            f"候选相对中证800十日收益为 {relative}%；按固定阈值映射为 {assessment}。"
            if relative is not None
            else "同期相对收益不可计算，市场定价保持 UNKNOWN。"
        ),
        "evidence_refs": refs if qualified else [],
        "evidence_qualified": qualified,
        "points": _PRICING_POINTS[assessment] if qualified else 0,
        "max_points": max(_PRICING_POINTS.values()),
        "origin": "deterministic_market_signal",
        "declared_assessment": declared["assessment"],
    }


def _impact_chain(
    candidate: dict[str, Any],
    theme: dict[str, Any],
    usable: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    raw_chain = candidate.get("impact_chain")
    explicit = isinstance(raw_chain, list) and bool(raw_chain)
    if explicit:
        steps = []
        for raw in raw_chain:
            item = raw if isinstance(raw, dict) else {}
            refs = (
                list(item.get("evidence_refs", []))
                if isinstance(item.get("evidence_refs", []), list)
                else []
            )
            status = item.get("status", "unknown")
            qualified = status in {"supported", "partial"} and _refs_qualified(
                refs,
                usable,
                evidence_by_id,
                {"official", "industry", "structured_market"},
            )
            steps.append(
                {
                    "step": item.get("step", "UNKNOWN"),
                    "status": status,
                    "evidence_refs": refs,
                    "evidence_qualified": qualified,
                }
            )
    else:
        steps = [
            {
                "step": step,
                "status": "unknown",
                "evidence_refs": [],
                "evidence_qualified": False,
            }
            for step in theme.get("transmission_chain", [])
        ]
    supported_units = sum(
        (
            1.0
            if step["status"] == "supported" and step["evidence_qualified"]
            else 0.5
            if step["status"] == "partial" and step["evidence_qualified"]
            else 0.0
        )
        for step in steps
    )
    denominator = max(3, len(steps))
    coverage = min(1.0, supported_units / denominator)
    return {
        "explicit_candidate_chain": explicit,
        "steps": steps,
        "coverage_ratio": round(coverage, 4),
        "points": round(10 * coverage, 2),
        "max_points": 10,
    }


def _catalyst(
    raw: Any,
    usable: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
    decision_at: datetime,
) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    scheduled_at = item.get("scheduled_at")
    valid_time = False
    if isinstance(scheduled_at, str):
        try:
            scheduled_time = parse_datetime(
                scheduled_at,
                "opportunity_profile.next_catalyst.scheduled_at",
            )
            valid_time = scheduled_time > decision_at
        except (TypeError, ValueError):
            valid_time = False
    refs = (
        list(item.get("evidence_refs", []))
        if isinstance(item.get("evidence_refs", []), list)
        else []
    )
    qualified = valid_time and _refs_qualified(refs, usable, evidence_by_id, {"official"})
    return {
        "description": item.get("description", "UNKNOWN"),
        "scheduled_at": scheduled_at if valid_time else None,
        "verification_rule": item.get("verification_rule", "UNKNOWN"),
        "evidence_refs": refs,
        "evidence_qualified": qualified,
        "points": 5 if qualified else 0,
        "max_points": 5,
    }


def _market_signal(
    evidence_by_id: dict[str, dict[str, Any]],
    usable: set[str],
    candidate_refs: set[str],
    benchmark_refs: set[str],
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    allowed_refs = candidate_refs.union(benchmark_refs).intersection(usable)
    for ref in sorted(allowed_refs):
        evidence = evidence_by_id[ref]
        if evidence.get("source_level") != "structured_market":
            continue
        latest = evidence.get("latest")
        derived = evidence.get("derived")
        if not isinstance(latest, dict) or not isinstance(derived, dict):
            continue
        one_day = _decimal(latest.get("pct_chg"))
        ten_day = _decimal(derived.get("return_10_sessions_pct"))
        instrument = evidence.get("instrument")
        instrument = instrument if isinstance(instrument, dict) else {}
        observations.append(
            {
                "evidence_ref": ref,
                "instrument_code": instrument.get("code"),
                "instrument_kind": instrument.get("kind"),
                "as_of": evidence.get("as_of"),
                "one_session_return_pct": float(one_day) if one_day is not None else None,
                "ten_session_return_pct": float(ten_day) if ten_day is not None else None,
            }
        )
    if not observations:
        return {"status": "unknown", "observations": []}
    candidates = [item for item in observations if item["evidence_ref"] in candidate_refs]
    benchmarks = [item for item in observations if item["instrument_code"] == "sh.000906"]
    if not candidates:
        return {"status": "unknown", "observations": observations}
    candidate_observation = max(
        candidates, key=lambda item: (item["as_of"] or "", item["evidence_ref"])
    )
    benchmark_observation = (
        max(benchmarks, key=lambda item: (item["as_of"] or "", item["evidence_ref"]))
        if benchmarks
        else None
    )
    candidate_ten_day = candidate_observation["ten_session_return_pct"]
    benchmark_ten_day = (
        benchmark_observation["ten_session_return_pct"]
        if benchmark_observation is not None
        else None
    )
    relative_ten_day = (
        candidate_ten_day - benchmark_ten_day
        if candidate_ten_day is not None and benchmark_ten_day is not None
        else None
    )
    signal_value = relative_ten_day if relative_ten_day is not None else candidate_ten_day
    if signal_value is None:
        label = "unknown"
    elif signal_value >= 15:
        label = "strong_positive"
    elif signal_value >= 3:
        label = "positive"
    elif signal_value <= -10:
        label = "strong_negative"
    elif signal_value <= -3:
        label = "negative"
    else:
        label = "flat"
    return {
        "status": "observed",
        "direction": label,
        "candidate": candidate_observation,
        "csi800_benchmark": benchmark_observation,
        "ten_session_relative_return_pct": relative_ten_day,
        "observations": observations,
    }


def _refs_qualified(
    refs: list[str],
    usable: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
    allowed_levels: set[str],
) -> bool:
    return bool(refs) and all(ref in usable for ref in refs) and any(
        evidence_by_id[ref].get("source_level") in allowed_levels for ref in refs
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
