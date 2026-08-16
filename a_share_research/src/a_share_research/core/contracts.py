from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = "0.1"
MARKET = "CN"
WORKFLOWS = {"daily_report", "stock_research", "theme_research"}
SNAPSHOT_SELECTORS = {"demo", "latest", "id"}
DIMENSIONS = ("big", "new", "many", "durable", "timely")
DIMENSION_LABELS = {
    "big": "大",
    "new": "新",
    "many": "多",
    "durable": "久",
    "timely": "准",
}
ASSESSMENTS = {"strong", "medium", "weak", "unknown"}
STAGES = {"organizing", "warming", "expanding", "climax", "diverging", "declining"}
DECISIONS = {"exclude", "continue_research", "observe"}
DECISION_LABELS = {
    "exclude": "排除",
    "continue_research": "继续研究",
    "observe": "观察",
}
SOURCE_LEVELS = {"official", "structured_market", "industry", "secondary"}
DISCLAIMER = "本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。"


class ContractError(ValueError):
    """Raised when a versioned input contract is invalid."""


def require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return value


def require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise ContractError(f"{field} must not be empty")
    return result


def optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return require_string(value, field)


def parse_datetime(value: Any, field: str) -> datetime:
    raw = require_string(value, field)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a timezone offset")
    return parsed


def validate_url(value: Any, field: str) -> str:
    raw = require_string(value, field)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContractError(f"{field} must be an http(s) URL")
    return raw


def ensure_unique(values: Iterable[str], field: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        joined = ", ".join(sorted(duplicates))
        raise ContractError(f"{field} contains duplicate ids: {joined}")


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
    def from_dict(cls, raw: Mapping[str, Any]) -> RunRequest:
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
        version = require_string(data.get("schema_version"), "schema_version")
        if version != SCHEMA_VERSION:
            raise ContractError(f"unsupported schema_version: {version}")

        workflow = require_string(data.get("workflow"), "workflow")
        if workflow not in WORKFLOWS:
            raise ContractError(f"workflow must be one of {sorted(WORKFLOWS)}")

        market = data.get("market")
        if market is not None and market != MARKET:
            raise ContractError("this engine is permanently bound to market CN")

        decision_at = parse_datetime(data.get("decision_at"), "decision_at")
        snapshot = require_mapping(data.get("snapshot"), "snapshot")
        _reject_unknown_fields(snapshot, {"selector", "snapshot_id"}, "snapshot")
        selector = require_string(snapshot.get("selector"), "snapshot.selector")
        if selector not in SNAPSHOT_SELECTORS:
            raise ContractError(f"snapshot.selector must be one of {sorted(SNAPSHOT_SELECTORS)}")
        snapshot_id = optional_string(snapshot.get("snapshot_id"), "snapshot.snapshot_id")
        if selector == "id" and snapshot_id is None:
            raise ContractError("snapshot.snapshot_id is required when selector=id")
        if selector != "id" and snapshot_id is not None:
            raise ContractError("snapshot.snapshot_id is only allowed when selector=id")
        if snapshot_id is not None and not _is_safe_identifier(snapshot_id):
            raise ContractError("snapshot.snapshot_id contains unsupported characters")

        subject = optional_string(data.get("subject"), "subject")
        symbol = optional_string(data.get("symbol"), "symbol")
        if workflow == "theme_research" and subject is None:
            raise ContractError("subject is required for theme_research")
        if workflow == "stock_research" and symbol is None:
            raise ContractError("symbol is required for stock_research")
        if workflow != "theme_research" and subject is not None:
            raise ContractError("subject is only allowed for theme_research")
        if workflow != "stock_research" and symbol is not None:
            raise ContractError("symbol is only allowed for stock_research")

        top_n = data.get("top_n", 5)
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 20:
            raise ContractError("top_n must be an integer between 1 and 20")

        return cls(
            workflow=workflow,
            decision_at=decision_at,
            snapshot_selector=selector,
            snapshot_id=snapshot_id,
            subject=subject,
            symbol=symbol,
            top_n=top_n,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "market": MARKET,
            "workflow": self.workflow,
            "decision_at": self.decision_at.isoformat(),
            "snapshot": {
                "selector": self.snapshot_selector,
                **({"snapshot_id": self.snapshot_id} if self.snapshot_id else {}),
            },
            **({"subject": self.subject} if self.subject else {}),
            **({"symbol": self.symbol} if self.symbol else {}),
            "top_n": self.top_n,
        }


@dataclass(frozen=True)
class ArtifactReadRequest:
    artifact_id: str
    section: str
    max_chars: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ArtifactReadRequest:
        data = require_mapping(raw, "request")
        _reject_unknown_fields(data, {"artifact_id", "section", "max_chars"}, "request")
        artifact_id = require_string(data.get("artifact_id"), "artifact_id")
        if not _is_safe_identifier(artifact_id):
            raise ContractError("artifact_id contains unsupported characters")
        section = require_string(data.get("section", "summary"), "section")
        if section not in {"summary", "report", "manifest", "packet"}:
            raise ContractError("section must be summary, report, manifest, or packet")
        max_chars = data.get("max_chars", 12000)
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 500 <= max_chars <= 20000
        ):
            raise ContractError("max_chars must be an integer between 500 and 20000")
        return cls(artifact_id=artifact_id, section=section, max_chars=max_chars)


def _is_safe_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and all(character.isalnum() or character in {"-", "_", "."} for character in value)
    )


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{field} contains unsupported fields: {', '.join(unknown)}")
