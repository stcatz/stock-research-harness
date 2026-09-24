from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import (
    DECISION_LABELS,
    MARKET,
    SCHEMA_VERSION,
    RunRequest,
    parse_datetime,
)
from .feedback_metrics import build_feedback, expected_session, security_code
from .market_diagnostics import build_market_diagnostics
from .observation_plan import build_observation_plan
from .opportunity import assess_opportunity
from .quality import build_research_queue, decorate_quality
from .snapshot import ValidatedSnapshot
from .utils import sha256_value

DECISION_ORDER = {"observe": 0, "continue_research": 1, "exclude": 2}
ROLE_ORDER = {"core": 0, "midcap": 1, "elastic": 2, "follower": 3}
HARD_RISK_FLAGS = {"ST", "SUSPENDED", "REGULATORY_MAJOR", "LIQUIDITY_INSUFFICIENT"}
OFFICIAL_CATEGORIES = {"official_event", "company_disclosure", "policy", "regulatory"}
METHOD_ID = "a-share-theme-v2.0"
# Method identity (a-share-theme-v2.0) versions the research methodology; this constant
# versions the engine implementation that produced a run. Both feed the run seed, so a
# change in either yields a different run_id / artifact_id instead of silently reusing
# an artifact built by different code.
ENGINE_VERSION = "cn-plan-v3.0"


