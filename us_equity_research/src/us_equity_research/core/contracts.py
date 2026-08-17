from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = "0.1"
MARKET = "US"
WORKFLOWS = ("daily_report", "theme_research", "stock_research")
SNAPSHOT_SELECTORS = ("demo", "latest", "id")
SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.-]{0,14}$"
ARTIFACT_ID_PATTERN = r"^(?!\.)(?!.*\.\.)[A-Za-z0-9._-]{1,128}$"
ALLOWED_ARTIFACT_SECTIONS = ("summary", "report", "manifest", "packet")
SOURCE_LEVELS = ("official", "structured_market", "industry", "secondary")
STAGES = ("discovery", "confirming", "expanding", "crowded", "diverging", "fading")
ASSESSMENTS = ("strong", "medium", "weak", "unknown")
DIMENSIONS = ("materiality", "novelty", "breadth", "durability", "timeliness")
FINANCIAL_METRICS = (
    "revenue_ttm",
    "operating_income_ttm",
    "free_cash_flow_ttm",
    "revenue_fy",
    "operating_income_fy",
    "free_cash_flow_fy",
    "cash_and_equivalents",
    "total_debt",
    "diluted_shares",
    "close_price",
)
PRIMARY_EVIDENCE_CATEGORIES = ("sec_filing", "issuer_ir", "official_macro", "regulatory")
MARKET_EVIDENCE_CATEGORIES = ("market_price", "market_breadth", "rates")
CANDIDATE_ROLES = ("leader", "platform", "beneficiary", "speculative")

_SYMBOL_RE = re.compile(SYMBOL_PATTERN)
_SAFE_ID_RE = re.compile(ARTIFACT_ID_PATTERN)


class ContractError(ValueError):
    """Raised when a versioned public contract is invalid."""


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return value


def require_string(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    strip: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    result = value.strip() if strip else value
    if not allow_empty and result == "":
        raise ContractError(f"{field} must not be empty")
    return result


def optional_string(value: Any, field: str, *, strip: bool = True) -> str | None:
    if value is None:
        return None
    return require_string(value, field, strip=strip)


def parse_datetime(value: Any, field: str) -> datetime:
    raw = require_string(value, field, strip=False)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a timezone offset")
    return parsed


def validate_url(value: Any, field: str) -> str:
    raw = require_string(value, field, strip=False)
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ContractError(f"{field} must be an https URL")
    return raw


def validate_identifier(value: Any, field: str) -> str:
    raw = require_string(value, field, strip=False)
    if raw != raw.strip():
        raise ContractError(f"{field} must not include leading or trailing whitespace")
    if not _SAFE_ID_RE.fullmatch(raw):
        raise ContractError(f"{field} contains unsupported characters")
    return raw


def validate_symbol(value: Any, field: str = "symbol") -> str:
    raw = require_string(value, field, strip=False)
    if raw != raw.strip():
        raise ContractError(f"{field} must not include leading or trailing whitespace")
    if not _SYMBOL_RE.fullmatch(raw):
        raise ContractError(f"{field} must match {SYMBOL_PATTERN}")
    return raw


def reject_unknown_fields(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{field} contains unsupported fields: {', '.join(unknown)}")


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], field: str) -> None:
    reject_unknown_fields(value, allowed, field)


