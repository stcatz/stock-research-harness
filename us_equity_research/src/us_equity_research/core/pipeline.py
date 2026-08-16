from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from .contracts import MARKET, SCHEMA_VERSION, ArtifactReadRequest, RunRequest, parse_datetime
from .engine import build_research_packet
from .locking import run_lock
from .reporting import render_report
from .snapshot import load_snapshot, validate_snapshot
from .storage import artifact_record, database_path, initialize_workspace, record_run
from .utils import read_json, sha256_file, sha256_value, write_json_atomic, write_text_atomic

IMMUTABLE_FILE_NAMES = (
    "request.json",
    "snapshot.json",
    "research_packet.json",
    "report.md",
    "summary.json",
)
MANIFEST_FIELDS = {
    "schema_version",
    "run_id",
    "artifact_id",
    "request_hash",
    "snapshot_hash",
    "analysis_hash",
    "writer_mode",
    "method_id",
    "manifest_hash",
    "files",
    "paths",
    "integrity_hash",
}
PATH_FIELDS = {"run_dir", "request", "snapshot", "packet", "report", "summary", "manifest"}
STABLE_MANIFEST_FIELDS = (
    "schema_version",
    "run_id",
    "artifact_id",
    "request_hash",
    "snapshot_hash",
    "analysis_hash",
    "writer_mode",
    "method_id",
)


