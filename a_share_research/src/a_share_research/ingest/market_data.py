from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
BENCHMARK_SYMBOLS = ("sh.000001", "sz.399001", "sz.399006")

# BaoStock compatibility constants. They are deliberately not part of the canonical provider
# protocol; only the BaoStock adapter and legacy injected test providers consume them.
DAILY_FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "tradestatus",
    "pctChg",
    "isST",
)
UNADJUSTED_FLAG = "3"
MINIMUM_ACTIVE_SESSIONS = 11
DEFAULT_LOOKBACK_CALENDAR_DAYS = 60
UNKNOWN = "UNKNOWN"
NON_PIT_NOTICE = (
    "The upstream provider does not provide an immutable first-seen timestamp. "
    "This collection is RECONSTRUCTED_NON_PIT and must not be presented as strict PIT data."
)
BENCHMARK_COVERAGE_NOTE = (
    "Coverage contains only the Shanghai Composite, Shenzhen Component and ChiNext indices. "
    "It is not full-market breadth and contains no advancing/declining-stock counts."
)
DERIVATION_NOTE = (
    "10-session return = latest unadjusted close / unadjusted close 10 active sessions earlier "
    "- 1; 5-session average amount = arithmetic mean of the latest five active-session amounts."
)
_SESSION_STATUSES = {"0", "1", UNKNOWN}
_FORBIDDEN_METADATA_KEYS = {
    "api_key",
    "authorization",
    "body",
    "raw_body",
    "raw_response",
    "response_body",
    "token",
}


class CollectionError(RuntimeError):
    """Raised when provider data cannot support an honest market-data fragment."""


class ProviderUnavailableError(CollectionError):
    """Raised when an optional data-provider package is unavailable."""


class DailyMarketDataProvider(Protocol):
    """Provider-neutral daily-series boundary used by the deterministic collector."""

    name: str
    version: str
    source_url: str

    def login(self) -> None: ...

    def logout(self) -> None: ...

    def fetch_daily_series(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
        is_benchmark: bool,
    ) -> DailySeries: ...


@dataclass(frozen=True)
class DailyBar:
    """Canonical unadjusted observation for one active exchange session.

    Fields absent from an upstream contract remain ``None``/``UNKNOWN``. ``field_provenance``
    distinguishes reported values from safe deterministic derivations.
    """

    trade_date: date
    code: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    preclose: Decimal | None
    volume: Decimal
    amount: Decimal
    turn: Decimal | None
    pct_chg: Decimal | None
    trade_status: str
    is_st: bool | None
    field_provenance: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.trade_date.isoformat(),
            "code": self.code,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "preclose": str(self.preclose) if self.preclose is not None else None,
            "volume": str(self.volume),
            "amount": str(self.amount),
            "turn": str(self.turn) if self.turn is not None else None,
            "pct_chg": str(self.pct_chg) if self.pct_chg is not None else None,
            "trade_status": self.trade_status,
            "is_st": self.is_st,
            # Keep the historical field for existing snapshot consumers while making the
            # provider-neutral meaning explicit.
            "adjustflag": UNADJUSTED_FLAG,
            "adjustment": "none",
            "field_provenance": dict(self.field_provenance),
        }


@dataclass(frozen=True)
class DailySeries:
    """Canonical daily observations plus provider provenance for one instrument."""

    code: str
    bars: tuple[DailyBar, ...]
    session_statuses: Mapping[date, str]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    adjustment: str = "none"

    @property
    def latest_observed_date(self) -> date:
        if not self.session_statuses:
            raise CollectionError(f"{self.code} has no observed sessions")
        return max(self.session_statuses)


