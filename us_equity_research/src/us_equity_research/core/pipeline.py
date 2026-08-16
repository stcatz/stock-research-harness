from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
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
from .utils import (
    fsync_directory,
    sha256_bytes,
    sha256_file,
    sha256_value,
    write_json_atomic,
    write_text_atomic,
)

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


@dataclass(frozen=True)
class VerifiedRun:
    manifest: dict[str, Any]
    summary: dict[str, Any]
    packet: dict[str, Any]
    file_bytes: dict[str, bytes]


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

    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("stored artifact run id is invalid")
    with run_lock(workspace, run_id):
        run_dir = _resolve_recorded_run_dir(workspace, record.get("run_dir"))
        verified = _verify_complete_run(workspace, run_dir)
        _verify_database_record(record, verified.manifest)
        if verified.manifest["artifact_id"] != request.artifact_id:
            raise RuntimeError("artifact id does not match its immutable manifest")

        filename = {
            "summary": "summary.json",
            "report": "report.md",
            "manifest": "manifest.json",
            "packet": "research_packet.json",
        }[request.section]
        content = _decode_utf8(verified.file_bytes[filename], filename)
        relative_path = verified.manifest["paths"][request.section]
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
        "relative_path": relative_path,
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
    artifact_store = _artifact_store(workspace)
    run_dir = artifact_store / packet["run_id"]
    _ensure_direct_child(artifact_store, run_dir)
    existing = _prepare_existing_run(workspace, run_dir)
    if existing is not None:
        return _reuse_verified_run(workspace, existing)

    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{packet['run_id']}.tmp-",
            dir=artifact_store,
        )
    )
    _ensure_direct_child(artifact_store, staging_dir)

    request_payload = request.to_dict()
    stable_manifest = _stable_manifest(request_payload, packet)
    manifest_hash = sha256_value(stable_manifest)
    summary = _build_summary(packet, manifest_hash)
    report = render_report(packet)
    paths = _manifest_paths(workspace, run_dir)

    output_paths = {
        "request.json": staging_dir / "request.json",
        "snapshot.json": staging_dir / "snapshot.json",
        "research_packet.json": staging_dir / "research_packet.json",
        "report.md": staging_dir / "report.md",
        "summary.json": staging_dir / "summary.json",
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
    write_json_atomic(staging_dir / "manifest.json", manifest)

    _verify_complete_run(workspace, staging_dir, logical_run_dir=run_dir)
    _publish_staged_run(artifact_store, staging_dir, run_dir)
    verified = _verify_complete_run(workspace, run_dir)
    record_run(workspace, verified.packet, verified.summary, verified.manifest)
    _write_convenience_report(
        workspace,
        verified.packet,
        _decode_utf8(verified.file_bytes["report.md"], "report.md"),
    )
    return verified.summary


def _reuse_verified_run(workspace: Path, verified: VerifiedRun) -> dict[str, Any]:
    record_run(workspace, verified.packet, verified.summary, verified.manifest)
    report = _decode_utf8(verified.file_bytes["report.md"], "report.md")
    _write_convenience_report(workspace, verified.packet, report)
    return verified.summary


def _prepare_existing_run(workspace: Path, run_dir: Path) -> VerifiedRun | None:
    if not os.path.lexists(run_dir):
        return None
    if run_dir.is_symlink() or not run_dir.is_dir():
        _quarantine_incomplete_run(run_dir)
        return None
    if os.path.lexists(run_dir / "manifest.json"):
        # A published run with a manifest is immutable. Verification failures are
        # surfaced as tampering; this path is never moved or overwritten.
        return _verify_complete_run(workspace, run_dir)
    _quarantine_incomplete_run(run_dir)
    return None


def _quarantine_incomplete_run(run_dir: Path) -> Path:
    artifact_store = run_dir.parent
    _ensure_direct_child(artifact_store, run_dir)
    while True:
        quarantine = artifact_store / f".{run_dir.name}.stale-{uuid.uuid4().hex}"
        if not os.path.lexists(quarantine):
            break
    os.rename(run_dir, quarantine)
    fsync_directory(artifact_store)
    return quarantine


def _publish_staged_run(
    artifact_store: Path,
    staging_dir: Path,
    run_dir: Path,
) -> None:
    _ensure_direct_child(artifact_store, staging_dir)
    _ensure_direct_child(artifact_store, run_dir)
    if os.path.lexists(run_dir):
        raise RuntimeError("immutable run destination appeared during publication")
    os.rename(staging_dir, run_dir)
    fsync_directory(artifact_store)


def _verify_complete_run(
    workspace: Path,
    run_dir: Path,
    *,
    logical_run_dir: Path | None = None,
) -> VerifiedRun:
    workspace = workspace.resolve()
    artifact_store = _artifact_store(workspace)
    _ensure_direct_child(artifact_store, run_dir)
    if logical_run_dir is None:
        logical_run_dir = run_dir
    _ensure_direct_child(artifact_store, logical_run_dir)

    file_bytes = _read_immutable_run_bytes(run_dir)
    manifest = _read_json_bytes(file_bytes["manifest.json"], "manifest")
    if set(manifest) != MANIFEST_FIELDS:
        raise RuntimeError(f"immutable manifest fields mismatch: {logical_run_dir.name}")
    stable_manifest = {key: manifest[key] for key in STABLE_MANIFEST_FIELDS}
    if sha256_value(stable_manifest) != manifest["manifest_hash"]:
        raise RuntimeError(f"immutable manifest hash mismatch: {logical_run_dir.name}")
    if manifest["run_id"] != logical_run_dir.name:
        raise RuntimeError("immutable manifest run id mismatch")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("immutable manifest schema version mismatch")
    if manifest["writer_mode"] != "engine":
        raise RuntimeError("immutable manifest writer mode mismatch")

    file_hashes = _require_object(manifest["files"], "manifest.files")
    if set(file_hashes) != set(IMMUTABLE_FILE_NAMES):
        raise RuntimeError(f"immutable manifest file list mismatch: {logical_run_dir.name}")
    paths = _require_object(manifest["paths"], "manifest.paths")
    if set(paths) != PATH_FIELDS or paths != _manifest_paths(workspace, logical_run_dir):
        raise RuntimeError(f"immutable manifest path map mismatch: {logical_run_dir.name}")
    integrity_payload = {**stable_manifest, "files": file_hashes, "paths": paths}
    if sha256_value(integrity_payload) != manifest["integrity_hash"]:
        raise RuntimeError(f"immutable manifest integrity mismatch: {logical_run_dir.name}")

    for filename in IMMUTABLE_FILE_NAMES:
        if sha256_bytes(file_bytes[filename]) != file_hashes[filename]:
            raise RuntimeError(f"immutable artifact hash mismatch: {filename}")

    request_payload = _read_json_bytes(file_bytes["request.json"], "request")
    snapshot_payload = _read_json_bytes(file_bytes["snapshot.json"], "snapshot")
    packet = _read_json_bytes(file_bytes["research_packet.json"], "research packet")
    summary = _read_json_bytes(file_bytes["summary.json"], "summary")
    report = _decode_utf8(file_bytes["report.md"], "report.md")

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
    return VerifiedRun(
        manifest=manifest,
        summary=summary,
        packet=packet,
        file_bytes=file_bytes,
    )


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
        raise RuntimeError("artifact path escaped the immutable US artifact store")
    candidate = workspace / relative_path
    _ensure_direct_child(_artifact_store(workspace), candidate)
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


def _artifact_store(workspace: Path) -> Path:
    workspace = workspace.resolve()
    artifact_store = workspace / "artifacts" / "us" / "runs"
    if (
        not artifact_store.is_dir()
        or artifact_store.is_symlink()
        or artifact_store.resolve() != artifact_store
    ):
        raise RuntimeError("immutable US artifact store is not a safe directory")
    return artifact_store


def _ensure_direct_child(artifact_store: Path, path: Path) -> None:
    if path.parent != artifact_store or not path.name or path.name in {".", ".."}:
        raise RuntimeError("stored US artifact run directory is invalid")


def _read_immutable_run_bytes(run_dir: Path) -> dict[str, bytes]:
    expected_entries = {*IMMUTABLE_FILE_NAMES, "manifest.json"}
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(run_dir, directory_flags)
    except OSError as exc:
        raise RuntimeError("immutable run directory is invalid") from exc

    try:
        if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
            raise RuntimeError("immutable run directory is invalid")
        if set(os.listdir(directory_descriptor)) != expected_entries:
            raise RuntimeError(f"immutable run file set mismatch: {run_dir.name}")

        result: dict[str, bytes] = {}
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        for filename in (*IMMUTABLE_FILE_NAMES, "manifest.json"):
            try:
                file_descriptor = os.open(
                    filename,
                    file_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise RuntimeError(f"immutable artifact entry is invalid: {filename}") from exc
            try:
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    raise RuntimeError(f"immutable artifact entry is invalid: {filename}")
                with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
                    result[filename] = handle.read()
            finally:
                os.close(file_descriptor)

        if set(os.listdir(directory_descriptor)) != expected_entries:
            raise RuntimeError(
                f"immutable run file set changed during verification: {run_dir.name}"
            )
        return result
    finally:
        os.close(directory_descriptor)


def _read_json_bytes(value: bytes, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_utf8(value, field))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"immutable {field} is not valid JSON") from exc
    return _require_object(decoded, field)


def _decode_utf8(value: bytes, field: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"immutable {field} is not valid UTF-8") from exc


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"immutable {field} must be an object")
    return value
