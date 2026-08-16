from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from .contracts import (
    ASSESSMENTS,
    CANDIDATE_ROLES,
    DIMENSIONS,
    FINANCIAL_METRICS,
    MARKET,
    MARKET_EVIDENCE_CATEGORIES,
    PRIMARY_EVIDENCE_CATEGORIES,
    SCHEMA_VERSION,
    SOURCE_LEVELS,
    STAGES,
    ContractError,
    RunRequest,
    ensure_unique,
    parse_datetime,
    reject_unknown_fields,
    require_list,
    require_mapping,
    require_string,
    validate_identifier,
    validate_symbol,
    validate_url,
)

PIT_QUALITIES = ("P1", "P2", "P3", "RECONSTRUCTED_NON_PIT", "FIXTURE")
DATA_MODES = ("fixture", "snapshot")


@dataclass(frozen=True)
class ValidatedSnapshot:
    data: dict[str, Any]
    evidence_by_id: dict[str, dict[str, Any]]
    financial_facts_by_id: dict[str, dict[str, Any]]
    themes: list[dict[str, Any]]
    snapshot_hash: str
    retrieved_at: datetime

    @property
    def snapshot_id(self) -> str:
        return self.data["snapshot_id"]

    @property
    def data_mode(self) -> str:
        return self.data["data_mode"]

    @property
    def pit_quality(self) -> str:
        return self.data["pit_quality"]


def load_snapshot(workspace: Path, request: RunRequest) -> ValidatedSnapshot:
    if request.snapshot_selector == "demo":
        resource = files("us_equity_research.fixtures").joinpath("demo_snapshot.json")
        with resource.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return validate_snapshot(raw)

    if request.snapshot_selector == "latest":
        return _latest_snapshot(workspace)

    if request.snapshot_id is None:  # guarded by RunRequest; keeps type narrowing explicit
        raise ContractError("snapshot_id is required")
    validate_identifier(request.snapshot_id, "snapshot.snapshot_id")
    return validate_snapshot(_read_json(_snapshot_by_id(workspace, request.snapshot_id)))


def validate_snapshot(raw: Mapping[str, Any]) -> ValidatedSnapshot:
    data = dict(require_mapping(raw, "snapshot"))
    reject_unknown_fields(
        data,
        {
            "schema_version",
            "market",
            "snapshot_id",
            "data_mode",
            "pit_quality",
            "as_of",
            "retrieved_at",
            "fixture_notice",
            "evidence",
            "financial_facts",
            "market_context",
            "themes",
        },
        "snapshot",
    )
    version = require_string(data.get("schema_version"), "snapshot.schema_version", strip=False)
    if version != SCHEMA_VERSION:
        raise ContractError(f"unsupported snapshot schema_version: {version}")
    if data.get("market") != MARKET:
        raise ContractError("snapshot.market must be US")

    snapshot_id = validate_identifier(data.get("snapshot_id"), "snapshot.snapshot_id")

    data_mode = require_string(data.get("data_mode"), "snapshot.data_mode", strip=False)
    if data_mode not in DATA_MODES:
        raise ContractError(f"snapshot.data_mode must be one of {list(DATA_MODES)}")
    pit_quality = require_string(data.get("pit_quality"), "snapshot.pit_quality", strip=False)
    if pit_quality not in PIT_QUALITIES:
        raise ContractError(f"snapshot.pit_quality must be one of {list(PIT_QUALITIES)}")
    if data_mode == "fixture" and pit_quality != "FIXTURE":
        raise ContractError("fixture snapshots must use pit_quality=FIXTURE")
    if data_mode == "snapshot" and pit_quality == "FIXTURE":
        raise ContractError("non-fixture snapshots must not use pit_quality=FIXTURE")

    snapshot_as_of = parse_datetime(data.get("as_of"), "snapshot.as_of")
    snapshot_retrieved = parse_datetime(data.get("retrieved_at"), "snapshot.retrieved_at")
    if snapshot_as_of > snapshot_retrieved:
        raise ContractError("snapshot.as_of must not be later than snapshot.retrieved_at")

    fixture_notice = data.get("fixture_notice")
    if data_mode == "fixture" or fixture_notice is not None:
        require_string(fixture_notice, "snapshot.fixture_notice")

    evidence_items = require_list(data.get("evidence"), "snapshot.evidence")
    evidence_by_id = _validate_evidence(evidence_items, snapshot_retrieved)
    financial_fact_items = require_list(data.get("financial_facts"), "snapshot.financial_facts")
    financial_facts_by_id = _validate_financial_facts(
        financial_fact_items,
        snapshot_retrieved,
        evidence_by_id,
    )
    _validate_market_context(data.get("market_context"), evidence_by_id)

    theme_items = require_list(data.get("themes"), "snapshot.themes")
    themes: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    for index, raw_theme in enumerate(theme_items):
        theme = dict(require_mapping(raw_theme, f"snapshot.themes[{index}]"))
        candidate_ids.extend(_validate_theme(theme, index, evidence_by_id, financial_facts_by_id))
        themes.append(theme)
    ensure_unique((theme["theme_id"] for theme in themes), "snapshot.themes")
    ensure_unique(candidate_ids, "snapshot.themes candidates")

    normalized = {
        **data,
        "snapshot_id": snapshot_id,
        "evidence": evidence_items,
        "financial_facts": financial_fact_items,
        "themes": themes,
    }
    return ValidatedSnapshot(
        data=normalized,
        evidence_by_id=evidence_by_id,
        financial_facts_by_id=financial_facts_by_id,
        themes=themes,
        snapshot_hash=_sha256_value(normalized),
        retrieved_at=snapshot_retrieved,
    )


