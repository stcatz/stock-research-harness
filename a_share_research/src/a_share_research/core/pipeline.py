from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import SCHEMA_VERSION, ArtifactReadRequest, RunRequest
from .engine import build_research_packet
from .locking import run_lock
from .reporting import render_report
from .snapshot import load_snapshot
from .storage import artifact_record, initialize_workspace, record_run
from .utils import read_json, sha256_file, sha256_value, write_json_atomic, write_text_atomic


def run_research(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    request = RunRequest.from_dict(raw_request)
    workspace = workspace.resolve()
    snapshot = load_snapshot(workspace, request)
    generated_at = datetime.now().astimezone()
    packet = build_research_packet(snapshot, request, generated_at=generated_at)
    with run_lock(workspace, packet["run_id"]):
        return _materialize_run(workspace, request, snapshot.data, packet)


def _materialize_run(
    workspace: Path,
    request: RunRequest,
    snapshot_data: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    initialize_workspace(workspace)

    run_dir = workspace / "artifacts" / "runs" / packet["run_id"]
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        return _reuse_complete_run(workspace, run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"incomplete immutable run directory already exists: {packet['run_id']}")

    run_dir.mkdir(parents=True, exist_ok=True)
    request_path = run_dir / "request.json"
    snapshot_path = run_dir / "snapshot.json"
    packet_path = run_dir / "research_packet.json"
    report_path = run_dir / "report.md"
    summary_path = run_dir / "summary.json"
    daily_report_path = (
        workspace
        / "reports"
        / "daily"
        / f"{request.decision_at.date().isoformat()}-{packet['run_id'][-12:]}.md"
    )

    report = render_report(packet)
    summary = _build_summary(packet)
    write_json_atomic(request_path, request.to_dict())
    write_json_atomic(snapshot_path, snapshot_data)
    write_json_atomic(packet_path, packet)
    write_text_atomic(report_path, report)
    write_json_atomic(summary_path, summary)
    write_text_atomic(daily_report_path, report)

    relative_run_dir = run_dir.relative_to(workspace).as_posix()
    manifest_stable = {
        "schema_version": SCHEMA_VERSION,
        "run_id": packet["run_id"],
        "artifact_id": packet["artifact_id"],
        "request_hash": sha256_value(request.to_dict()),
        "snapshot_hash": packet["snapshot_hash"],
        "analysis_hash": packet["analysis_hash"],
        "writer_mode": "engine",
        "method_id": packet["method_id"],
    }
    files = {
        "request.json": sha256_file(request_path),
        "snapshot.json": sha256_file(snapshot_path),
        "research_packet.json": sha256_file(packet_path),
        "report.md": sha256_file(report_path),
        "summary.json": sha256_file(summary_path),
    }
    paths = {
        "run_dir": relative_run_dir,
        "summary": summary_path.relative_to(workspace).as_posix(),
        "report": report_path.relative_to(workspace).as_posix(),
        "packet": packet_path.relative_to(workspace).as_posix(),
        "manifest": manifest_path.relative_to(workspace).as_posix(),
        "daily_report": daily_report_path.relative_to(workspace).as_posix(),
    }
    manifest = {
        **manifest_stable,
        # Stable across replays of the same request and frozen snapshot.
        "manifest_hash": sha256_value(manifest_stable),
        "files": files,
        "paths": paths,
        # Covers the actual generated files and path map for tamper detection.
        "integrity_hash": sha256_value({**manifest_stable, "files": files, "paths": paths}),
    }
    write_json_atomic(manifest_path, manifest)
    record_run(workspace, packet, summary, manifest)
    return {**summary, "manifest_hash": manifest["manifest_hash"]}


def read_artifact(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    request = ArtifactReadRequest.from_dict(raw_request)
    workspace = workspace.resolve()
    record = artifact_record(workspace, request.artifact_id)
    if record is None:
        raise KeyError(f"artifact not found: {request.artifact_id}")

    run_dir = (workspace / record["run_dir"]).resolve()
    _ensure_inside_artifact_store(workspace, run_dir)
    manifest, _, _ = _verify_complete_run(workspace, run_dir)
    if manifest.get("artifact_id") != request.artifact_id:
        raise RuntimeError("artifact id does not match its immutable manifest")

    filename = {
        "summary": "summary.json",
        "report": "report.md",
        "manifest": "manifest.json",
        "packet": "research_packet.json",
    }[request.section]
    path = (run_dir / filename).resolve()
    _ensure_inside_artifact_store(workspace, path)
    relative_path = path.relative_to(workspace)
    content = path.read_text(encoding="utf-8")
    truncated = len(content) > request.max_chars
    if truncated:
        marker = "\n…[truncated]"
        content = content[: request.max_chars - len(marker)] + marker
    return {
        "schema_version": SCHEMA_VERSION,
        "market": "CN",
        "artifact_id": request.artifact_id,
        "section": request.section,
        "content_type": "text/markdown" if request.section == "report" else "application/json",
        "content": content,
        "truncated": truncated,
        "relative_path": relative_path.as_posix(),
    }


def doctor(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    initialized = (workspace / "data" / "stock_research.sqlite3").is_file()
    normalized = workspace / "data" / "normalized"
    snapshots = sorted(
        path.relative_to(workspace).as_posix() for path in normalized.glob("*/snapshot.json")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "market": "CN",
        "workspace_exists": workspace.is_dir(),
        "database_initialized": initialized,
        "normalized_snapshot_count": len(snapshots),
        "normalized_snapshots": snapshots[-10:],
        "demo_fixture_available": True,
        "broker_capability": False,
    }


def _build_summary(packet: dict[str, Any]) -> dict[str, Any]:
    counts = {"observe": 0, "continue_research": 0, "exclude": 0}
    for decision in packet["all_decisions"]:
        counts[decision["decision"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "market": "CN",
        "run_id": packet["run_id"],
        "artifact_id": packet["artifact_id"],
        "status": "completed",
        "writer_mode": "engine",
        "data_mode": packet["data_mode"],
        "pit_quality": packet["pit_quality"],
        "workflow": packet["workflow"],
        "decision_at": packet["decision_at"],
        "snapshot_id": packet["snapshot_id"],
        "analysis_hash": packet["analysis_hash"],
        "counts": counts,
        "focus": [
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "theme": item["theme_name"],
                "decision": item["decision"],
                "reason": item["reasons"][0],
            }
            for item in packet["focus"]
        ],
        "warnings": packet["warnings"],
        "gaps": packet["data_gaps"],
        "available_sections": ["summary", "report", "manifest", "packet"],
    }


def _reuse_complete_run(workspace: Path, run_dir: Path) -> dict[str, Any]:
    manifest, summary, packet = _verify_complete_run(workspace, run_dir)
    record_run(workspace, packet, summary, manifest)
    return {**summary, "manifest_hash": manifest["manifest_hash"], "reused": True}


def _verify_complete_run(
    workspace: Path, run_dir: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = read_json(run_dir / "manifest.json")
    stable_manifest = {
        key: manifest[key]
        for key in (
            "schema_version",
            "run_id",
            "artifact_id",
            "request_hash",
            "snapshot_hash",
            "analysis_hash",
            "writer_mode",
            "method_id",
        )
    }
    if sha256_value(stable_manifest) != manifest.get("manifest_hash"):
        raise RuntimeError(f"immutable manifest hash mismatch: {run_dir.name}")
    files = manifest.get("files")
    paths = manifest.get("paths")
    expected_filenames = {
        "request.json",
        "snapshot.json",
        "research_packet.json",
        "report.md",
        "summary.json",
    }
    if not isinstance(files, dict) or set(files) != expected_filenames:
        raise RuntimeError(f"immutable manifest file list mismatch: {run_dir.name}")
    if not isinstance(paths, dict):
        raise TypeError(f"immutable manifest path map must be an object: {run_dir.name}")
    integrity_payload = {**stable_manifest, "files": files, "paths": paths}
    if sha256_value(integrity_payload) != manifest.get("integrity_hash"):
        raise RuntimeError(f"immutable manifest integrity mismatch: {run_dir.name}")
    for filename, expected_hash in files.items():
        path = (run_dir / filename).resolve()
        _ensure_inside_artifact_store(workspace, path)
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise RuntimeError(f"immutable artifact hash mismatch: {filename}")
    summary = read_json(run_dir / "summary.json")
    packet = read_json(run_dir / "research_packet.json")
    if summary.get("analysis_hash") != packet.get("analysis_hash"):
        raise RuntimeError(f"artifact analysis hash mismatch: {run_dir.name}")
    return manifest, summary, packet


def _ensure_inside_artifact_store(workspace: Path, path: Path) -> None:
    artifact_store = (workspace / "artifacts" / "runs").resolve()
    try:
        path.relative_to(artifact_store)
    except ValueError as exc:
        raise RuntimeError("artifact path escaped the immutable artifact store") from exc
