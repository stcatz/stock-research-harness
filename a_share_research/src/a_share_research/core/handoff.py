"""Local shadow handoff. No network, model invocation, publisher, or scheduling authority."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, RunRequest, parse_datetime
from .locking import run_lock
from .pipeline import read_artifact, run_research, verified_research_packet
from .snapshot import load_snapshot
from .storage import connect, database_path, initialize_workspace
from .utils import sha256_file, sha256_value

SHANGHAI = ZoneInfo("Asia/Shanghai")
VERSION = "cn-shadow-handoff-v1"
MAX_CHARS = 20000
JOURNAL_SQL = """
CREATE TABLE IF NOT EXISTS cn_handoff_records (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('forecast', 'close_review')),
    business_date TEXT NOT NULL,
    parent_id TEXT REFERENCES cn_handoff_records(record_id),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cn_handoff_by_date
    ON cn_handoff_records(business_date, kind);
CREATE TRIGGER IF NOT EXISTS cn_handoff_no_update
    BEFORE UPDATE ON cn_handoff_records BEGIN
    SELECT RAISE(ABORT, 'handoff records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS cn_handoff_no_delete
    BEFORE DELETE ON cn_handoff_records BEGIN
    SELECT RAISE(ABORT, 'handoff records are append-only'); END;
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _fields(raw: Mapping[str, Any], allowed: set[str]) -> None:
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ContractError("unsupported handoff request fields")


def _business_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ContractError("business_date must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ContractError("business_date must be YYYY-MM-DD")
    return parsed


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("cn-handoff-") or len(value) > 80:
        raise ContractError("invalid CN handoff record ID")
    if not all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in value):
        raise ContractError("invalid CN handoff record ID")
    return value


def _initialize(workspace: Path) -> None:
    initialize_workspace(workspace)
    with run_lock(workspace, "workspace-database"), connect(database_path(workspace)) as db:
        db.executescript(JOURNAL_SQL)


def _read(workspace: Path, record_id: str) -> dict[str, Any]:
    record_id = _identifier(record_id)
    with connect(database_path(workspace)) as db:
        row = db.execute(
            "SELECT * FROM cn_handoff_records WHERE record_id = ?", (record_id,)
        ).fetchone()
    if row is None:
        raise KeyError("handoff record not found")
    payload = json.loads(row["payload_json"])
    if sha256_value(payload) != row["payload_hash"]:
        raise RuntimeError("handoff hash mismatch")
    if any(payload[key] != row[key] for key in ("record_id", "kind", "business_date", "parent_id")):
        raise RuntimeError("handoff index mismatch")
    packet = verified_research_packet(workspace, payload["artifact_id"])
    if packet["analysis_hash"] != payload["analysis_hash"]:
        raise RuntimeError("handoff research hash mismatch")
    return payload


def _save(workspace: Path, payload: dict[str, Any]) -> None:
    with connect(database_path(workspace)) as db:
        db.execute(
            "INSERT INTO cn_handoff_records VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload["record_id"],
                payload["kind"],
                payload["business_date"],
                payload["parent_id"],
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
                sha256_value(payload),
            ),
        )
    if _read(workspace, payload["record_id"]) != payload:
        raise RuntimeError("handoff readback mismatch")


def _exists(workspace: Path, record_id: str) -> bool:
    with connect(database_path(workspace)) as db:
        return (
            db.execute(
                "SELECT 1 FROM cn_handoff_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            is not None
        )


def _receipt(payload: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "record_id",
            "kind",
            "business_date",
            "parent_id",
            "run_id",
            "artifact_id",
            "sample_class",
            "publication",
            "quality_gaps",
        )
    } | {"readback_verified": True, "reused": reused, "record_hash": sha256_value(payload)}


def _versions() -> dict[str, Any]:
    package = Path(__file__).resolve().parents[1]
    project = package.parent.parent
    sources = {
        str(path.relative_to(package)): sha256_file(path) for path in sorted(package.rglob("*.py"))
    }
    assets = [
        project / "methods/a_share_theme_v1.toml",
        project.parent / "docs/cloud_handoff/CONTRACT.md",
        project.parent / "docs/cloud_handoff/LESSONS.json",
        project.parent / "docs/cloud_handoff/DAILY.md",
    ]
    return {
        "handoff": VERSION,
        "source_hash": sha256_value(sources),
        "source_files": sources,
        "assets": {p.name: sha256_file(p) if p.is_file() else "UNKNOWN" for p in assets},
        "model": {"requested": None, "returned": None, "status": "not_invoked"},
    }


