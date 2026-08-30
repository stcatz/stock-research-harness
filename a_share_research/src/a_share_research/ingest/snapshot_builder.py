from __future__ import annotations

import copy
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.contracts import (
    MARKET,
    SCHEMA_VERSION,
    ContractError,
    parse_datetime,
    require_list,
    require_mapping,
    require_string,
)
from ..core.snapshot import validate_snapshot
from ..core.utils import sha256_value
from .baostock import BaoStockProvider
from .hithink_client import (
    HITHINK_PROVIDER_NAME,
    HITHINK_PROVIDER_VERSION,
    HiThinkClient,
)
from .hithink_enrichment import EnrichmentResult, collect_hithink_enrichment
from .hithink_provider import HiThinkProvider
from .market_data import (
    SHANGHAI_TZ,
    CollectionError,
    DailyMarketDataProvider,
    collect_cn_market_data,
    normalize_baostock_symbol,
)
from .raw_store import ImmutableRawStore

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
    provider_kind: str = "baostock",
    raw_store_root: Path | str | None = None,
) -> SnapshotCollectionResult:
    """Collect provider observations and atomically persist one normalized CN snapshot.

    ``seed`` deliberately contains only editorial research data: official/industry evidence,
    themes, and candidates.  Market evidence and generated snapshot metadata are rejected so a
    stale response cannot be relabelled as a fresh collection.  ``provider`` is injectable for
    deterministic tests.  Production uses the explicitly selected provider and never falls back
    to another source after a provider failure.
    """

    _reject_synthetic_publication_seed(seed)
    normalized_seed, candidate_symbols = _validate_and_copy_seed(seed)
    resolved_workspace = Path(workspace).expanduser().resolve()
    target = _preflight_output_target(resolved_workspace, snapshot_id)
    _reject_public_retrieved_at_override(retrieved_at, provider)
    selected_provider = _normalize_provider_kind(provider_kind)
    if selected_provider == "baostock" and (
        raw_store_root is not None or "STOCK_RESEARCH_RAW_STORE" in os.environ
    ):
        raise CollectionError("HiThink raw store configuration requires provider_kind=hithink")

    enrichment: EnrichmentResult | None = None
    hithink_client: HiThinkClient | None = None
    if provider is not None:
        if selected_provider != "baostock":
            raise CollectionError(
                "an injected provider can only be used with provider_kind=baostock"
            )
        active_provider = provider
    elif selected_provider == "baostock":
        active_provider = BaoStockProvider()
    else:
        raw_root = _resolve_hithink_raw_store_root(
            raw_store_root,
            workspace=resolved_workspace,
        )
        raw_store = ImmutableRawStore(raw_root)
        hithink_client = HiThinkClient(raw_store=raw_store)
        active_provider = HiThinkProvider(hithink_client)

    settlement_watchlist = _unsettled_outcome_watchlist(
        resolved_workspace,
        reference_time=retrieved_at or datetime.now(SHANGHAI_TZ),
    )
    collection_symbols = tuple(sorted(set(candidate_symbols).union(settlement_watchlist)))
    collection = collect_cn_market_data(
        collection_symbols,
        provider=active_provider,
        retrieved_at=retrieved_at,
    )
    if hithink_client is not None:
        enrichment = collect_hithink_enrichment(
            hithink_client,
            latest_session=collection.latest_session,
            candidate_thscodes=tuple(
                _hithink_symbol_for_cn_symbol(symbol) for symbol in candidate_symbols
            ),
        )
        if enrichment.latest_session != collection.latest_session:
            raise CollectionError("HiThink enrichment latest_session differs from daily data")
    snapshot = _assemble_snapshot(
        normalized_seed,
        snapshot_id,
        collection,
        enrichment=enrichment,
    )
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


