from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .contracts import DISCLAIMER, ContractError, parse_datetime, validate_url
from .utils import write_json_atomic

_STRENGTHS = {"strong", "medium", "weak", "unknown"}
_EXPECTATIONS = {"positive", "neutral", "negative", "unknown"}
_PRICING = {"underreacted", "confirmed", "overreacted", "contradicted", "unknown"}
_VIEWS = {"positive", "neutral", "negative", "unclear"}
_BUCKETS = {"deep_dive", "monitor", "backlog", "closed"}
_STATES = {"exclude", "continue_research", "observe"}
_STEP_STATUSES = {"supported", "partial", "unknown", "contradicted"}
_AGING_ACTIONS = {"active", "deprioritize", "close_review", "closed", "unknown"}
_DIMENSIONS = ("big", "new", "many", "durable", "timely")
_MAGNITUDE_STATUSES = {"quantified", "partial", "unknown"}
_MAGNITUDE_BASES = {"revenue", "profit", "cash_flow", "capex", "assets", "other", "unknown"}
_FORMAL_SOURCE_TYPES = {"official", "exchange", "issuer", "tender", "industry"}
_EVIDENCE_SOURCE_TYPES = _FORMAL_SOURCE_TYPES | {"structured_market", "secondary"}


def normalize_bear_review(
    text: str,
    *,
    artifact_id: str,
    decision_at: str,
    canonical_candidates: dict[str, tuple[str, str]],
    expected_integrity: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    decision_time = parse_datetime(decision_at, "decision_at")
    expected_facts = _section_integrity(expected_integrity, "facts")

    def validator(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("artifact_id") != artifact_id:
            raise ContractError("bear artifact_id does not match the canonical artifact")
        if payload.get("decision_at") != decision_at:
            raise ContractError("bear decision_at does not match the canonical run")
        if payload.get("review_mode") != "independent_bear":
            raise ContractError("bear review_mode must be independent_bear")
        integrity = _mapping(payload.get("integrity"), "bear.integrity")
        if _sha256(
            integrity.get("content_sha256"), "bear.integrity.content_sha256"
        ) != expected_facts["content_sha256"]:
            raise ContractError("bear facts content hash does not match the canonical artifact")
        if _integer(
            integrity.get("total_chars"), "bear.integrity.total_chars", minimum=0
        ) != expected_facts["total_chars"]:
            raise ContractError("bear facts total_chars does not match the canonical artifact")
        if _integer(
            integrity.get("pages_read"), "bear.integrity.pages_read", minimum=1
        ) != expected_facts["expected_pages"]:
            raise ContractError("bear pages_read does not cover the canonical facts artifact")
        candidates = _list(payload.get("candidates"), "bear.candidates")
        candidate_ids: set[str] = set()
        for index, raw_candidate in enumerate(candidates):
            candidate = _mapping(raw_candidate, f"bear.candidates[{index}]")
            candidate_id = _text(
                candidate.get("candidate_id"), f"bear.candidates[{index}].candidate_id"
            )
            if candidate_id in candidate_ids:
                raise ContractError("bear candidates contain a duplicate candidate_id")
            candidate_ids.add(candidate_id)
            symbol = _text(candidate.get("symbol"), f"bear.candidates[{index}].symbol")
            expected = canonical_candidates.get(candidate_id)
            if expected is None or expected[0] != symbol:
                raise ContractError(
                    "bear candidate_id and symbol must match the canonical candidate index"
                )
            contradictions = _evidence_refs(
                candidate.get("contradicting_evidence"),
                f"bear.candidates[{index}].contradicting_evidence",
                decision_time,
            )
            if contradictions and not any(
                item["source_type"] != "secondary" for item in contradictions
            ):
                raise ContractError(
                    f"bear.candidates[{index}].contradicting_evidence is secondary-only"
                )
            for field in ("alternative_explanations", "invalidation_tests", "unknowns"):
                _text_list(
                    candidate.get(field),
                    f"bear.candidates[{index}].{field}",
                    allow_empty=True,
                )
            _probability(
                candidate.get("bear_confidence"),
                f"bear.candidates[{index}].bear_confidence",
            )
        _text_list(payload.get("global_data_risks"), "bear.global_data_risks", allow_empty=True)
        if candidate_ids != set(canonical_candidates):
            raise ContractError(
                "bear candidate IDs must exactly match the canonical candidate index"
            )
        # Parsing the cut-off here also rejects timezone-naive values before model output is saved.
        if decision_time.tzinfo is None:  # pragma: no cover - parse_datetime already rejects this
            raise ContractError("decision_at must be timezone-aware")
        return payload

    return _extract_one_valid_object(text, validator, "bear review")


def normalize_final_judgment(
    text: str,
    *,
    artifact_id: str,
    snapshot_id: str,
    decision_at: str,
    bear_candidates: dict[str, str],
    canonical_candidates: dict[str, tuple[str, str]],
    expected_integrity: dict[str, dict[str, Any]],
    expected_research_history: dict[str, dict[str, Any]],
    expected_outcome_sample_count: int,
) -> dict[str, Any]:
    decision_time = parse_datetime(decision_at, "decision_at")
    expected_report = _section_integrity(expected_integrity, "report")
    expected_packet = _section_integrity(expected_integrity, "packet")

    def validator(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("artifact_id") != artifact_id:
            raise ContractError("final artifact_id does not match the canonical artifact")
        if payload.get("snapshot_id") != snapshot_id:
            raise ContractError("final snapshot_id does not match the frozen snapshot")
        if payload.get("decision_at") != decision_at:
            raise ContractError("final decision_at does not match the canonical run")
        _text(payload.get("executive_summary"), "final.executive_summary")
        integrity = _mapping(payload.get("integrity"), "final.integrity")
        if _sha256(
            integrity.get("report_content_sha256"),
            "final.integrity.report_content_sha256",
        ) != expected_report["content_sha256"]:
            raise ContractError("final report hash does not match the canonical artifact")
        if _sha256(
            integrity.get("packet_content_sha256"),
            "final.integrity.packet_content_sha256",
        ) != expected_packet["content_sha256"]:
            raise ContractError("final packet hash does not match the canonical artifact")
        expected_pages = expected_report["expected_pages"] + expected_packet["expected_pages"]
        if _integer(
            integrity.get("pages_read"), "final.integrity.pages_read", minimum=2
        ) != expected_pages:
            raise ContractError("final pages_read does not cover report and packet")

        rankings = _list(payload.get("candidate_rankings"), "final.candidate_rankings")
        candidate_ids: set[str] = set()
        ranks: set[int] = set()
        deep_dive_count = 0
        for index, raw_candidate in enumerate(rankings):
            candidate = _mapping(raw_candidate, f"final.candidate_rankings[{index}]")
            prefix = f"final.candidate_rankings[{index}]"
            rank = _integer(candidate.get("rank"), f"{prefix}.rank", minimum=1)
            if rank in ranks:
                raise ContractError("candidate rankings contain a duplicate rank")
            ranks.add(rank)
            candidate_id = _text(candidate.get("candidate_id"), f"{prefix}.candidate_id")
            if candidate_id in candidate_ids:
                raise ContractError("candidate rankings contain a duplicate candidate_id")
            candidate_ids.add(candidate_id)
            symbol = _text(candidate.get("symbol"), f"{prefix}.symbol")
            if candidate_id in bear_candidates and bear_candidates[candidate_id] != symbol:
                raise ContractError("final candidate_id and symbol disagree with the bear review")
            expected = canonical_candidates.get(candidate_id)
            if expected is None or expected[0] != symbol:
                raise ContractError(
                    "final candidate_id and symbol must match the canonical candidate index"
                )
            _text(candidate.get("name"), f"{prefix}.name")
            canonical_state = _enum(
                candidate.get("canonical_state"),
                _STATES,
                f"{prefix}.canonical_state",
            )
            if canonical_state != expected[1]:
                raise ContractError(
                    "final canonical_state must match the deterministic canonical run"
                )
            bucket = _enum(
                candidate.get("attention_bucket"),
                _BUCKETS,
                f"{prefix}.attention_bucket",
            )
            if (canonical_state == "exclude") != (bucket == "closed"):
                raise ContractError(
                    "attention_bucket=closed must exactly match canonical_state=exclude"
                )
            deep_dive_count += bucket == "deep_dive"
            opportunity_view = _enum(
                candidate.get("opportunity_view"),
                _VIEWS,
                f"{prefix}.opportunity_view",
            )
            history = _mapping(candidate.get("history_context"), f"{prefix}.history_context")
            expected_history = expected_research_history.get(candidate_id)
            if expected_history is None:
                raise ContractError("candidate is missing from the frozen research history")
            if _text(
                history.get("candidate_id"), f"{prefix}.history_context.candidate_id"
            ) != candidate_id:
                raise ContractError("history_context candidate_id does not match the candidate")
            if _enum(
                history.get("latest_decision"),
                _STATES,
                f"{prefix}.history_context.latest_decision",
            ) != canonical_state:
                raise ContractError(
                    "history_context latest_decision does not match canonical_state"
                )
            run_count = _integer(
                history.get("run_count"),
                f"{prefix}.history_context.run_count",
                minimum=0,
            )
            consecutive = _integer(
                history.get("consecutive_continue_research"),
                f"{prefix}.history_context.consecutive_continue_research",
                minimum=0,
            )
            stale_days = _integer(
                history.get("stale_days"),
                f"{prefix}.history_context.stale_days",
                minimum=0,
            )
            aging_action = _enum(
                history.get("aging_action"),
                _AGING_ACTIONS,
                f"{prefix}.history_context.aging_action",
            )
            expected_values = (
                expected_history["run_count"],
                expected_history["consecutive_continue_research"],
                expected_history["stale_days"],
                expected_history["aging_action"],
            )
            if (run_count, consecutive, stale_days, aging_action) != expected_values:
                raise ContractError("history_context does not match the frozen research history")

            dimensions = _mapping(candidate.get("five_dimensions"), f"{prefix}.five_dimensions")
            for dimension in _DIMENSIONS:
                _assessment(
                    dimensions.get(dimension),
                    _STRENGTHS,
                    f"{prefix}.five_dimensions.{dimension}",
                    decision_time,
                    required_source_types=_EVIDENCE_SOURCE_TYPES - {"secondary"},
                )
            new_information = _assessment(
                candidate.get("new_information"),
                _STRENGTHS,
                f"{prefix}.new_information",
                decision_time,
                required_source_types=_FORMAL_SOURCE_TYPES,
            )
            economic_impact = _economic_assessment(
                candidate.get("economic_impact"),
                f"{prefix}.economic_impact",
                decision_time,
            )
            expectation = _assessment(
                candidate.get("expectation_gap"),
                _EXPECTATIONS,
                f"{prefix}.expectation_gap",
                decision_time,
                required_source_types=_EVIDENCE_SOURCE_TYPES - {"secondary"},
            )
            _text(expectation.get("baseline"), f"{prefix}.expectation_gap.baseline")
            market_pricing = _assessment(
                candidate.get("market_pricing"),
                _PRICING,
                f"{prefix}.market_pricing",
                decision_time,
                required_source_types={"structured_market"},
            )

            chain = _list(candidate.get("impact_chain"), f"{prefix}.impact_chain")
            if len(chain) < 3:
                raise ContractError(f"{prefix}.impact_chain must contain at least three steps")
            supported_chain_steps = 0
            for step_index, raw_step in enumerate(chain):
                step = _mapping(raw_step, f"{prefix}.impact_chain[{step_index}]")
                _text(step.get("step"), f"{prefix}.impact_chain[{step_index}].step")
                status = _enum(
                    step.get("status"),
                    _STEP_STATUSES,
                    f"{prefix}.impact_chain[{step_index}].status",
                )
                step_refs = _evidence_refs(
                    step.get("evidence_refs"),
                    f"{prefix}.impact_chain[{step_index}].evidence_refs",
                    decision_time,
                )
                if status in {"supported", "partial"} and not any(
                    item["source_type"] != "secondary" for item in step_refs
                ):
                    raise ContractError(
                        f"{prefix}.impact_chain[{step_index}] lacks eligible evidence"
                    )
                if status in {"supported", "partial"} and step_refs:
                    supported_chain_steps += 1
            if bucket == "deep_dive" and not (
                canonical_state != "exclude"
                and opportunity_view == "positive"
                and aging_action == "active"
                and new_information["assessment"] in {"strong", "medium"}
                and economic_impact["assessment"] in {"strong", "medium"}
                and economic_impact["magnitude"]["status"] in {"quantified", "partial"}
                and expectation["assessment"] == "positive"
                and market_pricing["assessment"] in {"underreacted", "confirmed"}
                and supported_chain_steps >= 2
            ):
                raise ContractError(
                    f"{prefix} does not satisfy the evidence contract for deep_dive"
                )
            if (
                canonical_state != "exclude"
                and aging_action in {"close_review", "closed"}
                and bucket != "backlog"
            ):
                raise ContractError(f"{prefix} stale candidate must be backlog")
            if (
                canonical_state != "exclude"
                and opportunity_view == "negative"
                and bucket != "backlog"
            ):
                raise ContractError(f"{prefix} negative opportunity must be backlog")
            _claim(candidate.get("strongest_support"), f"{prefix}.strongest_support", decision_time)
            _claim(
                candidate.get("strongest_counterevidence"),
                f"{prefix}.strongest_counterevidence",
                decision_time,
            )
            _catalyst(candidate.get("next_catalyst"), f"{prefix}.next_catalyst", decision_time)
            _text_list(candidate.get("invalidation_conditions"), f"{prefix}.invalidation_conditions")
            _text_list(candidate.get("unknowns"), f"{prefix}.unknowns", allow_empty=True)

        if ranks and ranks != set(range(1, len(ranks) + 1)):
            raise ContractError("candidate ranks must be contiguous from 1")
        if deep_dive_count > 3:
            raise ContractError("at most three candidates may use attention_bucket=deep_dive")
        if candidate_ids != set(bear_candidates):
            raise ContractError(
                "final candidate IDs must exactly match the independent bear review"
            )
        if candidate_ids != set(canonical_candidates):
            raise ContractError(
                "final candidate IDs must exactly match the canonical candidate index"
            )

        new_opportunities = _list(payload.get("new_opportunities"), "final.new_opportunities")
        if len(new_opportunities) > 5:
            raise ContractError("final.new_opportunities must contain at most five items")
        for index, raw_opportunity in enumerate(new_opportunities):
            opportunity = _mapping(raw_opportunity, f"final.new_opportunities[{index}]")
            prefix = f"final.new_opportunities[{index}]"
            _text(opportunity.get("theme"), f"{prefix}.theme")
            _text(opportunity.get("candidate"), f"{prefix}.candidate")
            if opportunity.get("state") != "continue_research" or opportunity.get("frozen") is not False:
                raise ContractError(f"{prefix} must be continue_research and frozen=false")
            sources = _evidence_refs(
                opportunity.get("formal_sources"),
                f"{prefix}.formal_sources",
                None,
            )
            if not sources:
                raise ContractError(f"{prefix}.formal_sources must not be empty")
            if not any(item["source_type"] in _FORMAL_SOURCE_TYPES for item in sources):
                raise ContractError(f"{prefix}.formal_sources needs a formal source")
            chain = _text_list(opportunity.get("impact_chain"), f"{prefix}.impact_chain")
            if len(chain) < 3:
                raise ContractError(f"{prefix}.impact_chain must contain at least three steps")
            _text(opportunity.get("why_now"), f"{prefix}.why_now")
            _text_list(opportunity.get("unknowns"), f"{prefix}.unknowns", allow_empty=True)

        requests = _list(payload.get("collection_requests"), "final.collection_requests")
        for index, raw_request in enumerate(requests):
            request = _mapping(raw_request, f"final.collection_requests[{index}]")
            prefix = f"final.collection_requests[{index}]"
            _integer(request.get("priority"), f"{prefix}.priority", minimum=1)
            validate_url(request.get("source_url"), f"{prefix}.source_url")
            _enum(
                request.get("source_type"),
                _FORMAL_SOURCE_TYPES,
                f"{prefix}.source_type",
            )
            _text(request.get("claim_to_verify"), f"{prefix}.claim_to_verify")
            _text(request.get("resolution_condition"), f"{prefix}.resolution_condition")

        feedback = _mapping(payload.get("outcome_feedback"), "final.outcome_feedback")
        if _integer(
            feedback.get("sample_count"),
            "final.outcome_feedback.sample_count",
            minimum=0,
        ) != expected_outcome_sample_count:
            raise ContractError("outcome_feedback sample_count does not match frozen history")
        _text(feedback.get("summary"), "final.outcome_feedback.summary")
        if expected_outcome_sample_count == 0:
            feedback["summary"] = "截至 decision_at 尚无可用结算样本。"
        else:
            feedback["summary"] = (
                f"截至 decision_at 已加载 {expected_outcome_sample_count} 条不可变结果观察，"
                "仅用于校准研究状态区分度。"
            )
        _text_list(payload.get("audit_notes"), "final.audit_notes", allow_empty=True)
        return payload

    return _extract_one_valid_object(text, validator, "final judgment")


def render_opportunity_memo(
    judgment: dict[str, Any],
    drift: dict[str, Any],
    settlement: dict[str, Any] | None = None,
) -> str:
    rankings = sorted(judgment["candidate_rankings"], key=lambda item: item["rank"])
    top = [item for item in rankings if item["attention_bucket"] == "deep_dive"][:3]
    remaining = [item for item in rankings if item not in top]
    lines = [
        f"# A 股每日机会简报（{judgment['decision_at'][:10]}）",
        "",
        f"> {judgment['executive_summary']}",
        "",
        "## 今天最值得花时间的候选",
        "",
    ]
    if not top:
        lines.append("本次没有满足结构化裁判合同的冻结候选；保持 UNKNOWN。")
        lines.append("")
    for item in top:
        lines.extend(_render_candidate(item))

    lines.extend(["## 其余候选", ""])
    if remaining:
        lines.extend(
            [
                "| 排名 | 候选 | canonical 状态 | 注意力层 | 机会视图 | 新信息 | 经济影响 | 预期差 | 市场定价 | 主要反证 | 核心来源 |",
                "|---:|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for item in remaining:
            lines.append(
                f"| {item['rank']} | {_cell(item['name'])}（{_cell(item['symbol'])}） | "
                f"{item['canonical_state']} | {item['attention_bucket']} | "
                f"{item['opportunity_view']} | {item['new_information']['assessment']} | "
                f"{item['economic_impact']['assessment']} | "
                f"{item['expectation_gap']['assessment']} | "
                f"{item['market_pricing']['assessment']} | "
                f"{_cell(item['strongest_counterevidence']['claim'])} | "
                f"{_source_links(item['strongest_support']['evidence_refs'])} |"
            )
        lines.append("")
    else:
        lines.extend(["- 无。", ""])

    lines.extend(["## 新发现但尚未冻结", ""])
    if judgment["new_opportunities"]:
        for item in judgment["new_opportunities"]:
            lines.append(
                f"- **{item['theme']} / {item['candidate']}**：{item['why_now']}；"
                f"传导链：{' → '.join(item['impact_chain'])}；状态："
                "`continue_research / 尚未冻结`。"
            )
    else:
        lines.append("- 无满足正式来源与三环传导要求的新线索。")
    lines.append("")

    lines.extend(["## 下一轮采集", ""])
    for request in sorted(judgment["collection_requests"], key=lambda item: item["priority"])[
        :10
    ]:
        lines.append(
            f"- P{request['priority']} [{request['source_type']}]({request['source_url']})："
            f"{request['claim_to_verify']}；完成条件：{request['resolution_condition']}"
        )
    if not judgment["collection_requests"]:
        lines.append("- 无。")
    lines.append("")

    severity_counts = drift.get("severity_counts", {}) if isinstance(drift, dict) else {}
    settlement = settlement if isinstance(settlement, dict) else {}
    lines.extend(
        [
            "## 评测与审计附录",
            "",
            f"- artifact_id：`{judgment['artifact_id']}`；snapshot_id：`{judgment['snapshot_id']}`。",
            f"- outcome 样本：{judgment['outcome_feedback']['sample_count']}；"
            f"{judgment['outcome_feedback']['summary']}",
            f"- 本次自动结算：新增 {settlement.get('recorded_count', 'UNKNOWN')}；"
            f"待定 {settlement.get('pending_count', 'UNKNOWN')}；"
            f"超过 90 天仍未结算 {settlement.get('expired_unsettled_count', 'UNKNOWN')}；"
            f"网络访问：{settlement.get('network_accessed', False)}。",
            f"- 漂移状态：{drift.get('status', 'not_available')}；实质变化 "
            f"{drift.get('material_change_count', 'UNKNOWN')}；critical "
            f"{severity_counts.get('critical', 'UNKNOWN')} / high "
            f"{severity_counts.get('high', 'UNKNOWN')} / info "
            f"{severity_counts.get('info', 'UNKNOWN')}。",
        ]
    )
    lines.extend(f"- {note}" for note in judgment["audit_notes"])
    lines.extend(["", DISCLAIMER, ""])
    return "\n".join(lines)


def _render_candidate(item: dict[str, Any]) -> list[str]:
    dimensions = item["five_dimensions"]
    catalyst = item["next_catalyst"]
    magnitude = item["economic_impact"]["magnitude"]
    lines = [
        f"### {item['rank']}. {item['name']}（{item['symbol']}）",
        "",
        f"- 研究状态：`{item['canonical_state']}`；裁判注意力层："
        f"`{item['attention_bucket']}`；机会视图：`{item['opportunity_view']}`。",
        f"- 历史老化：连续 continue_research "
        f"{item['history_context']['consecutive_continue_research']} 次；"
        f"距上次状态/缺口变化 {item['history_context']['stale_days']} 天；动作："
        f"`{item['history_context']['aging_action']}`。",
        "- 题材五维："
        + "；".join(f"{name}={dimensions[name]['assessment']}" for name in _DIMENSIONS)
        + "。",
        f"- 新信息：{item['new_information']['assessment']} — "
        f"{item['new_information']['reason']}",
        f"- 经济影响：{item['economic_impact']['assessment']} — "
        f"{item['economic_impact']['reason']}；量级：{magnitude['status']} / "
        f"{magnitude['basis']} / ratio={magnitude['ratio'] if magnitude['ratio'] is not None else 'UNKNOWN'}。",
        f"- 预期差：{item['expectation_gap']['assessment']}（基准："
        f"{item['expectation_gap']['baseline']}）— {item['expectation_gap']['reason']}",
        f"- 市场定价：{item['market_pricing']['assessment']} — "
        f"{item['market_pricing']['reason']}",
        f"- 候选传导链：{' → '.join(step['step'] + '[' + step['status'] + ']' for step in item['impact_chain'])}",
        f"- 最强支持：{item['strongest_support']['claim']}（来源："
        f"{_source_links(item['strongest_support']['evidence_refs'])}）。",
        f"- 最强反证：{item['strongest_counterevidence']['claim']}（来源："
        f"{_source_links(item['strongest_counterevidence']['evidence_refs'])}）。",
        f"- 下一正式催化：{catalyst['description']}；"
        f"{catalyst['scheduled_at'] or 'UNKNOWN'}；核验：{catalyst['verification_rule']}；"
        f"来源：{_source_links(catalyst['evidence_refs'])}。",
        f"- 证伪条件：{'；'.join(item['invalidation_conditions'])}",
        f"- 关键 UNKNOWN：{'；'.join(item['unknowns']) if item['unknowns'] else '无'}",
        "",
    ]
    return lines


def _assessment(
    raw: Any,
    allowed: set[str],
    field: str,
    decision_at: Any,
    *,
    required_source_types: set[str] | None = None,
) -> dict[str, Any]:
    item = _mapping(raw, field)
    assessment = _enum(item.get("assessment"), allowed, f"{field}.assessment")
    _text(item.get("reason"), f"{field}.reason")
    refs = _evidence_refs(item.get("evidence_refs"), f"{field}.evidence_refs", decision_at)
    if assessment != "unknown" and not refs:
        raise ContractError(f"{field} needs evidence_refs unless assessment=unknown")
    if (
        assessment != "unknown"
        and required_source_types is not None
        and not any(ref["source_type"] in required_source_types for ref in refs)
    ):
        raise ContractError(f"{field} is not supported by an eligible source type")
    return item


def _economic_assessment(raw: Any, field: str, decision_at: Any) -> dict[str, Any]:
    item = _assessment(
        raw,
        _STRENGTHS,
        field,
        decision_at,
        required_source_types=_FORMAL_SOURCE_TYPES,
    )
    _magnitude(item.get("magnitude"), f"{field}.magnitude")
    return item


def _magnitude(raw: Any, field: str) -> dict[str, Any]:
    magnitude = _mapping(raw, field)
    status = _enum(magnitude.get("status"), _MAGNITUDE_STATUSES, f"{field}.status")
    _enum(magnitude.get("basis"), _MAGNITUDE_BASES, f"{field}.basis")
    _text(magnitude.get("formula"), f"{field}.formula")
    values = {
        name: _decimal_or_none(magnitude.get(name), f"{field}.{name}")
        for name in ("numerator", "denominator", "ratio")
    }
    present = {name for name, value in values.items() if value is not None}
    if status == "quantified":
        if present != {"numerator", "denominator", "ratio"}:
            raise ContractError(f"{field} quantified status requires all numeric fields")
        denominator = values["denominator"]
        if denominator == 0:
            raise ContractError(f"{field}.denominator must not be zero")
        expected = values["numerator"] / denominator
        tolerance = max(Decimal("0.000001"), abs(expected) * Decimal("0.000001"))
        if abs(values["ratio"] - expected) > tolerance:
            raise ContractError(f"{field}.ratio does not match numerator / denominator")
    elif status == "partial":
        if not present or present == {"numerator", "denominator", "ratio"}:
            raise ContractError(f"{field} partial status requires some numeric fields")
    elif present:
        raise ContractError(f"{field} unknown status requires null numeric fields")
    return magnitude


def _decimal_or_none(raw: Any, field: str) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ContractError(f"{field} must be a finite decimal or null")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ContractError(f"{field} must be a finite decimal or null") from exc
    if not value.is_finite():
        raise ContractError(f"{field} must be a finite decimal or null")
    return value


def _claim(raw: Any, field: str, decision_at: Any) -> None:
    item = _mapping(raw, field)
    _text(item.get("claim"), f"{field}.claim")
    refs = _evidence_refs(item.get("evidence_refs"), f"{field}.evidence_refs", decision_at)
    if refs and not any(ref["source_type"] != "secondary" for ref in refs):
        raise ContractError(f"{field} is supported only by secondary evidence")


def _catalyst(raw: Any, field: str, decision_at: Any) -> None:
    item = _mapping(raw, field)
    _text(item.get("description"), f"{field}.description")
    scheduled_at = item.get("scheduled_at")
    refs = _evidence_refs(item.get("evidence_refs"), f"{field}.evidence_refs", decision_at)
    if scheduled_at is not None:
        scheduled = parse_datetime(scheduled_at, f"{field}.scheduled_at")
        if scheduled <= decision_at:
            raise ContractError(f"{field}.scheduled_at must be later than decision_at")
        if not refs:
            raise ContractError(f"{field} needs formal evidence for a scheduled date")
        if not any(ref["source_type"] in _FORMAL_SOURCE_TYPES for ref in refs):
            raise ContractError(f"{field} scheduled date needs a formal source")
    _text(item.get("verification_rule"), f"{field}.verification_rule")


def _evidence_refs(raw: Any, field: str, decision_at: Any | None) -> list[dict[str, Any]]:
    refs = _list(raw, field)
    for index, raw_ref in enumerate(refs):
        ref = _mapping(raw_ref, f"{field}[{index}]")
        _text(ref.get("source_ref"), f"{field}[{index}].source_ref")
        validate_url(ref.get("source_url"), f"{field}[{index}].source_url")
        _enum(
            ref.get("source_type"),
            _EVIDENCE_SOURCE_TYPES,
            f"{field}[{index}].source_type",
        )
        published = parse_datetime(
            ref.get("published_at"), f"{field}[{index}].published_at"
        )
        available = parse_datetime(ref.get("available_at"), f"{field}[{index}].available_at")
        if published > available:
            raise ContractError(f"{field}[{index}] published_at is after available_at")
        if decision_at is not None and available > decision_at:
            raise ContractError(f"{field}[{index}] was not available by decision_at")
        _text(ref.get("summary"), f"{field}[{index}].summary")
    return refs


def _extract_one_valid_object(
    text: str,
    validator: Callable[[dict[str, Any]], dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, offset = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        try:
            normalized = validator(deepcopy(value))
        except (ContractError, KeyError, TypeError, ValueError):
            continue
        matches.append((start, start + offset, normalized))
    if len(matches) != 1:
        raise ContractError(f"{label} must contain exactly one valid contract object")
    return matches[0][2]


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value


def _text_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    items = _list(value, field)
    if not items and not allow_empty:
        raise ContractError(f"{field} must not be empty")
    for index, item in enumerate(items):
        _text(item, f"{field}[{index}]")
    return items


def _integer(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{field} must be an integer >= {minimum}")
    return value


def _sha256(value: Any, field: str) -> str:
    result = _text(value, field)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ContractError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _probability(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be a number between 0 and 1")
    result = float(value)
    if not 0 <= result <= 1:
        raise ContractError(f"{field} must be a number between 0 and 1")
    return result


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{field} must be one of {sorted(allowed)}")
    return value


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _source_links(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "UNKNOWN"
    return "、".join(
        f"[{_link_label(ref['source_ref'])}]({ref['source_url']})" for ref in refs
    )


def _link_label(value: Any) -> str:
    return str(value).replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _canonical_candidates(
    summary: dict[str, Any],
    *,
    artifact_id: str,
    snapshot_id: str | None = None,
) -> dict[str, tuple[str, str]]:
    if summary.get("artifact_id") != artifact_id:
        raise ContractError("canonical summary artifact_id does not match")
    if snapshot_id is not None and summary.get("snapshot_id") != snapshot_id:
        raise ContractError("canonical summary snapshot_id does not match")
    result: dict[str, tuple[str, str]] = {}
    candidates = _list(summary.get("candidate_index"), "canonical.candidate_index")
    for index, raw_candidate in enumerate(candidates):
        candidate = _mapping(raw_candidate, f"canonical.candidate_index[{index}]")
        candidate_id = _text(
            candidate.get("candidate_id"),
            f"canonical.candidate_index[{index}].candidate_id",
        )
        if candidate_id in result:
            raise ContractError("canonical candidate index contains a duplicate candidate_id")
        symbol = _text(
            candidate.get("symbol"), f"canonical.candidate_index[{index}].symbol"
        )
        state = _enum(
            candidate.get("canonical_state"),
            _STATES,
            f"canonical.candidate_index[{index}].canonical_state",
        )
        result[candidate_id] = (symbol, state)
    return result


def _section_integrity(
    expected: dict[str, dict[str, Any]], section: str
) -> dict[str, Any]:
    item = _mapping(expected.get(section), f"artifact_integrity.sections.{section}")
    return {
        "content_sha256": _sha256(
            item.get("content_sha256"),
            f"artifact_integrity.sections.{section}.content_sha256",
        ),
        "total_chars": _integer(
            item.get("total_chars"),
            f"artifact_integrity.sections.{section}.total_chars",
            minimum=0,
        ),
        "expected_pages": _integer(
            item.get("expected_pages"),
            f"artifact_integrity.sections.{section}.expected_pages",
            minimum=1,
        ),
    }


def _expected_research_history(
    document: dict[str, Any],
    canonical_candidates: dict[str, tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, raw_candidate in enumerate(
        _list(document.get("candidates"), "research_history.candidates")
    ):
        candidate = _mapping(raw_candidate, f"research_history.candidates[{index}]")
        candidate_id = _text(
            candidate.get("candidate_id"),
            f"research_history.candidates[{index}].candidate_id",
        )
        if candidate_id in result:
            raise ContractError("research history contains a duplicate candidate_id")
        expected = canonical_candidates.get(candidate_id)
        if expected is None or candidate.get("symbol") != expected[0]:
            raise ContractError("research history candidate does not match canonical index")
        latest_decision = _enum(
            candidate.get("latest_decision"),
            _STATES,
            f"research_history.candidates[{index}].latest_decision",
        )
        if latest_decision != expected[1]:
            raise ContractError("research history latest decision is not canonical")
        result[candidate_id] = {
            "run_count": _integer(
                candidate.get("run_count"),
                f"research_history.candidates[{index}].run_count",
                minimum=0,
            ),
            "consecutive_continue_research": _integer(
                candidate.get("consecutive_continue_research"),
                f"research_history.candidates[{index}].consecutive_continue_research",
                minimum=0,
            ),
            "stale_days": _integer(
                candidate.get("stale_days"),
                f"research_history.candidates[{index}].stale_days",
                minimum=0,
            ),
            "aging_action": _enum(
                candidate.get("aging_action"),
                _AGING_ACTIONS,
                f"research_history.candidates[{index}].aging_action",
            ),
        }
    if set(result) != set(canonical_candidates):
        raise ContractError("research history must exactly cover canonical candidates")
    return result


def _outcome_sample_count(document: dict[str, Any]) -> int:
    total = 0
    for index, raw_run in enumerate(_list(document.get("runs"), "outcome_history.runs")):
        run = _mapping(raw_run, f"outcome_history.runs[{index}]")
        total += _integer(
            run.get("observation_count"),
            f"outcome_history.runs[{index}].observation_count",
            minimum=0,
        )
    return total


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize and render CN Harness model outputs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bear = subparsers.add_parser("normalize-bear")
    final = subparsers.add_parser("render-final")
    for command in (bear, final):
        command.add_argument("--input", required=True, type=Path)
        command.add_argument("--artifact-id", required=True)
        command.add_argument("--decision-at", required=True)
        command.add_argument("--canonical-json", required=True, type=Path)
        command.add_argument("--integrity-json", required=True, type=Path)
    bear.add_argument("--output", required=True, type=Path)
    final.add_argument("--snapshot-id", required=True)
    final.add_argument("--bear-json", required=True, type=Path)
    final.add_argument("--drift-json", required=True, type=Path)
    final.add_argument("--settlement-json", required=True, type=Path)
    final.add_argument("--outcome-history-json", required=True, type=Path)
    final.add_argument("--research-history-json", required=True, type=Path)
    final.add_argument("--judgment-output", required=True, type=Path)
    final.add_argument("--memo-output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        raw = args.input.read_text(encoding="utf-8")
        canonical = json.loads(args.canonical_json.read_text(encoding="utf-8"))
        canonical_candidates = _canonical_candidates(
            canonical,
            artifact_id=args.artifact_id,
            snapshot_id=getattr(args, "snapshot_id", None),
        )
        integrity_document = json.loads(args.integrity_json.read_text(encoding="utf-8"))
        if integrity_document.get("artifact_id") != args.artifact_id:
            raise ContractError("artifact integrity receipt artifact_id does not match")
        expected_integrity = _mapping(
            integrity_document.get("sections"), "artifact_integrity.sections"
        )
        if args.command == "normalize-bear":
            payload = normalize_bear_review(
                raw,
                artifact_id=args.artifact_id,
                decision_at=args.decision_at,
                canonical_candidates=canonical_candidates,
                expected_integrity=expected_integrity,
            )
            write_json_atomic(args.output, payload)
            return 0

        bear = json.loads(args.bear_json.read_text(encoding="utf-8"))
        bear_candidates = {
            item["candidate_id"]: item["symbol"]
            for item in bear.get("candidates", [])
            if isinstance(item, dict)
        }
        research_history_document = json.loads(
            args.research_history_json.read_text(encoding="utf-8")
        )
        expected_research_history = _expected_research_history(
            research_history_document,
            canonical_candidates,
        )
        outcome_history_document = json.loads(
            args.outcome_history_json.read_text(encoding="utf-8")
        )
        judgment = normalize_final_judgment(
            raw,
            artifact_id=args.artifact_id,
            snapshot_id=args.snapshot_id,
            decision_at=args.decision_at,
            bear_candidates=bear_candidates,
            canonical_candidates=canonical_candidates,
            expected_integrity=expected_integrity,
            expected_research_history=expected_research_history,
            expected_outcome_sample_count=_outcome_sample_count(
                outcome_history_document
            ),
        )
        drift = json.loads(args.drift_json.read_text(encoding="utf-8"))
        settlement = json.loads(args.settlement_json.read_text(encoding="utf-8"))
        write_json_atomic(args.judgment_output, judgment)
        args.memo_output.write_text(
            render_opportunity_memo(judgment, drift, settlement), encoding="utf-8"
        )
        return 0
    except OSError:
        print("harness_review: filesystem operation failed", file=sys.stderr)
    except (ContractError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"harness_review: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
