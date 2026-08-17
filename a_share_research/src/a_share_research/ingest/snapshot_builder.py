from __future__ import annotations

import copy
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.contracts import (
    MARKET,
    SCHEMA_VERSION,
    ContractError,
    require_list,
    require_mapping,
    require_string,
)
from ..core.snapshot import validate_snapshot
from ..core.utils import sha256_value
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
_SYNTHETIC_TEXT_MARKERS = (
    "[合成示例]",
    "example_only",
    "synthetic format example",
)


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

    _reject_synthetic_publication_seed(seed)
    normalized_seed, candidate_symbols = _validate_and_copy_seed(seed)
    resolved_workspace = Path(workspace).expanduser().resolve()
    target = _preflight_output_target(resolved_workspace, snapshot_id)
    _reject_public_retrieved_at_override(retrieved_at, provider)

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


def _reject_public_retrieved_at_override(
    retrieved_at: datetime | None, provider: DailyMarketDataProvider | None
) -> None:
    if retrieved_at is not None and provider is None:
        raise CollectionError("retrieved_at override requires an explicitly injected provider")


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
        candidates = require_list(theme.get("candidates"), f"seed.themes[{theme_index}].candidates")
        for candidate_index, raw_candidate in enumerate(candidates):
            prefix = f"seed.themes[{theme_index}].candidates[{candidate_index}]"
            candidate = require_mapping(raw_candidate, prefix)
            existing_refs = candidate.get("market_evidence_refs", [])
            if require_list(existing_refs, f"{prefix}.market_evidence_refs"):
                raise CollectionError(
                    "research seed candidate market_evidence_refs must be absent or empty"
                )
            symbol = require_string(candidate.get("symbol"), f"{prefix}.symbol")
            normalized_symbol = normalize_baostock_symbol(symbol)
            security_id = require_string(candidate.get("security_id"), f"{prefix}.security_id")
            expected_security_id = _security_id_for_baostock_symbol(normalized_symbol)
            if security_id != expected_security_id:
                raise CollectionError(
                    f"{prefix}.security_id must be {expected_security_id} for symbol {symbol}"
                )
            candidate_symbols.append(normalized_symbol)
    if not candidate_symbols:
        raise CollectionError("research seed must contain at least one candidate")
    return data, tuple(candidate_symbols)


def _reject_synthetic_publication_seed(seed: Mapping[str, Any]) -> None:
    data = require_mapping(seed, "seed")
    synthetic_markers = sorted(
        marker for marker in ("example_notice", "fixture_notice") if marker in data
    )
    if synthetic_markers:
        raise CollectionError(
            "synthetic example or fixture seeds cannot publish real snapshots: "
            + ", ".join(synthetic_markers)
        )
    if _contains_synthetic_text(data):
        raise CollectionError("synthetic example content cannot publish a real snapshot")
    for index, raw_evidence in enumerate(require_list(data.get("evidence"), "seed.evidence")):
        item = require_mapping(raw_evidence, f"seed.evidence[{index}]")
        source_url = require_string(item.get("source_url"), f"seed.evidence[{index}].source_url")
        hostname = (urlsplit(source_url).hostname or "").casefold()
        if hostname == "invalid" or hostname.endswith(".invalid"):
            raise CollectionError(
                f"seed.evidence[{index}] uses a reserved synthetic invalid source URL"
            )


