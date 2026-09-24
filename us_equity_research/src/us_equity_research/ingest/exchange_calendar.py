"""Official Nasdaq core-session calendar, cross-checked against supplier dates.

Unknown publication time remains unknown. Never stores source HTML.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from ..core.contracts import ContractError, parse_datetime
from .futu_quotes import NoRedirect

URL = "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule"
NY = ZoneInfo("America/New_York")


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None


def parse_schedule(html, year, retrieved_at):
    p = Tables()
    p.feed(html)
    if ["Holiday", "Date", "Market Status"] not in p.rows:
        raise ContractError("Official calendar layout changed")
    if not any(
        len(r) == 2 and r[0] == "The Nasdaq Stock Market" and re.search(r"9:30 am.*4:00 pm", r[1])
        for r in p.rows
    ):
        raise ContractError("Core session hours require review")
    closed = []
    early = []
    for row in p.rows:
        if len(row) != 3 or not re.search(r",\s*" + str(year) + r"$", row[1]):
            continue
        try:
            day = datetime.strptime(row[1], "%B %d, %Y").replace(tzinfo=NY).date().isoformat()
        except ValueError:
            raise ContractError("Unrecognized calendar date") from None
        if row[2] == "Closed":
            closed.append(day)
        elif row[2] == "1:00 p.m.":
            early.append(day)
        else:
            raise ContractError("Unrecognized exchange session status")
    if (
        not 9 <= len(closed) <= 12
        or len(early) > 3
        or len(set(closed + early)) != len(closed + early)
    ):
        raise ContractError("Incomplete or contradictory official calendar")
    return {
        "year": year,
        "closed": sorted(closed),
        "early_close": sorted(early),
        "source_url": URL,
        "published_at": "UNKNOWN",
        "retrieved_at": retrieved_at,
        "source_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "scope": "US_CORE_EQUITIES",
        "availability_basis": "first_observed_at_collection",
    }


def fetch_schedule(year):
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "StockResearch/1.0"})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("size")
        return parse_schedule(raw.decode(), year, datetime.now(UTC).isoformat())
    except (OSError, ValueError):
        raise ContractError("Official calendar unavailable; no weekday-only fallback") from None


def build_calendar(schedule, start, end, decision_at, supplier_days=None):
    cutoff = parse_datetime(decision_at, "decision_at")
    retrieved = parse_datetime(schedule["retrieved_at"], "retrieved_at")
    if not timedelta(0) <= cutoff - retrieved <= timedelta(days=7):
        raise ContractError("Calendar source must be available and reviewed within 7 days")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or first.year != schedule["year"] or last.year != schedule["year"]:
        raise ContractError("Calendar range outside verified year")
    sessions = []
    day = first
    while day <= last:
        d = day.isoformat()
        if day.weekday() < 5 and d not in schedule["closed"]:
            close_hour = 13 if d in schedule["early_close"] else 16
            sessions.append(
                {
                    "date": d,
                    "open_at": datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).isoformat(),
                    "close_at": datetime(
                        day.year, day.month, day.day, close_hour, tzinfo=NY
                    ).isoformat(),
                }
            )
        day += timedelta(days=1)
    if supplier_days is not None:
        a, b = supplier_days["start"], supplier_days["end"]
        expected = {
            s["date"]: int(
                (
                    parse_datetime(s["close_at"], "close") - parse_datetime(s["open_at"], "open")
                ).total_seconds()
            )
            for s in sessions
            if a <= s["date"] <= b
        }
        actual = {r["date"]: r["trade_second"] for r in supplier_days["days"]}
        if len(actual) != len(supplier_days["days"]) or expected != actual:
            raise ContractError("Exchange and supplier calendars disagree; manual review required")
    ref = "CAL-NASDAQ-" + schedule["source_sha256"][:16]
    return {
        "session_calendar": {
            "scope": "US_CORE_EQUITIES",
            "complete": True,
            "source_evidence_ref": ref,
            "coverage_start": start,
            "coverage_end": end,
            "sessions": sessions,
        },
        "evidence": {
            "evidence_id": ref,
            "source_level": "official",
            "source_url": URL,
            "published_at": "UNKNOWN",
            "effective_at": f"{schedule['year']}-01-01T00:00:00-05:00",
            "available_at": schedule["retrieved_at"],
            "retrieved_at": schedule["retrieved_at"],
            "as_of": schedule["retrieved_at"],
            "source_sha256": schedule["source_sha256"],
            "formal_evidence_eligible": False,
            "availability_basis": "first_observed_at_collection",
        },
    }
