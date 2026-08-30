"""Verified official-document collection and direct-issuer candidate discovery.

The networked model is deliberately limited to proposing source coordinates and literal
quotes.  This module owns every trust-bearing step: source allowlisting, byte retrieval,
immutable first-seen storage, text extraction, quote verification, conservative time semantics,
deterministic fact extraction and direct-issuer candidate compilation.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ..core.contracts import (
    MARKET,
    SCHEMA_VERSION,
    ContractError,
    parse_datetime,
    require_list,
    require_mapping,
    require_string,
)
from ..core.utils import sha256_bytes, sha256_value
from .market_data import CollectionError, normalize_cn_symbol
from .snapshot_builder import validate_research_seed

MAX_DISCOVERY_SOURCES = 20
MAX_DISCOVERY_WINDOW = timedelta(days=7)
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_DOCUMENT_TEXT = 100_000

_COMPANY_HOSTS = {
    "bse.cn",
    "cninfo.com.cn",
    "sse.com.cn",
    "static.cninfo.com.cn",
    "szse.cn",
}
_POLICY_HOSTS = {
    "csrc.gov.cn",
    "gov.cn",
    "miit.gov.cn",
    "mof.gov.cn",
    "ndrc.gov.cn",
    "pbc.gov.cn",
    "samr.gov.cn",
}
_SENSITIVE_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "token",
    "x_api_key",
}
_ALLOWED_MEDIA_TYPES = {"application/pdf", "text/html", "text/plain"}
_DISCOVERY_TOP_LEVEL_FIELDS = {"schema_version", "market", "discovery_window", "sources"}
_SOURCE_FIELDS = {
    "source_url",
    "category",
    "title",
    "title_quote",
    "published_at",
    "published_at_precision",
    "published_at_quote",
    "effective_at",
    "effective_at_quote",
    "issuer",
}
_ISSUER_FIELDS = {"security_id", "symbol", "name", "name_quote", "symbol_quote"}
_EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("periodic_report", ("年度报告", "半年度报告", "季度报告", "年报摘要", "半年报摘要")),
    (
        "regulatory_risk",
        ("立案", "行政处罚", "监管措施", "风险警示", "终止上市", "退市", "纪律处分"),
    ),
    ("order_contract", ("中标", "合同", "订单", "成交通知", "项目预中标")),
    ("financing", ("向特定对象发行", "可转换公司债", "募集资金", "配股", "非公开发行")),
    ("capital_investment", ("投资建设", "项目投资", "对外投资", "扩建", "新建项目")),
    ("merger_acquisition", ("收购", "出售资产", "资产重组", "重大资产", "并购")),
    ("earnings_update", ("业绩预告", "业绩快报", "盈利预测")),
    ("shareholder_action", ("增持", "回购", "减持")),
)
_AUTO_CANDIDATE_EVENT_TYPES = {
    "capital_investment",
    "earnings_update",
    "financing",
    "merger_acquisition",
    "order_contract",
    "regulatory_risk",
    "shareholder_action",
}
_AMOUNT_LABELS = {
    "合同金额": "disclosed_contract_amount_cny",
    "合同总金额": "disclosed_contract_amount_cny",
    "中标金额": "disclosed_award_amount_cny",
    "中标价": "disclosed_award_amount_cny",
    "项目总投资": "disclosed_project_investment_cny",
    "投资金额": "disclosed_investment_amount_cny",
    "交易价格": "disclosed_transaction_price_cny",
    "交易对价": "disclosed_transaction_price_cny",
    "募集资金总额": "disclosed_fundraising_amount_cny",
}
_AMOUNT_PATTERN = re.compile(
    "(" + "|".join(sorted(map(re.escape, _AMOUNT_LABELS), key=len, reverse=True)) + ")"
    r"\s*(?:为|约为|约|达|不超过|合计为|合计)?\s*[:：]?\s*(?:人民币)?\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(亿元|万元|元)"
)


class OfficialDocumentError(CollectionError):
    """Raised when official evidence cannot be accepted safely."""


@dataclass(frozen=True)
class OfficialFetchRequest:
    url: str
    max_bytes: int = MAX_DOCUMENT_BYTES


@dataclass(frozen=True)
class OfficialFetchResponse:
    requested_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    retrieved_at: datetime


class OfficialTransport(Protocol):
    def fetch(self, request: OfficialFetchRequest) -> OfficialFetchResponse: ...


@dataclass(frozen=True)
class OfficialRawArtifact:
    artifact_id: str
    body_sha256: str
    first_seen_at: datetime
    created: bool


@dataclass(frozen=True)
class OfficialCollectionResult:
    seed: dict[str, Any]
    receipt: dict[str, Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class UrllibOfficialTransport:
    """Small bounded HTTP client that never follows redirects."""

    def fetch(self, request: OfficialFetchRequest) -> OfficialFetchResponse:
        source_url = validate_official_url(request.url)
        opener = urllib.request.build_opener(_NoRedirect())
        network_request = urllib.request.Request(
            source_url,
            headers={
                "Accept": "application/pdf,text/html,text/plain;q=0.9",
                "Accept-Encoding": "identity",
                "User-Agent": "stock-research-harness/official-evidence-v1",
            },
            method="GET",
        )
        try:
            with opener.open(network_request, timeout=30) as response:
                status = int(response.status)
                final_url = response.geturl()
                headers = {str(key): str(value) for key, value in response.headers.items()}
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > request.max_bytes:
                    raise OfficialDocumentError("official document exceeds the byte limit")
                body = response.read(request.max_bytes + 1)
        except OfficialDocumentError:
            raise
        except urllib.error.HTTPError as exc:
            raise OfficialDocumentError(f"official source returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise OfficialDocumentError("official source could not be retrieved") from None
        if status != 200:
            raise OfficialDocumentError(f"official source returned HTTP {status}")
        if final_url != source_url:
            raise OfficialDocumentError("official source redirected; submit the exact final URL")
        if len(body) > request.max_bytes:
            raise OfficialDocumentError("official document exceeds the byte limit")
        if not body:
            raise OfficialDocumentError("official document body is empty")
        return OfficialFetchResponse(
            requested_url=source_url,
            final_url=final_url,
            status=status,
            headers=headers,
            body=body,
            retrieved_at=datetime.now(UTC),
        )


class OfficialRawStore:
    """Content-addressed immutable store preserving the first observed official bytes."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise OfficialDocumentError("official raw store root is not a safe directory")
        os.chmod(self.root, 0o700)

    def publish(
        self,
        *,
        source_url: str,
        media_type: str,
        body: bytes,
        retrieved_at: datetime,
    ) -> OfficialRawArtifact:
        canonical_url = validate_official_url(source_url)
        if media_type not in _ALLOWED_MEDIA_TYPES:
            raise OfficialDocumentError("official response media type is unsupported")
        if not body or len(body) > MAX_DOCUMENT_BYTES:
            raise OfficialDocumentError("official document body size is invalid")
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise OfficialDocumentError("official retrieved_at must include a timezone")
        digest = sha256_bytes(body)
        identity = hashlib.sha256(f"{canonical_url}\n{digest}".encode()).hexdigest()
        artifact_id = f"official-{identity[:24]}"
        destination = self.root / artifact_id
        if destination.exists():
            return self._read_existing(destination, artifact_id, canonical_url, media_type, body)

        first_seen = retrieved_at.astimezone(UTC)
        manifest = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "canonical_url": canonical_url,
            "media_type": media_type,
            "body_file": "source.bin",
            "body_bytes": len(body),
            "body_sha256": digest,
            "first_seen_at": first_seen.isoformat(),
        }
        staging = self.root / f".{artifact_id}.tmp-{secrets.token_hex(8)}"
        try:
            staging.mkdir(mode=0o700)
            _write_private_file(staging / "source.bin", body)
            _write_private_file(
                staging / "manifest.json",
                (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )
            os.rename(staging, destination)
            _fsync_directory(self.root)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            if destination.exists():
                return self._read_existing(
                    destination, artifact_id, canonical_url, media_type, body
                )
            raise OfficialDocumentError("official raw artifact could not be published") from None
        return OfficialRawArtifact(artifact_id, digest, first_seen, True)

    def _read_existing(
        self,
        directory: Path,
        artifact_id: str,
        source_url: str,
        media_type: str,
        body: bytes,
    ) -> OfficialRawArtifact:
        try:
            if directory.is_symlink() or not directory.is_dir():
                raise OfficialDocumentError("official raw artifact has an unsafe type")
            manifest_path = directory / "manifest.json"
            body_path = directory / "source.bin"
            if (
                manifest_path.is_symlink()
                or body_path.is_symlink()
                or not manifest_path.is_file()
                or not body_path.is_file()
            ):
                raise OfficialDocumentError("official raw artifact files have an unsafe type")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            stored = body_path.read_bytes()
            first_seen = parse_datetime(manifest.get("first_seen_at"), "first_seen_at")
        except OfficialDocumentError:
            raise
        except (OSError, json.JSONDecodeError, ContractError, TypeError):
            raise OfficialDocumentError("existing official raw artifact is invalid") from None
        expected = {
            "artifact_id": artifact_id,
            "canonical_url": source_url,
            "media_type": media_type,
            "body_bytes": len(body),
            "body_sha256": sha256_bytes(body),
        }
        if any(manifest.get(key) != value for key, value in expected.items()) or stored != body:
            raise OfficialDocumentError("existing official raw artifact failed integrity checks")
        return OfficialRawArtifact(
            artifact_id=artifact_id,
            body_sha256=expected["body_sha256"],
            first_seen_at=first_seen,
            created=False,
        )


def normalize_official_discovery_output(
    raw_text: str,
    *,
    window_start: datetime | str,
    window_end: datetime | str,
) -> dict[str, Any]:
    """Extract exactly one valid discovery contract from an untrusted model response."""

    start = _aware_datetime(window_start, "window_start")
    end = _aware_datetime(window_end, "window_end")
    if start >= end:
        raise ContractError("discovery window_start must be earlier than window_end")
    if end - start > MAX_DISCOVERY_WINDOW:
        raise ContractError("official discovery window must not exceed seven days")

    def validator(candidate: dict[str, Any]) -> dict[str, Any]:
        return _validate_discovery_contract(candidate, start=start, end=end)

    return _extract_one_valid_object(raw_text, validator)


def validate_official_discovery(
    raw: Mapping[str, Any],
    *,
    expected_window_start: datetime | str | None = None,
    expected_window_end: datetime | str | None = None,
) -> dict[str, Any]:
    window = require_mapping(raw.get("discovery_window"), "discovery.discovery_window")
    start = _aware_datetime(window.get("start"), "discovery.discovery_window.start")
    end = _aware_datetime(window.get("end"), "discovery.discovery_window.end")
    if expected_window_start is not None and start != _aware_datetime(
        expected_window_start, "expected_window_start"
    ):
        raise ContractError("discovery window start does not match the requested boundary")
    if expected_window_end is not None and end != _aware_datetime(
        expected_window_end, "expected_window_end"
    ):
        raise ContractError("discovery window end does not match the requested boundary")
    return _validate_discovery_contract(dict(raw), start=start, end=end)


def collect_official_evidence(
    seed: Mapping[str, Any],
    discovery: Mapping[str, Any],
    *,
    workspace: Path,
    raw_store_root: Path | str | None = None,
    transport: OfficialTransport | None = None,
) -> OfficialCollectionResult:
    """Verify proposed sources and compile only direct issuer mappings into a seed."""

    base_seed = validate_research_seed(seed)
    normalized_discovery = validate_official_discovery(discovery)
    active_transport = transport or UrllibOfficialTransport()
    store: OfficialRawStore | None = None
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    evidence_items: list[dict[str, Any]] = []
    generated_themes: list[dict[str, Any]] = []

    for source in normalized_discovery["sources"]:
        if store is None:
            root = resolve_official_raw_store_root(raw_store_root, workspace=workspace)
            store = OfficialRawStore(root)
        source_key = sha256_value(
            {"source_url": source["source_url"], "published_at": source["published_at"]}
        )[:16]
        try:
            response = active_transport.fetch(OfficialFetchRequest(source["source_url"]))
            evidence, theme, artifact, event_type = _accept_source(source, response, store)
            _ensure_seed_source_compatible(base_seed, evidence)
        except (OfficialDocumentError, ContractError, ValueError) as exc:
            rejected.append(
                {
                    "source_key": source_key,
                    "source_url": source["source_url"],
                    "reason": str(exc),
                }
            )
            continue
        evidence_items.append(evidence)
        if theme is not None:
            generated_themes.append(theme)
        accepted.append(
            {
                "source_key": source_key,
                "source_url": source["source_url"],
                "category": source["category"],
                "evidence_id": evidence["evidence_id"],
                "event_type": event_type,
                "issuer_security_id": (
                    source["issuer"]["security_id"] if source["issuer"] is not None else None
                ),
                "candidate_generated": bool(theme and theme["candidates"]),
                "raw_artifact_id": artifact.artifact_id,
                "body_sha256": artifact.body_sha256,
                "first_seen_at": artifact.first_seen_at.isoformat(),
                "retrieved_at": response.retrieved_at.isoformat(),
                "raw_artifact_created": artifact.created,
            }
        )

    compiled = _merge_seed(base_seed, evidence_items, generated_themes)
    validate_research_seed(compiled)
    receipt_stable = {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "contract_version": "official-evidence-v1",
        "discovery_window": normalized_discovery["discovery_window"],
        "proposed_source_count": len(normalized_discovery["sources"]),
        "accepted_source_count": len(accepted),
        "rejected_source_count": len(rejected),
        "generated_candidate_count": sum(bool(item["candidate_generated"]) for item in accepted),
        "policy_lead_count": sum(item["category"] == "policy" for item in accepted),
        "accepted": accepted,
        "rejected": rejected,
        "compiled_seed_sha256": sha256_value(compiled),
        "raw_documents_embedded_in_receipt": False,
    }
    return OfficialCollectionResult(
        seed=compiled,
        receipt={**receipt_stable, "receipt_sha256": sha256_value(receipt_stable)},
    )


def validate_official_url(value: Any, category: str | None = None) -> str:
    raw = require_string(value, "official source_url")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ContractError("official source_url must be an HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ContractError("official source_url must not contain user information")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ContractError("official source_url contains an invalid port") from exc
    if port not in {None, 443}:
        raise ContractError("official source_url may only use the default HTTPS port")
    if parsed.fragment:
        raise ContractError("official source_url must not contain a fragment")
    hostname = parsed.hostname.casefold().rstrip(".")
    allowed = _allowed_host(hostname, category)
    if not allowed:
        raise ContractError("official source_url host is not on the official allowlist")
    for name, _value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = name.casefold().replace("-", "_")
        if lowered in _SENSITIVE_QUERY_NAMES or any(
            marker in lowered for marker in ("token", "secret", "password", "credential")
        ):
            raise ContractError("official source_url contains a sensitive query parameter")
    netloc = hostname if port is None else f"{hostname}:443"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def resolve_official_raw_store_root(
    value: Path | str | None,
    *,
    workspace: Path,
) -> Path:
    configured: Path | str | None = value
    if configured is None:
        environment = os.environ.get("STOCK_RESEARCH_OFFICIAL_RAW_STORE")
        if environment is not None:
            if not environment.strip():
                raise OfficialDocumentError("STOCK_RESEARCH_OFFICIAL_RAW_STORE must not be empty")
            configured = environment
    if configured is None:
        home = Path.home().resolve()
        suffix = Path("stock-research-harness") / "raw" / "cn" / "official"
        if sys.platform == "darwin":
            resolved = home / "Library" / "Application Support" / suffix
        elif os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
            resolved = base / suffix
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            base = Path(xdg).expanduser() if xdg else home / ".local" / "share"
            if not base.is_absolute():
                raise OfficialDocumentError("XDG_DATA_HOME must be an absolute path")
            resolved = base / suffix
    else:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            raise OfficialDocumentError("official raw store root must be an absolute path")
        if configured_path.exists() and configured_path.is_symlink():
            raise OfficialDocumentError("official raw store root must not be a symbolic link")
        resolved = configured_path
    resolved = resolved.resolve()
    repository = Path(__file__).resolve().parents[4]
    if _is_within(resolved, Path(workspace).expanduser().resolve()):
        raise OfficialDocumentError("official raw store root must be outside the runtime workspace")
    if _is_within(resolved, repository):
        raise OfficialDocumentError("official raw store root must be outside the source repository")
    return resolved


def _validate_discovery_contract(
    raw: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    _reject_unknown_fields(raw, _DISCOVERY_TOP_LEVEL_FIELDS, "discovery")
    if require_string(raw.get("schema_version"), "discovery.schema_version") != SCHEMA_VERSION:
        raise ContractError("unsupported discovery schema_version")
    if raw.get("market") != MARKET:
        raise ContractError("discovery.market must be CN")
    window = require_mapping(raw.get("discovery_window"), "discovery.discovery_window")
    _reject_unknown_fields(window, {"start", "end"}, "discovery.discovery_window")
    submitted_start = parse_datetime(window.get("start"), "discovery.discovery_window.start")
    submitted_end = parse_datetime(window.get("end"), "discovery.discovery_window.end")
    if submitted_start != start or submitted_end != end:
        raise ContractError("discovery window does not match the requested exact boundary")
    if start >= end or end - start > MAX_DISCOVERY_WINDOW:
        raise ContractError("discovery window is invalid")

    sources = require_list(raw.get("sources"), "discovery.sources")
    if len(sources) > MAX_DISCOVERY_SOURCES:
        raise ContractError(f"discovery.sources must contain at most {MAX_DISCOVERY_SOURCES} items")
    normalized_sources: list[dict[str, Any]] = []
    urls: set[str] = set()
    for index, raw_source in enumerate(sources):
        prefix = f"discovery.sources[{index}]"
        source = require_mapping(raw_source, prefix)
        _reject_unknown_fields(source, _SOURCE_FIELDS, prefix)
        category = require_string(source.get("category"), f"{prefix}.category")
        if category not in {"company_disclosure", "policy"}:
            raise ContractError(f"{prefix}.category must be company_disclosure or policy")
        source_url = validate_official_url(source.get("source_url"), category)
        if source_url in urls:
            raise ContractError("discovery.sources contains duplicate source_url values")
        urls.add(source_url)
        title = _bounded_text(source.get("title"), f"{prefix}.title", 500)
        title_quote = _bounded_text(source.get("title_quote"), f"{prefix}.title_quote", 500)
        published = parse_datetime(source.get("published_at"), f"{prefix}.published_at")
        if not start <= published <= end:
            raise ContractError(f"{prefix}.published_at is outside the discovery window")
        precision = require_string(
            source.get("published_at_precision"), f"{prefix}.published_at_precision"
        )
        if precision not in {"date", "datetime"}:
            raise ContractError(f"{prefix}.published_at_precision must be date or datetime")
        if published.utcoffset() != timedelta(hours=8):
            raise ContractError(f"{prefix}.published_at must use the Asia/Shanghai +08:00 offset")
        if precision == "date" and any(
            (published.hour, published.minute, published.second, published.microsecond)
        ):
            raise ContractError(f"{prefix}.published_at must be local midnight for date precision")
        if precision == "datetime" and (published.second or published.microsecond):
            raise ContractError(f"{prefix}.published_at datetime precision is bounded to minutes")
        published_quote = _bounded_text(
            source.get("published_at_quote"), f"{prefix}.published_at_quote", 500
        )
        effective_raw = source.get("effective_at")
        effective = (
            parse_datetime(effective_raw, f"{prefix}.effective_at")
            if effective_raw is not None
            else None
        )
        if effective is not None and effective.utcoffset() != timedelta(hours=8):
            raise ContractError(f"{prefix}.effective_at must use the Asia/Shanghai +08:00 offset")
        effective_quote_raw = source.get("effective_at_quote")
        if (effective is None) != (effective_quote_raw is None):
            raise ContractError(f"{prefix}.effective_at and effective_at_quote must be paired")
        effective_quote = (
            _bounded_text(effective_quote_raw, f"{prefix}.effective_at_quote", 500)
            if effective_quote_raw is not None
            else None
        )
        issuer_raw = source.get("issuer")
        if category == "policy":
            if issuer_raw is not None:
                raise ContractError(f"{prefix}.issuer must be null for policy sources")
            issuer = None
        else:
            issuer_mapping = require_mapping(issuer_raw, f"{prefix}.issuer")
            _reject_unknown_fields(issuer_mapping, _ISSUER_FIELDS, f"{prefix}.issuer")
            symbol = require_string(issuer_mapping.get("symbol"), f"{prefix}.issuer.symbol")
            normalized_symbol = normalize_cn_symbol(symbol)
            exchange, digits = normalized_symbol.split(".", 1)
            security_id = require_string(
                issuer_mapping.get("security_id"), f"{prefix}.issuer.security_id"
            )
            expected_security_id = f"CN.{exchange.upper()}.{digits}"
            if security_id != expected_security_id:
                raise ContractError(f"{prefix}.issuer.security_id must be {expected_security_id}")
            issuer = {
                "security_id": security_id,
                "symbol": digits,
                "name": _bounded_text(issuer_mapping.get("name"), f"{prefix}.issuer.name", 100),
                "name_quote": _bounded_text(
                    issuer_mapping.get("name_quote"), f"{prefix}.issuer.name_quote", 300
                ),
                "symbol_quote": _bounded_text(
                    issuer_mapping.get("symbol_quote"), f"{prefix}.issuer.symbol_quote", 300
                ),
            }
        normalized_sources.append(
            {
                "source_url": source_url,
                "category": category,
                "title": title,
                "title_quote": title_quote,
                "published_at": published.isoformat(),
                "published_at_precision": precision,
                "published_at_quote": published_quote,
                "effective_at": effective.isoformat() if effective is not None else None,
                "effective_at_quote": effective_quote,
                "issuer": issuer,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "discovery_window": {"start": start.isoformat(), "end": end.isoformat()},
        "sources": normalized_sources,
    }


def _accept_source(
    source: dict[str, Any],
    response: OfficialFetchResponse,
    store: OfficialRawStore,
) -> tuple[dict[str, Any], dict[str, Any] | None, OfficialRawArtifact, str]:
    expected_url = validate_official_url(source["source_url"], source["category"])
    if response.status != 200:
        raise OfficialDocumentError(f"official source returned HTTP {response.status}")
    if response.requested_url != expected_url or response.final_url != expected_url:
        raise OfficialDocumentError("official source URL changed during retrieval")
    if response.retrieved_at.tzinfo is None or response.retrieved_at.utcoffset() is None:
        raise OfficialDocumentError("official response retrieved_at must include a timezone")
    if not response.body or len(response.body) > MAX_DOCUMENT_BYTES:
        raise OfficialDocumentError("official response body size is invalid")
    media_type = _media_type(response.headers)
    artifact = store.publish(
        source_url=expected_url,
        media_type=media_type,
        body=response.body,
        retrieved_at=response.retrieved_at,
    )
    text, extraction_method = _extract_document_text(response.body, media_type, response.headers)
    _verify_source_quotes(source, text, artifact.first_seen_at)
    event_type = _classify_event(source["category"], source["title"])
    facts = _extract_labeled_amounts(
        text,
        evidence_seed=f"{expected_url}|{artifact.body_sha256}",
        published_at=(
            parse_datetime(source["published_at"], "source.published_at")
            if source["published_at_precision"] == "datetime"
            else artifact.first_seen_at
        ),
        available_at=artifact.first_seen_at,
        effective_at=(
            parse_datetime(source["effective_at"], "source.effective_at")
            if source["effective_at"] is not None
            else None
        ),
    )
    published = (
        parse_datetime(source["published_at"], "source.published_at")
        if source["published_at_precision"] == "datetime"
        else artifact.first_seen_at
    )
    effective = (
        parse_datetime(source["effective_at"], "source.effective_at")
        if source["effective_at"] is not None
        else published
    )
    evidence_id = (
        "OFFICIAL-"
        + hashlib.sha256(f"{expected_url}\n{artifact.body_sha256}".encode())
        .hexdigest()[:20]
        .upper()
    )
    amount_note = ""
    if facts:
        amount_note = f"；核验到 {len(facts)} 个带明确标签的人民币金额字段"
    evidence = {
        "evidence_id": evidence_id,
        "category": source["category"],
        "source_level": "official",
        "title": source["title"],
        "source_url": expected_url,
        "published_at": published.isoformat(),
        "effective_at": effective.isoformat(),
        "available_at": artifact.first_seen_at.isoformat(),
        # Canonical evidence retains the first observation time. Later byte-identical
        # retrievals are recorded by the collection receipt, not allowed to mutate identity.
        "retrieved_at": artifact.first_seen_at.isoformat(),
        "as_of": published.isoformat(),
        "summary": _evidence_summary(source, event_type) + amount_note + "。",
        "facts": facts,
        "document": {
            "source_document_id": artifact.artifact_id,
            "media_type": media_type,
            "extraction_method": extraction_method,
            "text": text,
            "text_sha256": sha256_bytes(text.encode("utf-8")),
        },
        "official_provenance": {
            "body_sha256": artifact.body_sha256,
            "first_seen_at": artifact.first_seen_at.isoformat(),
            "declared_published_at": source["published_at"],
            "published_at_precision": source["published_at_precision"],
            "time_semantics": (
                "official_datetime_verified"
                if source["published_at_precision"] == "datetime"
                else "date_only_first_seen_conservative"
            ),
            "issuer_security_id": (
                source["issuer"]["security_id"] if source["issuer"] is not None else None
            ),
        },
    }
    theme = _theme_from_verified_source(source, evidence, event_type, facts)
    return evidence, theme, artifact, event_type


def _verify_source_quotes(
    source: Mapping[str, Any],
    text: str,
    first_seen_at: datetime,
) -> None:
    normalized_text = _normalize_text(text)
    for field in ("title_quote", "published_at_quote"):
        quote = _normalize_text(require_string(source.get(field), f"source.{field}"))
        if quote not in normalized_text:
            raise OfficialDocumentError(f"{field} was not found in the official document")
    if _normalize_text(source["title"]) != _normalize_text(source["title_quote"]):
        raise OfficialDocumentError("title must exactly match title_quote after normalization")
    published = parse_datetime(source["published_at"], "source.published_at")
    _verify_date_quote(source["published_at_quote"], published, source["published_at_precision"])
    if published > first_seen_at:
        raise OfficialDocumentError("claimed publication time is later than first_seen_at")
    effective = source.get("effective_at")
    if effective is not None:
        effective_at = parse_datetime(effective, "source.effective_at")
        quote = _normalize_text(source["effective_at_quote"])
        if quote not in normalized_text:
            raise OfficialDocumentError("effective_at_quote was not found in the official document")
        _verify_date_quote(source["effective_at_quote"], effective_at, "date")
    issuer = source.get("issuer")
    if issuer is not None:
        name_quote = _normalize_text(issuer["name_quote"])
        symbol_quote = _normalize_text(issuer["symbol_quote"])
        if name_quote not in normalized_text or symbol_quote not in normalized_text:
            raise OfficialDocumentError("issuer quotes were not found in the official document")
        if _normalize_text(issuer["name"]) != name_quote:
            raise OfficialDocumentError("issuer name must exactly match name_quote")
        if issuer["symbol"] not in re.sub(r"\s+", "", symbol_quote):
            raise OfficialDocumentError("issuer symbol_quote does not contain the declared symbol")


def _verify_date_quote(quote: str, value: datetime, precision: str) -> None:
    normalized = unicodedata.normalize("NFKC", quote)
    date_patterns = {
        value.strftime("%Y-%m-%d"),
        value.strftime("%Y/%m/%d"),
        f"{value.year}年{value.month}月{value.day}日",
        f"{value.year}.{value.month:02d}.{value.day:02d}",
    }
    compact = re.sub(r"\s+", "", normalized)
    if not any(token in compact for token in date_patterns):
        raise OfficialDocumentError("date quote does not contain the declared calendar date")
    if precision == "datetime":
        time_tokens = {value.strftime("%H:%M"), value.strftime("%H时%M分")}
        if not any(token in compact for token in time_tokens):
            raise OfficialDocumentError("datetime quote does not contain the declared minute")


def _extract_document_text(
    body: bytes,
    media_type: str,
    headers: Mapping[str, str],
) -> tuple[str, str]:
    if media_type == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise OfficialDocumentError(
                "PDF evidence requires the optional 'official' dependency"
            ) from None
        try:
            reader = PdfReader(io.BytesIO(body))
            if reader.is_encrypted:
                raise OfficialDocumentError("encrypted official PDFs are unsupported")
            if len(reader.pages) > 300:
                raise OfficialDocumentError("official PDF exceeds the page limit")
            parts = [page.extract_text() or "" for page in reader.pages]
        except OfficialDocumentError:
            raise
        except Exception:  # noqa: BLE001 - third-party PDF parsers expose no stable base error
            raise OfficialDocumentError("official PDF text layer could not be extracted") from None
        text = "\n".join(parts).strip()
        method = "pdf_text_layer"
    else:
        decoded = _decode_text(body, headers)
        if media_type == "text/html":
            parser = _VisibleTextParser()
            try:
                parser.feed(decoded)
                parser.close()
            except Exception:  # noqa: BLE001 - normalize parser failures at the trust boundary
                raise OfficialDocumentError("official HTML could not be parsed") from None
            text = parser.text()
            method = "html_text"
        else:
            text = decoded.strip()
            method = "manual_verified"
    if not text:
        raise OfficialDocumentError("official document has no extractable text layer")
    if len(text) > MAX_DOCUMENT_TEXT:
        raise OfficialDocumentError("official extracted text exceeds 100000 characters")
    return text, method


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "\n".join(part.strip() for part in self._parts if part.strip())


def _decode_text(body: bytes, headers: Mapping[str, str]) -> str:
    content_type = _header(headers, "content-type")
    charset_match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
    candidates = [charset_match.group(1)] if charset_match else []
    candidates.extend(["utf-8", "gb18030"])
    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise OfficialDocumentError("official text encoding is unsupported")


def _media_type(headers: Mapping[str, str]) -> str:
    raw = _header(headers, "content-type")
    media_type = raw.split(";", 1)[0].strip().casefold()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise OfficialDocumentError("official response media type is unsupported")
    encoding = _header(headers, "content-encoding").strip().casefold()
    if encoding not in {"", "identity"}:
        raise OfficialDocumentError("compressed official responses are unsupported")
    return media_type


def _extract_labeled_amounts(
    text: str,
    *,
    evidence_seed: str,
    published_at: datetime,
    available_at: datetime,
    effective_at: datetime | None,
) -> list[dict[str, Any]]:
    normalized = _normalize_text(text)
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    multiplier = {"元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000)}
    for match in _AMOUNT_PATTERN.finditer(normalized):
        label, raw_value, unit = match.groups()
        try:
            value = Decimal(raw_value) * multiplier[unit]
        except (InvalidOperation, KeyError):
            continue
        decimal_value = format(value, "f")
        metric = _AMOUNT_LABELS[label]
        key = (metric, decimal_value)
        if key in seen:
            continue
        seen.add(key)
        source_field = match.group(0)
        fact_id = (
            "FACT-OFFICIAL-"
            + hashlib.sha256(f"{evidence_seed}|{metric}|{decimal_value}|{source_field}".encode())
            .hexdigest()[:16]
            .upper()
        )
        facts.append(
            {
                "fact_id": fact_id,
                "metric": metric,
                "unit": "CNY",
                "source_field": source_field,
                "status": "observed",
                "value": decimal_value,
                "as_of": published_at.isoformat(),
                "effective_at": (effective_at or published_at).isoformat(),
                "available_at": available_at.isoformat(),
            }
        )
        if len(facts) >= 10:
            break
    return facts


def _theme_from_verified_source(
    source: dict[str, Any],
    evidence: dict[str, Any],
    event_type: str,
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    evidence_id = evidence["evidence_id"]
    issuer = source["issuer"]
    if source["category"] == "policy":
        return _lead_theme(source, evidence_id, "policy")
    if event_type not in _AUTO_CANDIDATE_EVENT_TYPES:
        return _lead_theme(source, evidence_id, event_type)
    if issuer is None:  # guarded by the discovery contract
        return None

    amount = facts[0]["value"] if facts else None
    amount_reason = (
        f"原文中核验到带明确标签的人民币金额 {amount} 元；缺少公司收入/利润分母。"
        if amount is not None
        else "原文未核验到可用于量级计算的带标签人民币金额，经济影响保持 UNKNOWN。"
    )
    is_risk = event_type == "regulatory_risk"
    theme_id = (
        "theme-official-"
        + hashlib.sha256(
            f"{issuer['security_id']}|{evidence_id}|{event_type}".encode()
        ).hexdigest()[:16]
    )
    dimensions = {
        "big": _dimension("unknown", "缺少相对公司收入、利润或行业规模的分母。", [evidence_id]),
        "new": _dimension("medium", "该正式文件在本系统本次窗口内首次核验。", [evidence_id]),
        "many": _dimension("unknown", "尚未建立同行扩散与市场宽度证据。", [evidence_id]),
        "durable": _dimension("unknown", "单份正式文件不能证明影响持续期。", [evidence_id]),
        "timely": _dimension(
            "medium", "文件发布时间和 first-seen 已冻结，市场阶段仍待行情验证。", [evidence_id]
        ),
    }
    candidate = {
        "security_id": issuer["security_id"],
        "symbol": issuer["symbol"],
        "name": issuer["name"],
        "role": "core",
        "thesis": (
            f"正式文件直接指向公告主体，事件类型为 {event_type}；仅建立直接主体研究任务，"
            "不推断上下游受益公司。"
        ),
        "counter_thesis": "事件可能不生效、兑现周期过长，或不能转化为可确认的收入、利润及现金流。",
        "opportunity_profile": {
            "new_information": {
                "assessment": "medium",
                "reason": "新增信息来自已冻结并逐字核验的正式原文。",
                "evidence_refs": [evidence_id],
            },
            "economic_impact": {
                "assessment": "medium" if amount is not None and not is_risk else "unknown",
                "reason": amount_reason,
                "evidence_refs": [evidence_id],
                "magnitude": {
                    "status": "partial" if amount is not None else "unknown",
                    "basis": "other" if amount is not None else "unknown",
                    "numerator": amount,
                    "denominator": None,
                    "ratio": None,
                    "formula": (
                        "official_labeled_amount_cny / company_financial_denominator_UNKNOWN"
                        if amount is not None
                        else "UNKNOWN"
                    ),
                },
            },
            "expectation_gap": {
                "assessment": "unknown",
                "baseline": "none",
                "reason": "官方文件不提供市场一致预期或公告前市场隐含基线。",
                "evidence_refs": [],
            },
            "market_pricing": {
                "assessment": "unknown",
                "reason": "由 collect-snapshot 冻结同期结构化行情后确定性评估。",
                "evidence_refs": [],
            },
            "next_catalyst": {
                "description": "正式文件未明确给出可核验的下一催化时点。",
                "scheduled_at": None,
                "verification_rule": "仅在后续正式来源给出具体日期和可观察事项后更新。",
                "evidence_refs": [],
            },
        },
        "impact_chain": [
            {
                "step": f"正式文件确认 {event_type} 事件",
                "status": "supported",
                "evidence_refs": [evidence_id],
            },
            {
                "step": "事件对公告主体的订单、资产、成本或经营约束产生直接影响",
                "status": "partial",
                "evidence_refs": [evidence_id],
            },
            {
                "step": "直接影响进入公司收入、利润或经营现金流",
                "status": "unknown",
                "evidence_refs": [],
            },
        ],
        "evidence_refs": [evidence_id],
        "market_evidence_refs": [],
        "invalidation_conditions": [
            "正式文件被更正、撤回或其生效条件未满足",
            "后续正式披露未显示事件进入收入、利润或经营现金流",
        ],
        "data_gaps": [
            "缺少相对收入、利润或现金流的可比量级分母",
            "缺少公告前一致预期或市场隐含基线",
            "缺少有正式日期支持的下一催化",
        ],
        "risk_flags": ["REGULATORY_MAJOR"] if is_risk else [],
        "manual_review_items": [
            "复核生效条件、交易对手、履约期限及收入确认条款",
            "使用当时可得财务口径计算金额相对收入、利润或现金流的比例",
        ],
    }
    return {
        "theme_id": theme_id,
        "name": f"{issuer['name']}：{source['title']}",
        "event_type": event_type,
        "stage": "organizing",
        "dimensions": dimensions,
        "evidence_refs": [evidence_id],
        "transmission_chain": [
            "正式文件确认事件",
            "事件映射公告主体的订单、资产、成本或经营约束",
            "公司影响进入收入、利润或现金流",
        ],
        "next_catalyst_at": None,
        "counter_thesis": "正式事件不等于可持续经济收益，后续兑现和市场预期差均未验证。",
        "invalidation_conditions": [
            "正式文件失效或被更正",
            "事件未产生可验证的公司财务影响",
        ],
        "data_gaps": ["行业扩散、经济量级分母、预期差和下一催化仍待核验"],
        "candidates": [candidate],
    }


def _lead_theme(source: dict[str, Any], evidence_id: str, event_type: str) -> dict[str, Any]:
    theme_id = (
        "lead-official-"
        + hashlib.sha256(f"{source['source_url']}|{evidence_id}".encode()).hexdigest()[:16]
    )
    label = "政策线索" if source["category"] == "policy" else "公告线索"
    return {
        "theme_id": theme_id,
        "name": f"{label}：{source['title']}",
        "event_type": event_type,
        "stage": "organizing",
        "dimensions": {
            "big": _dimension("unknown", "原文不足以量化影响规模。", [evidence_id]),
            "new": _dimension("medium", "正式原文在本次窗口内首次核验。", [evidence_id]),
            "many": _dimension("unknown", "未从政策或一般公告自动推断受益公司。", [evidence_id]),
            "durable": _dimension("unknown", "影响持续期尚无正式证据。", [evidence_id]),
            "timely": _dimension("medium", "发布时间及 first-seen 已冻结。", [evidence_id]),
        },
        "evidence_refs": [evidence_id],
        "transmission_chain": [
            "正式文件发布",
            "产业或公司约束发生可观察变化",
            "变化映射至具名公司收入、利润或现金流",
        ],
        "next_catalyst_at": None,
        "counter_thesis": "文件可能没有可投资的公司级经济影响。",
        "invalidation_conditions": ["后续正式证据显示文件未生效或没有可观察影响"],
        "data_gaps": ["缺少原文直接点名且可核验的公司受益映射"],
        "candidates": [],
    }


def _merge_seed(
    seed: dict[str, Any],
    evidence_items: list[dict[str, Any]],
    themes: list[dict[str, Any]],
) -> dict[str, Any]:
    result = copy.deepcopy(seed)
    evidence_by_id = {item["evidence_id"]: item for item in result.get("evidence", [])}
    evidence_urls = {item.get("source_url") for item in evidence_by_id.values()}
    for evidence in evidence_items:
        existing = evidence_by_id.get(evidence["evidence_id"])
        if existing is not None:
            if existing != evidence:
                raise OfficialDocumentError("official evidence_id collides with different content")
            continue
        if evidence["source_url"] in evidence_urls:
            raise OfficialDocumentError(
                "seed already contains the official URL with a different frozen document"
            )
        result.setdefault("evidence", []).append(evidence)
        evidence_by_id[evidence["evidence_id"]] = evidence
        evidence_urls.add(evidence["source_url"])

    theme_by_id = {theme["theme_id"]: theme for theme in result.get("themes", [])}
    for theme in themes:
        existing = theme_by_id.get(theme["theme_id"])
        if existing is not None:
            if existing != theme:
                raise OfficialDocumentError("generated theme_id collides with different content")
            continue
        result.setdefault("themes", []).append(theme)
        theme_by_id[theme["theme_id"]] = theme
    return result


def _ensure_seed_source_compatible(
    seed: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    for existing in seed.get("evidence", []):
        if (
            not isinstance(existing, Mapping)
            or existing.get("source_url") != evidence["source_url"]
        ):
            continue
        if existing.get("evidence_id") == evidence["evidence_id"] and dict(existing) == dict(
            evidence
        ):
            return
        raise OfficialDocumentError(
            "seed already contains the official URL with a different frozen document"
        )


def _classify_event(category: str, title: str) -> str:
    if category == "policy":
        return "policy"
    normalized = _normalize_text(title)
    for event_type, keywords in _EVENT_RULES:
        if any(keyword in normalized for keyword in keywords):
            return event_type
    return "other_disclosure"


def _evidence_summary(source: Mapping[str, Any], event_type: str) -> str:
    issuer = source.get("issuer")
    if issuer is None:
        return f"已从允许的政府或监管官方域名核验政策原文，事件类型为 {event_type}"
    return (
        f"已从允许的法定披露域名核验 {issuer['name']}（{issuer['symbol']}）原文，"
        f"事件类型为 {event_type}"
    )


def _dimension(assessment: str, reason: str, refs: list[str]) -> dict[str, Any]:
    return {"assessment": assessment, "reason": reason, "evidence_refs": refs}


def _allowed_host(hostname: str, category: str | None) -> bool:
    if category == "company_disclosure":
        roots = _COMPANY_HOSTS
    elif category == "policy":
        roots = _POLICY_HOSTS
    else:
        roots = _COMPANY_HOSTS.union(_POLICY_HOSTS)
    return any(hostname == root or hostname.endswith("." + root) for root in roots)


def _header(headers: Mapping[str, str], name: str) -> str:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value)
    return ""


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", require_string(value, "quoted text"))
    return re.sub(r"\s+", " ", text).strip()


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    text = require_string(value, field)
    if len(text) > maximum or any(
        ord(character) < 32 and character not in "\t\n\r" for character in text
    ):
        raise ContractError(f"{field} is too long or contains control characters")
    return text


def _aware_datetime(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ContractError(f"{field} must include a timezone")
        return parsed
    return parse_datetime(value, field)


def _extract_one_valid_object(
    text: str,
    validator: Any,
) -> dict[str, Any]:
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise ContractError("official discovery output must be bounded text")
    decoder = json.JSONDecoder()
    matches: list[dict[str, Any]] = []
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _offset = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        try:
            normalized = validator(copy.deepcopy(value))
        except (ContractError, KeyError, TypeError, ValueError):
            continue
        matches.append(normalized)
    if len(matches) != 1:
        raise ContractError(
            "official discovery output must contain exactly one valid contract object"
        )
    return matches[0]


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{field} contains unsupported fields: " + ", ".join(unknown))


def _write_private_file(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(body):
            written = os.write(descriptor, body[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True
