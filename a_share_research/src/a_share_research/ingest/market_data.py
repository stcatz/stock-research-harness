from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
BENCHMARK_SYMBOLS = ("sh.000001", "sz.399001", "sz.399006")
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
NON_PIT_NOTICE = (
    "BaoStock is queried on demand and does not provide an immutable first-seen timestamp. "
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


class CollectionError(RuntimeError):
    """Raised when provider data cannot support an honest market-data fragment."""


class ProviderUnavailableError(CollectionError):
    """Raised when an optional data-provider package is unavailable."""


class DailyMarketDataProvider(Protocol):
    """Minimal provider boundary used by the deterministic collector."""

    name: str
    version: str
    source_url: str

    def login(self) -> None: ...

    def logout(self) -> None: ...

    def query_daily(
        self,
        code: str,
        *,
        fields: tuple[str, ...],
        start_date: date,
        end_date: date,
        adjustflag: str,
    ) -> Sequence[Mapping[str, str]]: ...


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    code: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    preclose: Decimal
    volume: Decimal
    amount: Decimal
    turn: Decimal | None
    pct_chg: Decimal
    trade_status: str
    is_st: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.trade_date.isoformat(),
            "code": self.code,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "preclose": str(self.preclose),
            "volume": str(self.volume),
            "amount": str(self.amount),
            "turn": str(self.turn) if self.turn is not None else None,
            "pct_chg": str(self.pct_chg),
            "trade_status": self.trade_status,
            "is_st": self.is_st,
            "adjustflag": UNADJUSTED_FLAG,
        }


@dataclass(frozen=True)
class InstrumentMarketData:
    code: str
    instrument_kind: str
    bars: tuple[DailyBar, ...]
    return_10_sessions_pct: Decimal
    average_amount_5_sessions: Decimal

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
                "return_10_sessions_pct": _format_decimal(
                    self.return_10_sessions_pct, places=4
                ),
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
            _evidence_id(instrument)
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
                "fields": list(DAILY_FIELDS),
                "frequency": "d",
                "adjustflag": UNADJUSTED_FLAG,
            },
            "latest_session": self.latest_session.isoformat(),
            "pit_quality": "RECONSTRUCTED_NON_PIT",
            "non_pit_notice": NON_PIT_NOTICE,
            "coverage_note": BENCHMARK_COVERAGE_NOTE,
            "instruments": [instrument.to_dict() for instrument in self.instruments],
            "evidence": self.evidence_fragments(),
            "market_context": self.market_context_fragment(),
        }


@dataclass(frozen=True)
class _ParsedSeries:
    active_bars: tuple[DailyBar, ...]
    statuses: Mapping[date, str]

    @property
    def latest_observed_date(self) -> date:
        return max(self.statuses)


