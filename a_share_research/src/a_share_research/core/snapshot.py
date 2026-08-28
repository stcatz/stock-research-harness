from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from .contracts import (
    ASSESSMENTS,
    DIMENSIONS,
    MARKET,
    SCHEMA_VERSION,
    SOURCE_LEVELS,
    STAGES,
    ContractError,
    RunRequest,
    ensure_unique,
    parse_datetime,
    require_list,
    require_mapping,
    require_string,
    validate_url,
)
from .utils import read_json, sha256_value

PIT_QUALITIES = {"P1", "P2", "P3", "RECONSTRUCTED_NON_PIT", "FIXTURE"}
DATA_MODES = {"snapshot", "fixture"}
ROLES = {"core", "midcap", "elastic", "follower"}
FACT_STATUSES = {"observed", "unknown"}
OPPORTUNITY_STRENGTHS = {"strong", "medium", "weak", "unknown"}
EXPECTATION_ASSESSMENTS = {"positive", "neutral", "negative", "unknown"}
EXPECTATION_BASELINES = {
    "consensus",
    "management_guidance",
    "historical",
    "market_implied",
    "none",
}
MARKET_PRICING_ASSESSMENTS = {
    "underreacted",
    "confirmed",
    "overreacted",
    "contradicted",
    "unknown",
}
IMPACT_STATUSES = {"supported", "partial", "unknown", "contradicted"}
MAGNITUDE_STATUSES = {"quantified", "partial", "unknown"}
MAGNITUDE_BASES = {"revenue", "profit", "cash_flow", "capex", "assets", "other", "unknown"}


@dataclass(frozen=True)
class ValidatedSnapshot:
    data: dict[str, Any]
    evidence_by_id: dict[str, dict[str, Any]]
    themes: list[dict[str, Any]]
    snapshot_hash: str

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
        resource = files("a_share_research.fixtures").joinpath("demo_snapshot.json")
        with resource.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    elif request.snapshot_selector == "latest":
        path = _latest_snapshot(workspace)
        raw = read_json(path)
    else:
        if request.snapshot_id is None:  # guarded by RunRequest; keeps type narrowing explicit
            raise ContractError("snapshot_id is required")
        path = _snapshot_by_id(workspace, request.snapshot_id)
        raw = read_json(path)
    return validate_snapshot(raw)


