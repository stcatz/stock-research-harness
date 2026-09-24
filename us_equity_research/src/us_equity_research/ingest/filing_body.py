"""Bounded SEC inline XBRL extraction. Raw filings live in memory only."""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlparse

from ..core.contracts import ContractError, parse_datetime
from .fundamentals import CONCEPTS, INSTANT

ALLOWED = set(INSTANT + [c for values in CONCEPTS.values() for c in values] + ["LongTermDebt"])


def sec_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.sec.gov"
        or not re.fullmatch(r"/Archives/edgar/data/\d+/\d+/[A-Za-z0-9_.-]+\.html?", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError("Expected an official SEC filing URL")
    return url


class InlineFacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts, self.units, self.numbers = {}, {}, []
        self.context = self.unit = self.number = self.field = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        short = tag.split(":")[-1]
        if short == "context":
            self.context = {"id": a.get("id"), "dimensioned": False}
        elif self.context is not None and short in {"segment", "scenario"}:
            self.context["dimensioned"] = True
        elif self.context is not None and short in {"instant", "startdate", "enddate"}:
            self.field = [short, ""]
        elif short == "unit":
            self.unit = {"id": a.get("id"), "measures": [], "divide": False}
        elif self.unit is not None and short == "divide":
            self.unit["divide"] = True
        elif self.unit is not None and short == "measure":
            self.field = [short, ""]
        elif (
            tag == "ix:nonfraction"
            and a.get("name", "").startswith("us-gaap:")
            and a["name"].split(":")[-1] in ALLOWED
        ):
            self.number = {**a, "text": ""}

    def handle_data(self, data):
        if self.field is not None:
            self.field[1] += data
        if self.number is not None:
            self.number["text"] += data

    def handle_endtag(self, tag):
        short = tag.split(":")[-1]
        if self.field is not None and self.field[0] == short:
            if short == "measure" and self.unit is not None:
                self.unit["measures"].append(self.field[1].strip())
            elif self.context is not None:
                self.context[short] = self.field[1].strip()
            self.field = None
        if short == "context" and self.context is not None:
            self.contexts[self.context["id"]] = self.context
            self.context = None
        elif short == "unit" and self.unit is not None:
            self.units[self.unit["id"]] = self.unit
            self.unit = None
        elif tag == "ix:nonfraction" and self.number is not None:
            self.numbers.append(self.number)
            self.number = None


def numeric(item):
    if item.get("xsi:nil") in {"true", "1"}:
        return None
    fmt = item.get("format", "").split(":")[-1].lower()
    if fmt not in {"", "numdotdecimal", "num-dot-decimal", "zerodash", "zero-dash", "numcommadot"}:
        return None
    text = item["text"].strip().replace(",", "").replace("\xa0", "")
    if text in {"—", "–", "-"} and fmt in {"zerodash", "zero-dash"}:
        text = "0"
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        scale = int(item.get("scale", "0"))
        if abs(scale) > 12 or item.get("sign", "") not in {"", "-"}:
            return None
        value = Decimal(text) * (Decimal(10) ** scale)
        if item.get("sign") == "-":
            value = -value
        result = float(value)
        return result if math.isfinite(result) else None
    except (InvalidOperation, ValueError):
        return None


def extract_body(html, evidence, symbol, retrieved_at):
    url = sec_url(evidence["source_url"])
    now = parse_datetime(retrieved_at, "retrieved_at")
    if parse_datetime(evidence["published_at"], "published_at") > now:
        raise ContractError("Filing publication exceeds collection time")
    parser = InlineFacts()
    parser.feed(html)
    facts = []
    seen = {}
    for item in parser.numbers:
        context = parser.contexts.get(item.get("contextref"), {})
        unit = parser.units.get(item.get("unitref"), {})
        if context.get("dimensioned", True) or unit.get("divide", True):
            continue
        measures = unit.get("measures")
        unit_name = (
            "USD"
            if measures == ["iso4217:USD"]
            else "shares"
            if measures == ["xbrli:shares"]
            else None
        )
        value = numeric(item)
        if value is None or unit_name is None:
            continue
        end = context.get("instant", context.get("enddate"))
        start = context.get("startdate")
        try:
            if date.fromisoformat(end) > now.date() or (
                start and date.fromisoformat(start) > date.fromisoformat(end)
            ):
                continue
        except (TypeError, ValueError):
            continue
        concept = item["name"].split(":")[-1]
        key = (concept, start, end, unit_name)
        if key in seen:
            if seen[key] != value:
                raise ContractError("Conflicting consolidated inline facts")
            continue
        seen[key] = value
        payload = {
            "symbol": symbol,
            "concept": concept,
            "unit": unit_name,
            "value": value,
            "period_start": start,
            "period_end": end,
            "context_id": context["id"],
            "source_url": url,
            "published_at": evidence["published_at"],
            "effective_at": end + "T00:00:00+00:00",
            "as_of": end + "T00:00:00+00:00",
            "available_at": retrieved_at,
            "retrieved_at": retrieved_at,
        }
        payload["fact_id"] = (
            "BODY-" + hashlib.sha256(repr((url, key, value)).encode()).hexdigest()[:20]
        )
        facts.append(payload)
    return {
        "symbol": symbol,
        "source_url": url,
        "document_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "retrieved_at": retrieved_at,
        "facts": facts,
        "status": "EXTRACTED" if facts else "NO_SUPPORTED_FACTS",
        "semantic_review": "UNKNOWN",
    }


def fetch_body(evidence, symbol, user_agent):
    url = sec_url(evidence["source_url"])
    # Header via stdin keeps the contact identity out of the process command line.
    if any(c in user_agent for c in '\r\n"\\'):
        raise ContractError("Invalid SEC request identity")
    try:
        result = subprocess.run(
            [
                "curl",
                "--compressed",
                "--max-time",
                "50",
                "--max-filesize",
                "20000000",
                "--fail",
                "--silent",
                "--config",
                "-",
                url,
            ],
            input=f'header = "User-Agent: {user_agent}"\n'.encode(),
            capture_output=True,
            timeout=55,
            check=False,
        )
        if result.returncode or len(result.stdout) > 20000000:
            raise ContractError("SEC filing body fetch failed")
        return extract_body(
            result.stdout.decode("utf-8"), evidence, symbol, datetime.now(UTC).isoformat()
        )
    except (subprocess.TimeoutExpired, UnicodeError, OSError):
        raise ContractError("SEC filing body fetch failed") from None


def analyze_bodies(bundle, fundamentals, decision_at):
    cutoff = parse_datetime(decision_at, "decision_at")
    results = []
    if bundle is None:
        return results
    if bundle.get("schema_version") != "us-filing-body-1":
        raise ContractError("Unsupported filing body bundle")
    financial = {c["symbol"]: c["facts"] for c in (fundamentals or {}).get("companies", [])}
    for body in bundle["documents"]:
        sec_url(body["source_url"])
        if parse_datetime(body["retrieved_at"], "retrieved_at") > cutoff:
            raise ContractError("Body exceeds decision time")
        checks = []
        seen = set()
        for f in body["facts"]:
            key = (f["concept"], f["period_start"], f["period_end"], f["unit"])
            if (
                key in seen
                or f["concept"] not in ALLOWED
                or isinstance(f["value"], bool)
                or not isinstance(f["value"], (int, float))
                or not math.isfinite(f["value"])
            ):
                raise ContractError("Invalid or duplicate body fact")
            seen.add(key)
            end = date.fromisoformat(f["period_end"])
            if end > cutoff.date() or (
                f["period_start"] and date.fromisoformat(f["period_start"]) > end
            ):
                raise ContractError("Invalid body fact period")
            if any(f[k] != f["period_end"] + "T00:00:00+00:00" for k in ("as_of", "effective_at")):
                raise ContractError("Invalid body fact effective date")
            if f["symbol"] != body["symbol"] or f["source_url"] != body["source_url"]:
                raise ContractError("Body fact ownership mismatch")
            if not (
                parse_datetime(f["published_at"], "published_at")
                <= parse_datetime(f["available_at"], "available_at")
                <= parse_datetime(f["retrieved_at"], "retrieved_at")
                <= parse_datetime(body["retrieved_at"], "retrieved_at")
                <= cutoff
            ):
                raise ContractError("Invalid body fact time boundary")
            matches = [
                x
                for x in financial.get(body["symbol"], [])
                if all(
                    x[k] == f[k]
                    for k in ("concept", "period_start", "period_end", "unit", "source_url")
                )
            ]
            checks.append(
                {
                    "fact_id": f["fact_id"],
                    "concept": f["concept"],
                    "period_end": f["period_end"],
                    "status": "MATCH"
                    if matches and all(x["value"] == f["value"] for x in matches)
                    else "CONFLICT"
                    if matches
                    else "BODY_ONLY",
                }
            )
        instant = [f for f in body["facts"] if f["period_start"] is None and f["unit"] == "USD"]
        end = max((f["period_end"] for f in instant), default=None)
        debt = {f["concept"]: f for f in instant if f["period_end"] == end}
        names = ["LongTermDebtCurrent", "LongTermDebtNoncurrent"]
        components = [debt[n] for n in names if n in debt]
        total = sum(f["value"] for f in components) if len(components) == 2 else None
        explicit = debt.get("LongTermDebt")
        reconciliation = (
            "MATCH"
            if total is not None and explicit and total == explicit["value"]
            else "CONFLICT"
            if total is not None and explicit
            else "UNKNOWN"
        )
        results.append(
            {
                "symbol": body["symbol"],
                "source_url": body["source_url"],
                "retrieved_at": body["retrieved_at"],
                "status": body["status"],
                "checks": checks,
                "long_term_debt_including_current": total,
                "period_end": end,
                "debt_input_ids": [f["fact_id"] for f in components],
                "debt_reconciliation": reconciliation,
                "total_debt": None,
                "semantic_review": "UNKNOWN",
            }
        )
    return results
