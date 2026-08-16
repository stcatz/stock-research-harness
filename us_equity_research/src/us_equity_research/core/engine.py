from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Any

from .calculations import UNKNOWN, build_calculation_bundle
from .contracts import (
    MARKET,
    PRIMARY_EVIDENCE_CATEGORIES,
    SCHEMA_VERSION,
    RunRequest,
    parse_datetime,
)
from .snapshot import ValidatedSnapshot

METHOD_ID = "us-equity-research-v0.1"

DECISION_LABELS = {
    "observe": "Observe",
    "continue_research": "Continue Research",
    "exclude": "Exclude",
}
DECISION_ORDER = {"observe": 0, "continue_research": 1, "exclude": 2}
ROLE_ORDER = {"leader": 0, "platform": 1, "beneficiary": 2, "speculative": 3}
HARD_RISKS = {
    "going_concern",
    "restatement",
    "late_filing",
    "sanctions",
    "liquidity_constraint",
}
ISSUER_SPECIFIC_CATEGORIES = {"sec_filing", "issuer_ir"}


def build_research_packet(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    selected_themes = _select_themes(snapshot.themes, request)
    request_seed = {"request": request.to_dict(), "snapshot_hash": snapshot.snapshot_hash}
    seed_hash = _sha256_value(request_seed)

    packet: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "writer_mode": "engine",
        "method_id": METHOD_ID,
        "run_id": f"us-run-{seed_hash[:16]}",
        "artifact_id": f"us-artifact-{seed_hash[:20]}",
        "workflow": request.workflow,
        "decision_at": request.decision_at.isoformat(),
        "generated_at": generated_at.isoformat(),
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "data_mode": snapshot.data_mode,
        "pit_quality": snapshot.pit_quality,
        "market_context": _canonicalize_market_context(snapshot.data["market_context"]),
    }
    if request.subject is not None:
        packet["subject"] = request.subject
    if request.symbol is not None:
        packet["symbol"] = request.symbol

    all_decisions: list[dict[str, Any]] = []
    theme_summaries: list[dict[str, Any]] = []
    time_leak_refs: set[str] = set()
    cutoff_excluded_fact_ids: set[str] = set()

    for theme in selected_themes:
        theme_decisions: list[dict[str, Any]] = []
        for candidate in theme["candidates"]:
            if request.workflow == "stock_research" and candidate["symbol"] != request.symbol:
                continue
            decision = _evaluate_candidate(snapshot, request, theme, candidate)
            theme_decisions.append(decision)
            all_decisions.append(decision)
            time_leak_refs.update(decision["time_leak_evidence_refs"])
            cutoff_excluded_fact_ids.update(decision.pop("_cutoff_excluded_fact_ids"))

        if theme_decisions or request.workflow != "stock_research":
            theme_summaries.append(_theme_summary(theme, theme_decisions))

    all_decisions.sort(
        key=lambda item: (
            DECISION_ORDER[item["decision"]],
            item["theme_id"],
            ROLE_ORDER.get(item["role"], 99),
            item["symbol"],
        )
    )
    focus = [item for item in all_decisions if item["decision"] != "exclude"][: request.top_n]
    excluded = [item for item in all_decisions if item["decision"] == "exclude"]

    warnings = _build_warnings(
        snapshot,
        request,
        selected_themes,
        all_decisions,
        time_leak_refs,
        cutoff_excluded_fact_ids,
    )

    packet["themes"] = theme_summaries
    packet["focus"] = focus
    packet["excluded"] = excluded
    packet["all_decisions"] = all_decisions
    packet["warnings"] = warnings
    packet["data_gaps"] = _unique_strings(
        [
            *[gap for theme in selected_themes for gap in theme.get("data_gaps", [])],
            *[gap for decision in all_decisions for gap in decision.get("data_gaps", [])],
        ]
    )
    packet["data_status"] = _data_status(snapshot, all_decisions)

    stable_packet = deepcopy(packet)
    stable_packet.pop("generated_at", None)
    # analysis_hash tracks stable analysis content, not raw snapshot serialization order.
    stable_packet.pop("snapshot_hash", None)
    stable_packet.pop("run_id", None)
    stable_packet.pop("artifact_id", None)
    packet["analysis_hash"] = _sha256_value(stable_packet)
    return packet