def _validate_evidence(
    items: list[Any],
    snapshot_retrieved: datetime,
) -> dict[str, dict[str, Any]]:
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(items):
        evidence = dict(require_mapping(raw_item, f"snapshot.evidence[{index}]"))
        reject_unknown_fields(
            evidence,
            {
                "evidence_id",
                "source_level",
                "category",
                "title",
                "summary",
                "source_url",
                "source_document_id",
                "published_at",
                "effective_at",
                "available_at",
                "retrieved_at",
                "as_of",
            },
            f"snapshot.evidence[{index}]",
        )
        evidence_id = validate_identifier(
            evidence.get("evidence_id"), f"evidence[{index}].evidence_id"
        )
        source_level = require_string(
            evidence.get("source_level"),
            f"evidence[{index}].source_level",
            strip=False,
        )
        if source_level not in SOURCE_LEVELS:
            raise ContractError(
                f"evidence[{index}].source_level must be one of {list(SOURCE_LEVELS)}"
            )
        category = require_string(evidence.get("category"), f"evidence[{index}].category")
        if source_level == "official" and category not in PRIMARY_EVIDENCE_CATEGORIES:
            raise ContractError(
                f"evidence[{index}].category must be one of {list(PRIMARY_EVIDENCE_CATEGORIES)}"
            )
        if source_level == "structured_market" and category not in MARKET_EVIDENCE_CATEGORIES:
            raise ContractError(
                f"evidence[{index}].category must be one of {list(MARKET_EVIDENCE_CATEGORIES)}"
            )
        require_string(evidence.get("title"), f"evidence[{index}].title")
        require_string(evidence.get("summary"), f"evidence[{index}].summary")
        validate_url(evidence.get("source_url"), f"evidence[{index}].source_url")
        validate_identifier(
            evidence.get("source_document_id"),
            f"evidence[{index}].source_document_id",
        )
        published_at = parse_datetime(
            evidence.get("published_at"),
            f"evidence[{index}].published_at",
        )
        effective_at = parse_datetime(
            evidence.get("effective_at"),
            f"evidence[{index}].effective_at",
        )
        available_at = parse_datetime(
            evidence.get("available_at"),
            f"evidence[{index}].available_at",
        )
        retrieved_at = parse_datetime(
            evidence.get("retrieved_at"),
            f"evidence[{index}].retrieved_at",
        )
        evidence_as_of = parse_datetime(evidence.get("as_of"), f"evidence[{index}].as_of")
        if published_at > available_at:
            raise ContractError(
                f"evidence[{index}].published_at must not be later than available_at"
            )
        if available_at > retrieved_at:
            raise ContractError(
                f"evidence[{index}].available_at must not be later than retrieved_at"
            )
        if retrieved_at > snapshot_retrieved:
            raise ContractError(
                f"evidence[{index}].retrieved_at must not be later than snapshot.retrieved_at"
            )
        if evidence_as_of > available_at:
            raise ContractError(f"evidence[{index}].as_of must not be later than available_at")
        evidence_by_id[evidence_id] = evidence
        _ = effective_at  # validated for timezone-aware ISO format; future timestamps are allowed
    ensure_unique((item["evidence_id"] for item in items), "snapshot.evidence")
    return evidence_by_id