def premarket(raw: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    """Freeze existing engine output in the same SQLite ledger, always unpublished shadow."""
    _fields(raw, {"business_date", "snapshot", "top_n"})
    started = _now()
    business = _business_date(raw.get("business_date"))
    cutoff = datetime.combine(business, time(7), SHANGHAI)
    if cutoff > started:
        raise ContractError("cannot run before the requested information cutoff")
    request = RunRequest.from_dict(
        {
            "schema_version": "0.1",
            "market": "CN",
            "workflow": "daily_report",
            "decision_at": cutoff.isoformat(),
            "snapshot": raw.get("snapshot"),
            "top_n": raw.get("top_n", 5),
        }
    )
    if request.snapshot_selector == "latest":
        raise ContractError("handoff requires an explicit snapshot ID or explicit demo")
    snapshot = load_snapshot(workspace, request)
    frozen_at = _now().isoformat()
    versions = _versions()
    record_id = (
        "cn-handoff-f-"
        + sha256_value(
            {
                "request": request.to_dict(),
                "snapshot_hash": snapshot.snapshot_hash,
                "versions": versions,
            }
        )[:24]
    )
    _initialize(workspace)
    with run_lock(workspace, record_id):
        if _exists(workspace, record_id):
            return _receipt(_read(workspace, record_id), reused=True)
        result = run_research(request.to_dict(), workspace)
        packet = verified_research_packet(workspace, result["artifact_id"])
        if packet["snapshot_hash"] != snapshot.snapshot_hash:
            raise RuntimeError("snapshot changed during handoff")
        report = read_artifact(
            {
                "artifact_id": result["artifact_id"],
                "section": "report",
                "max_chars": MAX_CHARS,
            },
            workspace,
        )
        gaps = (
            list(packet["data_gaps"])
            + list(packet["warnings"])
            + [
                "SHADOW_ONLY: production publication and formal sample admission disabled",
                "TRADING_CALENDAR_UNKNOWN: weekday is not proof of an exchange session",
                "INTRADAY_UNAVAILABLE: no auction/09:35 conclusions",
                "SEMANTIC_REVIEW_UNKNOWN: seed assertions require human verification",
                "OVERSEAS_PEERS_AND_CALENDAR_UNVERIFIED",
                "CONFIRMATION_AND_BENCHMARK_CONTRACT_PENDING",
                "ORIGINAL_HANDOFF_ATTACHMENTS_INCOMPLETE",
            ]
        )
        if report["truncated"]:
            gaps.append("REPORT_PREVIEW_TRUNCATED: canonical report retained, no full delivery")
        if any(value == "UNKNOWN" for value in versions["assets"].values()):
            gaps.append("VERSION_ASSET_MISSING")
        if business.weekday() >= 5:
            gaps.append("WEEKEND: not a formal premarket sample")
        if parse_datetime(snapshot.data["retrieved_at"], "retrieved_at") > cutoff:
            gaps.append("INPUT_RETRIEVED_AFTER_CUTOFF")
        if started.astimezone(SHANGHAI).date() != business:
            gaps.append("HISTORICAL_RECONSTRUCTION")
        focus_ids = [c["candidate_id"] for c in packet["focus"]]
        payload = {
            "record_id": record_id,
            "kind": "forecast",
            "parent_id": None,
            "business_date": business.isoformat(),
            "schema_version": VERSION,
            "market": "CN",
            "mode": "shadow",
            "formal_sample": False,
            "sample_class": "fixture" if snapshot.data_mode == "fixture" else "reconstructed",
            "data_mode": snapshot.data_mode,
            "pit_quality": snapshot.pit_quality,
            "run_id": packet["run_id"],
            "artifact_id": packet["artifact_id"],
            "analysis_hash": packet["analysis_hash"],
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "versions": versions,
            "canonical_writer_source_hash": "UNKNOWN"
            if result.get("reused")
            else versions["source_hash"],
            "times": {
                "scheduled_at": cutoff.isoformat(),
                "decision_at": cutoff.isoformat(),
                "started_at": started.isoformat(),
                "input_frozen_at": frozen_at,
                "input_as_of": snapshot.data["as_of"],
                "input_retrieved_at": snapshot.data["retrieved_at"],
                "report_generated_at": packet["generated_at"],
                "forecast_locked_at": _now().isoformat(),
                "published_at": None,
            },
            "publication": {"status": "not_published", "receipt": None},
            "themes": packet["themes"],
            "candidates": packet["all_decisions"],
            "focus_ids": focus_ids,
            "alternative_ids": [
                c["candidate_id"]
                for c in packet["all_decisions"]
                if c["candidate_id"] not in focus_ids and c["decision"] != "exclude"
            ],
            "priority_semantics": "existing engine order: decision, role, symbol; not a forecast score",
            "observation_action": "human_research_review",
            "confirmation_conditions": "UNKNOWN",
            "benchmark": "UNKNOWN",
            "quality_gaps": list(dict.fromkeys(gaps)),
        }
        _save(workspace, payload)
        return _receipt(payload)


def _session_result(
    candidate: dict[str, Any],
    evidence: list[dict[str, Any]],
    business: date,
    cutoff: datetime,
) -> dict[str, Any]:
    """No semantic outcome adjudication. Only same-session close/preclose arithmetic."""
    parts = candidate["security_id"].split(".")
    code = f"{parts[1].lower()}.{parts[2]}" if len(parts) == 3 and parts[0] == "CN" else None
    matched = []
    for item in evidence:
        if (
            code is None
            or item["source_level"] != "structured_market"
            or item.get("instrument", {}).get("code") != code
            or item.get("instrument", {}).get("kind") != "candidate"
            or item.get("provider", {}).get("frequency") != "1d"
            or item.get("latest", {}).get("date") != business.isoformat()
            or item.get("latest", {}).get("code") != code
            or item.get("provider", {}).get("adjustment") != "none"
            or parse_datetime(item["as_of"], "as_of").astimezone(SHANGHAI).date() != business
            or parse_datetime(item["as_of"], "as_of")
            < datetime.combine(business, time(15), SHANGHAI)
            or any(
                parse_datetime(item[field], field) > cutoff
                for field in ("available_at", "retrieved_at")
            )
        ):
            continue
        matched.append(item)
    value = None
    reason = "MISSING_OR_AMBIGUOUS_SAME_SESSION_MARKET_EVIDENCE"
    if len(matched) == 1:
        bar = matched[0]["latest"]
        try:
            if bar.get("trade_status") != "1":
                raise ValueError("trade status not verified active")
            close, preclose = Decimal(str(bar.get("close"))), Decimal(str(bar.get("preclose")))
            if not close.is_finite() or not preclose.is_finite() or close <= 0 or preclose <= 0:
                raise ValueError("invalid price")
            value = str(((close / preclose - 1) * 100).quantize(Decimal("0.0001")))
            reason = "same-session daily return; not forecast or strategy performance"
        except (InvalidOperation, ValueError, ZeroDivisionError):
            reason = (
                "TRADE_STATUS_NOT_VERIFIED_ACTIVE"
                if bar.get("trade_status") != "1"
                else "CLOSE_OR_REFERENCE_PRECLOSE_UNKNOWN"
            )
    return {
        "candidate_id": candidate["candidate_id"],
        "security_id": candidate["security_id"],
        "original_decision": candidate["decision"],
        "original_invalidation_conditions": candidate["invalidation_conditions"],
        "status": "UNKNOWN",
        "confirmation_result": "UNKNOWN",
        "invalidation_result": "UNKNOWN",
        "intraday_result": "UNKNOWN",
        "benchmark_excess_return": "UNKNOWN",
        "session_return": {
            "status": "OK" if value is not None else "UNKNOWN",
            "value": value,
            "unit": "%",
            "formula": "(close / preclose - 1) * 100",
            "reason": reason,
            "input_evidence_refs": [e["evidence_id"] for e in matched],
            "input_fields": ["latest.close", "latest.preclose"],
        },
        "evidence": matched,
    }


def close_review(raw: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    _fields(raw, {"forecast_id", "snapshot"})
    started = _now()
    _initialize(workspace)
    forecast = _read(workspace, _identifier(raw.get("forecast_id")))
    if forecast["kind"] != "forecast":
        raise ContractError("close review parent must be a frozen forecast")
    business = _business_date(forecast["business_date"])
    if started < datetime.combine(business, time(15), SHANGHAI):
        raise ContractError("cannot review before the business-day close")
    request = RunRequest.from_dict(
        {
            "schema_version": "0.1",
            "market": "CN",
            "workflow": "daily_report",
            "decision_at": started.isoformat(),
            "snapshot": raw.get("snapshot"),
        }
    )
    if request.snapshot_selector == "latest":
        raise ContractError("close review requires an explicit snapshot ID or demo")
    snapshot = load_snapshot(workspace, request)
    if snapshot.data_mode != forecast["data_mode"]:
        raise ContractError("cannot mix fixture and real snapshot handoffs")
    if parse_datetime(snapshot.data["retrieved_at"], "retrieved_at") > started:
        raise ContractError("review snapshot retrieved in the future")
    versions = _versions()
    record_id = (
        "cn-handoff-r-"
        + sha256_value(
            {
                "parent_id": forecast["record_id"],
                "snapshot_hash": snapshot.snapshot_hash,
                "versions": versions,
            }
        )[:24]
    )
    with run_lock(workspace, record_id):
        if _exists(workspace, record_id):
            return _receipt(_read(workspace, record_id), reused=True)
        outcomes = [
            _session_result(c, list(snapshot.evidence_by_id.values()), business, started)
            for c in forecast["candidates"]
        ]
        payload = {
            "record_id": record_id,
            "kind": "close_review",
            "schema_version": VERSION,
            "market": "CN",
            "mode": "shadow",
            "formal_sample": False,
            "business_date": forecast["business_date"],
            "parent_id": forecast["record_id"],
            "parent_hash": sha256_value(forecast),
            "versions": versions,
            "run_id": forecast["run_id"],
            "artifact_id": forecast["artifact_id"],
            "analysis_hash": forecast["analysis_hash"],
            "sample_class": forecast["sample_class"],
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "data_mode": snapshot.data_mode,
            "pit_quality": snapshot.pit_quality,
            "times": {"started_at": started.isoformat(), "generated_at": _now().isoformat()},
            "publication": {"status": "not_published", "receipt": None},
            "quality_gaps": [
                "SHADOW_ONLY",
                "SEMANTIC_OUTCOMES_UNKNOWN",
                "INTRADAY_UNAVAILABLE",
                "BENCHMARK_UNKNOWN",
                "TRADING_CALENDAR_UNKNOWN",
            ],
            "outcomes": outcomes,
        }
        if any(o["session_return"]["status"] == "UNKNOWN" for o in outcomes):
            payload["quality_gaps"].append("SAME_SESSION_RESULTS_INCOMPLETE")
        _save(workspace, payload)
        return _receipt(payload)


def journal_read(raw: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    _fields(raw, {"record_id", "max_chars", "offset"})
    limit = raw.get("max_chars", MAX_CHARS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 500 <= limit <= MAX_CHARS:
        raise ContractError("max_chars must be an integer between 500 and 20000")
    _initialize(workspace)
    payload = _read(workspace, _identifier(raw.get("record_id")))
    if payload["parent_id"]:
        parent = _read(workspace, payload["parent_id"])
        if sha256_value(parent) != payload["parent_hash"]:
            raise RuntimeError("review parent hash mismatch")
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    offset = raw.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(content):
        raise ContractError("offset must be inside the record")
    end = min(len(content), offset + limit)
    return _receipt(payload) | {
        "content": content[offset:end],
        "truncated": end < len(content),
        "offset": offset,
        "next_offset": end if end < len(content) else None,
        "content_type": "application/json",
        "total_chars": len(content),
    }


def journal_list(raw: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    _fields(raw, {"business_date"})
    business = _business_date(raw.get("business_date")).isoformat()
    _initialize(workspace)
    with connect(database_path(workspace)) as db:
        rows = db.execute(
            "SELECT record_id, kind, parent_id FROM cn_handoff_records "
            "WHERE business_date = ? ORDER BY record_id LIMIT 101",
            (business,),
        ).fetchall()
    return {
        "market": "CN",
        "business_date": business,
        "records": [dict(row) for row in rows[:100]],
        "truncated": len(rows) > 100,
    }