def build_research_packet(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    selected_themes = _select_themes(snapshot.themes, request)
    stable_seed = {
        "engine_version": ENGINE_VERSION,
        "request": request.to_dict(),
        "snapshot_hash": snapshot.snapshot_hash,
        "method_id": METHOD_ID,
    }
    seed_hash = sha256_value(stable_seed)
    run_id = f"cn-{request.decision_at.date().isoformat()}-{seed_hash[:12]}"
    artifact_id = f"cn-artifact-{seed_hash[:16]}"

    decisions: list[dict[str, Any]] = []
    theme_summaries: list[dict[str, Any]] = []
    referenced_time_leaks: set[str] = set()

    for theme in selected_themes:
        theme_decisions: list[dict[str, Any]] = []
        for candidate in theme["candidates"]:
            if (
                request.workflow == "stock_research"
                and candidate["symbol"].casefold() != (request.symbol or "").casefold()
            ):
                continue
            decision = _evaluate_candidate(snapshot, request, theme, candidate)
            referenced_time_leaks.update(decision["time_leak_evidence_refs"])
            theme_decisions.append(decision)
            decisions.append(decision)

        counts = Counter(item["decision"] for item in theme_decisions)
        theme_summaries.append(
            {
                "theme_id": theme["theme_id"],
                "name": theme["name"],
                "event_type": theme["event_type"],
                "stage": theme["stage"],
                "dimensions": deepcopy(theme["dimensions"]),
                "transmission_chain": list(theme["transmission_chain"]),
                "next_catalyst_at": theme["next_catalyst_at"],
                "counter_thesis": theme["counter_thesis"],
                "invalidation_conditions": list(theme["invalidation_conditions"]),
                "data_gaps": list(theme.get("data_gaps", [])),
                "candidate_counts": {
                    "observe": counts.get("observe", 0),
                    "continue_research": counts.get("continue_research", 0),
                    "exclude": counts.get("exclude", 0),
                },
            }
        )

    decorate_quality(decisions, request.decision_at.isoformat())
    decisions.sort(
        key=lambda item: (
            DECISION_ORDER[item["decision"]],
            -item["research_priority"],
            ROLE_ORDER[item["role"]],
            item["symbol"],
        )
    )
    focus = [item for item in decisions if item["decision"] != "exclude"][: request.top_n]
    excluded = [item for item in decisions if item["decision"] == "exclude"]

    warnings = _build_warnings(snapshot, request, selected_themes, decisions, referenced_time_leaks)
    data_status = _data_status(snapshot, request, decisions)
    packet_without_hash = {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "writer_mode": "engine",
        "method_id": METHOD_ID,
        "engine_version": ENGINE_VERSION,
        "run_id": run_id,
        "artifact_id": artifact_id,
        "workflow": request.workflow,
        "subject": request.subject,
        "symbol": request.symbol,
        "decision_at": request.decision_at.isoformat(),
        "generated_at": generated_at.isoformat(),
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "data_mode": snapshot.data_mode,
        "pit_quality": snapshot.pit_quality,
        "data_status": data_status,
        "research_feedback": build_feedback(snapshot.data, decisions, request.decision_at, request.top_n),
        "market_diagnostics": build_market_diagnostics(snapshot.data, decisions, request.decision_at),
        "market_context": _timely_context(snapshot, request),
        "market_discoveries": [deepcopy(item) for item in snapshot.data.get("market_discoveries", [])
                               if _refs_available(snapshot, request, [item.get("evidence_ref")])],
        "market_discovery_input_count": len(snapshot.data.get("market_discoveries", [])),
        "themes": theme_summaries,
        "focus": focus,
        "excluded": excluded,
        "all_decisions": decisions,
        "research_queue": build_research_queue(decisions),
        "evaluation_protocol": {
            "metric": "benchmark_relative_unadjusted_close_return",
            "benchmark": "000906.SH",
            "horizons_trading_days": [5, 20],
            "immutable_sidecar_required": True,
        },
        "warnings": warnings,
        "data_gaps": _unique_strings(
            gap for item in decisions for gap in item.get("data_gaps", [])
        ),
    }
    packet_without_hash['observation_plan'] = build_observation_plan(
        snapshot.data, decisions, packet_without_hash['market_diagnostics'],
        packet_without_hash['market_discoveries'], request.decision_at,
    )
    stable_packet = deepcopy(packet_without_hash)
    stable_packet.pop("generated_at", None)
    packet_without_hash["analysis_hash"] = sha256_value(stable_packet)
    return packet_without_hash


def _select_themes(themes: list[dict[str, Any]], request: RunRequest) -> list[dict[str, Any]]:
    if request.workflow == "daily_report":
        return list(themes)
    if request.workflow == "theme_research":
        query = (request.subject or "").casefold()
        return [
            theme
            for theme in themes
            if query in theme["name"].casefold() or query in theme["theme_id"].casefold()
        ]
    query = (request.symbol or "").casefold()
    return [
        theme
        for theme in themes
        if any(candidate["symbol"].casefold() == query for candidate in theme["candidates"])
    ]


def _evaluate_candidate(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    theme: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    evidence_refs = _unique_strings(
        [
            *theme.get("evidence_refs", []),
            *candidate.get("evidence_refs", []),
            *_opportunity_evidence_refs(candidate),
        ]
    )
    market_refs = _unique_strings(candidate.get("market_evidence_refs", []))
    all_refs = _unique_strings([*evidence_refs, *market_refs])
    usable_refs: list[str] = []
    time_leak_refs: list[str] = []
    for evidence_ref in all_refs:
        evidence = snapshot.evidence_by_id[evidence_ref]
        # Knowledge becomes usable when it was available, not when its legal/economic effect
        # begins. Future-effective policies are legitimate announced catalysts.
        if (
            parse_datetime(evidence["available_at"], f"evidence.{evidence_ref}.available_at")
            <= request.decision_at
        ):
            usable_refs.append(evidence_ref)
        else:
            time_leak_refs.append(evidence_ref)

    usable_evidence = [snapshot.evidence_by_id[ref] for ref in usable_refs]
    official_evidence = [
        item
        for item in usable_evidence
        if item["source_level"] == "official" and item["category"] in OFFICIAL_CATEGORIES
    ]
    structured_market = [
        snapshot.evidence_by_id[ref]
        for ref in market_refs
        if ref in usable_refs
        and snapshot.evidence_by_id[ref]["source_level"] == "structured_market"
    ]
    company_disclosure = [
        item for item in official_evidence if item["category"] == "company_disclosure"
    ]

    risk_flags = list(candidate.get("risk_flags", []))
    data_gaps = _unique_strings([*theme.get("data_gaps", []), *candidate.get("data_gaps", [])])
    opportunity = assess_opportunity(
        theme,
        candidate,
        snapshot.evidence_by_id,
        usable_refs,
        request.decision_at,
        benchmark_refs=[
            ref
            for ref in snapshot.data["market_context"].get("evidence_refs", [])
            if ref in snapshot.evidence_by_id
            and snapshot.evidence_by_id[ref]["source_level"] == "structured_market"
            and parse_datetime(
                snapshot.evidence_by_id[ref]["available_at"],
                f"evidence.{ref}.available_at",
            )
            <= request.decision_at
        ],
    )
    core_opportunity_factors = {
        "new_information",
        "economic_impact",
        "expectation_gap",
        "market_pricing",
    }
    # Session freshness is an extra fail-closed gate merged in from the local
    # handoff line. It can only hold a candidate back from `observe`; it never
    # promotes one, so the opportunity-engine thresholds above are unchanged.
    readiness = _market_readiness(snapshot, request)
    if readiness != "CURRENT_SESSION" and snapshot.data_mode != "fixture":
        data_gaps.append("MARKET_SESSION_" + readiness)
    target_session = (
        parse_datetime(snapshot.data["as_of"], "snapshot.as_of")
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    own_session = any(
        item.get("provider", {}).get("frequency") == "1d"
        and item.get("instrument", {}).get("code") == security_code(candidate["security_id"])
        and item.get("latest", {}).get("date") == target_session
        and parse_datetime(item["as_of"], "evidence.as_of")
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
        == target_session
        and parse_datetime(item["as_of"], "evidence.as_of")
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .hour
        >= 15
        and parse_datetime(item["as_of"], "evidence.as_of") <= request.decision_at
        for item in structured_market
    )
    if not own_session and snapshot.data_mode != "fixture":
        data_gaps.append("CANDIDATE_SESSION_UNVERIFIED")
    gates = {
        "official_event": bool(official_evidence),
        "structured_market": bool(structured_market),
        "transmission_chain": len(theme["transmission_chain"]) >= 3,
        "candidate_impact_chain": opportunity["impact_chain"]["coverage_ratio"] >= 2 / 3,
        "opportunity_profile": not core_opportunity_factors.intersection(
            opportunity["missing_opportunity_factors"]
        ),
        "counter_thesis": bool(candidate["counter_thesis"] and theme["counter_thesis"]),
        "invalidation_conditions": bool(
            candidate["invalidation_conditions"] and theme["invalidation_conditions"]
        ),
        "next_catalyst": opportunity["next_catalyst"]["evidence_qualified"],
        "time_boundary": not time_leak_refs,
        "stage_not_declining": theme["stage"] != "declining",
        "company_disclosure": bool(company_disclosure),
        "market_freshness": (readiness == "CURRENT_SESSION" and own_session)
        or snapshot.data_mode == "fixture",
    }

    reasons: list[str] = []
    if not gates["official_event"]:
        reasons.append("缺少研究时点前可用的官方事件或公司披露证据")
    if not gates["structured_market"]:
        reasons.append("缺少研究时点前可用的同期结构化市场证据")
    if not gates["transmission_chain"]:
        reasons.append("受益传导链不完整")
    if time_leak_refs:
        reasons.append("引用中包含研究时点之后才可见的证据，已从有效证据中剔除")
    if theme["stage"] == "climax":
        reasons.append("题材处于高潮阶段，不得包装成低风险机会")
    elif theme["stage"] == "declining":
        reasons.append("题材处于退潮阶段")
    if HARD_RISK_FLAGS.intersection(risk_flags):
        reasons.append("命中硬风险标志")
    if not gates["next_catalyst"]:
        reasons.append("缺少由研究时点前正式证据支持的未来催化剂及核验规则")
    if not gates["market_freshness"]:
        reasons.append("行情时点过期或尚未验证最新交易日，不能升级为观察")
    if not company_disclosure:
        reasons.append("缺少候选公司自身正式披露，业务纯度需人工复核")
    if opportunity["missing_opportunity_factors"]:
        reasons.append(
            "机会判断字段未被证据充分覆盖："
            + ", ".join(opportunity["missing_opportunity_factors"])
        )

    qualification_incomplete = (
        not gates["official_event"]
        or not gates["structured_market"]
        or not gates["transmission_chain"]
    )
    theme_timely = theme["dimensions"]["timely"]["assessment"]
    if opportunity["opportunity_view"] == "negative":
        decision = "exclude"
        reasons.append("候选级预期差、市场定价或风险证据形成负向机会判断")
    elif qualification_incomplete:
        decision = "continue_research"
        reasons.append("资格证据尚未齐全，保留在低注意力调查队列而不是自动排除")
    elif not (
        opportunity["opportunity_view"] == "positive"
        and gates["time_boundary"]
        and gates["company_disclosure"]
        and gates["next_catalyst"]
    ) or (
        theme["stage"] in {"climax", "diverging"}
        or theme_timely in {"weak", "unknown"}
        or not gates["market_freshness"]
        or bool(candidate.get("data_gaps"))
    ):
        decision = "continue_research"
    else:
        decision = "observe"

    if not reasons:
        reasons.append("官方事件、结构化市场数据和受益链均满足最低研究门槛")

    return {
        "candidate_id": f"{theme['theme_id']}:{candidate['security_id']}",
        "theme_id": theme["theme_id"],
        "theme_name": theme["name"],
        "event_type": theme["event_type"],
        "stage": theme["stage"],
        "stage_provenance": "seed_assertion_not_computed",
        "security_id": candidate["security_id"],
        "symbol": candidate["symbol"],
        "name": candidate["name"],
        "role": candidate["role"],
        "decision": decision,
        "decision_label": DECISION_LABELS[decision],
        "thesis": candidate["thesis"],
        "counter_thesis": candidate["counter_thesis"],
        "transmission_chain": list(theme["transmission_chain"]),
        "impact_chain": opportunity["impact_chain"],
        "method_dimensions": opportunity["method_dimensions"],
        "opportunity_profile": opportunity,
        "opportunity_view": opportunity["opportunity_view"],
        "attention_score": opportunity["attention_score"],
        "next_catalyst_at": opportunity["next_catalyst"]["scheduled_at"],
        "invalidation_conditions": _unique_strings(
            [*theme["invalidation_conditions"], *candidate["invalidation_conditions"]]
        ),
        "gates": gates,
        "reasons": reasons,
        "risk_flags": risk_flags,
        "data_gaps": data_gaps,
        "manual_review_items": list(candidate["manual_review_items"]),
        "usable_evidence_refs": usable_refs,
        "time_leak_evidence_refs": time_leak_refs,
        "official_evidence_refs": [item["evidence_id"] for item in official_evidence],
        "market_evidence_refs": [item["evidence_id"] for item in structured_market],
        "evidence": [_evidence_card(snapshot.evidence_by_id[ref]) for ref in usable_refs],
    }


def _opportunity_evidence_refs(candidate: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    profile = candidate.get("opportunity_profile")
    if isinstance(profile, dict):
        for name in (
            "new_information",
            "economic_impact",
            "expectation_gap",
            "market_pricing",
            "next_catalyst",
        ):
            factor = profile.get(name)
            if isinstance(factor, dict) and isinstance(factor.get("evidence_refs"), list):
                refs.extend(factor["evidence_refs"])
    chain = candidate.get("impact_chain")
    if isinstance(chain, list):
        for step in chain:
            if isinstance(step, dict) and isinstance(step.get("evidence_refs"), list):
                refs.extend(step["evidence_refs"])
    return _unique_strings(refs)


def _build_warnings(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    selected_themes: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    referenced_time_leaks: set[str],
) -> list[str]:
    warnings: list[str] = []
    if snapshot.data_mode == "fixture":
        warnings.append(
            snapshot.data.get("fixture_notice", "当前使用合成 fixture，不代表真实市场事实。")
        )
    if snapshot.pit_quality != "P1":
        warnings.append(f"输入时间质量为 {snapshot.pit_quality}，不得宣称严格历史 PIT 回放。")
    if parse_datetime(snapshot.data["retrieved_at"], "snapshot.retrieved_at") > request.decision_at:
        warnings.append("快照抓取时间晚于 decision_at；该运行只能视为重建，不得用于严格前视评估。")
    if referenced_time_leaks:
        warnings.append(
            "已剔除研究时点之后才可见的证据：" + ", ".join(sorted(referenced_time_leaks))
        )
    if not selected_themes:
        warnings.append("没有匹配当前研究请求的题材。")
    if selected_themes and not decisions:
        warnings.append("匹配到题材，但没有匹配当前研究请求的候选标的。")
    return warnings


def _data_status(
    snapshot: ValidatedSnapshot,
    request: RunRequest,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    used_refs = {
        evidence_ref for decision in decisions for evidence_ref in decision["usable_evidence_refs"]
    }
    used = [snapshot.evidence_by_id[ref] for ref in sorted(used_refs)]
    official_times = [
        parse_datetime(item["available_at"], "evidence.available_at")
        for item in used
        if item["source_level"] == "official"
    ]
    market_times = [
        parse_datetime(item["as_of"], "evidence.as_of")
        for item in used
        if item["source_level"] == "structured_market"
    ]
    available_inputs = [e for e in snapshot.data["evidence"]
                        if parse_datetime(e["available_at"],"available_at") <= request.decision_at]
    daily_times = [parse_datetime(e["as_of"],"as_of") for e in available_inputs
                   if e.get("provider",{}).get("frequency") == "1d"]
    return {
        "readiness": _market_readiness(snapshot, request),
        "decision_at": request.decision_at.isoformat(),
        "snapshot_as_of": snapshot.data["as_of"],
        "snapshot_retrieved_at": snapshot.data["retrieved_at"],
        "latest_official_available_at": max(official_times).isoformat() if official_times else None,
        "latest_market_as_of": max(market_times).isoformat() if market_times else None,
        "latest_daily_market_as_of": max(daily_times).isoformat() if daily_times else None,
        "latest_input_available_at": max(parse_datetime(e["available_at"],"available_at")
                                           for e in available_inputs).isoformat() if available_inputs else None,
        "latest_input_retrieved_at": max(parse_datetime(e["retrieved_at"],"retrieved_at")
                                           for e in available_inputs).isoformat() if available_inputs else None,
        "used_evidence_count": len(used),
    }


def _market_readiness(snapshot: ValidatedSnapshot, request: RunRequest) -> str:
    if snapshot.data_mode == "fixture":
        return "FIXTURE"
    cutoff = request.decision_at.astimezone(ZoneInfo("Asia/Shanghai"))
    observed = parse_datetime(snapshot.data["as_of"], "snapshot.as_of").astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    if observed > cutoff or parse_datetime(snapshot.data["retrieved_at"],"snapshot.retrieved_at") > cutoff:
        return "FUTURE_INPUT"
    expected = expected_session(snapshot.data, cutoff)
    if expected:
        return "CURRENT_SESSION" if observed.date().isoformat() == expected else "STALE"
    if observed.date() == cutoff.date() and observed.hour >= 15:
        return "CURRENT_SESSION"
    # This age cap flags stale input; it does not invent a weekday trading calendar.
    if (cutoff - observed).total_seconds() > 96 * 3600:
        return "STALE"
    return "UNKNOWN_CALENDAR"


def _refs_available(snapshot: ValidatedSnapshot, request: RunRequest, refs: list) -> bool:
    return bool(refs) and all(
        ref in snapshot.evidence_by_id and
        parse_datetime(snapshot.evidence_by_id[ref]["available_at"],"available_at") <= request.decision_at
        for ref in refs
    )


def _timely_context(snapshot: ValidatedSnapshot, request: RunRequest) -> dict:
    context = snapshot.data["market_context"]
    if _refs_available(snapshot, request, context["evidence_refs"]):
        return deepcopy(context)
    return {"regime":"UNKNOWN","breadth":"UNKNOWN","liquidity":"UNKNOWN",
            "calculation_note":"输入背景缺少研究时点内可用的证据，文本已隐藏。","evidence_refs":[]}


def _evidence_card(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": evidence["evidence_id"],
        "category": evidence["category"],
        "source_level": evidence["source_level"],
        "title": evidence["title"],
        "source_url": evidence["source_url"],
        "published_at": evidence["published_at"],
        "effective_at": evidence["effective_at"],
        "available_at": evidence["available_at"],
        "retrieved_at": evidence["retrieved_at"],
        "as_of": evidence["as_of"],
        "summary": evidence["summary"],
        "facts": deepcopy(evidence.get("facts", [])),
        "document": deepcopy(evidence.get("document")),
    }


def _unique_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