def _contains_synthetic_text(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.casefold()
        return any(marker in normalized for marker in _SYNTHETIC_TEXT_MARKERS)
    if isinstance(value, Mapping):
        return any(_contains_synthetic_text(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_synthetic_text(item) for item in value)
    return False


def _assemble_snapshot(seed: dict[str, Any], snapshot_id: str, collection: Any) -> dict[str, Any]:
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
                raise CollectionError(
                    f"collector returned no candidate market evidence for {code}"
                ) from exc
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
            raise CollectionError("snapshot output path must not be a symbolic link")
    target = target_directory / "snapshot.json"
    if target.is_symlink():
        raise CollectionError("snapshot output path must not be a symbolic link")
    if normalized_root.exists() and normalized_root.resolve() != normalized_root:
        raise CollectionError("normalized snapshot path escapes the workspace")
    if target_directory.exists() and target_directory.resolve() != target_directory:
        raise CollectionError("snapshot target path escapes the workspace")
    return target


def _persist_without_overwrite(target: Path, workspace: Path, snapshot: dict[str, Any]) -> bool:
    if os.name != "posix":
        raise CollectionError("snapshot publication requires a POSIX filesystem")

    snapshot_id = target.parent.name
    temporary_name = f".snapshot.{secrets.token_hex(8)}.tmp"
    serialized = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    workspace_fd: int | None = None
    data_fd: int | None = None
    normalized_fd: int | None = None
    snapshot_fd: int | None = None
    file_fd: int | None = None
    created_directory = False
    linked_target = False
    published = False
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_fd = _open_directory(workspace)
        data_fd = _open_or_create_directory(workspace_fd, "data")
        normalized_fd = _open_or_create_directory(data_fd, "normalized")

        try:
            os.mkdir(snapshot_id, mode=0o700, dir_fd=normalized_fd)
            created_directory = True
            _fsync_directory(normalized_fd)
        except FileExistsError:
            snapshot_fd = _open_child_directory(normalized_fd, snapshot_id)
            return _compare_existing_snapshot(snapshot_fd, snapshot)

        snapshot_fd = _open_child_directory(normalized_fd, snapshot_id)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            file_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        file_fd = os.open(temporary_name, file_flags, 0o600, dir_fd=snapshot_fd)
        written = 0
        while written < len(serialized):
            count = os.write(file_fd, serialized[written:])
            if count <= 0:
                raise OSError("snapshot write made no progress")
            written += count
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.link(
            temporary_name,
            "snapshot.json",
            src_dir_fd=snapshot_fd,
            dst_dir_fd=snapshot_fd,
            follow_symlinks=False,
        )
        linked_target = True
        _fsync_directory(snapshot_fd)
        published = True
        return True
    except FileExistsError as exc:
        raise CollectionError("snapshot output already exists") from exc
    except CollectionError:
        raise
    except OSError as exc:
        raise CollectionError("snapshot filesystem operation failed") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if snapshot_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=snapshot_fd)
            except OSError:
                pass
        if created_directory and not published and normalized_fd is not None:
            if linked_target and snapshot_fd is not None:
                try:
                    os.unlink("snapshot.json", dir_fd=snapshot_fd)
                    _fsync_directory(snapshot_fd)
                except OSError:
                    pass
            if snapshot_fd is not None:
                os.close(snapshot_fd)
                snapshot_fd = None
            try:
                os.rmdir(snapshot_id, dir_fd=normalized_fd)
                _fsync_directory(normalized_fd)
            except OSError:
                pass
        for descriptor in (snapshot_fd, normalized_fd, data_fd, workspace_fd):
            if descriptor is not None:
                os.close(descriptor)


def _compare_existing_snapshot(snapshot_fd: int, snapshot: dict[str, Any]) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open("snapshot.json", flags, dir_fd=snapshot_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CollectionError("existing snapshot is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            existing = json.load(handle)
    except CollectionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError("existing snapshot cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if sha256_value(existing) != sha256_value(snapshot):
        raise CollectionError("refusing to overwrite snapshot_id with different content")
    return False


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CollectionError("workspace must be a real directory")
    return descriptor


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise CollectionError("snapshot directory is unsafe") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CollectionError("snapshot directory is unsafe")
    return descriptor


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        _fsync_directory(parent_fd)
    except FileExistsError:
        pass
    return _open_child_directory(parent_fd, name)


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _security_id_for_baostock_symbol(normalized_symbol: str) -> str:
    exchange, symbol = normalized_symbol.split(".", 1)
    return f"CN.{exchange.upper()}.{symbol}"


def _is_safe_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value not in {".", ".."}
        and re.fullmatch(r"[A-Za-z0-9._-]+", value) is not None
    )
