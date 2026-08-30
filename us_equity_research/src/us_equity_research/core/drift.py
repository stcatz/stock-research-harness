from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    MARKET,
    SCHEMA_VERSION,
    ContractError,
    require_mapping,
    require_string,
    validate_identifier,
)
from .snapshot import validate_snapshot
from .utils import read_json, sha256_value, write_json_atomic


def audit_snapshot_drift(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    data = require_mapping(raw_request, "request")
    unknown = sorted(
        set(data) - {"schema_version", "market", "before_snapshot_id", "after_snapshot_id"}
    )
    if unknown:
        raise ContractError("request contains unsupported fields: " + ", ".join(unknown))
    if require_string(data.get("schema_version"), "schema_version") != SCHEMA_VERSION:
        raise ContractError("unsupported schema_version")
    if data.get("market") != MARKET:
        raise ContractError("market must be US")
    before_id = validate_identifier(data.get("before_snapshot_id"), "before_snapshot_id")
    after_id = validate_identifier(data.get("after_snapshot_id"), "after_snapshot_id")
    if before_id == after_id:
        raise ContractError("before_snapshot_id and after_snapshot_id must differ")

    workspace = workspace.resolve()
    before = validate_snapshot(read_json(_snapshot_path(workspace, before_id)))
    after = validate_snapshot(read_json(_snapshot_path(workspace, after_id)))
    if after.retrieved_at <= before.retrieved_at:
        raise ContractError("after snapshot must have a later retrieved_at")

    before_facts = _fact_index(before.data)
    after_facts = _fact_index(after.data)
    before_securities = _snapshot_security_ids(before.data)
    after_securities = _snapshot_security_ids(after.data)
    shared_securities = before_securities.intersection(after_securities)
    before_keys = {key for key in before_facts if key[0] in shared_securities}
    after_keys = {key for key in after_facts if key[0] in shared_securities}
    changes: list[dict[str, Any]] = []
    for key in sorted(before_keys.intersection(after_keys)):
        previous = before_facts[key]
        current = after_facts[key]
        changed_fields = [
            field
            for field in ("value", "unit", "available_at")
            if previous[field] != current[field]
        ]
        if not changed_fields:
            continue
        if "available_at" in changed_fields:
            change_type = "historical_time_semantics_changed"
            severity = "critical"
        elif "unit" in changed_fields:
            change_type = "historical_unit_changed"
            severity = "critical"
        else:
            change_type = "historical_value_changed"
            severity = "high"
        changes.append(
            {
                "semantic_key": list(key),
                "change_type": change_type,
                "severity": severity,
                "changed_fields": changed_fields,
                "before": previous,
                "after": current,
            }
        )
    for key in sorted(before_keys - after_keys):
        changes.append(
            {
                "semantic_key": list(key),
                "change_type": "historical_fact_disappeared",
                "severity": "high",
                "before": before_facts[key],
                "after": None,
            }
        )
    for key in sorted(after_keys - before_keys):
        changes.append(
            {
                "semantic_key": list(key),
                "change_type": "historical_fact_appeared",
                "severity": "info",
                "before": None,
                "after": after_facts[key],
            }
        )

    stable = {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "before_snapshot_id": before.snapshot_id,
        "before_snapshot_hash": before.snapshot_hash,
        "after_snapshot_id": after.snapshot_id,
        "after_snapshot_hash": after.snapshot_hash,
        "shared_security_count": len(shared_securities),
        "compared_semantic_fact_count": len(before_keys.union(after_keys)),
        "changes": changes,
    }
    drift_hash = sha256_value(stable)
    result = {
        **stable,
        "drift_id": f"us-drift-{drift_hash[:20]}",
        "drift_hash": drift_hash,
        "status": "alert" if any(item["severity"] != "info" for item in changes) else "ok",
        "change_count": len(changes),
    }
    destination = workspace / "data" / "audit" / "us" / "provider-drift" / f"{result['drift_id']}.json"
    if destination.is_file():
        if read_json(destination) != result:
            raise RuntimeError("immutable drift receipt conflicts with an existing file")
    else:
        write_json_atomic(destination, result)
    result["relative_path"] = destination.relative_to(workspace).as_posix()
    return result


def _fact_index(snapshot: Mapping[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fact in snapshot["financial_facts"]:
        key = (fact["security_id"], fact["metric"], fact["period_end"])
        card = {
            "fact_id": fact["fact_id"],
            "value": fact["value"],
            "unit": fact["unit"],
            "available_at": fact["available_at"],
            "evidence_ref": fact["evidence_ref"],
        }
        if key in result and result[key] != card:
            raise RuntimeError("snapshot contains conflicting semantic facts")
        result[key] = card
    return result


def _snapshot_security_ids(snapshot: Mapping[str, Any]) -> set[str]:
    return {
        candidate["security_id"]
        for theme in snapshot["themes"]
        for candidate in theme["candidates"]
    }


def _snapshot_path(workspace: Path, snapshot_id: str) -> Path:
    path = workspace / "data" / "normalized" / "us" / snapshot_id / "snapshot.json"
    if not path.is_file():
        raise KeyError(f"snapshot not found: {snapshot_id}")
    return path