def probe_hithink_provider(
    symbol: str,
    *,
    workspace: Path,
    raw_store_root: Path | str | None = None,
) -> dict[str, Any]:
    """Run one explicit network probe and return only credential-free receipt metadata."""

    # Resolve the workspace up front so an invalid caller path cannot be confused with provider
    # configuration.  Raw responses still live in the independent user-data store.
    resolved_workspace = Path(workspace).expanduser().resolve()
    thscode = _hithink_symbol_for_cn_symbol(normalize_baostock_symbol(symbol))
    raw_root = _resolve_hithink_raw_store_root(
        raw_store_root,
        workspace=resolved_workspace,
    )
    client = HiThinkClient(raw_store=ImmutableRawStore(raw_root))
    response = client.get(
        "/api/a-share/prices/snapshot",
        {"thscodes": thscode},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "provider": {
            "name": HITHINK_PROVIDER_NAME,
            "version": HITHINK_PROVIDER_VERSION,
        },
        "status": "ok",
        "endpoint": response.endpoint,
        "request_id": response.request_id,
        "retrieved_at": response.retrieved_at.isoformat(),
        "body_sha256": response.body_sha256,
        "raw_artifact_id": response.raw_artifact_id,
    }


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
    _validate_complete_editorial_seed(data)
    return data, tuple(candidate_symbols)


def _validate_complete_editorial_seed(seed: Mapping[str, Any]) -> None:
    """Exercise the complete snapshot contract before any provider object is constructed."""

    evidence = require_list(seed.get("evidence"), "seed.evidence")
    retrieved_candidates = [
        parse_datetime(
            require_mapping(item, f"seed.evidence[{index}]").get("retrieved_at"),
            f"seed.evidence[{index}].retrieved_at",
        )
        for index, item in enumerate(evidence)
    ]
    boundary = max(retrieved_candidates, default=datetime.now(UTC)).isoformat()
    validate_snapshot(
        {
            **copy.deepcopy(dict(seed)),
            "snapshot_id": "seed-preflight",
            "data_mode": "snapshot",
            "pit_quality": "RECONSTRUCTED_NON_PIT",
            "as_of": boundary,
            "retrieved_at": boundary,
            "market_context": {
                "regime": "UNKNOWN（seed preflight）",
                "breadth": "UNKNOWN（seed preflight）",
                "liquidity": "UNKNOWN（seed preflight）",
                "calculation_note": "Seed-only contract validation before network access.",
                "evidence_refs": [],
            },
        }
    )


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


def _assemble_snapshot(
    seed: dict[str, Any],
    snapshot_id: str,
    collection: Any,
    *,
    enrichment: EnrichmentResult | None = None,
) -> dict[str, Any]:
    market_fragments = collection.evidence_fragments()
    enrichment_fragments = enrichment.evidence_fragments() if enrichment is not None else []
    enrichment_refs = enrichment.candidate_evidence_refs() if enrichment is not None else {}
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
            if enrichment is not None:
                thscode = _hithink_symbol_for_cn_symbol(code)
                candidate["evidence_refs"] = _unique_strings(
                    [*candidate["evidence_refs"], *enrichment_refs.get(thscode, ())]
                )
            candidates.append(candidate)
        theme["candidates"] = candidates
        themes.append(theme)

    as_of = datetime.combine(collection.latest_session, time(15, 0), tzinfo=SHANGHAI_TZ)
    retrieved = collection.retrieved_at
    market_context = collection.market_context_fragment()
    if enrichment is not None:
        retrieved = max(retrieved, enrichment.retrieved_at)
        enriched_context = copy.deepcopy(enrichment.market_context_fragment())
        enriched_context["calculation_note"] = (
            f"{enriched_context['calculation_note']} "
            f"指数日线补充说明：{market_context['calculation_note']}"
        )
        enriched_context["evidence_refs"] = _unique_strings(
            [*market_context["evidence_refs"], *enriched_context["evidence_refs"]]
        )
        market_context = enriched_context

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "snapshot_id": snapshot_id,
        "data_mode": "snapshot",
        "pit_quality": "RECONSTRUCTED_NON_PIT",
        "as_of": as_of.isoformat(),
        "retrieved_at": retrieved.isoformat(),
        "market_context": market_context,
        "evidence": [
            *copy.deepcopy(seed["evidence"]),
            *market_fragments,
            *enrichment_fragments,
        ],
        "themes": themes,
    }
    if enrichment is not None:
        snapshot["provider_data_gaps"] = {
            code: list(gaps) for code, gaps in enrichment.data_gaps.items()
        }
    return snapshot


