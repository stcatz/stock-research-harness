from __future__ import annotations

import copy
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from ..core.contracts import (
    MARKET,
    SCHEMA_VERSION,
    ContractError,
    require_list,
    require_mapping,
    require_string,
)
from ..core.snapshot import validate_snapshot
from ..core.utils import read_json, sha256_value
from .market_data import (
    SHANGHAI_TZ,
    CollectionError,
    DailyMarketDataProvider,
    collect_cn_market_data,
    normalize_baostock_symbol,
)

_GENERATED_SEED_FIELDS = {
    "as_of",
    "data_mode",
    "market_context",
    "pit_quality",
    "retrieved_at",
    "snapshot_id",
}


@dataclass(frozen=True)
class SnapshotCollectionResult:
    """Stable, path-safe description of a persisted normalized snapshot."""

    snapshot_id: str
    snapshot_hash: str
    relative_path: str
    as_of: str
    retrieved_at: str
    latest_session: str
    provider_name: str
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "market": MARKET,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "relative_path": self.relative_path,
            "data_mode": "snapshot",
            "pit_quality": "RECONSTRUCTED_NON_PIT",
            "as_of": self.as_of,
            "retrieved_at": self.retrieved_at,
            "latest_session": self.latest_session,
            "provider": self.provider_name,
            "created": self.created,
        }


def collect_cn_snapshot(
    seed: Mapping[str, Any],
    *,
    workspace: Path,
    snapshot_id: str,
    retrieved_at: datetime | None = None,
    provider: DailyMarketDataProvider | None = None,
) -> SnapshotCollectionResult:
    """Collect BaoStock observations and atomically persist one normalized CN snapshot.

    ``seed`` deliberately contains only editorial research data: official/industry evidence,
    themes, and candidates.  Market evidence and generated snapshot metadata are rejected so a
    stale response cannot be relabelled as a fresh collection.  ``provider`` is injectable for
    deterministic tests; production lazily constructs the optional BaoStock adapter.
    """

    normalized_seed, candidate_symbols = _validate_and_copy_seed(seed)
    resolved_workspace = Path(workspace).expanduser().resolve()
    target = _preflight_output_target(resolved_workspace, snapshot_id)

    if provider is None:
        from .baostock import BaoStockProvider

        provider = BaoStockProvider()
    collection = collect_cn_market_data(
        candidate_symbols,
        provider=provider,
        retrieved_at=retrieved_at,
    )
    snapshot = _assemble_snapshot(normalized_seed, snapshot_id, collection)
    validated = validate_snapshot(snapshot)
    created = _persist_without_overwrite(target, resolved_workspace, validated.data)
    relative_path = target.relative_to(resolved_workspace).as_posix()
    return SnapshotCollectionResult(
        snapshot_id=validated.snapshot_id,
        snapshot_hash=validated.snapshot_hash,
        relative_path=relative_path,
        as_of=validated.data["as_of"],
        retrieved_at=validated.data["retrieved_at"],
        latest_session=collection.latest_session.isoformat(),
        provider_name=collection.provider_name,
        created=created,
    )


