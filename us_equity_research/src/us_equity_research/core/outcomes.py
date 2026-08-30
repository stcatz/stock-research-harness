from __future__ import annotations

import math
import statistics
from collections import defaultdict
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
    validate_url,
)
from .locking import database_lock
from .storage import connect, database_path, initialize_workspace
from .utils import sha256_value

ALLOWED_HORIZONS = {5, 20}
BENCHMARK_SYMBOL = "SPY"


def record_outcome(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    request = _validate_record_request(raw_request)
    workspace = workspace.resolve()
    initialize_workspace(workspace)
    with database_lock(workspace), connect(database_path(workspace)) as connection:
        run = connection.execute(
            "SELECT decision_at FROM runs WHERE run_id = ? AND market = ?",
            (request["run_id"], MARKET),
        ).fetchone()
        if run is None:
            raise KeyError(f"run not found: {request['run_id']}")
        candidate = _resolve_candidate(connection, request)
        decision_at = parse_datetime(run["decision_at"], "runs.decision_at")
        if request["observed_at"] <= decision_at:
            raise ContractError("observed_at must be later than the run decision_at")
        if request["available_at"] < request["observed_at"]:
            raise ContractError("available_at must not be earlier than observed_at")

        stable = {
            "schema_version": SCHEMA_VERSION,
            "market": MARKET,
            "run_id": request["run_id"],
            "candidate_id": candidate["candidate_id"],
            "symbol": request["symbol"],
            "decision": candidate["decision"],
            "horizon_trading_days": request["horizon_trading_days"],
            "observed_at": request["observed_at"].isoformat(),
            "available_at": request["available_at"].isoformat(),
            "candidate_return": request["candidate_return"],
            "benchmark_return": request["benchmark_return"],
            "excess_return": request["candidate_return"] - request["benchmark_return"],
            "benchmark_symbol": BENCHMARK_SYMBOL,
            "source_url": request["source_url"],
            "source_document_id": request["source_document_id"],
        }
        observation_hash = sha256_value(stable)
        observation_id = f"us-outcome-{observation_hash[:20]}"
        connection.execute(
            """
            INSERT INTO decision_outcomes (
                observation_id, observation_hash, run_id, candidate_id, symbol,
                decision, horizon_trading_days, observed_at, available_at,
                candidate_return, benchmark_return, excess_return, benchmark_symbol,
                source_url, source_document_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, candidate_id, horizon_trading_days) DO NOTHING
            """,
            (
                observation_id,
                observation_hash,
                stable["run_id"],
                stable["candidate_id"],
                stable["symbol"],
                stable["decision"],
                stable["horizon_trading_days"],
                stable["observed_at"],
                stable["available_at"],
                stable["candidate_return"],
                stable["benchmark_return"],
                stable["excess_return"],
                stable["benchmark_symbol"],
                stable["source_url"],
                stable["source_document_id"],
            ),
        )
        stored = connection.execute(
            """
            SELECT observation_id, observation_hash FROM decision_outcomes
            WHERE run_id = ? AND candidate_id = ? AND horizon_trading_days = ?
            """,
            (stable["run_id"], stable["candidate_id"], stable["horizon_trading_days"]),
        ).fetchone()
        if stored is None or stored["observation_hash"] != observation_hash:
            raise RuntimeError("outcome slot already contains a different immutable observation")
    return {
        **stable,
        "observation_id": observation_id,
        "observation_hash": observation_hash,
        "status": "recorded",
    }


def summarize_outcomes(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    data = require_mapping(raw_request, "request")
    _reject_unknown(data, {"schema_version", "market", "run_id", "evaluation_at"})
    _validate_common(data)
    run_id = require_string(data.get("run_id"), "run_id")
    evaluation_at = parse_datetime(data.get("evaluation_at"), "evaluation_at")
    workspace = workspace.resolve()
    initialize_workspace(workspace)
    with connect(database_path(workspace)) as connection:
        run = connection.execute(
            "SELECT decision_at FROM runs WHERE run_id = ? AND market = ?",
            (run_id, MARKET),
        ).fetchone()
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if evaluation_at < parse_datetime(run["decision_at"], "runs.decision_at"):
            raise ContractError("evaluation_at must not be earlier than the run decision_at")
        rows = connection.execute(
            """
            SELECT decision, horizon_trading_days, excess_return, available_at
            FROM decision_outcomes
            WHERE run_id = ?
            ORDER BY horizon_trading_days, decision, symbol
            """,
            (run_id,),
        ).fetchall()
        rows = _available_rows(rows, evaluation_at)
        candidate_count = connection.execute(
            "SELECT count(*) FROM candidate_decisions WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]

    grouped: dict[tuple[int, str], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(row["horizon_trading_days"], row["decision"])].append(row["excess_return"])
    scorecards: list[dict[str, Any]] = []
    for horizon in sorted(ALLOWED_HORIZONS):
        for decision in ("observe", "continue_research", "exclude"):
            values = grouped[(horizon, decision)]
            scorecards.append(
                {
                    "horizon_trading_days": horizon,
                    "decision": decision,
                    "sample_count": len(values),
                    "mean_excess_return": statistics.fmean(values) if values else None,
                    "median_excess_return": statistics.median(values) if values else None,
                    "positive_excess_hit_rate": (
                        sum(value > 0 for value in values) / len(values) if values else None
                    ),
                }
            )
    expected = candidate_count * len(ALLOWED_HORIZONS)
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "run_id": run_id,
        "decision_at": run["decision_at"],
        "evaluation_at": evaluation_at.isoformat(),
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "observation_count": len(rows),
        "expected_observation_count": expected,
        "status": "complete" if len(rows) == expected else "incomplete",
        "scorecards": scorecards,
        "interpretation": (
            "These scorecards evaluate research-state discrimination only and are not trading "
            "instructions. Missing future observations remain UNKNOWN."
        ),
    }


def summarize_outcome_history(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    data = require_mapping(raw_request, "request")
    _reject_unknown(data, {"schema_version", "market", "evaluation_at", "limit"})
    _validate_common(data)
    evaluation_at = parse_datetime(data.get("evaluation_at"), "evaluation_at")
    limit = data.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ContractError("limit must be an integer between 1 and 20")
    workspace = workspace.resolve()
    initialize_workspace(workspace)
    with connect(database_path(workspace)) as connection:
        runs = list(
            connection.execute(
                """
                SELECT r.run_id, r.artifact_id, r.decision_at
                FROM runs AS r
                WHERE r.market = ? AND EXISTS (
                    SELECT 1 FROM decision_outcomes AS outcome
                    WHERE outcome.run_id = r.run_id
                )
                """,
                (MARKET,),
            ).fetchall()
        )
        runs.sort(
            key=lambda row: (
                parse_datetime(row["decision_at"], "runs.decision_at"),
                row["run_id"],
            ),
            reverse=True,
        )
        history: list[dict[str, Any]] = []
        for run in runs:
            rows = connection.execute(
                """
                SELECT decision, horizon_trading_days, excess_return, available_at
                FROM decision_outcomes
                WHERE run_id = ?
                ORDER BY horizon_trading_days, decision, symbol
                """,
                (run["run_id"],),
            ).fetchall()
            rows = _available_rows(rows, evaluation_at)
            if not rows:
                continue
            candidate_count = connection.execute(
                "SELECT count(*) FROM candidate_decisions WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()[0]
            history.append(
                {
                    "run_id": run["run_id"],
                    "artifact_id": run["artifact_id"],
                    "decision_at": run["decision_at"],
                    "observation_count": len(rows),
                    "expected_observation_count": candidate_count * len(ALLOWED_HORIZONS),
                    "scorecards": _scorecards(rows),
                }
            )
            if len(history) == limit:
                break
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "evaluation_at": evaluation_at.isoformat(),
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "run_count": len(history),
        "runs": history,
        "interpretation": (
            "Only observations available by evaluation_at are included. Use this memory to "
            "calibrate research discrimination, never as a trading instruction."
        ),
    }


def _scorecards(rows: Any) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(row["horizon_trading_days"], row["decision"])].append(row["excess_return"])
    result: list[dict[str, Any]] = []
    for horizon in sorted(ALLOWED_HORIZONS):
        for decision in ("observe", "continue_research", "exclude"):
            values = grouped[(horizon, decision)]
            result.append(
                {
                    "horizon_trading_days": horizon,
                    "decision": decision,
                    "sample_count": len(values),
                    "mean_excess_return": statistics.fmean(values) if values else None,
                    "median_excess_return": statistics.median(values) if values else None,
                    "positive_excess_hit_rate": (
                        sum(value > 0 for value in values) / len(values) if values else None
                    ),
                }
            )
    return result


def _available_rows(rows: Any, evaluation_at: Any) -> list[Any]:
    return [
        row
        for row in rows
        if parse_datetime(row["available_at"], "decision_outcomes.available_at")
        <= evaluation_at
    ]


def _validate_record_request(raw_request: Mapping[str, Any]) -> dict[str, Any]:
    data = require_mapping(raw_request, "request")
    _reject_unknown(
        data,
        {
            "schema_version",
            "market",
            "run_id",
            "candidate_id",
            "symbol",
            "horizon_trading_days",
            "observed_at",
            "available_at",
            "candidate_return",
            "benchmark_return",
            "benchmark_symbol",
            "source_url",
            "source_document_id",
        },
    )
    _validate_common(data)
    horizon = data.get("horizon_trading_days")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon not in ALLOWED_HORIZONS:
        raise ContractError("horizon_trading_days must be 5 or 20")
    benchmark = require_string(data.get("benchmark_symbol"), "benchmark_symbol")
    if benchmark != BENCHMARK_SYMBOL:
        raise ContractError(f"benchmark_symbol must be {BENCHMARK_SYMBOL}")
    source_url = validate_url(data.get("source_url"), "source_url")
    if not source_url.startswith("https://"):
        raise ContractError("source_url must use https")
    return {
        "run_id": require_string(data.get("run_id"), "run_id"),
        "candidate_id": (
            require_string(data.get("candidate_id"), "candidate_id")
            if data.get("candidate_id") is not None
            else None
        ),
        "symbol": require_string(data.get("symbol"), "symbol"),
        "horizon_trading_days": horizon,
        "observed_at": parse_datetime(data.get("observed_at"), "observed_at"),
        "available_at": parse_datetime(data.get("available_at"), "available_at"),
        "candidate_return": _finite_number(data.get("candidate_return"), "candidate_return"),
        "benchmark_return": _finite_number(data.get("benchmark_return"), "benchmark_return"),
        "source_url": source_url,
        "source_document_id": require_string(
            data.get("source_document_id"), "source_document_id"
        ),
    }


def _resolve_candidate(connection: Any, request: Mapping[str, Any]) -> Any:
    candidate_id = request.get("candidate_id")
    if candidate_id is not None:
        candidate = connection.execute(
            """
            SELECT decision, candidate_id, symbol FROM candidate_decisions
            WHERE run_id = ? AND candidate_id = ? AND market = ?
            """,
            (request["run_id"], candidate_id, MARKET),
        ).fetchone()
        if candidate is None or candidate["symbol"] != request["symbol"]:
            raise KeyError(
                "candidate_id and symbol do not identify the same candidate in the run"
            )
        return candidate

    candidates = connection.execute(
        """
        SELECT decision, candidate_id, symbol FROM candidate_decisions
        WHERE run_id = ? AND symbol = ? AND market = ?
        ORDER BY candidate_id
        """,
        (request["run_id"], request["symbol"], MARKET),
    ).fetchall()
    if not candidates:
        raise KeyError(f"candidate not found in run: {request['symbol']}")
    if len(candidates) != 1:
        raise ContractError(
            "symbol maps to multiple candidates in this run; candidate_id is required"
        )
    return candidates[0]


def _validate_common(data: Mapping[str, Any]) -> None:
    if require_string(data.get("schema_version"), "schema_version") != SCHEMA_VERSION:
        raise ContractError("unsupported schema_version")
    if data.get("market") != MARKET:
        raise ContractError("market must be US")


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not -1 <= result <= 100:
        raise ContractError(f"{field} must be finite and between -1 and 100")
    return result


def _reject_unknown(data: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ContractError("request contains unsupported fields: " + ", ".join(unknown))