def _normalize_provider_kind(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("provider_kind must be a string")
    normalized = value.strip().lower()
    if normalized not in {"baostock", "hithink"}:
        raise CollectionError("provider_kind must be baostock or hithink")
    return normalized


def _unsettled_outcome_watchlist(
    workspace: Path,
    *,
    reference_time: datetime,
) -> tuple[str, ...]:
    """Keep recent removed candidates in market collection until T+20 can settle.

    The watchlist is read-only and bounded.  It adds market evidence to the frozen snapshot but
    never re-adds a company to the editorial candidate set or changes a canonical decision.
    """

    database = workspace / "data" / "stock_research.sqlite3"
    if not database.is_file():
        return ()
    cutoff = reference_time.astimezone(SHANGHAI_TZ) - timedelta(days=90)
    try:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT r.decision_at, c.symbol
                FROM candidate_decisions AS c
                JOIN runs AS r ON r.run_id = c.run_id
                LEFT JOIN decision_outcomes AS outcome
                  ON outcome.run_id = c.run_id AND outcome.candidate_id = c.candidate_id
                GROUP BY r.run_id, c.candidate_id
                HAVING count(outcome.observation_id) < 2
                ORDER BY r.decision_at DESC, c.symbol
                LIMIT 500
                """
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CollectionError("could not read the outcome settlement watchlist") from exc
    symbols: set[str] = set()
    for row in rows:
        try:
            decision_at = parse_datetime(row["decision_at"], "runs.decision_at")
            symbol = normalize_baostock_symbol(row["symbol"])
        except (ContractError, TypeError, ValueError):
            continue
        if decision_at >= cutoff:
            symbols.add(symbol)
    return tuple(sorted(symbols))


def _resolve_hithink_raw_store_root(
    value: Path | str | None,
    *,
    workspace: Path,
) -> Path:
    configured: Path | str | None = value
    if configured is None:
        environment_value = os.environ.get("STOCK_RESEARCH_RAW_STORE")
        if environment_value is not None:
            if not environment_value.strip():
                raise CollectionError("STOCK_RESEARCH_RAW_STORE must not be empty")
            configured = environment_value
    if configured is None:
        configured_path = _default_hithink_raw_store_root()
    else:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            raise CollectionError("HiThink raw store root must be an absolute path")

    resolved = configured_path.resolve()
    repository_root = Path(__file__).resolve().parents[4]
    if _is_within(resolved, workspace.resolve()):
        raise CollectionError("HiThink raw store root must be outside the runtime workspace")
    if _is_within(resolved, repository_root):
        raise CollectionError("HiThink raw store root must be outside the source repository")
    return resolved


def _default_hithink_raw_store_root() -> Path:
    home = Path.home().resolve()
    suffix = Path("stock-research-harness") / "raw" / "cn" / "hithink"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / suffix
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data).expanduser() if local_app_data else home / "AppData" / "Local"
        return base / suffix
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = Path(xdg_data_home).expanduser()
        if not base.is_absolute():
            raise CollectionError("XDG_DATA_HOME must be an absolute path")
    else:
        base = home / ".local" / "share"
    return base / suffix


def _hithink_symbol_for_cn_symbol(symbol: str) -> str:
    normalized = normalize_baostock_symbol(symbol)
    exchange, ticker = normalized.split(".", 1)
    return f"{ticker}.{exchange.upper()}"


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


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