def validate_research_seed(seed: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the non-market seed contract and return an independent normalized copy."""

    normalized, _candidate_symbols = _validate_and_copy_seed(seed)
    return normalized


def _validate_and_copy_seed(seed: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    data = copy.deepcopy(dict(require_mapping(seed, "seed")))
    generated = sorted(_GENERATED_SEED_FIELDS.intersection(data))
    if generated:
        raise CollectionError(
            "research seed must not contain generated snapshot fields: " + ", ".join(generated)
        )
    version = require_string(data.get("schema_version"), "seed.schema_version")
    if version != SCHEMA_VERSION:
        raise ContractError(f"unsupported seed schema_version: {version}")
    if data.get("market") != MARKET:
        raise ContractError("seed.market must be CN")

    evidence = require_list(data.get("evidence"), "seed.evidence")
    for index, raw_evidence in enumerate(evidence):
        item = require_mapping(raw_evidence, f"seed.evidence[{index}]")
        source_level = require_string(
            item.get("source_level"), f"seed.evidence[{index}].source_level"
        )
        category = require_string(item.get("category"), f"seed.evidence[{index}].category")
        if source_level == "structured_market" or category == "market_data":
            raise CollectionError(
                "research seed must not contain market data; it is collected at snapshot time"
            )

    candidate_symbols: list[str] = []
    themes = require_list(data.get("themes"), "seed.themes")
    for theme_index, raw_theme in enumerate(themes):
        theme = require_mapping(raw_theme, f"seed.themes[{theme_index}]")
        candidates = require_list(
            theme.get("candidates"), f"seed.themes[{theme_index}].candidates"
        )
        for candidate_index, raw_candidate in enumerate(candidates):
            prefix = f"seed.themes[{theme_index}].candidates[{candidate_index}]"
            candidate = require_mapping(raw_candidate, prefix)
            existing_refs = candidate.get("market_evidence_refs", [])
            if require_list(existing_refs, f"{prefix}.market_evidence_refs"):
                raise CollectionError(
                    "research seed candidate market_evidence_refs must be absent or empty"
                )
            symbol = require_string(candidate.get("symbol"), f"{prefix}.symbol")
            candidate_symbols.append(normalize_baostock_symbol(symbol))
    if not candidate_symbols:
        raise CollectionError("research seed must contain at least one candidate")
    return data, tuple(candidate_symbols)


def _assemble_snapshot(
    seed: dict[str, Any], snapshot_id: str, collection: Any
) -> dict[str, Any]:
    market_fragments = collection.evidence_fragments()
    candidate_market_refs = {
        fragment["instrument"]["code"]: fragment["evidence_id"]
        for fragment in market_fragments
        if fragment["instrument"]["kind"] == "candidate"
    }
    themes: list[dict[str, Any]] = []
    for raw_theme in seed["themes"]:
        theme = copy.deepcopy(raw_theme)
        candidates: list[dict[str, Any]] = []
        for raw_candidate in theme["candidates"]:
            candidate = copy.deepcopy(raw_candidate)
            code = normalize_baostock_symbol(candidate["symbol"])
            try:
                market_ref = candidate_market_refs[code]
            except KeyError as exc:
                raise CollectionError(f"collector returned no candidate market evidence for {code}") from exc
            candidate["market_evidence_refs"] = [market_ref]
            candidates.append(candidate)
        theme["candidates"] = candidates
        themes.append(theme)

    as_of = datetime.combine(collection.latest_session, time(15, 0), tzinfo=SHANGHAI_TZ)
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "snapshot_id": snapshot_id,
        "data_mode": "snapshot",
        "pit_quality": "RECONSTRUCTED_NON_PIT",
        "as_of": as_of.isoformat(),
        "retrieved_at": collection.retrieved_at.isoformat(),
        "market_context": collection.market_context_fragment(),
        "evidence": [*copy.deepcopy(seed["evidence"]), *market_fragments],
        "themes": themes,
    }


def _preflight_output_target(workspace: Path, snapshot_id: str) -> Path:
    if not _is_safe_identifier(snapshot_id):
        raise CollectionError("snapshot_id contains unsupported characters")
    data_root = workspace / "data"
    normalized_root = data_root / "normalized"
    target_directory = normalized_root / snapshot_id
    for path in (data_root, normalized_root, target_directory):
        if path.is_symlink():
            raise CollectionError(f"snapshot output path must not be a symbolic link: {path}")
    target = target_directory / "snapshot.json"
    if target.is_symlink():
        raise CollectionError(f"snapshot output path must not be a symbolic link: {target}")
    if normalized_root.exists() and normalized_root.resolve() != normalized_root:
        raise CollectionError("normalized snapshot path escapes the workspace")
    if target_directory.exists() and target_directory.resolve() != target_directory:
        raise CollectionError("snapshot target path escapes the workspace")
    return target


def _persist_without_overwrite(target: Path, workspace: Path, snapshot: dict[str, Any]) -> bool:
    _ensure_output_parents(workspace, target.parent.parent)
    if target.parent.exists():
        return _compare_existing_snapshot(target, snapshot)

    try:
        target.parent.mkdir(mode=0o700)
    except FileExistsError:
        return _compare_existing_snapshot(target, snapshot)
    if target.parent.is_symlink() or target.parent.resolve() != target.parent:
        target.parent.rmdir()
        raise CollectionError("snapshot target became a symbolic link or escaped the workspace")

    temporary = target.parent / f".snapshot.{secrets.token_hex(8)}.tmp"
    serialized = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target, follow_symlinks=False)
        try:
            _fsync_directory(target.parent)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        published = True
        return True
    except FileExistsError as exc:
        raise CollectionError(f"snapshot output already exists: {target}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if not published:
            try:
                target.parent.rmdir()
            except OSError:
                pass


def _ensure_output_parents(workspace: Path, normalized_root: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if workspace.is_symlink() or not workspace.is_dir():
        raise CollectionError("workspace must be a real directory")
    current = workspace
    for name in ("data", "normalized"):
        current = current / name
        if current.is_symlink():
            raise CollectionError(f"snapshot output path must not be a symbolic link: {current}")
        current.mkdir(exist_ok=True)
        if not current.is_dir() or current.resolve() != current:
            raise CollectionError(f"snapshot output path escapes the workspace: {current}")
    if current != normalized_root:
        raise CollectionError("normalized snapshot path does not match the workspace")


def _compare_existing_snapshot(target: Path, snapshot: dict[str, Any]) -> bool:
    if target.parent.is_symlink():
        raise CollectionError(f"snapshot output path must not be a symbolic link: {target.parent}")
    if not target.parent.is_dir():
        raise CollectionError(f"snapshot target is not a directory: {target.parent}")
    if target.is_symlink():
        raise CollectionError(f"snapshot output path must not be a symbolic link: {target}")
    if not target.is_file():
        raise CollectionError(f"snapshot target directory already exists without snapshot.json: {target}")
    try:
        existing = read_json(target)
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"existing snapshot cannot be read safely: {target}") from exc
    if sha256_value(existing) != sha256_value(snapshot):
        raise CollectionError(
            f"refusing to overwrite snapshot_id with different content: {target.parent.name}"
        )
    return False


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_safe_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value not in {".", ".."}
        and re.fullmatch(r"[A-Za-z0-9._-]+", value) is not None
    )