@dataclass(frozen=True)
class InstrumentMarketData:
    code: str
    instrument_kind: str
    bars: tuple[DailyBar, ...]
    return_10_sessions_pct: Decimal
    average_amount_5_sessions: Decimal
    series_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def latest(self) -> DailyBar:
        return self.bars[-1]

    @property
    def return_base(self) -> DailyBar:
        return self.bars[-MINIMUM_ACTIVE_SESSIONS]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "instrument_kind": self.instrument_kind,
            "bars": [bar.to_dict() for bar in self.bars],
            "derived": {
                "return_10_sessions_pct": _format_decimal(self.return_10_sessions_pct, places=4),
                "return_base_date": self.return_base.trade_date.isoformat(),
                "average_amount_5_sessions": _format_decimal(
                    self.average_amount_5_sessions, places=2
                ),
                "derivation_note": DERIVATION_NOTE,
            },
        }


@dataclass(frozen=True)
class MarketDataCollection:
    retrieved_at: datetime
    query_start_date: date
    query_end_date: date
    latest_session: date
    provider_name: str
    provider_version: str
    provider_source_url: str
    instruments: tuple[InstrumentMarketData, ...]

    def evidence_fragments(self) -> list[dict[str, Any]]:
        return [_evidence_fragment(self, instrument) for instrument in self.instruments]

    def market_context_fragment(self) -> dict[str, Any]:
        benchmark_ids = [
            _evidence_id(self.provider_name, instrument)
            for instrument in self.instruments
            if instrument.instrument_kind == "benchmark"
        ]
        return {
            "regime": "UNKNOWN（仅采集三只宽基指数，未配置市场状态判定规则）",
            "breadth": "UNKNOWN（未采集全市场上涨/下跌家数）",
            "liquidity": "UNKNOWN（三只指数与候选股成交额不能代表全市场流动性）",
            "calculation_note": f"{BENCHMARK_COVERAGE_NOTE} {DERIVATION_NOTE}",
            "evidence_refs": benchmark_ids,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieved_at": self.retrieved_at.isoformat(),
            "provider": {
                "name": self.provider_name,
                "version": self.provider_version,
                "source_url": self.provider_source_url,
            },
            "query": {
                "start_date": self.query_start_date.isoformat(),
                "end_date": self.query_end_date.isoformat(),
                "frequency": "1d",
                "adjustment": "none",
            },
            "latest_session": self.latest_session.isoformat(),
            "pit_quality": "RECONSTRUCTED_NON_PIT",
            "non_pit_notice": NON_PIT_NOTICE,
            "coverage_note": BENCHMARK_COVERAGE_NOTE,
            "instruments": [instrument.to_dict() for instrument in self.instruments],
            "evidence": self.evidence_fragments(),
            "market_context": self.market_context_fragment(),
        }


def collect_cn_market_data(
    candidate_symbols: Sequence[str],
    *,
    provider: DailyMarketDataProvider,
    retrieved_at: datetime | None = None,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
) -> MarketDataCollection:
    """Collect canonical candidate and benchmark observations without claiming PIT quality."""

    captured_at = _normalize_retrieved_at(retrieved_at)
    if lookback_calendar_days < 20:
        raise ValueError("lookback_calendar_days must be at least 20")

    candidates = tuple(sorted({normalize_cn_symbol(symbol) for symbol in candidate_symbols}))
    codes = (*BENCHMARK_SYMBOLS, *(code for code in candidates if code not in BENCHMARK_SYMBOLS))
    query_end_date = captured_at.date()
    query_start_date = query_end_date - timedelta(days=lookback_calendar_days)

    provider_name = _required_provider_text(provider.name, "provider.name")
    provider_version = _required_provider_text(provider.version, "provider.version")
    provider_source_url = _required_provider_text(provider.source_url, "provider.source_url")
    series_by_code: dict[str, DailySeries] = {}
    logged_in = False
    collection_error: Exception | None = None
    try:
        try:
            provider.login()
        except Exception as exc:
            raise CollectionError(f"provider login failed: {exc}") from exc
        logged_in = True
        for code in codes:
            try:
                raw_series = _fetch_canonical_or_legacy_series(
                    provider,
                    code,
                    start_date=query_start_date,
                    end_date=query_end_date,
                    is_benchmark=code in BENCHMARK_SYMBOLS,
                )
                series_by_code[code] = _validate_series(
                    code,
                    raw_series,
                    start_date=query_start_date,
                    end_date=query_end_date,
                )
            except Exception as exc:
                if isinstance(exc, CollectionError):
                    raise
                raise CollectionError(f"daily query failed for {code}: {exc}") from exc
    except Exception as exc:
        collection_error = exc
        raise
    finally:
        if logged_in:
            try:
                provider.logout()
            except Exception as exc:
                if collection_error is None:
                    raise CollectionError(f"provider logout failed: {exc}") from exc

    latest_session = _resolve_latest_market_session(series_by_code, captured_at)
    instruments = tuple(
        _derive_instrument(
            code,
            series_by_code[code],
            latest_session,
            is_benchmark=code in BENCHMARK_SYMBOLS,
        )
        for code in codes
    )
    return MarketDataCollection(
        retrieved_at=captured_at,
        query_start_date=query_start_date,
        query_end_date=query_end_date,
        latest_session=latest_session,
        provider_name=provider_name,
        provider_version=provider_version,
        provider_source_url=provider_source_url,
        instruments=instruments,
    )