def validate_snapshot(raw: Mapping[str, Any]) -> ValidatedSnapshot:
    data = dict(require_mapping(raw, "snapshot"))
    version = require_string(data.get("schema_version"), "snapshot.schema_version")
    if version != SCHEMA_VERSION:
        raise ContractError(f"unsupported snapshot schema_version: {version}")
    if data.get("market") != MARKET:
        raise ContractError("snapshot.market must be CN")

    snapshot_id = require_string(data.get("snapshot_id"), "snapshot.snapshot_id")
    if not all(character.isalnum() or character in {"-", "_", "."} for character in snapshot_id):
        raise ContractError("snapshot.snapshot_id contains unsupported characters")

    data_mode = require_string(data.get("data_mode"), "snapshot.data_mode")
    if data_mode not in DATA_MODES:
        raise ContractError(f"snapshot.data_mode must be one of {sorted(DATA_MODES)}")
    pit_quality = require_string(data.get("pit_quality"), "snapshot.pit_quality")
    if pit_quality not in PIT_QUALITIES:
        raise ContractError(f"snapshot.pit_quality must be one of {sorted(PIT_QUALITIES)}")
    if data_mode == "fixture" and pit_quality != "FIXTURE":
        raise ContractError("fixture snapshots must use pit_quality=FIXTURE")
    if data_mode == "snapshot" and pit_quality == "FIXTURE":
        raise ContractError("non-fixture snapshots must not use pit_quality=FIXTURE")

    snapshot_as_of = parse_datetime(data.get("as_of"), "snapshot.as_of")
    snapshot_retrieved = parse_datetime(data.get("retrieved_at"), "snapshot.retrieved_at")
    if snapshot_as_of > snapshot_retrieved:
        raise ContractError("snapshot.as_of must not be later than snapshot.retrieved_at")

    evidence_items = require_list(data.get("evidence"), "snapshot.evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(evidence_items):
        evidence = dict(require_mapping(item, f"snapshot.evidence[{index}]"))
        evidence_id = require_string(evidence.get("evidence_id"), f"evidence[{index}].evidence_id")
        source_level = require_string(
            evidence.get("source_level"), f"evidence[{index}].source_level"
        )
        if source_level not in SOURCE_LEVELS:
            raise ContractError(
                f"evidence[{index}].source_level must be one of {sorted(SOURCE_LEVELS)}"
            )
        require_string(evidence.get("category"), f"evidence[{index}].category")
        require_string(evidence.get("title"), f"evidence[{index}].title")
        require_string(evidence.get("summary"), f"evidence[{index}].summary")
        validate_url(evidence.get("source_url"), f"evidence[{index}].source_url")
        published_at = parse_datetime(
            evidence.get("published_at"), f"evidence[{index}].published_at"
        )
        # ``effective_at`` is not a knowledge gate. A policy formally announced today may
        # take effect next month and is still valid evidence at announcement availability.
        # Keep it independent from published/available time so reports can show that distinction.
        parse_datetime(evidence.get("effective_at"), f"evidence[{index}].effective_at")
        available_at = parse_datetime(
            evidence.get("available_at"), f"evidence[{index}].available_at"
        )
        retrieved_at = parse_datetime(
            evidence.get("retrieved_at"), f"evidence[{index}].retrieved_at"
        )
        if published_at > available_at:
            raise ContractError(
                f"evidence[{index}].published_at must not be later than available_at"
            )
        if available_at > retrieved_at:
            raise ContractError(
                f"evidence[{index}].available_at must not be later than retrieved_at"
            )
        evidence_as_of = parse_datetime(evidence.get("as_of"), f"evidence[{index}].as_of")
        if evidence_as_of > available_at:
            raise ContractError(f"evidence[{index}].as_of must not be later than available_at")
        if retrieved_at > snapshot_retrieved:
            raise ContractError(
                f"evidence[{index}].retrieved_at must not be later than snapshot.retrieved_at"
            )
        _validate_facts(evidence.get("facts", []), index, available_at, retrieved_at)
        _validate_document(evidence.get("document"), index)
        evidence_by_id[evidence_id] = evidence
    ensure_unique((item["evidence_id"] for item in evidence_items), "snapshot.evidence")
    _validate_market_context(data.get("market_context"), evidence_by_id)

    theme_items = require_list(data.get("themes"), "snapshot.themes")
    themes: list[dict[str, Any]] = []
    for index, item in enumerate(theme_items):
        theme = dict(require_mapping(item, f"snapshot.themes[{index}]"))
        _validate_theme(theme, index, evidence_by_id)
        themes.append(theme)
    ensure_unique((theme["theme_id"] for theme in themes), "snapshot.themes")

    normalized = {
        **data,
        "evidence": evidence_items,
        "themes": themes,
    }
    return ValidatedSnapshot(
        data=normalized,
        evidence_by_id=evidence_by_id,
        themes=themes,
        snapshot_hash=sha256_value(normalized),
    )


def _validate_facts(
    raw: Any,
    evidence_index: int,
    evidence_available_at: datetime,
    evidence_retrieved_at: datetime,
) -> None:
    """Validate deterministic structured facts embedded in one evidence record.

    Facts stay subordinate to their source evidence.  Values are strings so provider decimals
    are never silently rounded through a binary JSON float; missing inputs are represented by
    ``status=unknown`` and ``value=null`` rather than by a fabricated zero.
    """

    field = f"evidence[{evidence_index}].facts"
    facts = require_list(raw, field)
    fact_ids: list[str] = []
    for fact_index, raw_fact in enumerate(facts):
        prefix = f"{field}[{fact_index}]"
        fact = require_mapping(raw_fact, prefix)
        fact_ids.append(require_string(fact.get("fact_id"), f"{prefix}.fact_id"))
        require_string(fact.get("metric"), f"{prefix}.metric")
        require_string(fact.get("unit"), f"{prefix}.unit")
        require_string(fact.get("source_field"), f"{prefix}.source_field")
        status = require_string(fact.get("status"), f"{prefix}.status")
        if status not in FACT_STATUSES:
            raise ContractError(f"{prefix}.status must be one of {sorted(FACT_STATUSES)}")
        value = fact.get("value")
        if status == "observed":
            require_string(value, f"{prefix}.value")
        elif value is not None:
            raise ContractError(f"{prefix}.value must be null when status=unknown")
        fact_as_of = parse_datetime(fact.get("as_of"), f"{prefix}.as_of")
        # Future-effective facts are allowed when the future effective date itself is already
        # known. ``available_at`` below remains the anti-look-ahead boundary.
        parse_datetime(fact.get("effective_at"), f"{prefix}.effective_at")
        fact_available_at = parse_datetime(fact.get("available_at"), f"{prefix}.available_at")
        if fact_as_of > fact_available_at:
            raise ContractError(f"{prefix}.as_of must not be later than available_at")
        if fact_available_at > evidence_available_at:
            raise ContractError(
                f"{prefix}.available_at must not be later than its evidence available_at"
            )
        if fact_available_at > evidence_retrieved_at:
            raise ContractError(
                f"{prefix}.available_at must not be later than its evidence retrieved_at"
            )
    ensure_unique(fact_ids, field)


def _validate_document(raw: Any, evidence_index: int) -> None:
    if raw is None:
        return
    field = f"evidence[{evidence_index}].document"
    document = require_mapping(raw, field)
    document_id = require_string(document.get("source_document_id"), f"{field}.source_document_id")
    if len(document_id) > 256:
        raise ContractError(f"{field}.source_document_id is too long")
    media_type = require_string(document.get("media_type"), f"{field}.media_type")
    if media_type not in {"text/plain", "text/html", "application/pdf"}:
        raise ContractError(f"{field}.media_type is unsupported")
    extraction_method = require_string(
        document.get("extraction_method"), f"{field}.extraction_method"
    )
    if extraction_method not in {"manual_verified", "pdf_text_layer", "ocr", "html_text"}:
        raise ContractError(f"{field}.extraction_method is unsupported")
    text = require_string(document.get("text"), f"{field}.text")
    if len(text) > 100_000:
        raise ContractError(f"{field}.text exceeds 100000 characters")
    text_hash = require_string(document.get("text_sha256"), f"{field}.text_sha256")
    actual_hash = sha256(text.encode("utf-8")).hexdigest()
    if text_hash != actual_hash:
        raise ContractError(f"{field}.text_sha256 does not match text")


def _validate_market_context(raw: Any, evidence_by_id: Mapping[str, Any]) -> None:
    context = require_mapping(raw, "snapshot.market_context")
    require_string(context.get("regime"), "market_context.regime")
    require_string(context.get("breadth"), "market_context.breadth")
    require_string(context.get("liquidity"), "market_context.liquidity")
    require_string(context.get("calculation_note"), "market_context.calculation_note")
    refs = require_list(context.get("evidence_refs"), "market_context.evidence_refs")
    for index, ref in enumerate(refs):
        evidence_ref = require_string(ref, f"market_context.evidence_refs[{index}]")
        if evidence_ref not in evidence_by_id:
            raise ContractError(
                f"market_context.evidence_refs[{index}] references unknown evidence: {evidence_ref}"
            )


def _validate_theme(
    theme: dict[str, Any],
    index: int,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> None:
    prefix = f"snapshot.themes[{index}]"
    require_string(theme.get("theme_id"), f"{prefix}.theme_id")
    require_string(theme.get("name"), f"{prefix}.name")
    require_string(theme.get("event_type"), f"{prefix}.event_type")
    stage = require_string(theme.get("stage"), f"{prefix}.stage")
    if stage not in STAGES:
        raise ContractError(f"{prefix}.stage must be one of {sorted(STAGES)}")

    dimensions = require_mapping(theme.get("dimensions"), f"{prefix}.dimensions")
    for key in DIMENSIONS:
        dimension = require_mapping(dimensions.get(key), f"{prefix}.dimensions.{key}")
        assessment = require_string(
            dimension.get("assessment"), f"{prefix}.dimensions.{key}.assessment"
        )
        if assessment not in ASSESSMENTS:
            raise ContractError(
                f"{prefix}.dimensions.{key}.assessment must be one of {sorted(ASSESSMENTS)}"
            )
        require_string(dimension.get("reason"), f"{prefix}.dimensions.{key}.reason")
        _validate_refs(
            dimension.get("evidence_refs"),
            f"{prefix}.dimensions.{key}.evidence_refs",
            evidence_by_id,
        )

    chain = require_list(theme.get("transmission_chain"), f"{prefix}.transmission_chain")
    for chain_index, link in enumerate(chain):
        require_string(link, f"{prefix}.transmission_chain[{chain_index}]")
    parse_datetime(theme.get("next_catalyst_at"), f"{prefix}.next_catalyst_at")
    require_string(theme.get("counter_thesis"), f"{prefix}.counter_thesis")
    _validate_string_list(theme.get("invalidation_conditions"), f"{prefix}.invalidation_conditions")
    _validate_string_list(theme.get("data_gaps", []), f"{prefix}.data_gaps", allow_empty=True)
    _validate_refs(theme.get("evidence_refs"), f"{prefix}.evidence_refs", evidence_by_id)

    candidates = require_list(theme.get("candidates"), f"{prefix}.candidates")
    for candidate_index, candidate_raw in enumerate(candidates):
        candidate = require_mapping(candidate_raw, f"{prefix}.candidates[{candidate_index}]")
        candidate_prefix = f"{prefix}.candidates[{candidate_index}]"
        require_string(candidate.get("security_id"), f"{candidate_prefix}.security_id")
        require_string(candidate.get("symbol"), f"{candidate_prefix}.symbol")
        require_string(candidate.get("name"), f"{candidate_prefix}.name")
        role = require_string(candidate.get("role"), f"{candidate_prefix}.role")
        if role not in ROLES:
            raise ContractError(f"{candidate_prefix}.role must be one of {sorted(ROLES)}")
        require_string(candidate.get("thesis"), f"{candidate_prefix}.thesis")
        require_string(candidate.get("counter_thesis"), f"{candidate_prefix}.counter_thesis")
        _validate_refs(
            candidate.get("evidence_refs"), f"{candidate_prefix}.evidence_refs", evidence_by_id
        )
        _validate_refs(
            candidate.get("market_evidence_refs"),
            f"{candidate_prefix}.market_evidence_refs",
            evidence_by_id,
        )
        _validate_string_list(
            candidate.get("invalidation_conditions"),
            f"{candidate_prefix}.invalidation_conditions",
        )
        _validate_string_list(
            candidate.get("data_gaps", []), f"{candidate_prefix}.data_gaps", allow_empty=True
        )
        _validate_string_list(
            candidate.get("risk_flags", []), f"{candidate_prefix}.risk_flags", allow_empty=True
        )
        _validate_string_list(
            candidate.get("manual_review_items"),
            f"{candidate_prefix}.manual_review_items",
        )
        _validate_opportunity_profile(
            candidate.get("opportunity_profile"),
            f"{candidate_prefix}.opportunity_profile",
            evidence_by_id,
        )
        _validate_impact_chain(
            candidate.get("impact_chain"),
            f"{candidate_prefix}.impact_chain",
            evidence_by_id,
        )
    ensure_unique(
        (candidate["security_id"] for candidate in candidates),
        f"{prefix}.candidates",
    )


def _validate_refs(raw: Any, field: str, evidence_by_id: Mapping[str, Any]) -> None:
    refs = require_list(raw, field)
    for index, ref_raw in enumerate(refs):
        ref = require_string(ref_raw, f"{field}[{index}]")
        if ref not in evidence_by_id:
            raise ContractError(f"{field}[{index}] references unknown evidence: {ref}")


def _validate_string_list(raw: Any, field: str, *, allow_empty: bool = False) -> None:
    items = require_list(raw, field)
    if not items and not allow_empty:
        raise ContractError(f"{field} must not be empty")
    for index, item in enumerate(items):
        require_string(item, f"{field}[{index}]")


def _validate_opportunity_profile(
    raw: Any,
    field: str,
    evidence_by_id: Mapping[str, Any],
) -> None:
    # V2 fields are optional so immutable V1 snapshots remain replayable.  Once present, the
    # complete factor contract is required; partial prose must not masquerade as a scored profile.
    if raw is None:
        return
    profile = require_mapping(raw, field)
    required = {
        "new_information",
        "economic_impact",
        "expectation_gap",
        "market_pricing",
        "next_catalyst",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise ContractError(f"{field} is missing required fields: " + ", ".join(missing))

    for name in ("new_information", "economic_impact"):
        factor = require_mapping(profile.get(name), f"{field}.{name}")
        assessment = require_string(factor.get("assessment"), f"{field}.{name}.assessment")
        if assessment not in OPPORTUNITY_STRENGTHS:
            raise ContractError(
                f"{field}.{name}.assessment must be one of {sorted(OPPORTUNITY_STRENGTHS)}"
            )
        require_string(factor.get("reason"), f"{field}.{name}.reason")
        _validate_refs(factor.get("evidence_refs"), f"{field}.{name}.evidence_refs", evidence_by_id)
        if name == "economic_impact":
            _validate_magnitude(factor.get("magnitude"), f"{field}.{name}.magnitude")

    expectation = require_mapping(profile.get("expectation_gap"), f"{field}.expectation_gap")
    expectation_assessment = require_string(
        expectation.get("assessment"), f"{field}.expectation_gap.assessment"
    )
    if expectation_assessment not in EXPECTATION_ASSESSMENTS:
        raise ContractError(
            f"{field}.expectation_gap.assessment must be one of "
            f"{sorted(EXPECTATION_ASSESSMENTS)}"
        )
    baseline = require_string(expectation.get("baseline"), f"{field}.expectation_gap.baseline")
    if baseline not in EXPECTATION_BASELINES:
        raise ContractError(
            f"{field}.expectation_gap.baseline must be one of {sorted(EXPECTATION_BASELINES)}"
        )
    require_string(expectation.get("reason"), f"{field}.expectation_gap.reason")
    _validate_refs(
        expectation.get("evidence_refs"),
        f"{field}.expectation_gap.evidence_refs",
        evidence_by_id,
    )

    pricing = require_mapping(profile.get("market_pricing"), f"{field}.market_pricing")
    pricing_assessment = require_string(
        pricing.get("assessment"), f"{field}.market_pricing.assessment"
    )
    if pricing_assessment not in MARKET_PRICING_ASSESSMENTS:
        raise ContractError(
            f"{field}.market_pricing.assessment must be one of "
            f"{sorted(MARKET_PRICING_ASSESSMENTS)}"
        )
    require_string(pricing.get("reason"), f"{field}.market_pricing.reason")
    _validate_refs(
        pricing.get("evidence_refs"),
        f"{field}.market_pricing.evidence_refs",
        evidence_by_id,
    )

    catalyst = require_mapping(profile.get("next_catalyst"), f"{field}.next_catalyst")
    require_string(catalyst.get("description"), f"{field}.next_catalyst.description")
    scheduled_at = catalyst.get("scheduled_at")
    if scheduled_at is not None:
        parse_datetime(scheduled_at, f"{field}.next_catalyst.scheduled_at")
    require_string(
        catalyst.get("verification_rule"), f"{field}.next_catalyst.verification_rule"
    )
    _validate_refs(
        catalyst.get("evidence_refs"),
        f"{field}.next_catalyst.evidence_refs",
        evidence_by_id,
    )


def _validate_magnitude(raw: Any, field: str) -> None:
    # Missing magnitude remains valid for immutable V1/V2 snapshots but earns no economic-impact
    # points in the current method. New schema-conforming snapshots make the object explicit.
    if raw is None:
        return
    magnitude = require_mapping(raw, field)
    status = require_string(magnitude.get("status"), f"{field}.status")
    if status not in MAGNITUDE_STATUSES:
        raise ContractError(f"{field}.status must be one of {sorted(MAGNITUDE_STATUSES)}")
    basis = require_string(magnitude.get("basis"), f"{field}.basis")
    if basis not in MAGNITUDE_BASES:
        raise ContractError(f"{field}.basis must be one of {sorted(MAGNITUDE_BASES)}")
    require_string(magnitude.get("formula"), f"{field}.formula")
    values = {
        name: _optional_decimal(magnitude.get(name), f"{field}.{name}")
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
            raise ContractError(f"{field} partial status requires some, but not all, numeric fields")
    elif present:
        raise ContractError(f"{field} unknown status requires null numeric fields")


def _optional_decimal(raw: Any, field: str) -> Decimal | None:
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


def _validate_impact_chain(
    raw: Any,
    field: str,
    evidence_by_id: Mapping[str, Any],
) -> None:
    if raw is None:
        return
    chain = require_list(raw, field)
    if len(chain) < 3:
        raise ContractError(f"{field} must contain at least three candidate-level steps")
    for index, raw_step in enumerate(chain):
        step = require_mapping(raw_step, f"{field}[{index}]")
        require_string(step.get("step"), f"{field}[{index}].step")
        status = require_string(step.get("status"), f"{field}[{index}].status")
        if status not in IMPACT_STATUSES:
            raise ContractError(
                f"{field}[{index}].status must be one of {sorted(IMPACT_STATUSES)}"
            )
        _validate_refs(
            step.get("evidence_refs"),
            f"{field}[{index}].evidence_refs",
            evidence_by_id,
        )


def _latest_snapshot(workspace: Path) -> Path:
    normalized = workspace / "data" / "normalized"
    candidates = list(normalized.glob("*/snapshot.json"))
    if not candidates:
        raise ContractError(
            f"no normalized snapshot found below {normalized}; use snapshot.selector=demo explicitly"
        )
    dated_candidates: list[tuple[datetime, str, Path]] = []
    for path in candidates:
        raw = require_mapping(read_json(path), f"snapshot file {path}")
        retrieved_at = parse_datetime(raw.get("retrieved_at"), f"snapshot file {path}.retrieved_at")
        snapshot_id = require_string(raw.get("snapshot_id"), f"snapshot file {path}.snapshot_id")
        dated_candidates.append((retrieved_at, snapshot_id, path))
    return max(dated_candidates, key=lambda item: (item[0], item[1]))[2]


def _snapshot_by_id(workspace: Path, snapshot_id: str) -> Path:
    candidates = (
        workspace / "data" / "normalized" / snapshot_id / "snapshot.json",
        workspace / "data" / "normalized" / f"{snapshot_id}.json",
    )
    for path in candidates:
        if path.is_file():
            return path
    raise ContractError(f"snapshot not found: {snapshot_id}")
