from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import MARKET, SCHEMA_VERSION
from .locking import database_lock

DATABASE_RELATIVE_PATH = Path("data") / "us_stock_research.sqlite3"
RUNTIME_DIRECTORIES = (
    Path("data") / "normalized" / "us",
    Path("artifacts") / "us" / "runs",
    Path("artifacts") / "us" / ".locks",
    Path("reports") / "us" / "daily",
)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE,
    market TEXT NOT NULL CHECK (market = 'US'),
    workflow TEXT NOT NULL,
    subject TEXT,
    symbol TEXT,
    decision_at TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    analysis_hash TEXT NOT NULL,
    data_mode TEXT NOT NULL,
    pit_quality TEXT NOT NULL,
    writer_mode TEXT NOT NULL CHECK (writer_mode = 'engine'),
    method_id TEXT NOT NULL,
    status TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    gaps_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    market TEXT NOT NULL CHECK (market = 'US'),
    run_dir TEXT NOT NULL,
    request_path TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    packet_path TEXT NOT NULL,
    report_path TEXT NOT NULL,
    summary_path TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    integrity_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    candidate_id TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market = 'US'),
    theme_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (
        decision IN ('exclude', 'continue_research', 'observe')
    ),
    reasons_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    data_gaps_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS candidate_decisions_symbol_idx
    ON candidate_decisions (symbol, run_id);
CREATE INDEX IF NOT EXISTS candidate_decisions_theme_idx
    ON candidate_decisions (theme_id, run_id);

PRAGMA user_version = 1;
"""


def initialize_workspace(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    for relative in RUNTIME_DIRECTORIES:
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    database = database_path(workspace)
    with database_lock(workspace), connect(database) as connection:
        connection.executescript(SCHEMA_SQL)
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "status": "initialized",
        "database": DATABASE_RELATIVE_PATH.as_posix(),
        "directories": [path.as_posix() for path in RUNTIME_DIRECTORIES],
        "broker_capability": False,
    }


def database_path(workspace: Path) -> Path:
    return workspace.resolve() / DATABASE_RELATIVE_PATH


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_run(
    workspace: Path,
    packet: dict[str, Any],
    summary: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    if packet.get("market") != MARKET or summary.get("market") != MARKET:
        raise RuntimeError("refusing to record a non-US research run")

    with database_lock(workspace), connect(database_path(workspace)) as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, artifact_id, market, workflow, subject, symbol, decision_at,
                generated_at, snapshot_id, snapshot_hash, analysis_hash, data_mode,
                pit_quality, writer_mode, method_id, status, manifest_hash,
                warnings_json, gaps_json, summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO NOTHING
            """,
            (
                packet["run_id"],
                packet["artifact_id"],
                MARKET,
                packet["workflow"],
                packet.get("subject"),
                packet.get("symbol"),
                packet["decision_at"],
                packet["generated_at"],
                packet["snapshot_id"],
                packet["snapshot_hash"],
                packet["analysis_hash"],
                packet["data_mode"],
                packet["pit_quality"],
                packet["writer_mode"],
                packet["method_id"],
                summary["status"],
                manifest["manifest_hash"],
                _json(packet["warnings"]),
                _json(packet["data_gaps"]),
                _json(summary),
                packet["generated_at"],
            ),
        )
        _assert_existing_run_matches(connection, packet, manifest)

        paths = manifest["paths"]
        connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, run_id, market, run_dir, request_path, snapshot_path,
                packet_path, report_path, summary_path, manifest_path,
                manifest_hash, integrity_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO NOTHING
            """,
            (
                packet["artifact_id"],
                packet["run_id"],
                MARKET,
                paths["run_dir"],
                paths["request"],
                paths["snapshot"],
                paths["packet"],
                paths["report"],
                paths["summary"],
                paths["manifest"],
                manifest["manifest_hash"],
                manifest["integrity_hash"],
            ),
        )
        _assert_existing_artifact_matches(connection, packet, manifest)

        for decision in packet["all_decisions"]:
            serialized_decision = _json(decision)
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    run_id, candidate_id, market, theme_id, security_id, symbol,
                    name, decision, reasons_json, evidence_refs_json,
                    data_gaps_json, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, candidate_id) DO NOTHING
                """,
                (
                    packet["run_id"],
                    decision["candidate_id"],
                    MARKET,
                    decision["theme_id"],
                    decision["security_id"],
                    decision["symbol"],
                    decision["name"],
                    decision["decision"],
                    _json(decision["reasons"]),
                    _json(decision["usable_evidence_refs"]),
                    _json(decision["data_gaps"]),
                    serialized_decision,
                ),
            )
            stored = connection.execute(
                """
                SELECT decision_json FROM candidate_decisions
                WHERE run_id = ? AND candidate_id = ?
                """,
                (packet["run_id"], decision["candidate_id"]),
            ).fetchone()
            if stored is None or stored["decision_json"] != serialized_decision:
                raise RuntimeError("stored candidate decision conflicts with immutable run")


def artifact_record(workspace: Path, artifact_id: str) -> dict[str, Any] | None:
    database = database_path(workspace)
    if not database.is_file():
        return None
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND market = ?",
            (artifact_id, MARKET),
        ).fetchone()
    return dict(row) if row else None


def _assert_existing_run_matches(
    connection: sqlite3.Connection,
    packet: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    row = connection.execute(
        """
        SELECT artifact_id, market, snapshot_hash, analysis_hash, writer_mode,
               method_id, manifest_hash
        FROM runs WHERE run_id = ?
        """,
        (packet["run_id"],),
    ).fetchone()
    expected = (
        packet["artifact_id"],
        MARKET,
        packet["snapshot_hash"],
        packet["analysis_hash"],
        packet["writer_mode"],
        packet["method_id"],
        manifest["manifest_hash"],
    )
    if row is None or tuple(row) != expected:
        raise RuntimeError("stored run conflicts with immutable research identity")


def _assert_existing_artifact_matches(
    connection: sqlite3.Connection,
    packet: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    row = connection.execute(
        """
        SELECT run_id, market, run_dir, manifest_hash, integrity_hash
        FROM artifacts WHERE artifact_id = ?
        """,
        (packet["artifact_id"],),
    ).fetchone()
    expected = (
        packet["run_id"],
        MARKET,
        manifest["paths"]["run_dir"],
        manifest["manifest_hash"],
        manifest["integrity_hash"],
    )
    if row is None or tuple(row) != expected:
        raise RuntimeError("stored artifact conflicts with immutable research identity")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