def normalize_cn_symbol(raw_symbol: str) -> str:
    """Normalize A-share exchange codes to the internal ``sh.600000`` representation."""

    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise ValueError("candidate symbol must be a non-empty string")
    symbol = raw_symbol.strip().lower()
    direct = re.fullmatch(r"(sh|sz|bj)\.(\d{6})", symbol)
    if direct:
        return f"{direct.group(1)}.{direct.group(2)}"
    suffix = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", symbol)
    if suffix:
        return f"{suffix.group(2)}.{suffix.group(1)}"
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError(f"unsupported BaoStock symbol {raw_symbol!r}; use sh.600000 or 600000.SH")
    if symbol.startswith("6"):
        return f"sh.{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz.{symbol}"
    if symbol.startswith(("4", "8")):
        return f"bj.{symbol}"
    raise ValueError(
        f"exchange is ambiguous for {raw_symbol!r}; use an explicit sh./sz./bj. prefix"
    )


# Public compatibility name used by the seed/snapshot builder.
normalize_baostock_symbol = normalize_cn_symbol


def derive_adjacent_close_fields(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
    """Fill only safely derivable prior-close fields on sorted unadjusted raw bars."""

    result: list[DailyBar] = []
    previous: DailyBar | None = None
    origin = "derived_from_adjacent_unadjusted_close"
    for bar in bars:
        current = bar
        if (
            previous is not None
            and previous.trade_date < bar.trade_date
            and previous.close > 0
            and bar.preclose is None
            and bar.pct_chg is None
        ):
            provenance = dict(bar.field_provenance)
            provenance["preclose"] = origin
            provenance["pct_chg"] = origin
            current = replace(
                bar,
                preclose=previous.close,
                pct_chg=((bar.close / previous.close) - Decimal(1)) * Decimal(100),
                field_provenance=provenance,
            )
        result.append(current)
        previous = bar
    return tuple(result)


def _fetch_canonical_or_legacy_series(
    provider: Any,
    code: str,
    *,
    start_date: date,
    end_date: date,
    is_benchmark: bool,
) -> DailySeries:
    fetch_daily_series = getattr(provider, "fetch_daily_series", None)
    if callable(fetch_daily_series):
        return fetch_daily_series(
            code,
            start_date=start_date,
            end_date=end_date,
            is_benchmark=is_benchmark,
        )

    # Temporary compatibility for existing injected providers. The BaoStock row contract is
    # converted inside baostock.py before core validation.
    query_daily = getattr(provider, "query_daily", None)
    if not callable(query_daily):
        raise CollectionError("provider implements neither fetch_daily_series nor query_daily")
    try:
        rows = query_daily(
            code,
            fields=DAILY_FIELDS,
            start_date=start_date,
            end_date=end_date,
            adjustflag=UNADJUSTED_FLAG,
        )
    except Exception as exc:
        raise CollectionError(f"daily query failed for {code}: {exc}") from exc
    from .baostock import normalize_baostock_daily_rows

    return normalize_baostock_daily_rows(code, rows, is_benchmark=is_benchmark)


def _validate_series(
    expected_code: str,
    series: DailySeries,
    *,
    start_date: date,
    end_date: date,
) -> DailySeries:
    if not isinstance(series, DailySeries):
        raise CollectionError(f"provider returned non-canonical daily series for {expected_code}")
    if series.code != expected_code:
        raise CollectionError(
            f"{expected_code} provider series returned mismatched code {series.code}"
        )
    if series.adjustment != "none":
        raise CollectionError(
            f"{expected_code} series is adjusted (adjustment={series.adjustment}); "
            "only unadjusted data is accepted"
        )
    if not series.bars:
        raise CollectionError(f"no active trading sessions returned for {expected_code}")

    statuses: dict[date, str] = {}
    for session_date, status in series.session_statuses.items():
        if not isinstance(session_date, date) or isinstance(session_date, datetime):
            raise CollectionError(f"{expected_code} session status has invalid date")
        if session_date < start_date or session_date > end_date:
            raise CollectionError(
                f"{expected_code} session {session_date} falls outside the requested window"
            )
        if status not in _SESSION_STATUSES:
            raise CollectionError(
                f"{expected_code} has invalid trade status {status!r} on {session_date}"
            )
        statuses[session_date] = status

    bars_by_date: dict[date, DailyBar] = {}
    for index, bar in enumerate(series.bars):
        if not isinstance(bar, DailyBar):
            raise CollectionError(f"{expected_code} canonical bar {index} has invalid type")
        if bar.code != expected_code:
            raise CollectionError(
                f"{expected_code} canonical bar {index} returned mismatched code {bar.code}"
            )
        if bar.trade_date < start_date or bar.trade_date > end_date:
            raise CollectionError(
                f"{expected_code} bar {bar.trade_date} falls outside the requested window"
            )
        if bar.trade_date in bars_by_date:
            raise CollectionError(f"{expected_code} contains duplicate date {bar.trade_date}")
        if bar.trade_status not in _SESSION_STATUSES:
            raise CollectionError(
                f"{expected_code} bar {index} has invalid trade_status={bar.trade_status!r}"
            )
        if statuses.get(bar.trade_date, bar.trade_status) == "0":
            raise CollectionError(f"{expected_code} has an active bar marked suspended")
        _validate_canonical_bar(expected_code, bar, index)
        bars_by_date[bar.trade_date] = bar
        statuses.setdefault(bar.trade_date, bar.trade_status)

    if not statuses:
        raise CollectionError(f"{expected_code} has no session observations")
    metadata = _safe_metadata(series.metadata, f"{expected_code}.metadata")
    return DailySeries(
        code=expected_code,
        bars=tuple(bars_by_date[item] for item in sorted(bars_by_date)),
        session_statuses={item: statuses[item] for item in sorted(statuses)},
        metadata=metadata,
        adjustment="none",
    )


def _validate_canonical_bar(code: str, bar: DailyBar, index: int) -> None:
    for field_name in ("open", "high", "low", "close"):
        _validate_decimal_value(
            getattr(bar, field_name), f"{code} bar {index}.{field_name}", positive=True
        )
    for field_name in ("volume", "amount"):
        _validate_decimal_value(
            getattr(bar, field_name), f"{code} bar {index}.{field_name}", nonnegative=True
        )
    for field_name in ("preclose", "turn", "pct_chg"):
        value = getattr(bar, field_name)
        if value is not None:
            _validate_decimal_value(
                value,
                f"{code} bar {index}.{field_name}",
                positive=field_name == "preclose",
                nonnegative=field_name == "turn",
            )
    if bar.high < max(bar.open, bar.low, bar.close) or bar.low > min(bar.open, bar.high, bar.close):
        raise CollectionError(f"{code} bar {index} has inconsistent OHLC values")
    if bar.is_st is not None and not isinstance(bar.is_st, bool):
        raise CollectionError(f"{code} bar {index}.is_st must be boolean or None")
    for key, value in bar.field_provenance.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise CollectionError(f"{code} bar {index} has invalid field provenance")


def _validate_decimal_value(
    value: Any,
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CollectionError(f"{field_name} must be a finite Decimal")
    if positive and value <= 0:
        raise CollectionError(f"{field_name} must be positive")
    if nonnegative and value < 0:
        raise CollectionError(f"{field_name} must be non-negative")


def _resolve_latest_market_session(
    series_by_code: Mapping[str, DailySeries], captured_at: datetime
) -> date:
    benchmark_latest = {
        code: series_by_code[code].latest_observed_date for code in BENCHMARK_SYMBOLS
    }
    unique_dates = set(benchmark_latest.values())
    if len(unique_dates) != 1:
        details = ", ".join(f"{code}={value}" for code, value in benchmark_latest.items())
        raise CollectionError(f"benchmark latest dates disagree: {details}")
    latest_session = unique_dates.pop()
    for code in BENCHMARK_SYMBOLS:
        series = series_by_code[code]
        status = series.session_statuses[latest_session]
        if status == "0":
            raise CollectionError(f"latest benchmark session is not trading for {code}")
        if not any(bar.trade_date == latest_session for bar in series.bars):
            raise CollectionError(f"{code} has no active bar for latest session {latest_session}")
    session_close = datetime.combine(latest_session, time(15, 0), tzinfo=SHANGHAI_TZ)
    if session_close > captured_at:
        raise CollectionError(
            f"latest session {latest_session} is not closed at retrieved_at={captured_at.isoformat()}"
        )
    return latest_session


def _derive_instrument(
    code: str,
    series: DailySeries,
    latest_session: date,
    *,
    is_benchmark: bool,
) -> InstrumentMarketData:
    latest_status = series.session_statuses.get(latest_session)
    if latest_status is None:
        raise CollectionError(f"{code} is missing the latest market session {latest_session}")
    if latest_status == "0":
        raise CollectionError(f"{code} latest market session {latest_session} has tradestatus=0")
    bars = tuple(bar for bar in series.bars if bar.trade_date <= latest_session)
    if not bars or bars[-1].trade_date != latest_session:
        raise CollectionError(f"{code} has no active bar for latest session {latest_session}")
    if len(bars) < MINIMUM_ACTIVE_SESSIONS:
        raise CollectionError(
            f"{code} needs {MINIMUM_ACTIVE_SESSIONS} active sessions, received {len(bars)}"
        )
    if bars[-1].volume <= 0 or bars[-1].amount <= 0:
        raise CollectionError(f"{code} latest session has non-positive volume or amount")

    return_base = bars[-MINIMUM_ACTIVE_SESSIONS]
    return_pct = (bars[-1].close / return_base.close - Decimal(1)) * Decimal(100)
    average_amount = sum((bar.amount for bar in bars[-5:]), start=Decimal(0)) / Decimal(5)
    return InstrumentMarketData(
        code=code,
        instrument_kind="benchmark" if is_benchmark else "candidate",
        bars=bars,
        return_10_sessions_pct=return_pct,
        average_amount_5_sessions=average_amount,
        series_metadata=series.metadata,
    )


def _evidence_fragment(
    collection: MarketDataCollection, instrument: InstrumentMarketData
) -> dict[str, Any]:
    latest = instrument.latest
    retrieved_at = collection.retrieved_at.isoformat()
    as_of = datetime.combine(latest.trade_date, time(15, 0), tzinfo=SHANGHAI_TZ).isoformat()
    return_pct = _format_decimal(instrument.return_10_sessions_pct, places=4)
    average_amount = _format_decimal(instrument.average_amount_5_sessions, places=2)
    pct_chg = str(latest.pct_chg) if latest.pct_chg is not None else UNKNOWN
    summary = (
        f"{instrument.code} 在 {latest.trade_date.isoformat()} 收盘 {latest.close}，"
        f"当日涨跌幅 {pct_chg}%；10-session return {return_pct}%；"
        f"近5个活跃交易时段平均成交额 {average_amount} 元。"
    )
    unknown_fields = sorted(
        field_name
        for field_name, value in {
            "preclose": latest.preclose,
            "turn": latest.turn,
            "pct_chg": latest.pct_chg,
            "trade_status": None if latest.trade_status == UNKNOWN else latest.trade_status,
            "is_st": latest.is_st,
        }.items()
        if value is None
    )
    provider = {
        "name": collection.provider_name,
        "version": collection.provider_version,
        "query_start_date": collection.query_start_date.isoformat(),
        "query_end_date": collection.query_end_date.isoformat(),
        "frequency": "1d",
        "adjustment": "none",
        "series_metadata": dict(instrument.series_metadata),
    }
    return {
        "evidence_id": _evidence_id(collection.provider_name, instrument),
        "category": "market_data",
        "source_level": "structured_market",
        "title": f"{_provider_display_name(collection.provider_name)} 不复权日线：{instrument.code}",
        "source_url": collection.provider_source_url,
        "published_at": retrieved_at,
        "effective_at": as_of,
        "available_at": retrieved_at,
        "retrieved_at": retrieved_at,
        "as_of": as_of,
        "summary": summary,
        "provider": provider,
        "instrument": {
            "code": instrument.code,
            "kind": instrument.instrument_kind,
        },
        "latest": latest.to_dict(),
        "unknown_fields": unknown_fields,
        "derived": {
            "return_10_sessions_pct": return_pct,
            "return_base_date": instrument.return_base.trade_date.isoformat(),
            "average_amount_5_sessions": average_amount,
            "derivation_note": DERIVATION_NOTE,
        },
        "calculation_window": [bar.to_dict() for bar in instrument.bars[-MINIMUM_ACTIVE_SESSIONS:]],
        "pit_quality": "RECONSTRUCTED_NON_PIT",
        "non_pit_notice": NON_PIT_NOTICE,
        "coverage_note": (
            BENCHMARK_COVERAGE_NOTE
            if instrument.instrument_kind == "benchmark"
            else "Candidate-only observation; it does not represent market breadth."
        ),
        "time_semantics": (
            "effective_at/as_of are the session close; published_at and available_at are "
            "conservatively set to retrieved_at because the provider response has no immutable "
            "publication timestamp."
        ),
    }


def _evidence_id(provider_name: str, instrument: InstrumentMarketData) -> str:
    safe_code = instrument.code.replace(".", "-").upper()
    provider_slug = _provider_slug(provider_name)
    return f"MKT-{provider_slug}-{safe_code}-{instrument.latest.trade_date:%Y%m%d}"


def _provider_slug(provider_name: str) -> str:
    normalized = provider_name.casefold()
    if "baostock" in normalized:
        return "BAOSTOCK"
    if normalized.startswith("hithink") or "同花顺" in provider_name:
        return "HITHINK"
    slug = re.sub(r"[^A-Z0-9]+", "-", provider_name.upper()).strip("-")
    return slug or "PROVIDER"


def _provider_display_name(provider_name: str) -> str:
    if "baostock" in provider_name.casefold():
        return "BaoStock"
    if provider_name.casefold().startswith("hithink"):
        return "HiThink Financial API"
    return provider_name


def _safe_metadata(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise CollectionError(f"{field_name} contains a non-string metadata key")
            if raw_key.casefold() in _FORBIDDEN_METADATA_KEYS:
                raise CollectionError(f"{field_name} contains forbidden field {raw_key!r}")
            result[raw_key] = _safe_metadata(raw_value, f"{field_name}.{raw_key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, field_name) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CollectionError(f"{field_name} contains a naive datetime")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise CollectionError(f"{field_name} is not JSON safe") from exc
        return value
    raise CollectionError(f"{field_name} contains unsupported metadata type {type(value).__name__}")


def _normalize_retrieved_at(value: datetime | None) -> datetime:
    result = value or datetime.now(SHANGHAI_TZ)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("retrieved_at must include a timezone offset")
    return result.astimezone(SHANGHAI_TZ)


def _required_provider_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CollectionError(f"{field_name} must be a non-empty string")
    return value.strip()


def _format_decimal(value: Decimal, *, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f".{places}f")
