from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .hithink_client import (
    HITHINK_PROVIDER_NAME,
    HITHINK_PROVIDER_VERSION,
    HiThinkClient,
)
from .market_data import (
    SHANGHAI_TZ,
    UNKNOWN,
    CollectionError,
    DailyBar,
    DailySeries,
)

_STOCK_HISTORY_ENDPOINT = "/api/a-share/prices/historical"
_STOCK_SNAPSHOT_ENDPOINT = "/api/a-share/prices/snapshot"
_INDEX_HISTORY_ENDPOINT = "/api/a-share-index/prices/historical"
_INDEX_SNAPSHOT_ENDPOINT = "/api/a-share-index/prices/snapshot"
_CANONICAL_SYMBOL_PATTERN = re.compile(r"(sh|sz|bj)\.(\d{6})\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PCT_TOLERANCE = Decimal("0.0001")


class HiThinkGetClient(Protocol):
    def get(self, endpoint: str, params: Mapping[str, str | int]) -> Any: ...


class HiThinkProvider:
    """Provider-neutral adapter for HiThink's A-share and index daily endpoints."""

    name = HITHINK_PROVIDER_NAME
    source_url = "https://fuyao.aicubes.cn/docs/"

    def __init__(
        self,
        client: HiThinkGetClient | None = None,
        *,
        version: str | None = None,
    ) -> None:
        self._client = client or HiThinkClient()
        self.version = version or HITHINK_PROVIDER_VERSION

    def login(self) -> None:
        """REST authentication is performed per request by the credential-isolated client."""

    def logout(self) -> None:
        """The bounded REST client has no persistent authenticated session."""

    def fetch_daily_series(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
        is_benchmark: bool,
    ) -> DailySeries:
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise TypeError("start_date and end_date must be dates")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        thscode = self.to_hithink_symbol(code)
        history_endpoint = _INDEX_HISTORY_ENDPOINT if is_benchmark else _STOCK_HISTORY_ENDPOINT
        snapshot_endpoint = _INDEX_SNAPSHOT_ENDPOINT if is_benchmark else _STOCK_SNAPSHOT_ENDPOINT
        history_params: dict[str, str | int] = {
            "thscode": thscode,
            "interval": "1d",
            "start": _start_of_day_ms(start_date),
            "end": _end_of_day_ms(end_date),
        }
        if not is_benchmark:
            history_params["adjust"] = "none"

        history_response = self._get(history_endpoint, history_params)
        history_data = _response_data(history_response, history_endpoint)
        bars, upstream_timestamp = _parse_history(code, history_data)

        snapshot_params = {"thscodes": thscode}
        snapshot_response = self._get(snapshot_endpoint, snapshot_params)
        snapshot_data = _response_data(snapshot_response, snapshot_endpoint)
        bars, cross_check = _cross_check_latest_snapshot(
            thscode,
            bars,
            snapshot_data,
        )
        history_artifact = _response_artifact(history_response, history_endpoint)
        snapshot_artifact = _response_artifact(snapshot_response, snapshot_endpoint)
        raw_artifacts = [history_artifact, snapshot_artifact]
        retrieved_at = max(
            _response_retrieved_at(history_response, history_endpoint),
            _response_retrieved_at(snapshot_response, snapshot_endpoint),
        )
        return DailySeries(
            code=code,
            bars=bars,
            session_statuses={bar.trade_date: UNKNOWN for bar in bars},
            retrieved_at=retrieved_at,
            metadata={
                "adapter": "hithink-financial-api",
                "history_endpoint": history_endpoint,
                "snapshot_endpoint": snapshot_endpoint,
                "upstream_history_timestamp_ms": upstream_timestamp,
                "snapshot_cross_check": cross_check,
                "raw_artifacts": raw_artifacts,
                "time_semantics": (
                    "date_ms identifies the market observation date. HiThink history does not "
                    "supply immutable first-seen, suspension, ST or turnover-rate fields."
                ),
            },
            adjustment="none",
        )

    def _get(self, endpoint: str, params: Mapping[str, str | int]) -> Any:
        try:
            return self._client.get(endpoint, params)
        except Exception as exc:
            # Do not echo request parameters: a future provider extension may add values that
            # should not cross the collection boundary.
            raise CollectionError(f"HiThink request failed for {endpoint}: {exc}") from exc

    @staticmethod
    def to_hithink_symbol(code: str) -> str:
        if not isinstance(code, str):
            raise TypeError(f"unsupported canonical CN symbol {code!r}")
        match = _CANONICAL_SYMBOL_PATTERN.fullmatch(code.strip().lower())
        if match is None:
            raise ValueError(f"unsupported canonical CN symbol {code!r}")
        exchange, ticker = match.groups()
        return f"{ticker}.{exchange.upper()}"


# Descriptive alias for integrations that prefer the longer class name.
HiThinkMarketDataProvider = HiThinkProvider


def _response_data(response: Any, endpoint: str) -> Mapping[str, Any]:
    data = getattr(response, "data", None)
    if data is None and isinstance(response, Mapping):
        if "code" in response:
            code = response.get("code")
            if code != 0:
                raise CollectionError(f"HiThink {endpoint} returned business code {code!r}")
            data = response.get("data")
        else:
            data = response
    if not isinstance(data, Mapping):
        raise CollectionError(f"HiThink {endpoint} returned invalid data")
    return data


def _parse_history(code: str, data: Mapping[str, Any]) -> tuple[tuple[DailyBar, ...], int | None]:
    raw_items = data.get("item")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
        raise CollectionError(f"HiThink history for {code} omitted item list")
    if not raw_items:
        raise CollectionError(f"HiThink returned no historical daily bars for {code}")

    bars: list[DailyBar] = []
    seen_dates: set[date] = set()
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise CollectionError(f"HiThink history {code} item {index} is not an object")
        trade_date = _date_from_ms(raw_item.get("date_ms"), f"{code} item {index}.date_ms")
        if trade_date in seen_dates:
            raise CollectionError(f"HiThink history for {code} contains duplicate {trade_date}")
        seen_dates.add(trade_date)
        bars.append(
            DailyBar(
                trade_date=trade_date,
                code=code,
                open=_decimal(raw_item.get("open_price"), f"{code} item {index}.open_price"),
                high=_decimal(raw_item.get("high_price"), f"{code} item {index}.high_price"),
                low=_decimal(raw_item.get("low_price"), f"{code} item {index}.low_price"),
                close=_decimal(raw_item.get("close_price"), f"{code} item {index}.close_price"),
                preclose=None,
                volume=_decimal(raw_item.get("volume"), f"{code} item {index}.volume"),
                amount=_decimal(raw_item.get("turnover"), f"{code} item {index}.turnover"),
                turn=None,
                pct_chg=None,
                trade_status=UNKNOWN,
                is_st=None,
                field_provenance={
                    "open": "reported",
                    "high": "reported",
                    "low": "reported",
                    "close": "reported",
                    "preclose": UNKNOWN,
                    "volume": "reported",
                    "amount": "reported",
                    "turn": UNKNOWN,
                    "pct_chg": UNKNOWN,
                    "trade_status": UNKNOWN,
                    "is_st": UNKNOWN,
                },
            )
        )
    bars.sort(key=lambda bar: bar.trade_date)

    raw_timestamp = data.get("timestamp")
    if raw_timestamp is not None:
        _date_from_ms(raw_timestamp, f"{code} history timestamp")
        if _date_from_ms(raw_timestamp, f"{code} history timestamp") != bars[-1].trade_date:
            raise CollectionError(
                f"HiThink history timestamp does not identify the latest bar for {code}"
            )
    return tuple(bars), raw_timestamp


def _cross_check_latest_snapshot(
    thscode: str,
    bars: tuple[DailyBar, ...],
    data: Mapping[str, Any],
) -> tuple[tuple[DailyBar, ...], dict[str, Any]]:
    raw_items = data.get("item")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
        raise CollectionError(f"HiThink snapshot for {thscode} omitted item list")
    matching = [
        item
        for item in raw_items
        if isinstance(item, Mapping)
        and isinstance(item.get("thscode"), str)
        and item["thscode"].upper() == thscode
    ]
    if not matching:
        raise CollectionError(f"HiThink snapshot returned no matching entry for {thscode}")
    if len(matching) != 1:
        raise CollectionError(f"HiThink snapshot returned duplicate entries for {thscode}")

    item = matching[0]
    latest = bars[-1]
    expected = {
        "open": latest.open,
        "high": latest.high,
        "low": latest.low,
        "close": latest.close,
        "volume": latest.volume,
        "amount": latest.amount,
    }
    snapshot = {
        "open": _decimal(item.get("open_price"), f"{thscode} snapshot.open_price"),
        "high": _decimal(item.get("high_price"), f"{thscode} snapshot.high_price"),
        "low": _decimal(item.get("low_price"), f"{thscode} snapshot.low_price"),
        "close": _decimal(item.get("last_price"), f"{thscode} snapshot.last_price"),
        "volume": _decimal(item.get("volume"), f"{thscode} snapshot.volume"),
        "amount": _decimal(item.get("turnover"), f"{thscode} snapshot.turnover"),
    }
    mismatched = sorted(field for field in expected if expected[field] != snapshot[field])
    previous_close = _decimal(item.get("prev_price"), f"{thscode} snapshot.prev_price")
    pct_chg = _decimal(
        item.get("price_change_ratio_pct"),
        f"{thscode} snapshot.price_change_ratio_pct",
    )
    if previous_close <= 0:
        mismatched.append("preclose")
    calculated_pct = (
        ((snapshot["close"] / previous_close) - Decimal(1)) * Decimal(100)
        if previous_close > 0
        else None
    )
    if calculated_pct is None or abs(calculated_pct - pct_chg) > _PCT_TOLERANCE:
        mismatched.append("pct_chg")
    if mismatched:
        fields = ", ".join(sorted(set(mismatched)))
        raise CollectionError(f"HiThink snapshot mismatch for {thscode}: {fields}")

    provenance = dict(latest.field_provenance)
    provenance["preclose"] = "reported_snapshot_cross_validated"
    provenance["pct_chg"] = "reported_snapshot_cross_validated"
    updated_latest = replace(
        latest,
        preclose=previous_close,
        pct_chg=pct_chg,
        field_provenance=provenance,
    )
    return (*bars[:-1], updated_latest), {
        "status": "MATCHED",
        "matched_fields": sorted(expected),
        "note": (
            "Explicit snapshot values match the latest historical bar. The snapshot carries no "
            "session date, so this is a value cross-check rather than independent date evidence."
        ),
    }


def _response_artifact(response: Any, expected_endpoint: str) -> dict[str, Any]:
    if isinstance(response, Mapping):
        endpoint = response.get("endpoint", expected_endpoint)
        request_id = response.get("request_id")
        body_sha256 = response.get("body_sha256") or response.get("sha256")
        params = response.get("non_sensitive_params", {})
        artifact_id = response.get("raw_artifact_id")
    else:
        endpoint = getattr(response, "endpoint", None)
        request_id = getattr(response, "request_id", None)
        body_sha256 = getattr(response, "body_sha256", None)
        params = getattr(response, "non_sensitive_params", None)
        artifact_id = getattr(response, "raw_artifact_id", None)
    if endpoint != expected_endpoint:
        raise CollectionError(f"HiThink response endpoint mismatch for {expected_endpoint}")
    if not isinstance(request_id, str) or not request_id:
        raise CollectionError(f"HiThink {expected_endpoint} response omitted request_id")
    normalized_retrieved_at = _response_retrieved_at(response, expected_endpoint)
    if not isinstance(body_sha256, str) or _SHA256_PATTERN.fullmatch(body_sha256) is None:
        raise CollectionError(f"HiThink {expected_endpoint} response omitted valid SHA-256")
    if not isinstance(params, Mapping):
        raise CollectionError(f"HiThink {expected_endpoint} response omitted request parameters")
    artifact = {
        "endpoint": endpoint,
        "request_id": request_id,
        "retrieved_at": normalized_retrieved_at.isoformat(),
        "sha256": body_sha256,
        "params": dict(params),
    }
    if artifact_id is not None:
        if not isinstance(artifact_id, str) or not artifact_id:
            raise CollectionError(f"HiThink {expected_endpoint} returned invalid artifact id")
        artifact["artifact_id"] = artifact_id
    return artifact


def _response_retrieved_at(response: Any, expected_endpoint: str) -> datetime:
    retrieved_at = (
        response.get("retrieved_at")
        if isinstance(response, Mapping)
        else getattr(response, "retrieved_at", None)
    )
    if (
        not isinstance(retrieved_at, datetime)
        or retrieved_at.tzinfo is None
        or retrieved_at.utcoffset() is None
    ):
        raise CollectionError(f"HiThink {expected_endpoint} response omitted retrieved_at")
    return retrieved_at.astimezone(SHANGHAI_TZ)


def _decimal(value: Any, field_name: str) -> Decimal:
    if (
        isinstance(value, bool)
        or value is None
        or not isinstance(value, (str, int, float, Decimal))
    ):
        raise CollectionError(f"HiThink {field_name} is not numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise CollectionError(f"HiThink {field_name} is not numeric") from exc
    if not result.is_finite():
        raise CollectionError(f"HiThink {field_name} is not finite")
    return result


def _date_from_ms(value: Any, field_name: str) -> date:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CollectionError(f"HiThink {field_name} must be non-negative integer milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, tz=SHANGHAI_TZ).date()
    except (OverflowError, OSError, ValueError) as exc:
        raise CollectionError(f"HiThink {field_name} is outside the supported range") from exc


def _start_of_day_ms(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=SHANGHAI_TZ).timestamp() * 1000)


def _end_of_day_ms(value: date) -> int:
    next_day = datetime.combine(value + timedelta(days=1), time.min, tzinfo=SHANGHAI_TZ)
    return int(next_day.timestamp() * 1000) - 1
