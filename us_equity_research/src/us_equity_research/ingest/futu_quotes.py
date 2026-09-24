"""Read-only Futu REST collection. Never persists raw responses or credentials.

Unverified publication times remain UNKNOWN in the staging bundle. This bundle
is not by itself eligible for the strict SEC market-evidence contract.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.contracts import ContractError, parse_datetime, reject_unknown_fields

ORIGIN = "https://webapi.futunn.com"
NY = ZoneInfo("America/New_York")
SYMBOL = re.compile(r"^US\.[A-Z][A-Z0-9.-]{0,14}$")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError("Futu redirect refused")


class FutuQuoteClient:
    provider = "futu_rest"

    def source_url(self, symbol):
        return ORIGIN + f"/api/v1.0/quote/{symbol}/history-kline"

    def __init__(self, token=None, transport=None):
        self._token = token or os.environ.get("FUTU_QUOTE_ACCESS_TOKEN")
        if transport is None and not self._token:
            raise ContractError(
                "Futu quote authentication is missing; complete official quote:read authorization"
            )
        self._transport = transport

    def get(self, path, params):
        if not (
            path == "/api/v1.0/quote/trading-days"
            or re.fullmatch(r"/api/v1\.0/quote/US\.[A-Z][A-Z0-9.-]{0,14}/history-kline", path)
        ):
            raise ContractError("Only US quote history and trading calendar endpoints are allowed")
        if self._transport:
            payload = self._transport(path, params)
        else:
            request = urllib.request.Request(
                ORIGIN + path + "?" + urllib.parse.urlencode(params),
                headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"},
                method="GET",
            )
            try:
                with urllib.request.build_opener(NoRedirect()).open(
                    request, timeout=25
                ) as response:
                    raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ContractError("Futu response exceeds size limit")
                payload = json.loads(raw)
            except (urllib.error.URLError, OSError, ValueError):
                raise ContractError(
                    "Futu request failed; verify quote permission and connectivity"
                ) from None
        if (
            not isinstance(payload, dict)
            or type(payload.get("ret_code")) is not int
            or payload.get("ret_code") != 0
            or not isinstance(payload.get("data"), dict)
        ):
            raise ContractError("Futu returned an unsuccessful or invalid quote response")
        return payload["data"]

    def history(self, symbol, start, end, autype):
        if not SYMBOL.fullmatch(symbol):
            raise ContractError("Only explicit US symbols are allowed")
        cursor, seen, rows = end, set(), []
        for _ in range(100):
            data = self.get(
                f"/api/v1.0/quote/{symbol}/history-kline",
                {
                    "start": start,
                    "end": cursor,
                    "num": 370,
                    "ktype": 2,
                    "autype": autype,
                    "extended_time": 0,
                },
            )
            page = data.get("kline_list")
            if not isinstance(page, list):
                raise ContractError("Futu history list is missing")
            precision = data.get("volume_precision", 0)
            if type(precision) is not int or not 0 <= precision <= 8:
                raise ContractError("Invalid volume precision")
            rows.extend((row, precision) for row in page)
            next_time = data.get("next_time")
            if next_time in (None, 0):
                return rows
            if type(next_time) is not int or next_time <= 0 or next_time in seen:
                raise ContractError("Invalid or repeated Futu history cursor")
            if seen and next_time >= min(seen):
                raise ContractError("Futu history cursor did not move backwards")
            seen.add(next_time)
            cursor = next_time  # REST documentation: pass server next_time unchanged as end.
        raise ContractError("Futu history pagination exceeded limit")


class FutuMCPQuoteClient(FutuQuoteClient):
    """Quote-only MCP adapter; the caller owns authentication and RPC transport.

    MCP has a different timezone unit and pagination envelope from REST.
    Bound requests to 300 calendar days and reject any unconsumed continuation.
    Never read the host's credential store from the research engine.
    """

    provider = "futu_mcp"

    def __init__(self, rpc):
        super().__init__(transport=self._request)
        self._rpc = rpc

    def source_url(self, symbol):
        return "https://mcp.futunn.com/mcp#quote_history_kline"

    def _request(self, path, params):
        if path == "/api/v1.0/quote/trading-days":
            if params.get("market") != "US":
                raise ContractError("Only US calendar requests are allowed")
            name, args = "quote_trading_days", dict(params)
        else:
            name = "quote_history_kline"
            args = {**params, "symbol": path.split("/")[-2]}
        try:
            result = self._rpc(name, args)
            if result.get("isError"):
                raise ValueError("MCP tool failed")
            texts = [b["text"] for b in result.get("content", []) if b.get("type") == "text"]
            if len(texts) != 1:
                raise ValueError("Expected one structured response")
            payload = json.loads(texts[0])
            pagination = payload.get("pagination", {})
            if pagination.get("has_more") is not False and name == "quote_history_kline":
                raise ValueError("Incomplete or unknown pagination")
            if name == "quote_history_kline":
                for row in payload["data"]["kline_list"]:
                    # Live MCP exposes hours (-4/-5), REST documentation uses minutes.
                    offset = row.get("time_zone")
                    if type(offset) is not int or offset not in (-4, -5):
                        raise ValueError("Unsupported US MCP timezone")
                    row["time_zone"] = offset * 60
            return payload
        except (ValueError, TypeError, KeyError, AttributeError, OSError, RuntimeError):
            raise ContractError(
                "Futu MCP response failed validation; no partial result accepted"
            ) from None

    def history(self, symbol, start, end, autype):
        from datetime import timedelta

        if not SYMBOL.fullmatch(symbol) or autype not in (0, 1):
            raise ContractError("Only explicit US daily research series are allowed")
        current, final = date.fromisoformat(start), date.fromisoformat(end)
        rows = []
        while current <= final:
            last = min(final, current + timedelta(days=299))
            rows.extend(super().history(symbol, current.isoformat(), last.isoformat(), autype))
            current = last + timedelta(days=1)
        return rows


def validate_plan(plan):
    reject_unknown_fields(
        plan, {"symbols", "benchmark", "start", "end", "license_attestation"}, "futu plan"
    )
    if plan.get("license_attestation") != "authorized_for_local_research_snapshot":
        raise ContractError("Local research snapshot authorization must be explicitly confirmed")
    symbols = plan.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20:
        raise ContractError("Plan requires 1-20 explicit symbols")
    if any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in symbols) or len(
        set(symbols)
    ) != len(symbols):
        raise ContractError("Invalid or duplicate US symbols")
    if plan.get("benchmark") not in symbols:
        raise ContractError("Benchmark must be included in the frozen symbols")
    start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    if start > end or (end - start).days > 550:
        raise ContractError("Plan date range must be ordered and at most 550 days")


def _number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"Invalid {label}")
    if value < 0 or (positive and value == 0):
        raise ContractError(f"Invalid {label}")
    return value


def normalize(rows, symbol, start, end, retrieved):
    by_date = {}
    for row, precision in rows:
        if not isinstance(row, dict):
            raise ContractError("Invalid Futu bar")
        if row.get("code", symbol) != symbol:
            raise ContractError("Futu returned a different security")
        day = date.fromisoformat(str(row.get("date"))).isoformat()
        if not start <= day <= end:
            continue
        stamp = _number(row.get("time_key"), "bar timestamp", positive=True)
        ny = datetime.fromtimestamp(stamp / 1000, UTC).astimezone(NY)
        if ny.date().isoformat() != day:
            raise ContractError("Futu bar date and New York timestamp disagree")
        offset = row.get("time_zone")
        if offset is not None and offset != int(ny.utcoffset().total_seconds() / 60):
            raise ContractError("Futu timezone disagrees with America/New_York")
        bar = {k: _number(row.get(k), k, positive=True) for k in ("open", "high", "low", "close")}
        if (
            not bar["low"]
            <= min(bar["open"], bar["close"])
            <= max(bar["open"], bar["close"])
            <= bar["high"]
        ):
            raise ContractError("Futu OHLC values are inconsistent")
        bar.update(
            date=day,
            volume=_number(row.get("volume"), "volume") / (10**precision),
            bar_label_at=ny.isoformat(),
            retrieved_at=retrieved,
            available_at=retrieved,
            published_at="UNKNOWN",
            effective_at="UNKNOWN",
            as_of="UNKNOWN",
            availability_basis="first_observed_at_collection",
        )
        if day in by_date and by_date[day] != bar:
            raise ContractError("Conflicting duplicate Futu daily bar")
        by_date[day] = bar
    return [by_date[d] for d in sorted(by_date)]


def collect(plan, client=None, clock=None):
    validate_plan(plan)
    client = client or FutuQuoteClient()
    clock = clock or (lambda: datetime.now(UTC))
    began = clock()
    if date.fromisoformat(plan["end"]) > began.astimezone(NY).date():
        raise ContractError("Futu plan end must not be in the future")
    calendar = client.get(
        "/api/v1.0/quote/trading-days", {"market": "US", "start": plan["start"], "end": plan["end"]}
    )
    days = calendar.get("trading_days")
    if not isinstance(days, list):
        raise ContractError("Futu trading calendar is missing")
    normalized_days = []
    for d in days:
        day = date.fromisoformat(d["time"]).isoformat()
        seconds = d.get("trade_second")
        if not plan["start"] <= day <= plan["end"] or type(seconds) is not int or seconds <= 0:
            raise ContractError("Invalid supplier calendar day")
        normalized_days.append(
            {"date": day, "trade_second": seconds, "type": str(d.get("trade_date_type", "UNKNOWN"))}
        )
    if len({d["date"] for d in normalized_days}) != len(normalized_days):
        raise ContractError("Duplicate supplier calendar day")
    securities = []
    for symbol in plan["symbols"]:
        raw = client.history(symbol, plan["start"], plan["end"], 0)
        adjusted = client.history(symbol, plan["start"], plan["end"], 1)
        received = clock()
        if received < began:
            raise ContractError("Collection clock moved backwards")
        stamp = received.isoformat()
        securities.append(
            {
                "symbol": symbol,
                "unadjusted": normalize(raw, symbol, plan["start"], plan["end"], stamp),
                "split_adjusted": normalize(adjusted, symbol, plan["start"], plan["end"], stamp),
                "source_url": client.source_url(symbol),
                "publication_time_status": "UNKNOWN",
                "formal_evidence_eligible": False,
            }
        )
    return {
        "schema_version": "futu-stage-1",
        "market": "US",
        "provider": client.provider,
        "plan": plan,
        "retrieved_at": clock().isoformat(),
        "supplier_calendar": normalized_days,
        "securities": securities,
        "status": "STAGED_NOT_FORMAL_EVIDENCE",
        "data_gaps": [
            "Publication time unavailable; original availability is not reconstructed.",
            "Supplier calendar requires exchange verification of exact session times.",
            "No real-time or full-market coverage claim; adjusted bars are current historical versions.",
        ],
    }


def write_new_bundle(bundle, output):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves earlier collections. Never store raw provider payloads.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def observation_report(bundle, session_calendar, decision_at):
    """Render staged observations, never official candidate decisions or PIT claims."""
    cutoff = parse_datetime(decision_at, "decision_at")
    if bundle.get("schema_version") != "futu-stage-1" or bundle.get("market") != "US":
        raise ContractError("Unsupported Futu staging bundle")
    if parse_datetime(bundle["retrieved_at"], "retrieved_at") > cutoff:
        raise ContractError("Staging data was retrieved after the requested decision time")
    # Calendar is an explicit reviewed input; don't infer 16:00 from a weekday.
    from ..core.research_diagnostics import validate_session_calendar

    cal = session_calendar["session_calendar"]
    evidence = session_calendar["evidence"]
    validate_session_calendar({"session_calendar": cal}, {evidence["evidence_id"]: evidence})
    if parse_datetime(evidence["available_at"], "calendar.available_at") > cutoff:
        raise ContractError("Calendar unavailable at decision time")
    if not cal["coverage_start"] <= cutoff.astimezone(NY).date().isoformat() <= cal["coverage_end"]:
        raise ContractError("Calendar does not cover decision time")
    expected = [
        r["date"] for r in cal["sessions"] if parse_datetime(r["close_at"], "close_at") <= cutoff
    ]
    if not expected:
        raise ContractError("No completed sessions in reviewed calendar")
    latest = expected[-1]
    rows = {
        s["symbol"]: {
            b["date"]: b
            for b in s["split_adjusted"]
            if parse_datetime(b["available_at"], "available_at") <= cutoff
        }
        for s in bundle["securities"]
    }

    def change(symbol, k):
        if len(expected) < k + 1:
            return None
        required = expected[-k - 1 :]
        series = rows.get(symbol, {})
        if any(d not in series for d in required):
            return None
        return series[latest]["close"] / series[required[0]]["close"] - 1

    benchmark = bundle["plan"]["benchmark"]

    def display(value):
        return "UNKNOWN" if value is None else f"{value:.2%}"

    lines = [
        "# 美股行情观察附页（待验数据）",
        "",
        f"- 生成时间：{datetime.now(UTC).isoformat()}",
        f"- decision_at：{decision_at}",
        f"- 数据取得时间：{bundle['retrieved_at']}；最近完整交易日：{latest}",
        "- 性质：供应商历史数据的本次观察；发布时间 UNKNOWN，不构成严格 PIT 或正式候选证据。",
        "- 回报使用当前拆股复权版本，不含股息；不代表全市场广度。",
        f"- 基准：{benchmark}；仅比较冻结研究名单。",
        "",
        "| 标的 | 1日 | 5日 | 20日 | 60日 | 20日相对基准 | 成交量/此前20日均量 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for security in bundle["securities"]:
        symbol = security["symbol"]
        returns = [change(symbol, k) for k in [1, 5, 20, 60]]
        base = change(benchmark, 20)
        relative = returns[2] - base if returns[2] is not None and base is not None else None
        series = rows[symbol]
        volume_ratio = None
        if len(expected) >= 21 and all(d in series for d in expected[-21:]):
            prior = sum(series[d]["volume"] for d in expected[-21:-1]) / 20
            if prior > 0:
                volume_ratio = series[latest]["volume"] / prior
        lines.append(
            "| "
            + symbol
            + " | "
            + " | ".join(
                [
                    *(display(r) for r in returns),
                    display(relative),
                    "UNKNOWN" if volume_ratio is None else f"{volume_ratio:.2f}",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 来源与时间边界",
            "",
            *[
                f"- {s['symbol']}：[行情来源]({s.get('source_url', 'https://mcp.futunn.com/mcp')})；"
                "等级：结构化供应商待验数据；published_at / effective_at / as_of 未核实部分为 UNKNOWN；"
                "available_at 为本次取得时间。K线日期标签不等于发布时间。"
                for s in bundle["securities"]
            ],
            (
                f"- [日历来源]({evidence.get('source_url', 'https://www.nyse.com/trade/hours-calendars')})；"
                f"日历取得时间：{evidence.get('retrieved_at', 'UNKNOWN')}；"
                f"原始发布时间：{evidence.get('published_at', 'UNKNOWN')}。"
            ),
            "",
            "## 后续复核",
            "",
            "- 先核供应商覆盖、复权与公司行为；相对上涨只是一项观察，不证明经营命题。",
            "- 日线不足时不生成盘中判断；缺事前收入/EPS预期时不评价超预期。",
            "- 核原始披露、反例和日历，补齐正式证据后才重新运行主研究报告。",
            "",
            "本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。",
            "",
        ]
    )
    return "\n".join(lines)
