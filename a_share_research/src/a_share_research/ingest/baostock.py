from __future__ import annotations

import importlib
import io
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib import metadata
from typing import Any

from .market_data import (
    DAILY_FIELDS,
    UNADJUSTED_FLAG,
    CollectionError,
    DailyBar,
    DailySeries,
    ProviderUnavailableError,
)


class BaoStockProvider:
    """Thin adapter around the optional official ``baostock`` Python client."""

    name = "baostock"
    source_url = "https://www.baostock.com/"

    def __init__(self, client: Any | None = None, *, version: str | None = None) -> None:
        self._client = client or self._import_client()
        self.version = version or self._detect_version(self._client)
        self._logged_in = False

    def login(self) -> None:
        try:
            result = self._call_silently(self._client.login)
        except Exception as exc:
            raise CollectionError(f"BaoStock login raised an exception: {exc}") from exc
        self._require_success(result, "login")
        self._logged_in = True

    def logout(self) -> None:
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            result = self._call_silently(self._client.logout)
        except Exception as exc:
            raise CollectionError(f"BaoStock logout raised an exception: {exc}") from exc
        # Older clients do not document a logout return contract.  Validate a status when one
        # is supplied, while accepting ``None`` after the socket-close call completed.
        if result is not None:
            self._require_success(result, "logout")

    def query_daily(
        self,
        code: str,
        *,
        fields: tuple[str, ...],
        start_date: date,
        end_date: date,
        adjustflag: str,
    ) -> Sequence[Mapping[str, str]]:
        if not self._logged_in:
            raise CollectionError("BaoStock query attempted before a successful login")
        try:
            result = self._call_silently(
                self._client.query_history_k_data_plus,
                code,
                ",".join(fields),
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                frequency="d",
                adjustflag=adjustflag,
            )
        except Exception as exc:
            raise CollectionError(f"BaoStock query raised an exception for {code}: {exc}") from exc
        self._require_success(result, f"query {code}")

        response_fields = getattr(result, "fields", None)
        if not isinstance(response_fields, (list, tuple)):
            raise CollectionError(f"BaoStock query {code} returned no field metadata")
        normalized_fields = tuple(str(field) for field in response_fields)
        missing_fields = sorted(set(fields) - set(normalized_fields))
        if missing_fields:
            raise CollectionError(
                f"BaoStock query {code} omitted fields: {', '.join(missing_fields)}"
            )

        rows: list[dict[str, str]] = []
        while True:
            try:
                has_next = result.next()
            except Exception as exc:
                raise CollectionError(
                    f"BaoStock result iteration failed for {code}: {exc}"
                ) from exc
            self._require_success(result, f"query iteration {code}")
            if not has_next:
                break
            try:
                values = result.get_row_data()
            except Exception as exc:
                raise CollectionError(f"BaoStock row decoding failed for {code}: {exc}") from exc
            if not isinstance(values, (list, tuple)) or len(values) != len(normalized_fields):
                raise CollectionError(f"BaoStock query {code} returned a malformed row")
            rows.append(dict(zip(normalized_fields, (str(value) for value in values), strict=True)))
        return rows

    def fetch_daily_series(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
        is_benchmark: bool,
    ) -> DailySeries:
        """Translate the provider-specific row shape into the canonical daily contract."""

        rows = self.query_daily(
            code,
            fields=DAILY_FIELDS,
            start_date=start_date,
            end_date=end_date,
            adjustflag=UNADJUSTED_FLAG,
        )
        series = normalize_baostock_daily_rows(code, rows, is_benchmark=is_benchmark)
        return DailySeries(
            code=series.code,
            bars=series.bars,
            session_statuses=series.session_statuses,
            adjustment=series.adjustment,
            metadata={
                "adapter": "baostock",
                "query": {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "frequency": "d",
                    "adjustment": "none",
                    "fields": list(DAILY_FIELDS),
                },
            },
        )

    @staticmethod
    def _import_client() -> Any:
        try:
            return importlib.import_module("baostock")
        except ModuleNotFoundError as exc:
            if exc.name != "baostock":
                raise
            raise ProviderUnavailableError(
                "BaoStock support is optional. Install it into the A-share environment with "
                "`uv pip install baostock`, then retry the collection."
            ) from exc

    @staticmethod
    def _detect_version(client: Any) -> str:
        try:
            return metadata.version("baostock")
        except metadata.PackageNotFoundError:
            client_version = getattr(client, "__version__", None)
            if isinstance(client_version, str) and client_version.strip():
                return client_version.strip()
            return "unknown"

    @staticmethod
    def _require_success(result: Any, operation: str) -> None:
        code = getattr(result, "error_code", None)
        message = getattr(result, "error_msg", "")
        if str(code) != "0":
            raise CollectionError(
                f"BaoStock {operation} failed: error_code={code!r}, error_msg={message!r}"
            )

    @staticmethod
    def _call_silently(function: Any, *args: Any, **kwargs: Any) -> Any:
        """Keep third-party console chatter out of the CLI's canonical JSON channel."""

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return function(*args, **kwargs)


def normalize_baostock_daily_rows(
    expected_code: str,
    rows: Sequence[Mapping[str, str]],
    *,
    is_benchmark: bool,
) -> DailySeries:
    """Canonicalize a BaoStock response without leaking its field contract into the core."""

    if not rows:
        raise CollectionError(f"no daily rows returned for {expected_code}")

    statuses: dict[date, str] = {}
    bars: list[DailyBar] = []
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
        bars.append(
            _parse_active_bar(
                expected_code,
                row,
                index,
                trade_date,
                allow_missing_turn=is_benchmark,
            )
        )

    if not bars:
        raise CollectionError(f"no active trading sessions returned for {expected_code}")
    bars.sort(key=lambda bar: bar.trade_date)
    return DailySeries(
        code=expected_code,
        bars=tuple(bars),
        session_statuses=statuses,
        metadata={"adapter": "baostock-row-normalizer"},
        adjustment="none",
    )


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
    is_st = _parse_optional_boolean(row, "isST", code, index)
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
        is_st=is_st,
        field_provenance={
            "open": "reported",
            "high": "reported",
            "low": "reported",
            "close": "reported",
            "preclose": "reported",
            "volume": "reported",
            "amount": "reported",
            "turn": "reported" if turn is not None else "UNKNOWN",
            "pct_chg": "reported",
            "trade_status": "reported",
            "is_st": "reported" if is_st is not None else "UNKNOWN",
        },
    )


def _parse_trade_date(row: Mapping[str, str], code: str, index: int) -> date:
    raw = _required_row_text(row, "date", code, index)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise CollectionError(f"{code} row {index} has invalid date={raw!r}") from exc


def _required_row_text(row: Mapping[str, str], field: str, code: str, index: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CollectionError(f"{code} row {index} is missing {field}")
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
