from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import MARKET, SCHEMA_VERSION, ContractError, parse_datetime, require_mapping
from .storage import connect, database_path, initialize_workspace


def summarize_research_history(
    raw_request: Mapping[str, Any], workspace: Path
) -> dict[str, Any]:
    data = require_mapping(raw_request, "request")
    unknown = sorted(
        set(data)
        - {"schema_version", "market", "evaluation_at", "limit", "candidate_ids"}
    )
    if unknown:
        raise ContractError("request contains unsupported fields: " + ", ".join(unknown))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("unsupported schema_version")
    if data.get("market") != MARKET:
        raise ContractError("market must be CN")
    evaluation_at = parse_datetime(data.get("evaluation_at"), "evaluation_at")
    limit = data.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ContractError("limit must be an integer between 1 and 500")
    candidate_ids = _candidate_ids(data.get("candidate_ids"))

    workspace = workspace.resolve()
    initialize_workspace(workspace)
    with connect(database_path(workspace)) as connection:
        rows = connection.execute(
            """
            SELECT r.run_id, r.decision_at, c.candidate_id, c.symbol, c.name,
                   c.theme_id, c.decision, c.evidence_refs_json, c.data_gaps_json
            FROM candidate_decisions AS c
            JOIN runs AS r ON r.run_id = c.run_id
            ORDER BY r.decision_at, r.run_id, c.candidate_id
            """
        ).fetchall()

    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if parse_datetime(row["decision_at"], "runs.decision_at") <= evaluation_at:
            if candidate_ids is not None and row["candidate_id"] not in candidate_ids:
                continue
            groups[row["candidate_id"]].append(row)

    candidates = [
        _history_card(candidate_id, candidate_rows, evaluation_at)
        for candidate_id, candidate_rows in groups.items()
    ]
    priority = {"close_review": 0, "deprioritize": 1, "active": 2, "closed": 3}
    candidates.sort(
        key=lambda item: (
            priority[item["aging_action"]],
            -item["stale_days"],
            item["symbol"],
            item["candidate_id"],
        )
    )
    selected = candidates[:limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "evaluation_at": evaluation_at.isoformat(),
        "candidate_filter_count": len(candidate_ids) if candidate_ids is not None else None,
        "candidate_count": len(candidates),
        "returned_count": len(selected),
        "aging_counts": {
            action: sum(item["aging_action"] == action for item in candidates)
            for action in ("active", "deprioritize", "close_review", "closed")
        },
        "candidates": selected,
        "policy": {
            "deprioritize_after_stale_days": 14,
            "close_review_after_stale_days": 30,
            "meaning": (
                "Aging changes the daily research-attention queue only. It never rewrites an "
                "immutable canonical decision and is not a trading signal."
            ),
        },
    }


def _history_card(
    candidate_id: str,
    rows: list[Any],
    evaluation_at: datetime,
) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (
            parse_datetime(row["decision_at"], "runs.decision_at"),
            row["run_id"],
        ),
    )
    first = ordered[0]
    latest = ordered[-1]
    last_progress_at = parse_datetime(first["decision_at"], "runs.decision_at")
    previous_signature = _progress_signature(first)
    transitions = 0
    for row in ordered[1:]:
        signature = _progress_signature(row)
        if signature != previous_signature:
            last_progress_at = parse_datetime(row["decision_at"], "runs.decision_at")
            transitions += 1
            previous_signature = signature

    consecutive_continue = 0
    for row in reversed(ordered):
        if row["decision"] != "continue_research":
            break
        consecutive_continue += 1
    stale_days = max(0, (evaluation_at - last_progress_at).days)
    if latest["decision"] == "exclude":
        aging_action = "closed"
    elif latest["decision"] == "continue_research" and (
        stale_days >= 30 or consecutive_continue >= 20
    ):
        aging_action = "close_review"
    elif latest["decision"] == "continue_research" and (
        stale_days >= 14 or consecutive_continue >= 10
    ):
        aging_action = "deprioritize"
    else:
        aging_action = "active"

    return {
        "candidate_id": candidate_id,
        "symbol": latest["symbol"],
        "name": latest["name"],
        "theme_id": latest["theme_id"],
        "latest_decision": latest["decision"],
        "latest_run_id": latest["run_id"],
        "first_seen_at": first["decision_at"],
        "latest_seen_at": latest["decision_at"],
        "run_count": len(ordered),
        "state_or_gap_transition_count": transitions,
        "consecutive_continue_research": consecutive_continue,
        "last_progress_at": last_progress_at.isoformat(),
        "stale_days": stale_days,
        "latest_data_gaps": json.loads(latest["data_gaps_json"]),
        "latest_usable_evidence_refs": json.loads(latest["evidence_refs_json"]),
        "aging_action": aging_action,
    }


def _progress_signature(row: Any) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    gaps = json.loads(row["data_gaps_json"])
    if not isinstance(gaps, list):
        raise RuntimeError("candidate_decisions.data_gaps_json is not an array")
    evidence_refs = json.loads(row["evidence_refs_json"])
    if not isinstance(evidence_refs, list):
        raise RuntimeError("candidate_decisions.evidence_refs_json is not an array")
    return (
        row["decision"],
        tuple(sorted(str(gap) for gap in gaps)),
        tuple(sorted(str(evidence_ref) for evidence_ref in evidence_refs)),
    )


def _candidate_ids(raw: Any) -> set[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > 50:
        raise ContractError("candidate_ids must be an array with at most 50 items")
    result: set[str] = set()
    for index, candidate_id in enumerate(raw):
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.strip()
            or len(candidate_id) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in candidate_id)
        ):
            raise ContractError(f"candidate_ids[{index}] is invalid")
        normalized = candidate_id.strip()
        if normalized in result:
            raise ContractError("candidate_ids must not contain duplicates")
        result.add(normalized)
    return result