def _validate_financial_facts(
    items: list[Any],
    snapshot_retrieved: datetime,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    facts_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(items):
        fact = dict(require_mapping(raw_item, f"snapshot.financial_facts[{index}]"))
        reject_unknown_fields(
            fact,
            {
                "fact_id",
                "security_id",
                "metric",
                "value",
                "unit",
                "period_end",
                "available_at",
                "evidence_ref",
            },
            f"snapshot.financial_facts[{index}]",
        )
        fact_id = validate_identifier(fact.get("fact_id"), f"financial_facts[{index}].fact_id")
        validate_identifier(
            fact.get("security_id"),
            f"financial_facts[{index}].security_id",
        )
        metric = require_string(fact.get("metric"), f"financial_facts[{index}].metric", strip=False)
        if metric not in FINANCIAL_METRICS:
            raise ContractError(
                f"financial_facts[{index}].metric must be one of {list(FINANCIAL_METRICS)}"
            )
        value = fact.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ContractError(f"financial_facts[{index}].value must be a finite number")
        require_string(fact.get("unit"), f"financial_facts[{index}].unit")
        _parse_date(fact.get("period_end"), f"financial_facts[{index}].period_end")
        available_at = parse_datetime(
            fact.get("available_at"),
            f"financial_facts[{index}].available_at",
        )
        if available_at > snapshot_retrieved:
            raise ContractError(
                f"financial_facts[{index}].available_at must not be later than snapshot.retrieved_at"
            )
        evidence_ref = require_string(
            fact.get("evidence_ref"),
            f"financial_facts[{index}].evidence_ref",
            strip=False,
        )
        if evidence_ref not in evidence_by_id:
            raise ContractError(
                f"financial_facts[{index}].evidence_ref references unknown evidence: {evidence_ref}"
            )
        source_level = evidence_by_id[evidence_ref]["source_level"]
        if metric == "close_price":
            if source_level != "structured_market":
                raise ContractError("close_price must reference structured_market evidence")
        elif source_level != "official":
            raise ContractError(
                f"financial_facts[{index}].evidence_ref must reference official evidence"
            )
        facts_by_id[fact_id] = fact
    ensure_unique((item["fact_id"] for item in items), "snapshot.financial_facts")
    return facts_by_id


def _validate_market_context(raw: Any, evidence_by_id: Mapping[str, dict[str, Any]]) -> None:
    context = require_mapping(raw, "snapshot.market_context")
    reject_unknown_fields(
        context,
        {
            "regime",
            "breadth",
            "rates",
            "liquidity",
            "calculation_note",
            "evidence_refs",
        },
        "snapshot.market_context",
    )
    require_string(context.get("regime"), "market_context.regime")
    require_string(context.get("breadth"), "market_context.breadth")
    require_string(context.get("rates"), "market_context.rates")
    require_string(context.get("liquidity"), "market_context.liquidity")
    require_string(context.get("calculation_note"), "market_context.calculation_note")
    _validate_refs(context.get("evidence_refs"), "market_context.evidence_refs", evidence_by_id)


def _validate_theme(
    theme: dict[str, Any],
    index: int,
    evidence_by_id: Mapping[str, dict[str, Any]],
    financial_facts_by_id: Mapping[str, dict[str, Any]],
) -> list[str]:
    prefix = f"snapshot.themes[{index}]"
    reject_unknown_fields(
        theme,
        {
            "theme_id",
            "name",
            "event_type",
            "stage",
            "dimensions",
            "transmission_chain",
            "next_catalyst_at",
            "evidence_refs",
            "data_gaps",
            "candidates",
        },
        prefix,
    )
    validate_identifier(theme.get("theme_id"), f"{prefix}.theme_id")
    require_string(theme.get("name"), f"{prefix}.name")
    require_string(theme.get("event_type"), f"{prefix}.event_type")
    stage = require_string(theme.get("stage"), f"{prefix}.stage", strip=False)
    if stage not in STAGES:
        raise ContractError(f"{prefix}.stage must be one of {list(STAGES)}")

    dimensions = require_mapping(theme.get("dimensions"), f"{prefix}.dimensions")
    reject_unknown_fields(dimensions, set(DIMENSIONS), f"{prefix}.dimensions")
    for key in DIMENSIONS:
        dimension = require_mapping(dimensions.get(key), f"{prefix}.dimensions.{key}")
        reject_unknown_fields(
            dimension,
            {"assessment", "reason", "evidence_refs"},
            f"{prefix}.dimensions.{key}",
        )
        assessment = require_string(
            dimension.get("assessment"),
            f"{prefix}.dimensions.{key}.assessment",
            strip=False,
        )
        if assessment not in ASSESSMENTS:
            raise ContractError(
                f"{prefix}.dimensions.{key}.assessment must be one of {list(ASSESSMENTS)}"
            )
        require_string(dimension.get("reason"), f"{prefix}.dimensions.{key}.reason")
        _validate_refs(
            dimension.get("evidence_refs"),
            f"{prefix}.dimensions.{key}.evidence_refs",
            evidence_by_id,
        )

    transmission_chain = require_list(
        theme.get("transmission_chain"), f"{prefix}.transmission_chain"
    )
    if not transmission_chain:
        raise ContractError(f"{prefix}.transmission_chain must not be empty")
    for chain_index, raw_link in enumerate(transmission_chain):
        require_string(raw_link, f"{prefix}.transmission_chain[{chain_index}]")
    parse_datetime(theme.get("next_catalyst_at"), f"{prefix}.next_catalyst_at")
    _validate_refs(theme.get("evidence_refs"), f"{prefix}.evidence_refs", evidence_by_id)
    _validate_string_list(theme.get("data_gaps"), f"{prefix}.data_gaps", allow_empty=True)

    candidates = require_list(theme.get("candidates"), f"{prefix}.candidates")
    if not candidates:
        raise ContractError(f"{prefix}.candidates must not be empty")
    candidate_ids: list[str] = []
    for candidate_index, raw_candidate in enumerate(candidates):
        candidate = require_mapping(raw_candidate, f"{prefix}.candidates[{candidate_index}]")
        candidate_prefix = f"{prefix}.candidates[{candidate_index}]"
        reject_unknown_fields(
            candidate,
            {
                "security_id",
                "symbol",
                "name",
                "role",
                "thesis",
                "bull_case",
                "bear_case",
                "risk_verdict",
                "evidence_refs",
                "market_evidence_refs",
                "financial_fact_refs",
                "invalidation_conditions",
                "data_gaps",
                "risk_flags",
                "manual_review_items",
            },
            candidate_prefix,
        )
        security_id = validate_identifier(
            candidate.get("security_id"),
            f"{candidate_prefix}.security_id",
        )
        candidate_ids.append(security_id)
        validate_symbol(candidate.get("symbol"), f"{candidate_prefix}.symbol")
        require_string(candidate.get("name"), f"{candidate_prefix}.name")
        role = require_string(candidate.get("role"), f"{candidate_prefix}.role", strip=False)
        if role not in CANDIDATE_ROLES:
            raise ContractError(f"{candidate_prefix}.role must be one of {list(CANDIDATE_ROLES)}")
        require_string(candidate.get("thesis"), f"{candidate_prefix}.thesis")
        _validate_case(candidate.get("bull_case"), f"{candidate_prefix}.bull_case", evidence_by_id)
        _validate_case(candidate.get("bear_case"), f"{candidate_prefix}.bear_case", evidence_by_id)
        _validate_case(
            candidate.get("risk_verdict"),
            f"{candidate_prefix}.risk_verdict",
            evidence_by_id,
        )
        _validate_refs(
            candidate.get("evidence_refs"),
            f"{candidate_prefix}.evidence_refs",
            evidence_by_id,
        )
        _validate_market_refs(
            candidate.get("market_evidence_refs"),
            f"{candidate_prefix}.market_evidence_refs",
            evidence_by_id,
        )
        _validate_fact_refs(
            candidate.get("financial_fact_refs"),
            f"{candidate_prefix}.financial_fact_refs",
            financial_facts_by_id,
        )
        _validate_string_list(
            candidate.get("invalidation_conditions"),
            f"{candidate_prefix}.invalidation_conditions",
        )
        _validate_string_list(
            candidate.get("data_gaps"),
            f"{candidate_prefix}.data_gaps",
            allow_empty=True,
        )
        _validate_string_list(
            candidate.get("risk_flags"),
            f"{candidate_prefix}.risk_flags",
            allow_empty=True,
        )
        _validate_string_list(
            candidate.get("manual_review_items"),
            f"{candidate_prefix}.manual_review_items",
            allow_empty=True,
        )
    return candidate_ids


def _validate_case(
    raw: Any,
    field: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> None:
    case = require_mapping(raw, field)
    reject_unknown_fields(case, {"text", "evidence_refs"}, field)
    require_string(case.get("text"), f"{field}.text")
    _validate_refs(case.get("evidence_refs"), f"{field}.evidence_refs", evidence_by_id)


def _validate_refs(
    raw: Any,
    field: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> None:
    refs = require_list(raw, field)
    for index, ref_raw in enumerate(refs):
        ref = validate_identifier(ref_raw, f"{field}[{index}]")
        if ref not in evidence_by_id:
            raise ContractError(f"{field}[{index}] references unknown evidence: {ref}")


def _validate_market_refs(
    raw: Any,
    field: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> None:
    refs = require_list(raw, field)
    for index, ref_raw in enumerate(refs):
        ref = validate_identifier(ref_raw, f"{field}[{index}]")
        evidence = evidence_by_id.get(ref)
        if evidence is None:
            raise ContractError(f"{field}[{index}] references unknown evidence: {ref}")
        if evidence["source_level"] != "structured_market":
            raise ContractError(f"{field}[{index}] must reference structured_market evidence")


def _validate_fact_refs(raw: Any, field: str, facts_by_id: Mapping[str, dict[str, Any]]) -> None:
    refs = require_list(raw, field)
    for index, ref_raw in enumerate(refs):
        ref = validate_identifier(ref_raw, f"{field}[{index}]")
        if ref not in facts_by_id:
            raise ContractError(f"{field}[{index}] references unknown financial fact: {ref}")


def _validate_string_list(raw: Any, field: str, *, allow_empty: bool = False) -> None:
    items = require_list(raw, field)
    if not items and not allow_empty:
        raise ContractError(f"{field} must not be empty")
    for index, item in enumerate(items):
        require_string(item, f"{field}[{index}]")


def _latest_snapshot(workspace: Path) -> ValidatedSnapshot:
    normalized = workspace / "data" / "normalized" / "us"
    candidates = list(normalized.glob("*/snapshot.json"))
    if not candidates:
        raise ContractError(
            f"no normalized snapshot found below {normalized}; use snapshot.selector=demo explicitly"
        )
    validated_candidates: list[ValidatedSnapshot] = []
    for path in candidates:
        validated_candidates.append(validate_snapshot(_read_json(path)))
    return max(validated_candidates, key=lambda item: (item.retrieved_at, item.snapshot_id))


def _snapshot_by_id(workspace: Path, snapshot_id: str) -> Path:
    path = workspace / "data" / "normalized" / "us" / snapshot_id / "snapshot.json"
    if path.is_file():
        return path
    raise ContractError(f"snapshot not found: {snapshot_id}")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return require_mapping(raw, f"snapshot file {path}")


def _parse_date(value: Any, field: str) -> date:
    raw = require_string(value, field, strip=False)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO-8601 date") from exc


def _sha256_value(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
