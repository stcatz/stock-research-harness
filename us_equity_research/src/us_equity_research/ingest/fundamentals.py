"""Multi-source SEC financial calculation bundle with explicit TTM input lineage."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, timedelta

from ..core.contracts import ContractError, parse_datetime, validate_url
from .sec import _filing_is_superseded, _parse_filings

CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}
INSTANT = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CommonStockSharesOutstanding",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "ShortTermBorrowings",
    "CommercialPaper",
]


def extract(companyfacts, filings, symbol, cik, decision_at, retrieved_at):
    cutoff = parse_datetime(decision_at, "decision_at")
    observed = parse_datetime(retrieved_at, "retrieved_at")
    if observed > cutoff:
        raise ContractError("Financial inputs retrieved after decision time")
    records = []
    by_period = {}
    for concept in [c for concepts in CONCEPTS.values() for c in concepts] + INSTANT:
        unit = "shares" if concept == "CommonStockSharesOutstanding" else "USD"
        entries = (
            companyfacts.get("facts", {})
            .get("us-gaap", {})
            .get(concept, {})
            .get("units", {})
            .get(unit, [])
        )
        for entry in entries:
            filing = filings.get(entry.get("accn"))
            if not filing or _filing_is_superseded(filing, filings) or filing["available"] > cutoff:
                continue
            try:
                end = date.fromisoformat(entry["end"])
                start = date.fromisoformat(entry["start"]) if entry.get("start") else None
            except (ValueError, KeyError):
                continue
            value = entry.get("val")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                continue
            if end > cutoff.date() or (start and start > end):
                continue
            key = (concept, unit, start, end, entry["accn"])
            if key in by_period and by_period[key] != value:
                raise ContractError("Conflicting SEC facts for same accession and period")
            if key in by_period:
                continue
            by_period[key] = value
            accn = entry["accn"]
            compact = accn.replace("-", "")
            rid = "SECFACT-" + hashlib.sha256(str(key).encode()).hexdigest()[:20]
            records.append(
                {
                    "fact_id": rid,
                    "symbol": symbol,
                    "concept": concept,
                    "unit": unit,
                    "value": value,
                    "period_start": start.isoformat() if start else None,
                    "period_end": end.isoformat(),
                    "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{compact}/{filing['primary_document']}",
                    "source_document_id": accn,
                    "published_at": filing["acceptance"].isoformat(),
                    "effective_at": datetime.combine(end, datetime.min.time(), UTC).isoformat(),
                    "available_at": observed.isoformat(),
                    "retrieved_at": observed.isoformat(),
                    "as_of": datetime.combine(end, datetime.min.time(), UTC).isoformat(),
                    "availability_basis": "first_observed_at_collection",
                    "source_level": "official",
                }
            )
    return records


def _choose(records):
    return max(records, key=lambda r: (r["period_end"], r["published_at"]), default=None)


def ttm(records, concepts, decision_at):
    cutoff = parse_datetime(decision_at, "decision_at")
    eligible = [
        r
        for r in records
        if parse_datetime(r["available_at"], "available_at") <= cutoff
        and parse_datetime(r["retrieved_at"], "retrieved_at") <= cutoff
    ]
    for concept in concepts:
        rows = [
            r
            for r in eligible
            if r["concept"] == concept and r["unit"] == "USD" and r["period_start"]
        ]
        annual = _choose(
            [
                r
                for r in rows
                if 330
                <= (
                    date.fromisoformat(r["period_end"]) - date.fromisoformat(r["period_start"])
                ).days
                + 1
                <= 380
            ]
        )
        if annual is None:
            continue
        annual_end = date.fromisoformat(annual["period_end"])
        newer = [r for r in rows if r["period_end"] > annual["period_end"]]
        if not newer:
            return {
                "status": "OK",
                "basis": "FY_EQUALS_TTM",
                "value": annual["value"],
                "period_start": annual["period_start"],
                "period_end": annual["period_end"],
                "formula": "FY",
                "input_fact_ids": [annual["fact_id"]],
            }
        current = _choose(
            [
                r
                for r in newer
                if date.fromisoformat(r["period_start"]) == annual_end + timedelta(days=1)
                and 45 <= (date.fromisoformat(r["period_end"]) - annual_end).days <= 330
            ]
        )
        if current is None:
            return {
                "status": "UNKNOWN",
                "reason": "Newer period exists but a compatible cumulative YTD is missing",
            }
        # Do not accept a shorter YTD while a later quarter is already present.
        if current["period_end"] != max(r["period_end"] for r in newer):
            return {"status": "UNKNOWN", "reason": "Latest interim period cannot be bridged"}
        end = date.fromisoformat(current["period_end"])
        try:
            prior_end = end.replace(year=end.year - 1)
        except ValueError:
            return {"status": "UNKNOWN", "reason": "Leap-day fiscal alignment needs review"}
        prior = _choose(
            [
                r
                for r in rows
                if r["period_start"] == annual["period_start"]
                and r["period_end"] == prior_end.isoformat()
            ]
        )
        if prior is None:
            return {
                "status": "UNKNOWN",
                "reason": "Matching prior-year YTD missing; never substitute a quarter or another concept",
            }
        return {
            "status": "OK",
            "basis": "FY_PLUS_YTD_MINUS_PRIOR_YTD",
            "value": annual["value"] + current["value"] - prior["value"],
            "period_start": (prior_end + timedelta(days=1)).isoformat(),
            "period_end": current["period_end"],
            "formula": "FY + current YTD - prior YTD",
            "input_fact_ids": [annual["fact_id"], current["fact_id"], prior["fact_id"]],
        }
    return {"status": "UNKNOWN", "reason": "No compatible annual and interim concept series"}


def analyze_fundamentals(records, decision_at):
    ids = set()
    for r in records:
        if r["fact_id"] in ids:
            raise ContractError("Duplicate financial fact ID")
        ids.add(r["fact_id"])
        validate_url(r["source_url"], "financial source_url")
        stamps = {
            key: parse_datetime(r[key], key)
            for key in ("published_at", "effective_at", "available_at", "retrieved_at", "as_of")
        }
        if (
            not stamps["published_at"] <= stamps["available_at"] <= stamps["retrieved_at"]
            or stamps["as_of"] > stamps["available_at"]
        ):
            raise ContractError("Financial source times are inconsistent")
        if (
            r.get("source_level") != "official"
            or isinstance(r["value"], bool)
            or not isinstance(r["value"], (int, float))
            or not math.isfinite(r["value"])
        ):
            raise ContractError("Invalid primary financial fact")
    result = {name: ttm(records, concepts, decision_at) for name, concepts in CONCEPTS.items()}
    cash, capex = result["operating_cash_flow"], result["capex"]
    result["free_cash_flow"] = {
        "status": "UNKNOWN",
        "reason": "Cash flow and capex period mismatch or missing",
    }
    if cash["status"] == capex["status"] == "OK" and (cash["period_start"], cash["period_end"]) == (
        capex["period_start"],
        capex["period_end"],
    ):
        result["free_cash_flow"] = {
            **cash,
            "value": cash["value"] - capex["value"],
            "formula": "operating cash flow TTM - capex TTM",
            "input_fact_ids": cash["input_fact_ids"] + capex["input_fact_ids"],
        }
    for name, numerator in [
        ("operating_margin", "operating_income"),
        ("free_cash_flow_margin", "free_cash_flow"),
    ]:
        n, d = result[numerator], result["revenue"]
        result[name] = {
            "status": "UNKNOWN",
            "reason": "Compatible TTM numerator/denominator missing",
        }
        if (
            n["status"] == d["status"] == "OK"
            and d["value"] > 0
            and (n["period_start"], n["period_end"]) == (d["period_start"], d["period_end"])
        ):
            result[name] = {
                **d,
                "value": n["value"] / d["value"],
                "formula": numerator + " TTM / revenue TTM",
                "unit": "ratio",
                "input_fact_ids": n["input_fact_ids"] + d["input_fact_ids"],
            }
    cutoff = parse_datetime(decision_at, "decision_at")
    instant = {
        concept: _choose(
            [
                r
                for r in records
                if r["concept"] == concept
                and r["period_start"] is None
                and parse_datetime(r["available_at"], "available_at") <= cutoff
                and parse_datetime(r["retrieved_at"], "retrieved_at") <= cutoff
            ]
        )
        for concept in INSTANT
    }
    return {
        "calculations": result,
        "instant_components": instant,
        "total_debt": {
            "status": "UNKNOWN",
            "reason": "Components are exposed separately; short-term borrowings may overlap commercial paper. No automatic sum without coverage review.",
        },
        "valuation_status": "UNKNOWN_NO_QUALIFIED_CURRENT_PRICE_AND_SHARE_BASIS",
    }


def collect_fundamentals(seed, client):
    companies = []
    for theme in seed["themes"]:
        for candidate in theme["candidates"]:
            cik = candidate["cik"]
            symbol = candidate["symbol"]
            submissions = client.get_submissions(cik)
            raw = client.get_companyfacts(cik)
            from .sec import _require_matching_sec_cik, _require_symbol_ownership

            _require_matching_sec_cik(raw.get("cik"), cik, "companyfacts")
            _require_symbol_ownership(candidate, submissions)
            now = datetime.now(UTC)
            stamp = now.isoformat()
            filings = _parse_filings(submissions, cik=cik, retrieved=now, snapshot_as_of=now)
            records = extract(raw, filings, symbol, cik, stamp, stamp)
            companies.append(
                {
                    "symbol": symbol,
                    "facts": records,
                    "analysis": analyze_fundamentals(records, stamp),
                }
            )
    return {
        "schema_version": "us-fundamentals-1",
        "market": "US",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "companies": companies,
        "publication_policy": "SEC acceptance time recorded; availability is first actual collection, not reconstructed historical availability.",
    }