def run_research(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    request = RunRequest.from_dict(dict(raw_request))
    workspace = workspace.resolve()
    snapshot = load_snapshot(workspace, request)
    generated_at = datetime.now().astimezone()
    packet = build_research_packet(snapshot, request, generated_at=generated_at)

    with run_lock(workspace, packet["run_id"]):
        initialize_workspace(workspace)
        return _materialize_run(workspace, request, snapshot.data, packet)


def read_artifact(raw_request: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    request = ArtifactReadRequest.from_dict(dict(raw_request))
    workspace = workspace.resolve()
    record = artifact_record(workspace, request.artifact_id)
    if record is None:
        raise KeyError(f"artifact not found: {request.artifact_id}")

    run_dir = _resolve_recorded_run_dir(workspace, record.get("run_dir"))
    manifest, _, _ = _verify_complete_run(workspace, run_dir)
    _verify_database_record(record, manifest)
    if manifest["artifact_id"] != request.artifact_id:
        raise RuntimeError("artifact id does not match its immutable manifest")

    filename = {
        "summary": "summary.json",
        "report": "report.md",
        "manifest": "manifest.json",
        "packet": "research_packet.json",
    }[request.section]
    path = run_dir / filename
    _ensure_inside_artifact_store(workspace, path.resolve())
    content = path.read_text(encoding="utf-8")
    truncated = len(content) > request.max_chars
    if truncated:
        marker = "\n…[truncated]"
        content = content[: request.max_chars - len(marker)] + marker
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "artifact_id": request.artifact_id,
        "section": request.section,
        "content_type": "text/markdown" if request.section == "report" else "application/json",
        "content": content,
        "truncated": truncated,
        "relative_path": path.relative_to(workspace).as_posix(),
    }


def doctor(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    normalized = workspace / "data" / "normalized" / "us"
    snapshots = sorted(
        path.relative_to(workspace).as_posix()
        for path in normalized.glob("*/snapshot.json")
        if path.is_file()
    )
    fixture = resource_files("us_equity_research.fixtures").joinpath("demo_snapshot.json")
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "status": "ok",
        "workspace_exists": workspace.is_dir(),
        "database_initialized": database_path(workspace).is_file(),
        "database": "data/us_stock_research.sqlite3",
        "normalized_snapshot_count": len(snapshots),
        "normalized_snapshots": snapshots[-10:],
        "demo_fixture_available": fixture.is_file(),
        "broker_capability": False,
    }


def _materialize_run(
    workspace: Path,
    request: RunRequest,
    snapshot_data: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    artifact_store = workspace / "artifacts" / "us" / "runs"
    run_dir = artifact_store / packet["run_id"]
    _ensure_run_directory(workspace, run_dir.resolve())
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        return _reuse_complete_run(workspace, run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"incomplete immutable run directory exists: {packet['run_id']}")
    run_dir.mkdir(parents=True, exist_ok=True)

    request_payload = request.to_dict()
    stable_manifest = _stable_manifest(request_payload, packet)
    manifest_hash = sha256_value(stable_manifest)
    summary = _build_summary(packet, manifest_hash)
    report = render_report(packet)
    paths = _manifest_paths(workspace, run_dir)

    output_paths = {
        "request.json": run_dir / "request.json",
        "snapshot.json": run_dir / "snapshot.json",
        "research_packet.json": run_dir / "research_packet.json",
        "report.md": run_dir / "report.md",
        "summary.json": run_dir / "summary.json",
    }
    write_json_atomic(output_paths["request.json"], request_payload)
    write_json_atomic(output_paths["snapshot.json"], snapshot_data)
    write_json_atomic(output_paths["research_packet.json"], packet)
    write_text_atomic(output_paths["report.md"], report)
    write_json_atomic(output_paths["summary.json"], summary)

    file_hashes = {
        filename: sha256_file(output_paths[filename]) for filename in IMMUTABLE_FILE_NAMES
    }
    integrity_hash = sha256_value({**stable_manifest, "files": file_hashes, "paths": paths})
    manifest = {
        **stable_manifest,
        "manifest_hash": manifest_hash,
        "files": file_hashes,
        "paths": paths,
        "integrity_hash": integrity_hash,
    }
    write_json_atomic(manifest_path, manifest)

    verified_manifest, verified_summary, verified_packet = _verify_complete_run(workspace, run_dir)
    record_run(workspace, verified_packet, verified_summary, verified_manifest)
    _write_convenience_report(workspace, verified_packet, report)
    return verified_summary


def _reuse_complete_run(workspace: Path, run_dir: Path) -> dict[str, Any]:
    manifest, summary, packet = _verify_complete_run(workspace, run_dir)
    record_run(workspace, packet, summary, manifest)
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    _write_convenience_report(workspace, packet, report)
    return summary


def _verify_complete_run(
    workspace: Path,
    run_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    workspace = workspace.resolve()
    run_dir = run_dir.resolve()
    _ensure_run_directory(workspace, run_dir)
    expected_entries = {*IMMUTABLE_FILE_NAMES, "manifest.json"}
    if not run_dir.is_dir() or {path.name for path in run_dir.iterdir()} != expected_entries:
        raise RuntimeError(f"immutable run file set mismatch: {run_dir.name}")

    manifest = _require_object(read_json(run_dir / "manifest.json"), "manifest")
    if set(manifest) != MANIFEST_FIELDS:
        raise RuntimeError(f"immutable manifest fields mismatch: {run_dir.name}")
    stable_manifest = {key: manifest[key] for key in STABLE_MANIFEST_FIELDS}
    if sha256_value(stable_manifest) != manifest["manifest_hash"]:
        raise RuntimeError(f"immutable manifest hash mismatch: {run_dir.name}")
    if manifest["run_id"] != run_dir.name:
        raise RuntimeError("immutable manifest run id mismatch")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("immutable manifest schema version mismatch")
    if manifest["writer_mode"] != "engine":
        raise RuntimeError("immutable manifest writer mode mismatch")

    file_hashes = _require_object(manifest["files"], "manifest.files")
    if set(file_hashes) != set(IMMUTABLE_FILE_NAMES):
        raise RuntimeError(f"immutable manifest file list mismatch: {run_dir.name}")
    paths = _require_object(manifest["paths"], "manifest.paths")
    if set(paths) != PATH_FIELDS or paths != _manifest_paths(workspace, run_dir):
        raise RuntimeError(f"immutable manifest path map mismatch: {run_dir.name}")
    integrity_payload = {**stable_manifest, "files": file_hashes, "paths": paths}
    if sha256_value(integrity_payload) != manifest["integrity_hash"]:
        raise RuntimeError(f"immutable manifest integrity mismatch: {run_dir.name}")

    for filename in IMMUTABLE_FILE_NAMES:
        path = (run_dir / filename).resolve()
        _ensure_inside_artifact_store(workspace, path)
        if (
            path.parent != run_dir
            or not path.is_file()
            or sha256_file(path) != file_hashes[filename]
        ):
            raise RuntimeError(f"immutable artifact hash mismatch: {filename}")

    request_payload = _require_object(read_json(run_dir / "request.json"), "request")
    snapshot_payload = _require_object(read_json(run_dir / "snapshot.json"), "snapshot")
    packet = _require_object(read_json(run_dir / "research_packet.json"), "research packet")
    summary = _require_object(read_json(run_dir / "summary.json"), "summary")
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    if sha256_value(request_payload) != manifest["request_hash"]:
        raise RuntimeError("immutable request hash mismatch")
    validated_snapshot = validate_snapshot(snapshot_payload)
    if validated_snapshot.snapshot_hash != manifest["snapshot_hash"]:
        raise RuntimeError("immutable snapshot hash mismatch")
    stored_request = RunRequest.from_dict(request_payload)
    generated_at = parse_datetime(packet.get("generated_at"), "packet.generated_at")
    rebuilt_packet = build_research_packet(
        validated_snapshot,
        stored_request,
        generated_at=generated_at,
    )
    if rebuilt_packet != packet:
        raise RuntimeError("immutable research packet semantic mismatch")
    if any(
        (
            packet["run_id"] != manifest["run_id"],
            packet["artifact_id"] != manifest["artifact_id"],
            packet["snapshot_hash"] != manifest["snapshot_hash"],
            packet["analysis_hash"] != manifest["analysis_hash"],
            packet["writer_mode"] != manifest["writer_mode"],
            packet["method_id"] != manifest["method_id"],
        )
    ):
        raise RuntimeError("immutable packet and manifest identity mismatch")
    if render_report(packet) != report:
        raise RuntimeError("immutable report semantic mismatch")
    if _build_summary(packet, manifest["manifest_hash"]) != summary:
        raise RuntimeError("immutable summary semantic mismatch")
    return manifest, summary, packet


def _stable_manifest(
    request_payload: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": packet["run_id"],
        "artifact_id": packet["artifact_id"],
        "request_hash": sha256_value(request_payload),
        "snapshot_hash": packet["snapshot_hash"],
        "analysis_hash": packet["analysis_hash"],
        "writer_mode": packet["writer_mode"],
        "method_id": packet["method_id"],
    }


def _manifest_paths(workspace: Path, run_dir: Path) -> dict[str, str]:
    return {
        "run_dir": run_dir.relative_to(workspace).as_posix(),
        "request": (run_dir / "request.json").relative_to(workspace).as_posix(),
        "snapshot": (run_dir / "snapshot.json").relative_to(workspace).as_posix(),
        "packet": (run_dir / "research_packet.json").relative_to(workspace).as_posix(),
        "report": (run_dir / "report.md").relative_to(workspace).as_posix(),
        "summary": (run_dir / "summary.json").relative_to(workspace).as_posix(),
        "manifest": (run_dir / "manifest.json").relative_to(workspace).as_posix(),
    }


def _build_summary(packet: dict[str, Any], manifest_hash: str) -> dict[str, Any]:
    counts = {"observe": 0, "continue_research": 0, "exclude": 0}
    for decision in packet["all_decisions"]:
        counts[decision["decision"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "run_id": packet["run_id"],
        "artifact_id": packet["artifact_id"],
        "status": packet["data_status"]["status"],
        "writer_mode": packet["writer_mode"],
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
                "reason": item["reasons"][0] if item["reasons"] else "UNKNOWN",
            }
            for item in packet["focus"]
        ],
        "warnings": packet["warnings"],
        "gaps": packet["data_gaps"],
        "available_sections": ["summary", "report", "manifest", "packet"],
        "manifest_hash": manifest_hash,
    }


def _write_convenience_report(
    workspace: Path,
    packet: dict[str, Any],
    report: str,
) -> None:
    decision_date = parse_datetime(packet["decision_at"], "packet.decision_at").date().isoformat()
    filename = f"{decision_date}-{packet['run_id'].removeprefix('us-run-')}.md"
    write_text_atomic(workspace / "reports" / "us" / "daily" / filename, report)


def _resolve_recorded_run_dir(workspace: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("stored artifact path is invalid")
    relative_path = Path(raw_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        candidate = (workspace / relative_path).resolve()
        _ensure_inside_artifact_store(workspace, candidate)
        raise RuntimeError("stored artifact path is invalid")
    candidate = (workspace / relative_path).resolve()
    _ensure_inside_artifact_store(workspace, candidate)
    return candidate


def _verify_database_record(record: dict[str, Any], manifest: dict[str, Any]) -> None:
    expected = {
        "run_id": manifest["run_id"],
        "market": MARKET,
        "run_dir": manifest["paths"]["run_dir"],
        "request_path": manifest["paths"]["request"],
        "snapshot_path": manifest["paths"]["snapshot"],
        "packet_path": manifest["paths"]["packet"],
        "report_path": manifest["paths"]["report"],
        "summary_path": manifest["paths"]["summary"],
        "manifest_path": manifest["paths"]["manifest"],
        "manifest_hash": manifest["manifest_hash"],
        "integrity_hash": manifest["integrity_hash"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise RuntimeError("stored artifact index conflicts with immutable manifest")


def _ensure_inside_artifact_store(workspace: Path, path: Path) -> None:
    artifact_store = (workspace / "artifacts" / "us" / "runs").resolve()
    try:
        path.relative_to(artifact_store)
    except ValueError as exc:
        raise RuntimeError("artifact path escaped the immutable US artifact store") from exc


def _ensure_run_directory(workspace: Path, run_dir: Path) -> None:
    _ensure_inside_artifact_store(workspace, run_dir)
    artifact_store = (workspace / "artifacts" / "us" / "runs").resolve()
    if run_dir.parent != artifact_store or not run_dir.name:
        raise RuntimeError("stored US artifact run directory is invalid")


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"immutable {field} must be an object")
    return value
