from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    MARKET,
    SCHEMA_VERSION,
    ContractError,
    parse_datetime,
    require_mapping,
    require_string,
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
        raise ContractError("market must be CN")
    before_id = _identifier(data.get("before_snapshot_id"), "before_snapshot_id")
    after_id = _identifier(data.get("after_snapshot_id"), "after_snapshot_id")
    if before_id == after_id:
        raise ContractError("before_snapshot_id and after_snapshot_id must differ")

    workspace = workspace.resolve()
    before = validate_snapshot(read_json(_snapshot_path(workspace, before_id)))
    after = validate_snapshot(read_json(_snapshot_path(workspace, after_id)))
    if parse_datetime(after.data["retrieved_at"], "after.retrieved_at") <= parse_datetime(
        before.data["retrieved_at"], "before.retrieved_at"
    ):
        raise ContractError("after snapshot must have a later retrieved_at")

    before_facts = _fact_index(before.data)
    after_facts = _fact_index(after.data)
    before_entities = {key[0] for key in before_facts}
    after_entities = {key[0] for key in after_facts}
    shared_entities = before_entities.intersection(after_entities)
    before_keys = {key for key in before_facts if key[0] in shared_entities}
    after_keys = {key for key in after_facts if key[0] in shared_entities}
    changes: list[dict[str, Any]] = []
    for key in sorted(before_keys.intersection(after_keys)):
        previous = before_facts[key]
        current = after_facts[key]
        changed_fields = [
            field
            for field in ("status", "value", "unit", "available_at", "effective_at")
            if previous[field] != current[field]
        ]
        if not changed_fields:
            continue
        if previous["status"] == "observed" and current["status"] == "unknown":
            severity = "critical"
            change_type = "observed_to_unknown"
        elif previous["status"] == "unknown" and current["status"] == "observed":
            severity = "info"
            change_type = "unknown_to_observed"
        elif "available_at" in changed_fields or "effective_at" in changed_fields:
            severity = "critical"
            change_type = "historical_time_semantics_changed"
        elif previous["unit"] != current["unit"]:
            severity = "critical"
            change_type = "historical_unit_changed"
        else:
            severity = "high"
            change_type = "historical_value_changed"
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
                "severity": "critical",
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
        "shared_entity_count": len(shared_entities),
        "compared_semantic_fact_count": len(before_keys.union(after_keys)),
        "changes": changes,
    }
    drift_hash = sha256_value(stable)
    result = {
        **stable,
        "drift_id": f"cn-drift-{drift_hash[:20]}",
        "drift_hash": drift_hash,
        "status": "alert" if any(item["severity"] != "info" for item in changes) else "ok",
        "change_count": len(changes),
    }
    destination = workspace / "data" / "audit" / "cn" / "provider-drift" / f"{result['drift_id']}.json"
    if destination.is_file():
        if read_json(destination) != result:
            raise RuntimeError("immutable drift receipt conflicts with an existing file")
    else:
        write_json_atomic(destination, result)
    result["relative_path"] = destination.relative_to(workspace).as_posix()
    return result


def _fact_index(snapshot: Mapping[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for evidence in snapshot["evidence"]:
        provider = evidence.get("provider")
        instrument = None
        if isinstance(provider, Mapping):
            raw_instrument = provider.get("instrument")
            instrument = raw_instrument if isinstance(raw_instrument, str) else None
        entity = instrument or evidence["evidence_id"]
        for fact in evidence.get("facts", []):
            key = (entity, fact["metric"], fact["as_of"])
            card = {
                "fact_id": fact["fact_id"],
                "status": fact["status"],
                "value": fact["value"],
                "unit": fact["unit"],
                "available_at": fact["available_at"],
                "effective_at": fact["effective_at"],
                "evidence_id": evidence["evidence_id"],
            }
            if key in result and result[key] != card:
                raise RuntimeError("snapshot contains conflicting semantic facts")
            result[key] = card
    return result


def _snapshot_path(workspace: Path, snapshot_id: str) -> Path:
    path = workspace / "data" / "normalized" / snapshot_id / "snapshot.json"
    if not path.is_file():
        raise KeyError(f"snapshot not found: {snapshot_id}")
    return path


def _identifier(value: Any, field: str) -> str:
    raw = require_string(value, field)
    if len(raw) > 128 or any(not (character.isalnum() or character in "-_.") for character in raw):
        raise ContractError(f"{field} contains unsupported characters")
    return raw
