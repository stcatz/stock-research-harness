"""Append-only, first-observed research records; no retroactive expectations."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from .contracts import ContractError, parse_datetime, validate_identifier, validate_url

KINDS = {"expectation", "catalyst", "plan", "outcome"}


def append_record(directory, record, *, clock=None):
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None:
        raise ContractError("Ledger clock must be timezone-aware")
    allowed = {
        "kind",
        "symbol",
        "metric",
        "period_end",
        "value",
        "unit",
        "source_url",
        "published_at",
        "effective_at",
        "available_at",
        "retrieved_at",
        "as_of",
        "source_level",
        "text",
        "supersedes",
        "event_at",
        "plan_ref",
        "result",
        "license_attestation",
        "artifact_ref",
        "period_type",
        "accounting_basis",
    }
    if set(record) - allowed:
        raise ContractError("Unknown ledger fields")
    if record.get("kind") not in KINDS:
        raise ContractError("Unsupported ledger record kind")
    validate_identifier(record.get("symbol"), "symbol")
    if record["kind"] in {"plan", "outcome"} and record.get("source_url") == "UNKNOWN":
        ref = record.get("artifact_ref")
        if (
            not isinstance(ref, str)
            or len(ref) != 64
            or any(c not in "0123456789abcdef" for c in ref)
        ):
            raise ContractError("Internal plan/outcome requires an exact artifact hash")
    else:
        validate_url(record.get("source_url"), "source_url")
    for key in ("published_at", "effective_at", "available_at", "retrieved_at", "as_of"):
        parse_datetime(record.get(key), key)
    published = parse_datetime(record["published_at"], "published_at")
    available = parse_datetime(record["available_at"], "available_at")
    retrieved = parse_datetime(record["retrieved_at"], "retrieved_at")
    if (
        not published <= available <= retrieved <= now
        or parse_datetime(record["as_of"], "as_of") > available
    ):
        raise ContractError("Ledger source times are contradictory or in the future")
    if record["kind"] == "expectation":
        if record.get("period_type") not in {"annual", "quarterly"} or record.get(
            "accounting_basis"
        ) not in {"GAAP", "adjusted"}:
            raise ContractError("Expectation period and accounting basis must be explicit")
        if record.get("source_level") != "structured_market":
            raise ContractError(
                "Consensus must use a structured source; issuer guidance is different"
            )
        if record.get("license_attestation") != "authorized_for_local_research_snapshot":
            raise ContractError("Expectation imports require local research data authorization")
        if record.get("metric") not in {"revenue", "eps"}:
            raise ContractError("Expectation metric must be revenue or eps")
        from datetime import date

        date.fromisoformat(record["period_end"])
        value = record.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ContractError("Invalid expectation value")
        if record.get("unit") != {"revenue": "USD", "eps": "USD/share"}[record["metric"]]:
            raise ContractError("Expectation unit does not match metric")
    if record["kind"] == "catalyst":
        if record.get("source_level") != "official":
            raise ContractError("Confirmed catalysts require official source")
        parse_datetime(record["event_at"], "event_at")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if record.get("supersedes"):
        prior = read_record(directory, record["supersedes"])
        if (
            prior["record"]["symbol"] != record["symbol"]
            or prior["record"]["kind"] != record["kind"]
        ):
            raise ContractError("Revision must refer to same symbol and kind")
    if record["kind"] == "outcome":
        prior = read_record(directory, record["plan_ref"])
        if prior["record"]["kind"] != "plan" or prior["record"]["symbol"] != record["symbol"]:
            raise ContractError("Outcome requires matching recorded plan")
        if record.get("result") not in {"supported", "invalidated", "unknown"}:
            raise ContractError("Invalid research outcome")
    serialized = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    rid = hashlib.sha256(serialized.encode()).hexdigest()
    payload = {
        "record_id": rid,
        "record": record,
        "first_observed_at": now.isoformat(),
        "usable_at": max(available, now).isoformat(),
    }
    payload["integrity_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with (directory / (rid + ".json")).open("x", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True)
    return {
        "record_id": rid,
        "first_observed_at": payload["first_observed_at"],
        "usable_at": payload["usable_at"],
    }


def read_record(directory, rid):
    if not isinstance(rid, str) or len(rid) != 64 or any(c not in "0123456789abcdef" for c in rid):
        raise ContractError("Invalid ledger ID")
    payload = json.loads((Path(directory) / (rid + ".json")).read_text())
    checksum = payload.pop("integrity_hash")
    if (
        hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        != checksum
    ):
        raise ContractError("Ledger record integrity failure")
    calculated = hashlib.sha256(
        json.dumps(payload["record"], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    if calculated != rid or payload["record_id"] != rid:
        raise ContractError("Ledger record ID mismatch")
    return payload


def select_expectation(
    directory, symbol, metric, period_end, decision_at, *, period_type, accounting_basis
):
    cutoff = parse_datetime(decision_at, "decision_at")
    matches = []
    for path in sorted(Path(directory).glob("*.json")):
        payload = read_record(directory, path.stem)
        r = payload["record"]
        if (
            r["kind"] == "expectation"
            and r["symbol"] == symbol
            and r["metric"] == metric
            and r["period_end"] == period_end
            and r["period_type"] == period_type
            and r["accounting_basis"] == accounting_basis
            and parse_datetime(payload["usable_at"], "usable_at") <= cutoff
        ):
            matches.append(payload)
    return max(matches, key=lambda p: (p["usable_at"], p["record_id"]), default=None)