def ensure_unique(values: Iterable[str], field: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        else:
            seen.add(value)
    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise ContractError(f"{field} must contain unique values; duplicates: {duplicate_list}")


def _validate_top_n(value: Any) -> int:
    if value is None:
        return 5
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise ContractError("top_n must be an integer between 1 and 20")
    return value


@dataclass(frozen=True)
class RunRequest:
    workflow: str
    decision_at: datetime
    snapshot_selector: str
    snapshot_id: str | None
    subject: str | None
    symbol: str | None
    top_n: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunRequest:
        data = require_mapping(raw, "request")
        _reject_unknown_fields(
            data,
            {
                "schema_version",
                "market",
                "workflow",
                "decision_at",
                "snapshot",
                "subject",
                "symbol",
                "top_n",
            },
            "request",
        )

        version = require_string(data.get("schema_version"), "schema_version", strip=False)
        if version != SCHEMA_VERSION:
            raise ContractError(f"unsupported schema_version: {version}")

        market = require_string(data.get("market"), "market", strip=False)
        if market != MARKET:
            raise ContractError("this engine is permanently bound to market US")

        workflow = require_string(data.get("workflow"), "workflow", strip=False)
        if workflow not in WORKFLOWS:
            raise ContractError(f"workflow must be one of {list(WORKFLOWS)}")

        decision_at = parse_datetime(data.get("decision_at"), "decision_at")

        snapshot = require_mapping(data.get("snapshot"), "snapshot")
        _reject_unknown_fields(snapshot, {"selector", "snapshot_id"}, "snapshot")
        snapshot_selector = require_string(
            snapshot.get("selector"), "snapshot.selector", strip=False
        )
        if snapshot_selector not in SNAPSHOT_SELECTORS:
            raise ContractError(f"snapshot.selector must be one of {list(SNAPSHOT_SELECTORS)}")
        snapshot_id = snapshot.get("snapshot_id")
        if snapshot_selector == "id":
            if snapshot_id is None:
                raise ContractError("snapshot.snapshot_id is required when selector=id")
            normalized_snapshot_id = validate_identifier(snapshot_id, "snapshot.snapshot_id")
        else:
            if snapshot_id is not None:
                raise ContractError("snapshot.snapshot_id is only allowed when selector=id")
            normalized_snapshot_id = None

        subject = optional_string(data.get("subject"), "subject")
        symbol = data.get("symbol")

        if workflow == "daily_report":
            if subject is not None:
                raise ContractError("subject is not allowed for daily_report")
            if symbol is not None:
                raise ContractError("symbol is not allowed for daily_report")
            normalized_symbol = None
        elif workflow == "theme_research":
            if subject is None:
                raise ContractError("subject is required for theme_research")
            if symbol is not None:
                raise ContractError("symbol is not allowed for theme_research")
            normalized_symbol = None
        else:
            if subject is not None:
                raise ContractError("subject is not allowed for stock_research")
            if symbol is None:
                raise ContractError("symbol is required for stock_research")
            normalized_symbol = validate_symbol(symbol, "symbol")

        return cls(
            workflow=workflow,
            decision_at=decision_at,
            snapshot_selector=snapshot_selector,
            snapshot_id=normalized_snapshot_id,
            subject=subject,
            symbol=normalized_symbol,
            top_n=_validate_top_n(data.get("top_n")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "market": MARKET,
            "workflow": self.workflow,
            "decision_at": self.decision_at.isoformat(),
            "snapshot": {"selector": self.snapshot_selector},
            "top_n": self.top_n,
        }
        if self.snapshot_id is not None:
            payload["snapshot"]["snapshot_id"] = self.snapshot_id
        if self.subject is not None:
            payload["subject"] = self.subject
        if self.symbol is not None:
            payload["symbol"] = self.symbol
        return payload


@dataclass(frozen=True)
class ArtifactReadRequest:
    artifact_id: str
    section: str
    max_chars: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ArtifactReadRequest:
        data = require_mapping(raw, "request")
        _reject_unknown_fields(data, {"artifact_id", "section", "max_chars"}, "request")

        artifact_id = validate_identifier(data.get("artifact_id"), "artifact_id")

        section = require_string(data.get("section", "summary"), "section", strip=False)
        if section not in ALLOWED_ARTIFACT_SECTIONS:
            raise ContractError("section must be summary, report, manifest, or packet")

        max_chars = data.get("max_chars", 12000)
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 500 <= max_chars <= 20000
        ):
            raise ContractError("max_chars must be an integer between 500 and 20000")

        return cls(artifact_id=artifact_id, section=section, max_chars=max_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "section": self.section,
            "max_chars": self.max_chars,
        }
