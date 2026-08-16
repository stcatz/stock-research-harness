from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .locking import run_lock

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE,
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
    status TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    run_dir TEXT NOT NULL,
    summary_path TEXT NOT NULL,
    report_path TEXT NOT NULL,
    packet_path TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('exclude', 'continue_research', 'observe')),
    reasons_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    data_gaps_json TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

PRAGMA user_version = 1;
"""


def initialize_workspace(workspace: Path) -> dict[str, str]:
    workspace = workspace.resolve()
    for relative in (
        "data/normalized",
        "artifacts/runs",
        "reports/daily",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    database = database_path(workspace)
    with run_lock(workspace, "workspace-database"), connect(database) as connection:
        connection.executescript(SCHEMA_SQL)
    return {
        "workspace": str(workspace),
        "database": str(database),
        "schema_version": "1",
    }


def database_path(workspace: Path) -> Path:
    return workspace / "data" / "stock_research.sqlite3"


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
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
    with run_lock(workspace, "workspace-database"), connect(database_path(workspace)) as connection:
        connection.execute(
            """
                INSERT INTO runs (
                    run_id, artifact_id, workflow, subject, symbol, decision_at, generated_at,
                    snapshot_id, snapshot_hash, analysis_hash, data_mode, pit_quality,
                    status, warnings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    warnings_json = excluded.warnings_json
                """,
            (
                packet["run_id"],
                packet["artifact_id"],
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
                summary["status"],
                json.dumps(packet["warnings"], ensure_ascii=False),
                packet["generated_at"],
            ),
        )
        paths = manifest["paths"]
        connection.execute(
            """
                INSERT INTO artifacts (
                    artifact_id, run_id, run_dir, summary_path, report_path,
                    packet_path, manifest_path, manifest_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    manifest_hash = excluded.manifest_hash
                """,
            (
                packet["artifact_id"],
                packet["run_id"],
                paths["run_dir"],
                paths["summary"],
                paths["report"],
                paths["packet"],
                paths["manifest"],
                manifest["manifest_hash"],
            ),
        )
        for decision in packet["all_decisions"]:
            connection.execute(
                """
                    INSERT INTO candidate_decisions (
                        run_id, candidate_id, symbol, name, theme_id, decision,
                        reasons_json, evidence_refs_json, data_gaps_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, candidate_id) DO NOTHING
                    """,
                (
                    packet["run_id"],
                    decision["candidate_id"],
                    decision["symbol"],
                    decision["name"],
                    decision["theme_id"],
                    decision["decision"],
                    json.dumps(decision["reasons"], ensure_ascii=False),
                    json.dumps(decision["usable_evidence_refs"], ensure_ascii=False),
                    json.dumps(decision["data_gaps"], ensure_ascii=False),
                ),
            )


def artifact_record(workspace: Path, artifact_id: str) -> dict[str, str] | None:
    database = database_path(workspace)
    if not database.is_file():
        return None
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
    return dict(row) if row else None
