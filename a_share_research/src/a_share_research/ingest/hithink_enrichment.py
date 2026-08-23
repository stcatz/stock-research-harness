"""Deterministic HiThink enrichment for a frozen A-share collection session.

The module deliberately sits above :mod:`hithink_client` and below snapshot assembly.  It
never writes a snapshot and never exposes credentials or raw response bodies.  Instead it
returns typed, immutable-ish value objects that a snapshot builder can map to
``structured_market`` evidence.

HiThink's public API does not expose immutable provider vintages.  Consequently, response
retrieval is the conservative availability boundary and upstream ``timestamp`` values remain
metadata only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .market_data import CollectionError

HITHINK_PRICES_SNAPSHOT_ENDPOINT = "/api/a-share/prices/snapshot"
HITHINK_VALUATIONS_ENDPOINT = "/api/a-share/valuations/snapshot"
HITHINK_POOL_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "limit_up": "/api/a-share/special-data/limit-up-pool",
        "limit_down": "/api/a-share/special-data/limit-down-pool",
        "limit_break": "/api/a-share/special-data/limit-break-pool",
    }
)
HITHINK_FINANCIAL_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "income": "/api/a-share/financials/income-statements",
        "balance": "/api/a-share/financials/balance-sheets",
        "cashflow": "/api/a-share/financials/cash-flow-statements",
    }
)
HITHINK_PUBLIC_SOURCE_URL = "https://fuyao.aicubes.cn/docs/"
HITHINK_PROVIDER_NAME = "hithink-financial-api"
HITHINK_PROVIDER_VERSION = "public-api-unversioned"
HITHINK_NON_PIT_NOTICE = (
    "The provider exposes no immutable first-published vintage. available_at is the first "
    "local retrieval time and upstream timestamp is retained as metadata only."
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_THSCODE_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_SAFE_ENDPOINT_PATTERN = re.compile(r"/api/[A-Za-z0-9_/-]+\Z")

_VALUATION_FIELDS: tuple[str, ...] = ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm")
_FINANCIAL_FIELDS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "income": (
            ("basic_eps", "CNY/share"),
            ("operating_income", "CNY"),
            ("operating_costs", "CNY"),
            ("operating_expenses", "CNY"),
            ("operating_profit", "CNY"),
            ("profit_total", "CNY"),
            ("net_profit", "CNY"),
            ("parent_holder_net_profit", "CNY"),
            ("income_tax_expense", "CNY"),
            ("interest_expenses", "CNY"),
            ("manage_fee", "CNY"),
            ("sales_fee", "CNY"),
            ("research_and_development_expenses", "CNY"),
        ),
        "balance": (
            ("total_current_assets", "CNY"),
            ("non_current_nets_total", "CNY"),
            ("assets_total", "CNY"),
            ("total_debt", "CNY"),
            ("holder_equity_total", "CNY"),
            ("cash", "CNY"),
            ("accounts_receivable", "CNY"),
        ),
        "cashflow": (
            ("act_cash_flow_net", "CNY"),
            ("invest_cash_flow_net", "CNY"),
            ("financing_cash_flow_net", "CNY"),
            ("cash_equivalents_net_addition", "CNY"),
            ("pay_dividends_profits_interest_cash", "CNY"),
            ("pay_fixed_assets_etc_cash", "CNY"),
        ),
    }
)
_POOL_NUMERIC_FIELDS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "limit_up": (
            ("price_change_ratio_pct", "percent"),
            ("continue_day_cnt", "count"),
            ("seal_money", "CNY"),
            ("max_seal_money", "CNY"),
        ),
        "limit_down": (
            ("price_change_ratio_pct", "percent"),
            ("turnover_ratio_pct", "percent"),
        ),
        "limit_break": (
            ("price_change_ratio_pct", "percent"),
            ("open_times", "count"),
            ("turnover_ratio_pct", "percent"),
            ("turnover", "CNY"),
        ),
    }
)
_POOL_SORT_FIELDS = {
    "limit_up": "last_price",
    "limit_down": "last_limit_time",
    "limit_break": "price_change_ratio_pct",
}


class HiThinkEnrichmentError(CollectionError):
    """Raised when a provider response cannot support an honest enrichment result."""


class HiThinkEnrichmentClient(Protocol):
    """Small structural boundary implemented by ``HiThinkClient`` and test fakes."""

    def get(self, endpoint: str, params: Mapping[str, str | int]) -> Any: ...


@dataclass(frozen=True)
class ProviderResponseRef:
    """Credential-free handle for one already persisted provider response."""

    endpoint: str
    request_id: str
    retrieved_at: datetime
    body_sha256: str
    raw_artifact_id: str | None
    upstream_timestamp_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        artifact: dict[str, str] = {"sha256": self.body_sha256}
        if self.raw_artifact_id is not None:
            artifact["artifact_id"] = self.raw_artifact_id
        return {
            "endpoint": self.endpoint,
            "request_id": self.request_id,
            "retrieved_at": self.retrieved_at.isoformat(),
            "upstream_timestamp_ms": self.upstream_timestamp_ms,
            "raw_artifact": artifact,
        }


@dataclass(frozen=True)
class EnrichmentFact:
    fact_id: str
    metric: str
    value: str | None
    unit: str
    status: str
    as_of: datetime
    effective_at: datetime
    available_at: datetime
    source_field: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "as_of": self.as_of.isoformat(),
            "effective_at": self.effective_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source_field": self.source_field,
        }


@dataclass(frozen=True)
class FullMarketBreadth:
    latest_session: date
    upstream_timestamp_ms: int
    total: int
    advancing: int
    declining: int
    unchanged: int
    no_trade: int
    turnover_total: Decimal
    sources: tuple[ProviderResponseRef, ...]

    @property
    def traded(self) -> int:
        return self.total - self.no_trade

    @property
    def evidence_id(self) -> str:
        return f"MKT-HITHINK-BREADTH-{self.latest_session:%Y%m%d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "latest_session": self.latest_session.isoformat(),
            "upstream_timestamp_ms": self.upstream_timestamp_ms,
            "total": self.total,
            "advancing": self.advancing,
            "declining": self.declining,
            "unchanged": self.unchanged,
            "no_trade": self.no_trade,
            "traded": self.traded,
            "turnover_total": _format_decimal(self.turnover_total),
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class SpecialPoolSummary:
    latest_session: date
    counts: Mapping[str, int]
    sources: tuple[ProviderResponseRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))

    @property
    def evidence_id(self) -> str:
        return f"MKT-HITHINK-POOLS-{self.latest_session:%Y%m%d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "latest_session": self.latest_session.isoformat(),
            "counts": dict(self.counts),
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class CandidatePoolRecord:
    thscode: str
    pool: str
    name: str | None
    provider_reason: str | None
    facts: tuple[EnrichmentFact, ...]
    source: ProviderResponseRef
    latest_session: date

    @property
    def is_official_evidence(self) -> bool:
        return False

    @property
    def evidence_id(self) -> str:
        return (
            f"MKT-HITHINK-{self.pool.replace('_', '-').upper()}-"
            f"{_safe_code(self.thscode)}-{self.latest_session:%Y%m%d}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thscode": self.thscode,
            "pool": self.pool,
            "name": self.name,
            "provider_reason": self.provider_reason,
            "provider_reason_classification": "provider_derived_unverified",
            "is_official_evidence": False,
            "facts": [fact.to_dict() for fact in self.facts],
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class CandidateValuation:
    thscode: str
    name: str | None
    facts: tuple[EnrichmentFact, ...]
    source: ProviderResponseRef
    latest_session: date

    @property
    def evidence_id(self) -> str:
        return f"MKT-HITHINK-VALUATION-{_safe_code(self.thscode)}-{self.latest_session:%Y%m%d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "thscode": self.thscode,
            "name": self.name,
            "facts": [fact.to_dict() for fact in self.facts],
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class FinancialPeriod:
    thscode: str
    period_end: datetime
    published_date_hints: Mapping[str, str]
    facts: tuple[EnrichmentFact, ...]
    sources: tuple[ProviderResponseRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "published_date_hints",
            MappingProxyType(dict(self.published_date_hints)),
        )

    @property
    def evidence_id(self) -> str:
        period_date = self.period_end.astimezone(SHANGHAI_TZ).date()
        return f"MKT-HITHINK-FINANCIAL-{_safe_code(self.thscode)}-{period_date:%Y%m%d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "thscode": self.thscode,
            "period_end": self.period_end.isoformat(),
            "published_date_hints": dict(self.published_date_hints),
            "facts": [fact.to_dict() for fact in self.facts],
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class EnrichmentResult:
    latest_session: date
    retrieved_at: datetime
    breadth: FullMarketBreadth
    pool_summary: SpecialPoolSummary
    candidate_pool_records: tuple[CandidatePoolRecord, ...]
    valuations: tuple[CandidateValuation, ...]
    financial_periods: tuple[FinancialPeriod, ...]
    data_gaps: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "data_gaps",
            MappingProxyType({code: tuple(gaps) for code, gaps in self.data_gaps.items()}),
        )

    def evidence_fragments(self) -> list[dict[str, Any]]:
        fragments = [
            _breadth_evidence(self.breadth),
            _pool_summary_evidence(self.pool_summary),
        ]
        fragments.extend(_pool_record_evidence(record) for record in self.candidate_pool_records)
        fragments.extend(_valuation_evidence(record) for record in self.valuations)
        fragments.extend(_financial_evidence(record) for record in self.financial_periods)
        return fragments

    def candidate_evidence_refs(self) -> dict[str, tuple[str, ...]]:
        references: dict[str, list[str]] = {
            valuation.thscode: [valuation.evidence_id] for valuation in self.valuations
        }
        for record in self.candidate_pool_records:
            references.setdefault(record.thscode, []).append(record.evidence_id)
        for period in self.financial_periods:
            references.setdefault(period.thscode, []).append(period.evidence_id)
        return {code: tuple(items) for code, items in references.items()}

    def market_context_fragment(self) -> dict[str, Any]:
        breadth = self.breadth
        counts = self.pool_summary.counts
        return {
            "regime": ("UNKNOWN（尚未配置确定性的市场状态判定规则；以下宽度和特色池仅作研究输入）"),
            "breadth": (
                f"全市场 {breadth.total} 只：上涨 {breadth.advancing}、"
                f"下跌 {breadth.declining}、平盘 {breadth.unchanged}、"
                f"无成交 {breadth.no_trade}。"
            ),
            "liquidity": f"全市场快照成交额合计 {_format_decimal(breadth.turnover_total)} 元。",
            "calculation_note": (
                "仅 volume>0 的股票进入涨跌/平盘分类；volume<=0 计为无成交。"
                f"特色池：涨停 {counts['limit_up']}、跌停 {counts['limit_down']}、"
                f"炸板 {counts['limit_break']}。供应商特色原因不属于官方证据。"
            ),
            "evidence_refs": [breadth.evidence_id, self.pool_summary.evidence_id],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": {
                "name": HITHINK_PROVIDER_NAME,
                "version": HITHINK_PROVIDER_VERSION,
                "source_url": HITHINK_PUBLIC_SOURCE_URL,
            },
            "latest_session": self.latest_session.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "pit_quality": "RECONSTRUCTED_NON_PIT",
            "non_pit_notice": HITHINK_NON_PIT_NOTICE,
            "breadth": self.breadth.to_dict(),
            "pool_summary": self.pool_summary.to_dict(),
            "candidate_pool_records": [item.to_dict() for item in self.candidate_pool_records],
            "valuations": [item.to_dict() for item in self.valuations],
            "financial_periods": [item.to_dict() for item in self.financial_periods],
            "data_gaps": {code: list(gaps) for code, gaps in self.data_gaps.items()},
            "market_context": self.market_context_fragment(),
            "evidence": self.evidence_fragments(),
        }


@dataclass(frozen=True)
class _StatementRow:
    statement: str
    values: Mapping[str, Any]
    period_end: datetime
    report_date: datetime
    source: ProviderResponseRef


class _Gateway:
    def __init__(self, client: HiThinkEnrichmentClient) -> None:
        self._client = client
        self.sources: list[ProviderResponseRef] = []

    def get(
        self, endpoint: str, params: Mapping[str, str | int]
    ) -> tuple[Mapping[str, Any], ProviderResponseRef]:
        response = self._client.get(endpoint, params)
        data, source = _extract_response(response, endpoint)
        self.sources.append(source)
        return data, source


def collect_hithink_enrichment(
    client: HiThinkEnrichmentClient,
    *,
    latest_session: date,
    candidate_thscodes: Sequence[str],
    page_size: int = 200,
) -> EnrichmentResult:
    """Collect fail-closed, evidence-ready enrichment for one known trading session."""

    if isinstance(latest_session, datetime) or not isinstance(latest_session, date):
        raise TypeError("latest_session must be a date")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 200:
        raise ValueError("page_size must be an integer in [1, 200]")
    candidates = _normalize_candidates(candidate_thscodes)
    gateway = _Gateway(client)
    gaps: dict[str, list[str]] = {code: [] for code in candidates}

    breadth = _collect_full_market_breadth(
        gateway,
        latest_session=latest_session,
        candidates=candidates,
        page_size=page_size,
    )
    pool_summary, pool_records = _collect_special_pools(
        gateway,
        latest_session=latest_session,
        candidates=set(candidates),
        page_size=page_size,
        gaps=gaps,
    )
    valuations = _collect_valuations(
        gateway,
        latest_session=latest_session,
        candidates=candidates,
        gaps=gaps,
    )
    financial_periods = _collect_financials(
        gateway,
        candidates=candidates,
        gaps=gaps,
    )
    if not gateway.sources:
        raise HiThinkEnrichmentError("HiThink enrichment collected no provider responses")
    retrieved_at = max(source.retrieved_at for source in gateway.sources)
    return EnrichmentResult(
        latest_session=latest_session,
        retrieved_at=retrieved_at,
        breadth=breadth,
        pool_summary=pool_summary,
        candidate_pool_records=tuple(pool_records),
        valuations=tuple(valuations),
        financial_periods=tuple(financial_periods),
        data_gaps={code: tuple(items) for code, items in gaps.items()},
    )


def _collect_full_market_breadth(
    gateway: _Gateway,
    *,
    latest_session: date,
    candidates: tuple[str, ...],
    page_size: int,
) -> FullMarketBreadth:
    offset = 0
    expected_total: int | None = None
    expected_timestamp: int | None = None
    seen: set[str] = set()
    sources: list[ProviderResponseRef] = []
    advancing = declining = unchanged = no_trade = 0
    turnover_total = Decimal(0)

    while True:
        data, source = gateway.get(
            HITHINK_PRICES_SNAPSHOT_ENDPOINT,
            {"limit": page_size, "offset": offset},
        )
        timestamp = _upstream_timestamp(data, "prices snapshot", allow_none=False)
        assert timestamp is not None
        source = _replace_gateway_source(gateway, source, timestamp)
        sources.append(source)
        if _shanghai_date_from_ms(timestamp, "prices snapshot.timestamp") != latest_session:
            raise HiThinkEnrichmentError("prices snapshot timestamp does not match latest_session")
        total = _nonnegative_int(data.get("total"), "prices snapshot.total")
        if total == 0:
            raise HiThinkEnrichmentError("prices snapshot has zero full-market coverage")
        if expected_total is None:
            expected_total = total
            expected_timestamp = timestamp
        elif total != expected_total:
            raise HiThinkEnrichmentError("prices snapshot total changed across pages")
        if timestamp != expected_timestamp:
            raise HiThinkEnrichmentError("prices snapshot timestamp changed across pages")

        items = _items(data, "prices snapshot")
        if len(items) > page_size:
            raise HiThinkEnrichmentError("prices snapshot page exceeds requested limit")
        for index, raw_item in enumerate(items):
            item = _mapping(raw_item, f"prices snapshot.item[{index}]")
            code = _required_thscode(item.get("thscode"), f"prices snapshot.item[{index}].thscode")
            if code in seen:
                raise HiThinkEnrichmentError(f"prices snapshot contains duplicate thscode: {code}")
            seen.add(code)
            _validate_ticker(item.get("ticker"), code, f"prices snapshot.item[{index}].ticker")
            volume = _required_decimal(item.get("volume"), f"prices snapshot {code}.volume")
            turnover = _required_decimal(item.get("turnover"), f"prices snapshot {code}.turnover")
            if volume < 0 or turnover < 0:
                raise HiThinkEnrichmentError(
                    f"prices snapshot {code} volume and turnover must be nonnegative"
                )
            turnover_total += turnover
            if volume <= 0:
                no_trade += 1
                continue
            ratio = _required_decimal(
                item.get("price_change_ratio_pct"),
                f"prices snapshot {code}.price_change_ratio_pct",
            )
            if ratio > 0:
                advancing += 1
            elif ratio < 0:
                declining += 1
            else:
                unchanged += 1

        if len(seen) > expected_total:
            raise HiThinkEnrichmentError("prices snapshot returned more rows than total")
        if len(seen) == expected_total:
            break
        if len(items) < page_size:
            raise HiThinkEnrichmentError(
                f"prices snapshot coverage incomplete: expected {expected_total}, received {len(seen)}"
            )
        offset += page_size

    missing_candidates = sorted(set(candidates).difference(seen))
    if missing_candidates:
        raise HiThinkEnrichmentError(
            "full-market snapshot does not cover candidates: " + ", ".join(missing_candidates)
        )
    assert expected_total is not None and expected_timestamp is not None
    return FullMarketBreadth(
        latest_session=latest_session,
        upstream_timestamp_ms=expected_timestamp,
        total=expected_total,
        advancing=advancing,
        declining=declining,
        unchanged=unchanged,
        no_trade=no_trade,
        turnover_total=turnover_total,
        sources=tuple(sources),
    )


def _collect_special_pools(
    gateway: _Gateway,
    *,
    latest_session: date,
    candidates: set[str],
    page_size: int,
    gaps: dict[str, list[str]],
) -> tuple[SpecialPoolSummary, list[CandidatePoolRecord]]:
    date_ms = int(datetime.combine(latest_session, time.min, tzinfo=SHANGHAI_TZ).timestamp() * 1000)
    counts: dict[str, int] = {}
    all_sources: list[ProviderResponseRef] = []
    records: list[CandidatePoolRecord] = []
    for pool, endpoint in HITHINK_POOL_ENDPOINTS.items():
        rows, sources, total = _collect_one_pool(
            gateway,
            pool=pool,
            endpoint=endpoint,
            latest_session=latest_session,
            date_ms=date_ms,
            page_size=page_size,
        )
        counts[pool] = total
        all_sources.extend(sources)
        for item, source in rows:
            code = _required_thscode(item.get("thscode"), f"{pool}.item.thscode")
            if code not in candidates:
                continue
            facts = [
                _fact(
                    fact_id=f"FACT-HITHINK-{_safe_code(code)}-{pool.upper()}-MEMBERSHIP-{latest_session:%Y%m%d}",
                    metric=f"special_pool.{pool}.membership",
                    value=pool,
                    unit="category",
                    as_of=_session_close(latest_session),
                    available_at=source.retrieved_at,
                    source_field="thscode",
                )
            ]
            for field, unit in _POOL_NUMERIC_FIELDS[pool]:
                value = _optional_decimal(item.get(field), f"{pool} {code}.{field}")
                facts.append(
                    _fact(
                        fact_id=(
                            f"FACT-HITHINK-{_safe_code(code)}-{pool.upper()}-"
                            f"{field.upper()}-{latest_session:%Y%m%d}"
                        ),
                        metric=f"special_pool.{pool}.{field}",
                        value=None if value is None else _format_decimal(value),
                        unit=unit,
                        as_of=_session_close(latest_session),
                        available_at=source.retrieved_at,
                        source_field=field,
                    )
                )
                if value is None:
                    gaps[code].append(f"{pool} provider row is missing {field}")
            name = _optional_text(item.get("name"), f"{pool} {code}.name")
            reason = None
            if pool == "limit_up":
                reason = _optional_text(
                    item.get("limit_up_reason"), f"{pool} {code}.limit_up_reason"
                )
            records.append(
                CandidatePoolRecord(
                    thscode=code,
                    pool=pool,
                    name=name,
                    provider_reason=reason,
                    facts=tuple(facts),
                    source=source,
                    latest_session=latest_session,
                )
            )
    records.sort(key=lambda record: (record.thscode, record.pool))
    return (
        SpecialPoolSummary(
            latest_session=latest_session,
            counts=counts,
            sources=tuple(all_sources),
        ),
        records,
    )


def _collect_one_pool(
    gateway: _Gateway,
    *,
    pool: str,
    endpoint: str,
    latest_session: date,
    date_ms: int,
    page_size: int,
) -> tuple[list[tuple[Mapping[str, Any], ProviderResponseRef]], list[ProviderResponseRef], int]:
    rows: list[tuple[Mapping[str, Any], ProviderResponseRef]] = []
    sources: list[ProviderResponseRef] = []
    seen: set[str] = set()
    expected_total: int | None = None
    expected_pages: int | None = None
    expected_timestamp: int | None = None
    page = 1
    while True:
        data, source = gateway.get(
            endpoint,
            {
                "date_ms": date_ms,
                "page": page,
                "size": page_size,
                "sort_field": _POOL_SORT_FIELDS[pool],
                "sort_dir": "desc",
            },
        )
        timestamp = _upstream_timestamp(data, f"{pool} pool", allow_none=False)
        assert timestamp is not None
        source = _replace_gateway_source(gateway, source, timestamp)
        sources.append(source)
        pagination = _mapping(data.get("pagination"), f"{pool}.pagination")
        total = _nonnegative_int(pagination.get("total"), f"{pool}.pagination.total")
        pages = _nonnegative_int(pagination.get("pages"), f"{pool}.pagination.pages")
        size = _positive_int(pagination.get("size"), f"{pool}.pagination.size")
        returned_page = _positive_int(pagination.get("page"), f"{pool}.pagination.page")
        if size != page_size:
            raise HiThinkEnrichmentError(f"{pool} pagination size differs from requested size")
        calculated_pages = math.ceil(total / page_size)
        valid_page_counts = {calculated_pages} if total else {0, 1}
        if pages not in valid_page_counts:
            raise HiThinkEnrichmentError(f"{pool} pagination pages is inconsistent with total")
        if returned_page != page:
            raise HiThinkEnrichmentError(f"{pool} pagination returned unexpected page")
        if expected_total is None:
            expected_total, expected_pages, expected_timestamp = total, pages, timestamp
        elif (total, pages) != (expected_total, expected_pages):
            raise HiThinkEnrichmentError(f"{pool} pagination metadata changed across pages")
        if timestamp != expected_timestamp:
            raise HiThinkEnrichmentError(f"{pool} timestamp changed across pages")
        items = _items(data, f"{pool} pool")
        if len(items) > page_size:
            raise HiThinkEnrichmentError(f"{pool} page exceeds requested size")
        for index, raw_item in enumerate(items):
            item = _mapping(raw_item, f"{pool}.item[{index}]")
            code = _required_thscode(item.get("thscode"), f"{pool}.item[{index}].thscode")
            if code in seen:
                raise HiThinkEnrichmentError(f"{pool} contains duplicate thscode: {code}")
            seen.add(code)
            rows.append((item, source))
        if pages == 0 or page == pages:
            break
        page += 1
    assert expected_total is not None
    if len(rows) != expected_total:
        raise HiThinkEnrichmentError(
            f"{pool} coverage incomplete: expected {expected_total}, received {len(rows)}"
        )
    return rows, sources, expected_total


def _collect_valuations(
    gateway: _Gateway,
    *,
    latest_session: date,
    candidates: tuple[str, ...],
    gaps: dict[str, list[str]],
) -> list[CandidateValuation]:
    records: list[CandidateValuation] = []
    for batch_start in range(0, len(candidates), 100):
        batch = candidates[batch_start : batch_start + 100]
        data, source = gateway.get(
            HITHINK_VALUATIONS_ENDPOINT,
            {"thscodes": ",".join(batch)},
        )
        timestamp = _upstream_timestamp(data, "valuations snapshot", allow_none=True)
        source = _replace_gateway_source(gateway, source, timestamp)
        total = _nonnegative_int(data.get("total"), "valuations snapshot.total")
        items = _items(data, "valuations snapshot")
        if total != len(items):
            raise HiThinkEnrichmentError("valuations snapshot total does not match returned rows")
        rows: dict[str, Mapping[str, Any]] = {}
        for index, raw_item in enumerate(items):
            item = _mapping(raw_item, f"valuations.item[{index}]")
            code = _required_thscode(item.get("thscode"), f"valuations.item[{index}].thscode")
            if code not in batch:
                raise HiThinkEnrichmentError(f"valuations returned unrequested thscode: {code}")
            if code in rows:
                raise HiThinkEnrichmentError(f"valuations contains duplicate thscode: {code}")
            rows[code] = item

        as_of = _session_close(latest_session)
        for code in batch:
            row = rows.get(code)
            name = (
                None if row is None else _optional_text(row.get("name"), f"valuation {code}.name")
            )
            facts: list[EnrichmentFact] = []
            if row is None:
                gaps[code].append(
                    "valuation row is missing from the snapshot; all valuation facts are UNKNOWN"
                )
            for field in _VALUATION_FIELDS:
                value = (
                    None
                    if row is None
                    else _optional_decimal(row.get(field), f"valuation {code}.{field}")
                )
                facts.append(
                    _fact(
                        fact_id=(
                            f"FACT-HITHINK-{_safe_code(code)}-VALUATION-"
                            f"{field.upper()}-{latest_session:%Y%m%d}"
                        ),
                        metric=field,
                        value=None if value is None else _format_decimal(value),
                        unit="x",
                        as_of=as_of,
                        available_at=source.retrieved_at,
                        source_field=field,
                    )
                )
                if row is not None and value is None:
                    gaps[code].append(f"valuation {field} is UNKNOWN")
            records.append(
                CandidateValuation(
                    thscode=code,
                    name=name,
                    facts=tuple(facts),
                    source=source,
                    latest_session=latest_session,
                )
            )
    return records


def _collect_financials(
    gateway: _Gateway,
    *,
    candidates: tuple[str, ...],
    gaps: dict[str, list[str]],
) -> list[FinancialPeriod]:
    results: list[FinancialPeriod] = []
    for code in candidates:
        by_statement: dict[str, dict[int, _StatementRow]] = {}
        for statement, endpoint in HITHINK_FINANCIAL_ENDPOINTS.items():
            data, source = gateway.get(
                endpoint,
                {"thscode": code, "period": "annual", "limit": 2},
            )
            timestamp = _upstream_timestamp(data, f"{statement} financials", allow_none=False)
            source = _replace_gateway_source(gateway, source, timestamp)
            items = _items(data, f"{statement} financials")
            if len(items) > 2:
                raise HiThinkEnrichmentError(f"{statement} returned more rows than limit=2")
            rows: dict[int, _StatementRow] = {}
            for index, raw_item in enumerate(items):
                item = _mapping(raw_item, f"{statement}.item[{index}]")
                row = _parse_statement_row(statement, code, item, source, index)
                period_key = _datetime_to_ms(row.period_end)
                if period_key in rows:
                    raise HiThinkEnrichmentError(
                        f"{statement} {code} contains duplicate period_end_ms"
                    )
                rows[period_key] = row
            if not rows:
                gaps[code].append(f"{statement} annual statements returned no rows")
            by_statement[statement] = rows

        period_sets = [set(rows) for rows in by_statement.values()]
        common_periods = set.intersection(*period_sets)
        all_periods = set.union(*period_sets)
        for period_key in sorted(all_periods.difference(common_periods), reverse=True):
            period = _datetime_from_ms(period_key, f"financial {code}.period_end_ms")
            gaps[code].append(
                f"financial period {period.date().isoformat()} is not present in all three "
                "statements; it was not joined"
            )
        if not common_periods:
            gaps[code].append("no common annual period across income, balance and cashflow")
            continue

        for period_key in sorted(common_periods, reverse=True)[:2]:
            rows = {
                statement: statement_rows[period_key]
                for statement, statement_rows in by_statement.items()
            }
            period_end = next(iter(rows.values())).period_end
            facts: list[EnrichmentFact] = []
            for statement in HITHINK_FINANCIAL_ENDPOINTS:
                row = rows[statement]
                for field, unit in _FINANCIAL_FIELDS[statement]:
                    value = _optional_decimal(
                        row.values.get(field), f"{statement} {code} {period_key}.{field}"
                    )
                    facts.append(
                        _fact(
                            fact_id=(
                                f"FACT-HITHINK-{_safe_code(code)}-{period_key}-"
                                f"{statement.upper()}-{field.upper()}"
                            ),
                            metric=f"{statement}.{field}",
                            value=None if value is None else _format_decimal(value),
                            unit=unit,
                            as_of=period_end,
                            available_at=row.source.retrieved_at,
                            source_field=field,
                        )
                    )
                    if value is None:
                        gaps[code].append(
                            f"{statement} {field} is UNKNOWN for period {period_end.date()}"
                        )
            results.append(
                FinancialPeriod(
                    thscode=code,
                    period_end=period_end,
                    published_date_hints={
                        statement: row.report_date.isoformat() for statement, row in rows.items()
                    },
                    facts=tuple(facts),
                    sources=tuple(
                        rows[statement].source for statement in HITHINK_FINANCIAL_ENDPOINTS
                    ),
                )
            )
    results.sort(key=lambda item: (item.thscode, -_datetime_to_ms(item.period_end)))
    return results


def _parse_statement_row(
    statement: str,
    expected_code: str,
    item: Mapping[str, Any],
    source: ProviderResponseRef,
    index: int,
) -> _StatementRow:
    prefix = f"{statement}.item[{index}]"
    code = _required_thscode(item.get("thscode"), f"{prefix}.thscode")
    if code != expected_code:
        raise HiThinkEnrichmentError(
            f"{statement} returned {code} while requesting {expected_code}"
        )
    if item.get("period") != "annual":
        raise HiThinkEnrichmentError(f"{prefix}.period must be annual")
    if item.get("currency") != "CNY":
        raise HiThinkEnrichmentError(f"{prefix}.currency must be CNY")
    period_end_ms = _positive_int(item.get("period_end_ms"), f"{prefix}.period_end_ms")
    report_date_ms = _positive_int(item.get("report_date_ms"), f"{prefix}.report_date_ms")
    period_end = _datetime_from_ms(period_end_ms, f"{prefix}.period_end_ms")
    report_date = _datetime_from_ms(report_date_ms, f"{prefix}.report_date_ms")
    if report_date < period_end:
        raise HiThinkEnrichmentError(f"{prefix}.report_date_ms is earlier than period_end_ms")
    if report_date > source.retrieved_at.astimezone(UTC):
        raise HiThinkEnrichmentError(f"{prefix}.report_date_ms is later than collection time")
    fiscal_year = _positive_int(item.get("fiscal_year"), f"{prefix}.fiscal_year")
    if fiscal_year != period_end.astimezone(SHANGHAI_TZ).year:
        raise HiThinkEnrichmentError(f"{prefix}.fiscal_year does not match period_end_ms")
    fiscal_period = item.get("fiscal_period")
    if not isinstance(fiscal_period, str) or not fiscal_period.strip():
        raise HiThinkEnrichmentError(f"{prefix}.fiscal_period must be a non-empty string")
    return _StatementRow(
        statement=statement,
        values=item,
        period_end=period_end,
        report_date=report_date,
        source=source,
    )


def _breadth_evidence(breadth: FullMarketBreadth) -> dict[str, Any]:
    as_of = _session_close(breadth.latest_session)
    available_at = max(source.retrieved_at for source in breadth.sources)
    values = (
        ("advancing_count", breadth.advancing, "count"),
        ("declining_count", breadth.declining, "count"),
        ("unchanged_count", breadth.unchanged, "count"),
        ("no_trade_count", breadth.no_trade, "count"),
        ("security_count", breadth.total, "count"),
        ("turnover_total", breadth.turnover_total, "CNY"),
    )
    facts = tuple(
        _fact(
            fact_id=f"FACT-HITHINK-BREADTH-{metric.upper()}-{breadth.latest_session:%Y%m%d}",
            metric=f"market_breadth.{metric}",
            value=_format_decimal(Decimal(value)),
            unit=unit,
            as_of=as_of,
            available_at=available_at,
            source_field=(
                "derived:volume,price_change_ratio_pct"
                if metric != "turnover_total"
                else "derived:turnover"
            ),
        )
        for metric, value, unit in values
    )
    return _evidence(
        evidence_id=breadth.evidence_id,
        title=f"同花顺全市场行情宽度：{breadth.latest_session.isoformat()}",
        summary=(
            f"覆盖 {breadth.total} 只 A 股；上涨 {breadth.advancing}、"
            f"下跌 {breadth.declining}、平盘 {breadth.unchanged}、"
            f"无成交 {breadth.no_trade}；成交额合计 "
            f"{_format_decimal(breadth.turnover_total)} 元。"
        ),
        as_of=as_of,
        published_at=available_at,
        available_at=available_at,
        sources=breadth.sources,
        facts=facts,
        provider_extra={"coverage": "full_market", "classification_rule": "volume>0"},
    )


def _pool_summary_evidence(summary: SpecialPoolSummary) -> dict[str, Any]:
    as_of = _session_close(summary.latest_session)
    available_at = max(source.retrieved_at for source in summary.sources)
    facts = tuple(
        _fact(
            fact_id=f"FACT-HITHINK-POOL-{pool.upper()}-COUNT-{summary.latest_session:%Y%m%d}",
            metric=f"special_pool.{pool}.count",
            value=str(count),
            unit="count",
            as_of=as_of,
            available_at=available_at,
            source_field="pagination.total",
        )
        for pool, count in summary.counts.items()
    )
    return _evidence(
        evidence_id=summary.evidence_id,
        title=f"同花顺涨跌停与炸板池统计：{summary.latest_session.isoformat()}",
        summary=(
            f"涨停 {summary.counts['limit_up']}、跌停 {summary.counts['limit_down']}、"
            f"炸板 {summary.counts['limit_break']}。特色原因属于供应商衍生标签，"
            "不能替代公告或监管原文。"
        ),
        as_of=as_of,
        published_at=available_at,
        available_at=available_at,
        sources=summary.sources,
        facts=facts,
        provider_extra={"reason_classification": "provider_derived_unverified"},
    )


def _pool_record_evidence(record: CandidatePoolRecord) -> dict[str, Any]:
    as_of = _session_close(record.latest_session)
    summary = f"供应商特色数据将 {record.thscode} 纳入 {record.pool} 池。"
    if record.provider_reason:
        summary += " 供应商同时给出衍生原因标签；该标签不是官方证据。"
    return _evidence(
        evidence_id=record.evidence_id,
        title=f"同花顺特色池命中：{record.thscode} / {record.pool}",
        summary=summary,
        as_of=as_of,
        published_at=record.source.retrieved_at,
        available_at=record.source.retrieved_at,
        sources=(record.source,),
        facts=record.facts,
        provider_extra={
            "instrument": record.thscode,
            "pool": record.pool,
            "provider_reason": record.provider_reason,
            "reason_classification": "provider_derived_unverified",
            "official_evidence": False,
        },
    )


def _valuation_evidence(record: CandidateValuation) -> dict[str, Any]:
    as_of = _session_close(record.latest_session)
    observed = sum(fact.status == "observed" for fact in record.facts)
    return _evidence(
        evidence_id=record.evidence_id,
        title=f"同花顺最新估值快照：{record.thscode}",
        summary=f"固定五项估值中 {observed} 项有值，{len(record.facts) - observed} 项 UNKNOWN。",
        as_of=as_of,
        published_at=record.source.retrieved_at,
        available_at=record.source.retrieved_at,
        sources=(record.source,),
        facts=record.facts,
        provider_extra={"instrument": record.thscode, "name": record.name},
    )


def _financial_evidence(record: FinancialPeriod) -> dict[str, Any]:
    available_at = max(source.retrieved_at for source in record.sources)
    published_at = max(
        datetime.fromisoformat(value) for value in record.published_date_hints.values()
    )
    observed = sum(fact.status == "observed" for fact in record.facts)
    return _evidence(
        evidence_id=record.evidence_id,
        title=f"同花顺年度财务三表共同报告期：{record.thscode}",
        summary=(
            f"利润表、资产负债表和现金流量表按同一 period_end 对齐；"
            f"{observed} 项有值，{len(record.facts) - observed} 项 UNKNOWN。"
        ),
        as_of=record.period_end,
        published_at=published_at,
        available_at=available_at,
        sources=record.sources,
        facts=record.facts,
        provider_extra={
            "instrument": record.thscode,
            "period": "annual",
            "period_end": record.period_end.isoformat(),
            "published_date_hints": dict(record.published_date_hints),
            "published_date_semantics": (
                "provider report_date_ms is retained only as a published-date hint; it is not "
                "used as available_at"
            ),
        },
    )


def _evidence(
    *,
    evidence_id: str,
    title: str,
    summary: str,
    as_of: datetime,
    published_at: datetime,
    available_at: datetime,
    sources: tuple[ProviderResponseRef, ...],
    facts: tuple[EnrichmentFact, ...],
    provider_extra: Mapping[str, Any],
) -> dict[str, Any]:
    responses = [source.to_dict() for source in sources]
    provider = {
        "name": HITHINK_PROVIDER_NAME,
        "version": HITHINK_PROVIDER_VERSION,
        "responses": responses,
        **dict(provider_extra),
    }
    return {
        "evidence_id": evidence_id,
        "category": "market_data",
        "source_level": "structured_market",
        "title": title,
        "source_url": HITHINK_PUBLIC_SOURCE_URL,
        "published_at": published_at.isoformat(),
        "effective_at": as_of.isoformat(),
        "available_at": available_at.isoformat(),
        "retrieved_at": available_at.isoformat(),
        "as_of": as_of.isoformat(),
        "summary": summary,
        "facts": [fact.to_dict() for fact in facts],
        "provider": provider,
        "pit_quality": "RECONSTRUCTED_NON_PIT",
        "non_pit_notice": HITHINK_NON_PIT_NOTICE,
    }


def _fact(
    *,
    fact_id: str,
    metric: str,
    value: str | None,
    unit: str,
    as_of: datetime,
    available_at: datetime,
    source_field: str,
) -> EnrichmentFact:
    return EnrichmentFact(
        fact_id=fact_id.replace("_", "-"),
        metric=metric,
        value=value,
        unit=unit,
        status="unknown" if value is None else "observed",
        as_of=as_of,
        effective_at=as_of,
        available_at=available_at,
        source_field=source_field,
    )


def _extract_response(
    response: Any, expected_endpoint: str
) -> tuple[Mapping[str, Any], ProviderResponseRef]:
    data = _response_field(response, "data")
    request_id = _response_field(response, "request_id")
    retrieved_at = _response_field(response, "retrieved_at")
    body_sha256 = _response_field(response, "body_sha256")
    raw_artifact_id = _response_field(response, "raw_artifact_id", required=False)
    response_endpoint = _response_field(response, "endpoint", required=False)

    if response_endpoint is not None and response_endpoint != expected_endpoint:
        raise HiThinkEnrichmentError("response endpoint does not match requested endpoint")
    if not _SAFE_ENDPOINT_PATTERN.fullmatch(expected_endpoint) or "//" in expected_endpoint:
        raise HiThinkEnrichmentError("expected endpoint is not a safe /api path")
    if not isinstance(request_id, str) or not _IDENTIFIER_PATTERN.fullmatch(request_id):
        raise HiThinkEnrichmentError("provider response request_id is invalid")
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
        raise HiThinkEnrichmentError("provider response retrieved_at must be timezone-aware")
    if not isinstance(body_sha256, str) or not _SHA256_PATTERN.fullmatch(body_sha256):
        raise HiThinkEnrichmentError("provider response body_sha256 is invalid")
    if raw_artifact_id is not None and (
        not isinstance(raw_artifact_id, str) or not _IDENTIFIER_PATTERN.fullmatch(raw_artifact_id)
    ):
        raise HiThinkEnrichmentError("provider response raw_artifact_id is invalid")
    return (
        _mapping(data, "provider response.data"),
        ProviderResponseRef(
            endpoint=expected_endpoint,
            request_id=request_id,
            retrieved_at=retrieved_at,
            body_sha256=body_sha256.lower(),
            raw_artifact_id=raw_artifact_id,
        ),
    )


def _response_field(response: Any, field: str, *, required: bool = True) -> Any:
    if isinstance(response, Mapping):
        if field in response:
            return response[field]
    elif hasattr(response, field):
        return getattr(response, field)
    if required:
        raise HiThinkEnrichmentError(f"provider response is missing {field}")
    return None


def _replace_gateway_source(
    gateway: _Gateway, source: ProviderResponseRef, timestamp: int | None
) -> ProviderResponseRef:
    enriched = replace(source, upstream_timestamp_ms=timestamp)
    gateway.sources[-1] = enriched
    return enriched


def _upstream_timestamp(data: Mapping[str, Any], field: str, *, allow_none: bool) -> int | None:
    value = data.get("timestamp")
    if value is None and allow_none:
        return None
    return _positive_int(value, f"{field}.timestamp")


def _normalize_candidates(raw_candidates: Sequence[str]) -> tuple[str, ...]:
    if isinstance(raw_candidates, (str, bytes)):
        raise TypeError("candidate_thscodes must be a sequence, not a string")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, str):
            raise TypeError(f"candidate_thscodes[{index}] must be a string")
        code = raw.strip().upper()
        if not _THSCODE_PATTERN.fullmatch(code):
            raise ValueError(f"candidate_thscodes[{index}] is not an A-share thscode")
        if code not in seen:
            seen.add(code)
            normalized.append(code)
    if not normalized:
        raise ValueError("candidate_thscodes must contain at least one code")
    return tuple(normalized)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HiThinkEnrichmentError(f"{field} must be an object")
    return value


def _items(data: Mapping[str, Any], field: str) -> list[Any]:
    items = data.get("item")
    if not isinstance(items, list):
        raise HiThinkEnrichmentError(f"{field}.item must be an array")
    return items


def _required_thscode(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise HiThinkEnrichmentError(f"{field} must be a string")
    code = value.strip().upper()
    if not _THSCODE_PATTERN.fullmatch(code):
        raise HiThinkEnrichmentError(f"{field} is not a valid A-share thscode")
    return code


def _validate_ticker(value: Any, code: str, field: str) -> None:
    if not isinstance(value, str) or value.strip() != code[:6]:
        raise HiThinkEnrichmentError(f"{field} does not match thscode")


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HiThinkEnrichmentError(f"{field} must be a string or null")
    normalized = value.strip()
    return normalized or None


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HiThinkEnrichmentError(f"{field} must be a nonnegative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result == 0:
        raise HiThinkEnrichmentError(f"{field} must be positive")
    return result


def _required_decimal(value: Any, field: str) -> Decimal:
    result = _optional_decimal(value, field)
    if result is None:
        raise HiThinkEnrichmentError(f"{field} must be numeric")
    return result


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise HiThinkEnrichmentError(f"{field} must be numeric or null")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise HiThinkEnrichmentError(f"{field} must be numeric or null") from None
    if not result.is_finite():
        raise HiThinkEnrichmentError(f"{field} must be finite")
    return result


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _datetime_from_ms(value: int, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise HiThinkEnrichmentError(f"{field} is outside the supported timestamp range") from None


def _datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _shanghai_date_from_ms(value: int, field: str) -> date:
    return _datetime_from_ms(value, field).astimezone(SHANGHAI_TZ).date()


def _session_close(value: date) -> datetime:
    return datetime.combine(value, time(15, 0), tzinfo=SHANGHAI_TZ)


def _safe_code(code: str) -> str:
    return code.replace(".", "-")