def _select_themes(themes: list[dict[str, Any]], request: RunRequest) -> list[dict[str, Any]]:
    if request.workflow == "daily_report":
        selected = list(themes)
    elif request.workflow == "theme_research":
        query = (request.subject or "").casefold()
        selected = [
            theme
            for theme in themes
            if query in theme["theme_id"].casefold() or query in theme["name"].casefold()
        ]
    else:
        selected = [
            theme
            for theme in themes
            if any(candidate["symbol"] == request.symbol for candidate in theme["candidates"])
        ]
    return sorted(selected, key=_theme_order_key)


def _evaluate_candidate(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    theme: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    referenced_evidence = _collect_candidate_evidence_refs(candidate)
    candidate_specific_evidence = referenced_evidence
    usable_evidence_refs: list[str] = []
    time_leak_evidence_refs: list[str] = []
    for evidence_ref in referenced_evidence:
        evidence = snapshot.evidence_by_id[evidence_ref]
        if (
            parse_datetime(evidence["available_at"], f"evidence.{evidence_ref}.available_at")
            <= request.decision_at
        ):
            usable_evidence_refs.append(evidence_ref)
        else:
            time_leak_evidence_refs.append(evidence_ref)

    usable_evidence_refs = sorted(usable_evidence_refs)
    time_leak_evidence_refs = sorted(time_leak_evidence_refs)

    usable_candidate_specific = [
        snapshot.evidence_by_id[evidence_ref]
        for evidence_ref in candidate_specific_evidence
        if evidence_ref in usable_evidence_refs
    ]
    official_primary = [
        evidence
        for evidence in usable_candidate_specific
        if evidence["source_level"] == "official"
        and evidence["category"] in PRIMARY_EVIDENCE_CATEGORIES
    ]
    issuer_specific_official = [
        evidence
        for evidence in official_primary
        if evidence["category"] in ISSUER_SPECIFIC_CATEGORIES
    ]
    structured_market = [
        snapshot.evidence_by_id[evidence_ref]
        for evidence_ref in _sorted_unique_strings(candidate.get("market_evidence_refs", []))
        if evidence_ref in usable_evidence_refs
    ]

    calculation_bundle = build_calculation_bundle(snapshot, candidate, request.decision_at)
    calculations = calculation_bundle["calculations"]
    calculations_complete = all(item["status"] != UNKNOWN for item in calculations)
    invalidation_conditions = _unique_strings(
        [*theme.get("invalidation_conditions", []), *candidate.get("invalidation_conditions", [])]
    )
    risk_flags = list(candidate.get("risk_flags", []))
    hard_risk_flags = sorted(flag for flag in risk_flags if flag in HARD_RISKS)
    next_catalyst_at = parse_datetime(theme["next_catalyst_at"], "theme.next_catalyst_at")
    stage = theme["stage"]

    gates = {
        "official_primary": bool(official_primary),
        "structured_market": bool(structured_market),
        "transmission_chain": len(theme["transmission_chain"]) >= 3,
        "three_viewpoints": _has_three_viewpoints(candidate, set(usable_evidence_refs)),
        "invalidation": bool(invalidation_conditions),
        "future_catalyst": next_catalyst_at > request.decision_at,
        "no_time_leaks": not time_leak_evidence_refs,
        "stage_not_fading": stage != "fading",
        "calculations_complete": calculations_complete,
        "issuer_specific_official": bool(issuer_specific_official),
    }

    candidate_data_gaps = list(candidate.get("data_gaps", []))
    if (
        not gates["official_primary"]
        or not gates["structured_market"]
        or not gates["transmission_chain"]
        or not gates["stage_not_fading"]
        or bool(hard_risk_flags)
    ):
        decision = "exclude"
    elif (
        stage in {"crowded", "diverging"}
        or not gates["three_viewpoints"]
        or not gates["no_time_leaks"]
        or not gates["issuer_specific_official"]
        or not gates["calculations_complete"]
        or not gates["future_catalyst"]
        or bool(candidate_data_gaps)
    ):
        decision = "continue_research"
    else:
        decision = "observe"

    reasons = _build_reasons(
        gates=gates,
        stage=stage,
        hard_risk_flags=hard_risk_flags,
        candidate_data_gaps=candidate_data_gaps,
    )
    evidence_cards = [
        _evidence_card(snapshot.evidence_by_id[evidence_ref])
        for evidence_ref in usable_evidence_refs
    ]

    decision_payload = {
        "candidate_id": f"{theme['theme_id']}:{candidate['security_id']}",
        "theme_id": theme["theme_id"],
        "theme_name": theme["name"],
        "security_id": candidate["security_id"],
        "symbol": candidate["symbol"],
        "name": candidate["name"],
        "role": candidate["role"],
        "decision": decision,
        "decision_label": DECISION_LABELS[decision],
        "thesis": candidate["thesis"],
        "bull_case": _canonicalize_case(candidate["bull_case"]),
        "bear_case": _canonicalize_case(candidate["bear_case"]),
        "risk_verdict": _canonicalize_case(candidate["risk_verdict"]),
        "transmission_chain": list(theme["transmission_chain"]),
        "next_catalyst_at": theme["next_catalyst_at"],
        "invalidation_conditions": invalidation_conditions,
        "gates": gates,
        "reasons": reasons,
        "risk_flags": risk_flags,
        "data_gaps": candidate_data_gaps,
        "manual_review_items": list(candidate.get("manual_review_items", [])),
        "usable_evidence_refs": usable_evidence_refs,
        "time_leak_evidence_refs": time_leak_evidence_refs,
        "evidence": evidence_cards,
        "facts": calculation_bundle["facts"],
        "calculations": calculations,
        "_cutoff_excluded_fact_ids": calculation_bundle["cutoff_excluded_fact_ids"],
    }
    return decision_payload


def _collect_candidate_evidence_refs(candidate: Mapping[str, Any]) -> list[str]:
    refs = [
        *candidate.get("evidence_refs", []),
        *candidate.get("market_evidence_refs", []),
        *candidate["bull_case"].get("evidence_refs", []),
        *candidate["bear_case"].get("evidence_refs", []),
        *candidate["risk_verdict"].get("evidence_refs", []),
    ]
    return _sorted_unique_strings(refs)


def _has_three_viewpoints(
    candidate: Mapping[str, Any],
    usable_evidence_refs: set[str],
) -> bool:
    return all(
        candidate[case_name].get("text")
        and any(
            evidence_ref in usable_evidence_refs
            for evidence_ref in candidate[case_name].get("evidence_refs", [])
        )
        for case_name in ("bull_case", "bear_case", "risk_verdict")
    )


def _build_reasons(
    *,
    gates: Mapping[str, bool],
    stage: str,
    hard_risk_flags: list[str],
    candidate_data_gaps: list[str],
) -> list[str]:
    reasons: list[str] = []
    if not gates["official_primary"]:
        reasons.append("Missing usable official primary evidence before decision_at.")
    if not gates["structured_market"]:
        reasons.append("Missing usable structured market evidence before decision_at.")
    if not gates["transmission_chain"]:
        reasons.append("Transmission chain must contain at least three links.")
    if not gates["three_viewpoints"]:
        reasons.append("Bull, bear, and risk viewpoints need usable supporting evidence.")
    if not gates["invalidation"]:
        reasons.append("Invalidation conditions are required.")
    if not gates["future_catalyst"]:
        reasons.append("Next catalyst is not in the future.")
    if not gates["no_time_leaks"]:
        reasons.append("Post-cutoff evidence was removed from the usable record.")
    if not gates["stage_not_fading"]:
        reasons.append("Theme stage is fading.")
    if stage in {"crowded", "diverging"}:
        reasons.append(f"Theme stage {stage} limits the decision to continue_research at most.")
    if not gates["calculations_complete"]:
        reasons.append("Deterministic calculations are incomplete at the decision cutoff.")
    if not gates["issuer_specific_official"]:
        reasons.append("Missing issuer-specific official disclosure.")
    if hard_risk_flags:
        reasons.append("Hard risk flags present: " + ", ".join(hard_risk_flags))
    if candidate_data_gaps:
        reasons.append("Material candidate data gaps remain.")
    if not reasons:
        reasons.append("All required evidence, gating, and calculation checks passed.")
    return reasons


def _theme_summary(
    theme: Mapping[str, Any],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_counts = {
        "observe": sum(1 for item in decisions if item["decision"] == "observe"),
        "continue_research": sum(
            1 for item in decisions if item["decision"] == "continue_research"
        ),
        "exclude": sum(1 for item in decisions if item["decision"] == "exclude"),
    }
    return {
        "theme_id": theme["theme_id"],
        "theme_name": theme["name"],
        "event_type": theme["event_type"],
        "stage": theme["stage"],
        "dimensions": _canonicalize_dimensions(theme["dimensions"]),
        "transmission_chain": list(theme["transmission_chain"]),
        "next_catalyst_at": theme["next_catalyst_at"],
        "evidence_refs": _sorted_unique_strings(theme.get("evidence_refs", [])),
        "data_gaps": list(theme.get("data_gaps", [])),
        "candidate_ids": sorted(item["security_id"] for item in decisions),
        "candidate_counts": candidate_counts,
    }


def _build_warnings(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    selected_themes: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    time_leak_refs: set[str],
    cutoff_excluded_fact_ids: set[str],
) -> list[str]:
    warnings: list[str] = []
    if snapshot.data_mode == "fixture":
        warnings.append(snapshot.data["fixture_notice"])
    if snapshot.pit_quality != "P1":
        warnings.append(f"pit_quality={snapshot.pit_quality}; this is not a strict P1 replay.")
    if snapshot.retrieved_at > request.decision_at:
        warnings.append(
            "snapshot.retrieved_at is later than decision_at; treat this as reconstructed."
        )
    if time_leak_refs:
        warnings.append("Excluded post-cutoff evidence refs: " + ", ".join(sorted(time_leak_refs)))
    if cutoff_excluded_fact_ids:
        warnings.append(
            "Excluded post-cutoff financial fact ids: "
            + ", ".join(sorted(cutoff_excluded_fact_ids))
        )
    if not selected_themes:
        warnings.append("No themes matched the request.")
    elif not decisions:
        warnings.append("Matched themes contained no candidates for the request.")
    return warnings


def _data_status(
    snapshot: ValidatedSnapshot,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    usable_refs = _unique_strings(
        [ref for decision in decisions for ref in decision["usable_evidence_refs"]]
    )
    official_times = [
        snapshot.evidence_by_id[ref]["available_at"]
        for ref in usable_refs
        if snapshot.evidence_by_id[ref]["source_level"] == "official"
        and snapshot.evidence_by_id[ref]["category"] in PRIMARY_EVIDENCE_CATEGORIES
    ]
    market_dates = [
        snapshot.evidence_by_id[ref]["as_of"]
        for ref in usable_refs
        if snapshot.evidence_by_id[ref]["source_level"] == "structured_market"
    ]
    return {
        "status": "completed",
        "decision_count": len(decisions),
        "official_primary_latest_available_at": _latest_timestamp_value(
            official_times,
            field_name="available_at",
        ),
        "market_latest_as_of": _latest_timestamp_value(
            market_dates,
            field_name="as_of",
        ),
    }


def _evidence_card(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": evidence["evidence_id"],
        "category": evidence["category"],
        "source_level": evidence["source_level"],
        "title": evidence["title"],
        "source_url": evidence["source_url"],
        "source_document_id": evidence["source_document_id"],
        "published_at": evidence["published_at"],
        "effective_at": evidence["effective_at"],
        "available_at": evidence["available_at"],
        "retrieved_at": evidence["retrieved_at"],
        "as_of": evidence["as_of"],
        "summary": evidence["summary"],
    }


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _sorted_unique_strings(values: list[str]) -> list[str]:
    return sorted(_unique_strings(list(values)))


def _canonicalize_case(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": case["text"],
        "evidence_refs": _sorted_unique_strings(case.get("evidence_refs", [])),
    }


def _canonicalize_dimensions(dimensions: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: {
            "assessment": value["assessment"],
            "reason": value["reason"],
            "evidence_refs": _sorted_unique_strings(value.get("evidence_refs", [])),
        }
        for key, value in dimensions.items()
    }


def _canonicalize_market_context(market_context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **deepcopy(market_context),
        "evidence_refs": _sorted_unique_strings(market_context.get("evidence_refs", [])),
    }


def _theme_order_key(theme: Mapping[str, Any]) -> tuple[str, str]:
    return (theme["theme_id"], theme["name"])


def _sha256_value(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _latest_timestamp_value(
    values: list[str],
    *,
    field_name: str,
) -> str | None:
    if not values:
        return None
    return max(
        values,
        key=lambda value: (
            parse_datetime(value, field_name),
            value,
        ),
    )