def collect_cn_market_data(
    candidate_symbols: Sequence[str],
    *,
    provider: DailyMarketDataProvider,
    retrieved_at: datetime | None = None,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
) -> MarketDataCollection:
    """Collect auditable candidate and benchmark observations without claiming PIT quality.

    BaoStock sessions are queried sequentially because its client keeps login state.  The
    supplied ``retrieved_at`` makes tests and replays deterministic; production callers should
    omit it and persist the returned collection immediately.
    """

    captured_at = _normalize_retrieved_at(retrieved_at)
    if lookback_calendar_days < 20:
        raise ValueError("lookback_calendar_days must be at least 20")

    candidates = tuple(sorted({normalize_baostock_symbol(symbol) for symbol in candidate_symbols}))
    codes = (*BENCHMARK_SYMBOLS, *(code for code in candidates if code not in BENCHMARK_SYMBOLS))
    query_end_date = captured_at.date()
    query_start_date = query_end_date - timedelta(days=lookback_calendar_days)

    provider_name = _required_provider_text(provider.name, "provider.name")
    provider_version = _required_provider_text(provider.version, "provider.version")
    provider_source_url = _required_provider_text(provider.source_url, "provider.source_url")
    raw_by_code: dict[str, Sequence[Mapping[str, str]]] = {}
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
                raw_by_code[code] = provider.query_daily(
                    code,
                    fields=DAILY_FIELDS,
                    start_date=query_start_date,
                    end_date=query_end_date,
                    adjustflag=UNADJUSTED_FLAG,
                )
            except Exception as exc:
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

    parsed_by_code = {
        code: _parse_series(code, rows, is_benchmark=code in BENCHMARK_SYMBOLS)
        for code, rows in raw_by_code.items()
    }
    latest_session = _resolve_latest_market_session(parsed_by_code, captured_at)

    instruments = tuple(
        _derive_instrument(
            code,
            parsed_by_code[code],
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


def normalize_baostock_symbol(raw_symbol: str) -> str:
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
        raise ValueError(
            f"unsupported BaoStock symbol {raw_symbol!r}; use sh.600000 or 600000.SH"
        )
    if symbol.startswith("6"):
        return f"sh.{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz.{symbol}"
    if symbol.startswith(("4", "8")):
        return f"bj.{symbol}"
    raise ValueError(
        f"exchange is ambiguous for {raw_symbol!r}; use an explicit sh./sz./bj. prefix"
    )


def _normalize_retrieved_at(value: datetime | None) -> datetime:
    result = value or datetime.now(SHANGHAI_TZ)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("retrieved_at must include a timezone offset")
    return result.astimezone(SHANGHAI_TZ)


def _parse_series(
    expected_code: str,
    rows: Sequence[Mapping[str, str]],
    *,
    is_benchmark: bool,
) -> _ParsedSeries:
    if not rows:
        raise CollectionError(f"no daily rows returned for {expected_code}")

    statuses: dict[date, str] = {}
    active_bars: list[DailyBar] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise CollectionError(f"{expected_code} row {index} is not a mapping")
        row_code = _required_row_text(row, "code", expected_code, index).lower()
        if row_code != expected_code:
            raise CollectionError(
                f"{expected_code} row {index} returned mismatched code {row_code}"
            )
        trade_date = _parse_trade_date(row, expected_code, index)
        if trade_date in statuses:
            raise CollectionError(f"{expected_code} contains duplicate date {trade_date}")
        adjustflag = _required_row_text(row, "adjustflag", expected_code, index)
        if adjustflag != UNADJUSTED_FLAG:
            raise CollectionError(
                f"{expected_code} row {index} is adjusted (adjustflag={adjustflag}); "
                "only unadjusted data is accepted"
            )
        trade_status = _required_row_text(row, "tradestatus", expected_code, index)
        if trade_status not in {"0", "1"}:
            raise CollectionError(
                f"{expected_code} row {index} has invalid tradestatus={trade_status!r}"
            )
        statuses[trade_date] = trade_status
        if trade_status == "0":
            continue
        active_bars.append(
            _parse_active_bar(
                expected_code,
                row,
                index,
                trade_date,
                allow_missing_turn=is_benchmark,
            )
        )

    if not active_bars:
        raise CollectionError(f"no active trading sessions returned for {expected_code}")
    active_bars.sort(key=lambda bar: bar.trade_date)
    normalized_active_dates = tuple(bar.trade_date for bar in active_bars)
    expected_active_dates = tuple(
        sorted(session_date for session_date, status in statuses.items() if status == "1")
    )
    if normalized_active_dates != expected_active_dates:
        raise CollectionError(f"{expected_code} active-session normalization failed")
    return _ParsedSeries(active_bars=tuple(active_bars), statuses=statuses)


def _parse_active_bar(
    code: str,
    row: Mapping[str, str],
    index: int,
    trade_date: date,
    *,
    allow_missing_turn: bool,
) -> DailyBar:
    open_price = _parse_decimal(row, "open", code, index, positive=True)
    high = _parse_decimal(row, "high", code, index, positive=True)
    low = _parse_decimal(row, "low", code, index, positive=True)
    close = _parse_decimal(row, "close", code, index, positive=True)
    preclose = _parse_decimal(row, "preclose", code, index, positive=True)
    volume = _parse_decimal(row, "volume", code, index, nonnegative=True)
    amount = _parse_decimal(row, "amount", code, index, nonnegative=True)
    turn = _parse_optional_decimal(
        row,
        "turn",
        code,
        index,
        allow_missing=allow_missing_turn,
        nonnegative=True,
    )
    pct_chg = _parse_decimal(row, "pctChg", code, index)
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        raise CollectionError(f"{code} row {index} has inconsistent OHLC values")
    return DailyBar(
        trade_date=trade_date,
        code=code,
        open=open_price,
        high=high,
        low=low,
        close=close,
        preclose=preclose,
        volume=volume,
        amount=amount,
        turn=turn,
        pct_chg=pct_chg,
        trade_status="1",
        is_st=_parse_optional_boolean(row, "isST", code, index),
    )


def _resolve_latest_market_session(
    parsed_by_code: Mapping[str, _ParsedSeries], captured_at: datetime
) -> date:
    benchmark_latest = {
        code: parsed_by_code[code].latest_observed_date for code in BENCHMARK_SYMBOLS
    }
    unique_dates = set(benchmark_latest.values())
    if len(unique_dates) != 1:
        details = ", ".join(f"{code}={value}" for code, value in benchmark_latest.items())
        raise CollectionError(f"benchmark latest dates disagree: {details}")
    latest_session = unique_dates.pop()
    for code in BENCHMARK_SYMBOLS:
        if parsed_by_code[code].statuses[latest_session] != "1":
            raise CollectionError(f"latest benchmark session is not trading for {code}")
    session_close = datetime.combine(latest_session, time(15, 0), tzinfo=SHANGHAI_TZ)
    if session_close > captured_at:
        raise CollectionError(
            f"latest session {latest_session} is not closed at retrieved_at={captured_at.isoformat()}"
        )
    return latest_session


def _derive_instrument(
    code: str,
    parsed: _ParsedSeries,
    latest_session: date,
    *,
    is_benchmark: bool,
) -> InstrumentMarketData:
    latest_status = parsed.statuses.get(latest_session)
    if latest_status is None:
        raise CollectionError(f"{code} is missing the latest market session {latest_session}")
    if latest_status != "1":
        raise CollectionError(
            f"{code} latest market session {latest_session} has tradestatus={latest_status}"
        )
    bars = tuple(bar for bar in parsed.active_bars if bar.trade_date <= latest_session)
    if not bars or bars[-1].trade_date != latest_session:
        raise CollectionError(f"{code} has no active bar for latest session {latest_session}")
    if len(bars) < MINIMUM_ACTIVE_SESSIONS:
        raise CollectionError(
            f"{code} needs {MINIMUM_ACTIVE_SESSIONS} active sessions, received {len(bars)}"
        )
    if bars[-1].volume <= 0 or bars[-1].amount <= 0:
        raise CollectionError(f"{code} latest session has non-positive volume or amount")
    if not is_benchmark and bars[-1].turn is None:
        raise CollectionError(f"{code} latest session is missing turnover")

    return_base = bars[-MINIMUM_ACTIVE_SESSIONS]
    return_pct = (bars[-1].close / return_base.close - Decimal(1)) * Decimal(100)
    average_amount = sum((bar.amount for bar in bars[-5:]), start=Decimal(0)) / Decimal(5)
    return InstrumentMarketData(
        code=code,
        instrument_kind="benchmark" if is_benchmark else "candidate",
        bars=bars,
        return_10_sessions_pct=return_pct,
        average_amount_5_sessions=average_amount,
    )


def _evidence_fragment(
    collection: MarketDataCollection, instrument: InstrumentMarketData
) -> dict[str, Any]:
    latest = instrument.latest
    retrieved_at = collection.retrieved_at.isoformat()
    as_of = datetime.combine(latest.trade_date, time(15, 0), tzinfo=SHANGHAI_TZ).isoformat()
    return_pct = _format_decimal(instrument.return_10_sessions_pct, places=4)
    average_amount = _format_decimal(instrument.average_amount_5_sessions, places=2)
    summary = (
        f"{instrument.code} 在 {latest.trade_date.isoformat()} 收盘 {latest.close}，"
        f"当日涨跌幅 {latest.pct_chg}%；10-session return {return_pct}%；"
        f"近5个活跃交易时段平均成交额 {average_amount} 元。"
    )
    return {
        "evidence_id": _evidence_id(instrument),
        "category": "market_data",
        "source_level": "structured_market",
        "title": f"BaoStock 不复权日线：{instrument.code}",
        "source_url": collection.provider_source_url,
        # BaoStock does not expose publication/first-seen timestamps.  Retrieval time is the
        # conservative availability boundary and the explicit non-PIT notice prevents overclaiming.
        "published_at": retrieved_at,
        "available_at": retrieved_at,
        "retrieved_at": retrieved_at,
        "as_of": as_of,
        "summary": summary,
        "provider": {
            "name": collection.provider_name,
            "version": collection.provider_version,
            "query_start_date": collection.query_start_date.isoformat(),
            "query_end_date": collection.query_end_date.isoformat(),
            "frequency": "d",
            "adjustflag": UNADJUSTED_FLAG,
            "fields": list(DAILY_FIELDS),
        },
        "instrument": {
            "code": instrument.code,
            "kind": instrument.instrument_kind,
        },
        "latest": latest.to_dict(),
        "derived": {
            "return_10_sessions_pct": return_pct,
            "return_base_date": instrument.return_base.trade_date.isoformat(),
            "average_amount_5_sessions": average_amount,
            "derivation_note": DERIVATION_NOTE,
        },
        "calculation_window": [
            bar.to_dict() for bar in instrument.bars[-MINIMUM_ACTIVE_SESSIONS:]
        ],
        "pit_quality": "RECONSTRUCTED_NON_PIT",
        "non_pit_notice": NON_PIT_NOTICE,
        "coverage_note": (
            BENCHMARK_COVERAGE_NOTE
            if instrument.instrument_kind == "benchmark"
            else "Candidate-only observation; it does not represent market breadth."
        ),
        "time_semantics": (
            "published_at and available_at are conservatively set to retrieved_at because "
            "the provider response has no immutable publication timestamp."
        ),
    }


def _evidence_id(instrument: InstrumentMarketData) -> str:
    safe_code = instrument.code.replace(".", "-").upper()
    return f"MKT-BAOSTOCK-{safe_code}-{instrument.latest.trade_date:%Y%m%d}"


def _parse_trade_date(row: Mapping[str, str], code: str, index: int) -> date:
    raw = _required_row_text(row, "date", code, index)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise CollectionError(f"{code} row {index} has invalid date={raw!r}") from exc


def _required_row_text(
    row: Mapping[str, str], field: str, code: str, index: int
) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CollectionError(f"{code} row {index} is missing {field}")
    return value.strip()


def _required_provider_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CollectionError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_decimal(
    row: Mapping[str, str],
    field: str,
    code: str,
    index: int,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    raw = _required_row_text(row, field, code, index)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CollectionError(f"{code} row {index} has invalid {field}={raw!r}") from exc
    if not value.is_finite():
        raise CollectionError(f"{code} row {index} has non-finite {field}={raw!r}")
    if positive and value <= 0:
        raise CollectionError(f"{code} row {index} requires positive {field}")
    if nonnegative and value < 0:
        raise CollectionError(f"{code} row {index} requires non-negative {field}")
    return value


def _parse_optional_decimal(
    row: Mapping[str, str],
    field: str,
    code: str,
    index: int,
    *,
    allow_missing: bool,
    nonnegative: bool,
) -> Decimal | None:
    raw = row.get(field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_missing:
            return None
        raise CollectionError(f"{code} row {index} is missing {field}")
    return _parse_decimal(row, field, code, index, nonnegative=nonnegative)


def _parse_optional_boolean(
    row: Mapping[str, str], field: str, code: str, index: int
) -> bool | None:
    raw = row.get(field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if raw == "1":
        return True
    if raw == "0":
        return False
    raise CollectionError(f"{code} row {index} has invalid {field}={raw!r}")


def _format_decimal(value: Decimal, *, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")
