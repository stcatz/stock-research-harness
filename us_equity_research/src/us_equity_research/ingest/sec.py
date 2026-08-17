from __future__ import annotations

import gzip
import json
import math
import os
import stat
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.contracts import (
    ASSESSMENTS,
    CANDIDATE_ROLES,
    DIMENSIONS,
    MARKET,
    MARKET_EVIDENCE_CATEGORIES,
    SCHEMA_VERSION,
    STAGES,
    ContractError,
    parse_datetime,
    reject_unknown_fields,
    require_list,
    require_mapping,
    require_string,
    validate_identifier,
    validate_symbol,
)
from ..core.snapshot import MAX_SNAPSHOT_BYTES, validate_snapshot

SEC_DATA_ORIGIN = "https://data.sec.gov"
SEC_ARCHIVES_ORIGIN = "https://www.sec.gov"
MAX_SEC_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_SEED_THEMES = 20
MAX_SEED_CANDIDATES = 50
MIN_REQUEST_INTERVAL_SECONDS = 0.11
SEC_AVAILABILITY_BUFFER = timedelta(minutes=3)
BASE_SUPPORTED_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "6-K", "40-F"})
SUPPORTED_FORMS = frozenset(
    {*BASE_SUPPORTED_FORMS, *(f"{form}/A" for form in BASE_SUPPORTED_FORMS)}
)
ANNUAL_BASE_FORMS = frozenset({"10-K", "20-F", "40-F"})
INTERIM_BASE_FORMS = frozenset({"10-Q", "6-K"})
LICENSE_ATTESTATION = "authorized_for_local_research_snapshot"

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    headers: Mapping[str, str]


Transport = Callable[[Request, float], HttpResponse]


class SecClient:
    """Small SEC JSON client with an injectable transport and conservative pacing."""

    def __init__(
        self,
        *,
        user_agent: str,
        transport: Transport | None = None,
        timeout: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_user_agent = user_agent.strip()
        if not normalized_user_agent:
            raise ValueError("SEC_USER_AGENT must be set to an application name and contact email")
        if "\n" in normalized_user_agent or "\r" in normalized_user_agent:
            raise ValueError("SEC_USER_AGENT must be a single HTTP header line")
        if "@" not in normalized_user_agent:
            raise ValueError("SEC_USER_AGENT must include a contact email")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("SEC timeout must be positive")
        self._user_agent = normalized_user_agent
        self._transport = transport or _urlopen_transport
        self._timeout = float(timeout)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_started: float | None = None

    def get_submissions(self, cik: str) -> dict[str, Any]:
        normalized = _normalize_cik(cik)
        return self._get_json(f"/submissions/CIK{normalized}.json")

    def get_companyfacts(self, cik: str) -> dict[str, Any]:
        normalized = _normalize_cik(cik)
        return self._get_json(f"/api/xbrl/companyfacts/CIK{normalized}.json")

    def _get_json(self, path: str) -> dict[str, Any]:
        request = Request(
            SEC_DATA_ORIGIN + path,
            headers={
                "User-Agent": self._user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "Host": "data.sec.gov",
            },
            method="GET",
        )
        self._pace()
        try:
            response = self._transport(request, self._timeout)
        except HTTPError as exc:
            raise RuntimeError(f"SEC request failed with HTTP status {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("SEC request failed") from exc
        if not isinstance(response, HttpResponse):
            raise TypeError("SEC transport returned an invalid response")
        payload = _decode_http_body(response)
        if len(payload) > MAX_SEC_RESPONSE_BYTES:
            raise RuntimeError("SEC response exceeds the configured size limit")
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("SEC returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise TypeError("SEC JSON root must be an object")
        return parsed

    def _pace(self) -> None:
        now = self._monotonic()
        if self._last_request_started is not None:
            remaining = MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_started = now


def collect_sec_snapshot(
    *,
    workspace: Path,
    seed_path: Path,
    snapshot_id: str,
    market_path: Path | None = None,
    client: SecClient | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Collect SEC metadata/companyfacts and atomically publish one normalized snapshot."""

    normalized_snapshot_id = validate_identifier(snapshot_id, "snapshot_id")
    seed = _read_json_file(seed_path, "seed JSON")
    if "example_notice" in seed:
        raise ContractError(
            "refusing to collect an example seed; copy it, remove example_notice, and provide reviewed inputs"
        )
    market = _read_json_file(market_path, "market JSON") if market_path else None
    if clock is not None and client is None:
        raise ContractError("an injected collection clock requires an injected SEC client")
    sec_client = client or SecClient(user_agent=os.environ.get("SEC_USER_AGENT", ""))
    collection_clock = clock or _system_utc_now
    retrieval_started = _read_clock(collection_clock)

    snapshot = _build_snapshot(
        seed=seed,
        snapshot_id=normalized_snapshot_id,
        retrieved=retrieval_started,
        client=sec_client,
        market=market,
    )
    retrieval_completed = _read_clock(collection_clock)
    if retrieval_completed < retrieval_started:
        raise ContractError("collection clock moved backwards")
    snapshot["retrieved_at"] = _isoformat(retrieval_completed)
    for evidence in snapshot["evidence"]:
        if evidence["source_level"] == "official" and evidence["category"] == "sec_filing":
            evidence["retrieved_at"] = _isoformat(retrieval_completed)
    validated = validate_snapshot(snapshot)
    relative_path = Path("data") / "normalized" / "us" / normalized_snapshot_id / "snapshot.json"
    _publish_snapshot(workspace, normalized_snapshot_id, snapshot)
    candidate_count = sum(len(theme["candidates"]) for theme in snapshot["themes"])
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "status": "published",
        "snapshot_id": normalized_snapshot_id,
        "relative_path": relative_path.as_posix(),
        "snapshot_hash": validated.snapshot_hash,
        "retrieved_at": _isoformat(retrieval_completed),
        "pit_quality": validated.pit_quality,
        "evidence_count": len(snapshot["evidence"]),
        "financial_fact_count": len(snapshot["financial_facts"]),
        "candidate_count": candidate_count,
        "broker_capability": False,
    }


def _build_snapshot(
    *,
    seed: Mapping[str, Any],
    snapshot_id: str,
    retrieved: datetime,
    client: SecClient,
    market: Mapping[str, Any] | None,
) -> dict[str, Any]:
    _validate_seed_root(seed, retrieved)
    snapshot_as_of = parse_datetime(seed["as_of"], "seed.as_of")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    facts_by_id: dict[str, dict[str, Any]] = {}
    themes: list[dict[str, Any]] = []

    for theme_index, raw_theme in enumerate(seed["themes"]):
        theme_seed = require_mapping(raw_theme, f"seed.themes[{theme_index}]")
        candidates: list[dict[str, Any]] = []
        theme_evidence_refs: list[str] = []
        for candidate_index, raw_candidate in enumerate(theme_seed["candidates"]):
            candidate_seed = require_mapping(
                raw_candidate,
                f"seed.themes[{theme_index}].candidates[{candidate_index}]",
            )
            candidate, candidate_evidence, candidate_facts = _collect_candidate(
                candidate_seed,
                retrieved=retrieved,
                snapshot_as_of=snapshot_as_of,
                client=client,
            )
            for item in candidate_evidence:
                evidence_by_id.setdefault(item["evidence_id"], item)
            for item in candidate_facts:
                facts_by_id.setdefault(item["fact_id"], item)
            theme_evidence_refs.extend(candidate["evidence_refs"])
            candidates.append(candidate)
        theme_refs = sorted(set(theme_evidence_refs))
        dimensions = {
            dimension: {
                "assessment": theme_seed["dimensions"][dimension]["assessment"],
                "reason": theme_seed["dimensions"][dimension]["reason"],
                # The collector does not parse filing prose. Researcher-authored
                # dimension claims therefore remain explicitly unsupported.
                "evidence_refs": [],
            }
            for dimension in DIMENSIONS
        }
        themes.append(
            {
                "theme_id": theme_seed["theme_id"],
                "name": theme_seed["name"],
                "event_type": theme_seed["event_type"],
                "stage": theme_seed["stage"],
                "dimensions": dimensions,
                "transmission_chain": list(theme_seed["transmission_chain"]),
                "next_catalyst_at": theme_seed["next_catalyst_at"],
                "evidence_refs": theme_refs,
                "data_gaps": _unique_strings(
                    [
                        *theme_seed.get("data_gaps", []),
                        "SEC filing availability uses a three-minute estimate; actual public availability can be later, so near-filing strict PIT replay remains UNKNOWN.",
                    ]
                ),
                "candidates": candidates,
            }
        )

    market_context = _unknown_market_context()
    if market is not None:
        market_evidence, market_facts, market_context = _validate_market_payload(
            market,
            retrieved=retrieved,
            snapshot_as_of=snapshot_as_of,
            security_ids={
                candidate["security_id"] for theme in themes for candidate in theme["candidates"]
            },
        )
        for item in market_evidence:
            if item["evidence_id"] in evidence_by_id:
                raise ContractError(f"duplicate evidence_id: {item['evidence_id']}")
            evidence_by_id[item["evidence_id"]] = item
        for item in market_facts:
            if item["fact_id"] in facts_by_id:
                raise ContractError(f"duplicate fact_id: {item['fact_id']}")
            facts_by_id[item["fact_id"]] = item
        close_by_security = {
            fact["security_id"]: fact for fact in market_facts if fact["metric"] == "close_price"
        }
        context_refs = list(market_context["evidence_refs"])
        for theme in themes:
            for candidate in theme["candidates"]:
                close = close_by_security[candidate["security_id"]]
                market_refs = sorted({close["evidence_ref"], *context_refs})
                candidate["market_evidence_refs"] = market_refs
                candidate["financial_fact_refs"].append(close["fact_id"])
                candidate["data_gaps"] = [
                    gap
                    for gap in candidate["data_gaps"]
                    if gap
                    not in {
                        "No licensed structured market snapshot was supplied.",
                        "No licensed market snapshot supplied.",
                    }
                ]

    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "snapshot_id": snapshot_id,
        "data_mode": "snapshot",
        "pit_quality": "P2",
        "as_of": _isoformat(snapshot_as_of),
        "retrieved_at": _isoformat(retrieved),
        "evidence": sorted(evidence_by_id.values(), key=lambda item: item["evidence_id"]),
        "financial_facts": sorted(facts_by_id.values(), key=lambda item: item["fact_id"]),
        "market_context": market_context,
        "themes": themes,
    }


def _collect_candidate(
    seed: Mapping[str, Any],
    *,
    retrieved: datetime,
    snapshot_as_of: datetime,
    client: SecClient,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    _validate_candidate_seed(seed)
    cik = _normalize_cik(seed["cik"])
    submissions = client.get_submissions(cik)
    companyfacts = client.get_companyfacts(cik)
    _require_matching_sec_cik(submissions.get("cik"), cik, "SEC submissions")
    _require_matching_sec_cik(companyfacts.get("cik"), cik, "SEC companyfacts")
    _require_symbol_ownership(seed, submissions)
    issuer_name = require_string(submissions.get("name"), "SEC submissions.name")
    filings = _parse_filings(
        submissions,
        cik=cik,
        retrieved=retrieved,
        snapshot_as_of=snapshot_as_of,
    )
    facts, fact_accessions, annual_period_end = _extract_financial_facts(
        companyfacts,
        filings=filings,
        security_id=seed["security_id"],
        symbol=seed["symbol"],
        observation_cutoff=snapshot_as_of.date(),
    )

    selected_accessions = set(fact_accessions)
    if filings:
        selected_accessions.add(max(filings.values(), key=lambda item: item["acceptance"])["accn"])
    evidence = [
        _filing_evidence(
            filing,
            cik=cik,
            issuer_name=issuer_name,
            retrieved=retrieved,
            metrics=[fact["metric"] for fact in facts if fact["_accn"] == filing["accn"]],
        )
        for accession, filing in sorted(filings.items())
        if accession in selected_accessions
    ]
    evidence_id_by_accession = {item.pop("_accn"): item["evidence_id"] for item in evidence}
    finalized_facts: list[dict[str, Any]] = []
    for fact in facts:
        accession = fact.pop("_accn")
        evidence_ref = evidence_id_by_accession.get(accession)
        if evidence_ref is None:
            continue
        fact["evidence_ref"] = evidence_ref
        fact["available_at"] = _isoformat(filings[accession]["available"])
        finalized_facts.append(fact)

    official_refs = sorted(item["evidence_id"] for item in evidence)
    data_gaps = list(seed.get("data_gaps", []))
    data_gaps.extend(
        [
            "Researcher-authored narrative has not been machine-verified against filing text.",
            "SEC acceptanceDateTime is not exact public availability; a three-minute estimate is used and actual availability can be later.",
        ]
    )
    if not evidence:
        data_gaps.append("No supported SEC filing was accepted by retrieved_at.")
    if not finalized_facts:
        data_gaps.append("No deterministic annual or instant SEC companyfacts were extractable.")
    if sum(fact["metric"] == "revenue_fy" for fact in finalized_facts) < 2:
        data_gaps.append(
            "A second distinct fiscal-year revenue fact is unavailable; annual revenue growth is UNKNOWN."
        )
    if _has_newer_interim(filings, annual_period_end):
        data_gaps.append(
            "A newer interim filing exists; FY + current YTD - prior YTD TTM roll-forward is not implemented, so annual-period metrics are stale."
        )
    if any(filing["form"].endswith("/A") for filing in filings.values()):
        data_gaps.append(
            "An SEC amendment exists; the latest accepted accession supersedes older facts for the same form and report period."
        )
    data_gaps.append("No licensed structured market snapshot was supplied.")
    data_gaps.append(
        "Instant period-end shares outstanding are not extracted; FY weighted-average diluted shares are not valid for current market-cap calculations."
    )
    data_gaps.append(
        "Total debt is not extracted because common SEC long-term-debt tags may omit short-term borrowings and commercial paper."
    )

    financial_refs = sorted(fact["fact_id"] for fact in finalized_facts)
    return (
        {
            "security_id": seed["security_id"],
            "symbol": seed["symbol"],
            "name": issuer_name,
            "role": seed["role"],
            "thesis": seed["thesis"],
            "bull_case": {"text": seed["bull_case"], "evidence_refs": []},
            "bear_case": {"text": seed["bear_case"], "evidence_refs": []},
            "risk_verdict": {"text": seed["risk_verdict"], "evidence_refs": []},
            "evidence_refs": official_refs,
            "market_evidence_refs": [],
            "financial_fact_refs": financial_refs,
            "invalidation_conditions": list(seed["invalidation_conditions"]),
            "data_gaps": _unique_strings(data_gaps),
            "risk_flags": _unique_strings(
                [*seed.get("risk_flags", []), "NARRATIVE_REQUIRES_HUMAN_VERIFICATION"]
            ),
            "manual_review_items": _unique_strings(
                [
                    *seed.get("manual_review_items", []),
                    "Verify every narrative claim in the SEC filing.",
                ]
            ),
        },
        evidence,
        finalized_facts,
    )


def _parse_filings(
    submissions: Mapping[str, Any],
    *,
    cik: str,
    retrieved: datetime,
    snapshot_as_of: datetime,
) -> dict[str, dict[str, Any]]:
    try:
        recent = require_mapping(
            require_mapping(submissions["filings"], "filings")["recent"], "recent"
        )
    except KeyError as exc:
        raise ContractError("SEC submissions is missing filings.recent") from exc
    required_columns = (
        "accessionNumber",
        "acceptanceDateTime",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
    )
    columns = {
        name: require_list(recent.get(name), f"filings.recent.{name}") for name in required_columns
    }
    lengths = {len(values) for values in columns.values()}
    if len(lengths) != 1:
        raise ContractError("SEC submissions recent columns have inconsistent lengths")

    filings: dict[str, dict[str, Any]] = {}
    for index in range(next(iter(lengths), 0)):
        form = str(columns["form"][index])
        if form not in SUPPORTED_FORMS:
            continue
        accession = require_string(
            columns["accessionNumber"][index],
            f"filings.recent.accessionNumber[{index}]",
            strip=False,
        )
        if not _valid_accession(accession):
            continue
        acceptance = _parse_sec_datetime(
            columns["acceptanceDateTime"][index],
            f"filings.recent.acceptanceDateTime[{index}]",
        )
        estimated_available = acceptance + SEC_AVAILABILITY_BUFFER
        if estimated_available > retrieved:
            continue
        primary_document = require_string(
            columns["primaryDocument"][index],
            f"filings.recent.primaryDocument[{index}]",
            strip=False,
        )
        if Path(primary_document).name != primary_document or ".." in primary_document:
            continue
        filing_date = _parse_date(columns["filingDate"][index], "filingDate")
        try:
            report_date = _parse_date(columns["reportDate"][index], "reportDate")
        except ContractError:
            # Some event filings have an empty reportDate. Filing date is a
            # conservative metadata fallback, not a claim about event timing.
            report_date = filing_date
        if report_date > snapshot_as_of.date():
            continue
        filings[accession] = {
            "accn": accession,
            "acceptance": acceptance,
            "available": estimated_available,
            "filing_date": filing_date,
            "report_date": report_date,
            "form": form,
            "primary_document": primary_document,
            "cik": cik,
        }
    return filings


def _extract_financial_facts(
    companyfacts: Mapping[str, Any],
    *,
    filings: Mapping[str, Mapping[str, Any]],
    security_id: str,
    symbol: str,
    observation_cutoff: date,
) -> tuple[list[dict[str, Any]], set[str], date | None]:
    metrics: list[dict[str, Any]] = []
    annual_period_end: date | None = None

    revenues = _select_annual_entries(
        companyfacts,
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "Revenue",
        ),
        "USD",
        filings,
        observation_cutoff=observation_cutoff,
        limit=2,
    )
    revenue = revenues[0] if revenues else None
    operating = _select_annual_entry(
        companyfacts,
        ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
        "USD",
        filings,
        observation_cutoff=observation_cutoff,
    )
    operating_cash = _select_annual_entry(
        companyfacts,
        ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
        "USD",
        filings,
        observation_cutoff=observation_cutoff,
    )
    capex = _select_annual_entry(
        companyfacts,
        ("PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipment"),
        "USD",
        filings,
        observation_cutoff=observation_cutoff,
    )
    cash = _select_instant_entry(
        companyfacts,
        ("CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"),
        "USD",
        filings,
        observation_cutoff=observation_cutoff,
    )
    for revenue_entry in revenues:
        metrics.append(_financial_fact("revenue_fy", revenue_entry, security_id, symbol, "USD"))

    for metric, entry, unit in (
        ("operating_income_fy", operating, "USD"),
        ("cash_and_equivalents", cash, "USD"),
    ):
        if entry is not None:
            metrics.append(_financial_fact(metric, entry, security_id, symbol, unit))
    annual_duration_entries = [
        entry for entry in (revenue, operating, operating_cash, capex) if entry is not None
    ]
    if annual_duration_entries:
        annual_period_end = max(entry["end"] for entry in annual_duration_entries)

    if operating_cash and capex and _same_period_and_accession(operating_cash, capex):
        fcf_entry = dict(operating_cash)
        fcf_entry["val"] = operating_cash["val"] - capex["val"]
        metrics.append(_financial_fact("free_cash_flow_fy", fcf_entry, security_id, symbol, "USD"))

    accessions = {fact["_accn"] for fact in metrics}
    return metrics, accessions, annual_period_end


def _select_annual_entry(
    companyfacts: Mapping[str, Any],
    concepts: tuple[str, ...],
    unit: str,
    filings: Mapping[str, Mapping[str, Any]],
    *,
    observation_cutoff: date,
) -> dict[str, Any] | None:
    entries = _select_annual_entries(
        companyfacts,
        concepts,
        unit,
        filings,
        observation_cutoff=observation_cutoff,
        limit=1,
    )
    return entries[0] if entries else None


def _select_annual_entries(
    companyfacts: Mapping[str, Any],
    concepts: tuple[str, ...],
    unit: str,
    filings: Mapping[str, Mapping[str, Any]],
    *,
    observation_cutoff: date,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[tuple[date, datetime, int, dict[str, Any]]] = []
    for priority, concept in enumerate(concepts):
        for entry in _concept_entries(companyfacts, concept, unit):
            accession = entry.get("accn")
            filing = filings.get(accession) if isinstance(accession, str) else None
            if (
                filing is None
                or _base_form(filing["form"]) not in ANNUAL_BASE_FORMS
                or _filing_is_superseded(filing, filings)
            ):
                continue
            if (
                entry.get("fp") != "FY"
                or _base_form(str(entry.get("form"))) not in ANNUAL_BASE_FORMS
            ):
                continue
            start = _optional_date(entry.get("start"))
            end = _optional_date(entry.get("end"))
            if (
                start is None
                or end is None
                or end > observation_cutoff
                or not 300 <= (end - start).days <= 400
            ):
                continue
            value = entry.get("val")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            normalized = {**entry, "start": start, "end": end, "val": float(value)}
            candidates.append((end, filing["acceptance"], -priority, normalized))
    by_period: dict[date, tuple[date, datetime, int, dict[str, Any]]] = {}
    for candidate in candidates:
        period = candidate[0]
        current = by_period.get(period)
        if current is None or candidate[:3] > current[:3]:
            by_period[period] = candidate
    ordered = sorted(by_period.values(), key=lambda item: item[:3], reverse=True)
    return [item[3] for item in ordered[:limit]]


def _select_instant_entry(
    companyfacts: Mapping[str, Any],
    concepts: tuple[str, ...],
    unit: str,
    filings: Mapping[str, Mapping[str, Any]],
    *,
    observation_cutoff: date,
) -> dict[str, Any] | None:
    candidates: list[tuple[date, datetime, int, dict[str, Any]]] = []
    for priority, concept in enumerate(concepts):
        for entry in _concept_entries(companyfacts, concept, unit):
            accession = entry.get("accn")
            filing = filings.get(accession) if isinstance(accession, str) else None
            if filing is None or _filing_is_superseded(filing, filings):
                continue
            end = _optional_date(entry.get("end"))
            value = entry.get("val")
            if (
                end is None
                or end > observation_cutoff
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                continue
            normalized = {**entry, "end": end, "val": float(value)}
            candidates.append((end, filing["acceptance"], -priority, normalized))
    return max(candidates, default=None, key=lambda item: item[:3])[3] if candidates else None


def _concept_entries(
    companyfacts: Mapping[str, Any], concept: str, unit: str
) -> list[dict[str, Any]]:
    root = companyfacts.get("facts")
    if not isinstance(root, dict):
        return []
    result: list[dict[str, Any]] = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        namespace = root.get(taxonomy)
        if not isinstance(namespace, dict):
            continue
        concept_data = namespace.get(concept)
        if not isinstance(concept_data, dict):
            continue
        units = concept_data.get("units")
        if not isinstance(units, dict) or not isinstance(units.get(unit), list):
            continue
        result.extend(item for item in units[unit] if isinstance(item, dict))
    return result


def _financial_fact(
    metric: str,
    entry: Mapping[str, Any],
    security_id: str,
    symbol: str,
    unit: str,
) -> dict[str, Any]:
    accession = str(entry["accn"])
    period_end: date = entry["end"]
    return {
        "fact_id": validate_identifier(
            f"FACT-{symbol}-{metric.upper().replace('_', '-')}-{period_end.isoformat().replace('-', '')}",
            "generated fact_id",
        ),
        "security_id": security_id,
        "metric": metric,
        "value": entry["val"],
        "unit": unit,
        "period_end": period_end.isoformat(),
        "available_at": "",
        "_accn": accession,
    }


def _filing_evidence(
    filing: Mapping[str, Any],
    *,
    cik: str,
    issuer_name: str,
    retrieved: datetime,
    metrics: list[str],
) -> dict[str, Any]:
    accession = filing["accn"]
    accession_compact = accession.replace("-", "")
    evidence_id = validate_identifier(f"EV-SEC-{cik}-{accession_compact}", "generated evidence_id")
    metric_note = ", ".join(sorted(metrics)) if metrics else "no deterministic metric"
    return {
        "evidence_id": evidence_id,
        "source_level": "official",
        "category": "sec_filing",
        "title": f"{issuer_name} {filing['form']} SEC filing",
        "summary": (
            f"SEC accession {accession}, accepted {_isoformat(filing['acceptance'])}; "
            f"collector mapped {metric_note}. Filing text and researcher narrative require human review."
        ),
        "source_url": (
            f"{SEC_ARCHIVES_ORIGIN}/Archives/edgar/data/{int(cik)}/"
            f"{accession_compact}/{filing['primary_document']}"
        ),
        "source_document_id": f"sec-{accession_compact}",
        "published_at": _isoformat(filing["acceptance"]),
        "effective_at": _date_at_utc(filing["report_date"]),
        "available_at": _isoformat(filing["available"]),
        "retrieved_at": _isoformat(retrieved),
        "as_of": _date_at_utc(filing["report_date"]),
        "_accn": accession,
    }


def _validate_seed_root(seed: Mapping[str, Any], retrieved: datetime) -> None:
    reject_unknown_fields(seed, {"schema_version", "market", "as_of", "themes"}, "seed")
    if seed.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"seed.schema_version must be {SCHEMA_VERSION}")
    if seed.get("market") != MARKET:
        raise ContractError("seed.market must be US")
    as_of = parse_datetime(seed.get("as_of"), "seed.as_of")
    if as_of > retrieved:
        raise ContractError("seed.as_of must not be later than retrieved_at")
    themes = require_list(seed.get("themes"), "seed.themes")
    if not themes:
        raise ContractError("seed.themes must not be empty")
    if len(themes) > MAX_SEED_THEMES:
        raise ContractError(f"seed.themes exceeds MAX_SEED_THEMES={MAX_SEED_THEMES}")
    candidate_count = 0
    for index, raw_theme in enumerate(themes):
        theme = require_mapping(raw_theme, f"seed.themes[{index}]")
        _validate_theme_seed(theme, index)
        candidate_count += len(theme["candidates"])
    if candidate_count > MAX_SEED_CANDIDATES:
        raise ContractError(f"seed candidates exceed MAX_SEED_CANDIDATES={MAX_SEED_CANDIDATES}")


def _validate_theme_seed(theme: Mapping[str, Any], index: int) -> None:
    prefix = f"seed.themes[{index}]"
    reject_unknown_fields(
        dict(theme),
        {
            "theme_id",
            "name",
            "event_type",
            "stage",
            "dimensions",
            "transmission_chain",
            "next_catalyst_at",
            "data_gaps",
            "candidates",
        },
        prefix,
    )
    validate_identifier(theme.get("theme_id"), f"{prefix}.theme_id")
    require_string(theme.get("name"), f"{prefix}.name")
    require_string(theme.get("event_type"), f"{prefix}.event_type")
    if theme.get("stage") not in STAGES:
        raise ContractError(f"{prefix}.stage must be one of {list(STAGES)}")
    dimensions = require_mapping(theme.get("dimensions"), f"{prefix}.dimensions")
    reject_unknown_fields(dimensions, set(DIMENSIONS), f"{prefix}.dimensions")
    if set(dimensions) != set(DIMENSIONS):
        raise ContractError(f"{prefix}.dimensions must define {list(DIMENSIONS)}")
    for name in DIMENSIONS:
        dimension = require_mapping(dimensions[name], f"{prefix}.dimensions.{name}")
        reject_unknown_fields(dimension, {"assessment", "reason"}, f"{prefix}.dimensions.{name}")
        if dimension.get("assessment") not in ASSESSMENTS:
            raise ContractError(f"{prefix}.dimensions.{name}.assessment is invalid")
        require_string(dimension.get("reason"), f"{prefix}.dimensions.{name}.reason")
    chain = require_list(theme.get("transmission_chain"), f"{prefix}.transmission_chain")
    for item_index, item in enumerate(chain):
        require_string(item, f"{prefix}.transmission_chain[{item_index}]")
    parse_datetime(theme.get("next_catalyst_at"), f"{prefix}.next_catalyst_at")
    _string_list(theme.get("data_gaps", []), f"{prefix}.data_gaps")
    candidates = require_list(theme.get("candidates"), f"{prefix}.candidates")
    if not candidates:
        raise ContractError(f"{prefix}.candidates must not be empty")
    for candidate in candidates:
        _validate_candidate_seed(require_mapping(candidate, f"{prefix}.candidate"))


def _validate_candidate_seed(seed: Mapping[str, Any]) -> None:
    allowed = {
        "security_id",
        "symbol",
        "name",
        "cik",
        "role",
        "thesis",
        "bull_case",
        "bear_case",
        "risk_verdict",
        "invalidation_conditions",
        "data_gaps",
        "risk_flags",
        "manual_review_items",
    }
    reject_unknown_fields(dict(seed), allowed, "candidate seed")
    required = allowed - {"data_gaps", "risk_flags", "manual_review_items"}
    missing = sorted(required - set(seed))
    if missing:
        raise ContractError("candidate seed missing fields: " + ", ".join(missing))
    validate_identifier(seed.get("security_id"), "candidate.security_id")
    validate_symbol(seed.get("symbol"), "candidate.symbol")
    require_string(seed.get("name"), "candidate.name")
    _normalize_cik(seed.get("cik"))
    if seed.get("role") not in CANDIDATE_ROLES:
        raise ContractError(f"candidate.role must be one of {list(CANDIDATE_ROLES)}")
    for field in ("thesis", "bull_case", "bear_case", "risk_verdict"):
        require_string(seed.get(field), f"candidate.{field}")
    for field in ("invalidation_conditions", "data_gaps", "risk_flags", "manual_review_items"):
        _string_list(seed.get(field, []), f"candidate.{field}")


def _validate_market_payload(
    market: Mapping[str, Any],
    *,
    retrieved: datetime,
    snapshot_as_of: datetime,
    security_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    reject_unknown_fields(
        market,
        {
            "schema_version",
            "market",
            "provider",
            "license_attestation",
            "evidence",
            "financial_facts",
            "market_context",
        },
        "market JSON",
    )
    if market.get("schema_version") != SCHEMA_VERSION or market.get("market") != MARKET:
        raise ContractError("market JSON schema_version/market must be 0.1/US")
    require_string(market.get("provider"), "market.provider")
    if market.get("license_attestation") != LICENSE_ATTESTATION:
        raise ContractError(f"market.license_attestation must be {LICENSE_ATTESTATION}")
    evidence = [
        dict(require_mapping(item, "market.evidence item"))
        for item in require_list(market.get("evidence"), "market.evidence")
    ]
    facts = [
        dict(require_mapping(item, "market.financial_facts item"))
        for item in require_list(market.get("financial_facts"), "market.financial_facts")
    ]
    context = dict(require_mapping(market.get("market_context"), "market.market_context"))
    evidence_ids: set[str] = set()
    for item in evidence:
        if item.get("source_level") != "structured_market":
            raise ContractError("market evidence must use source_level=structured_market")
        if item.get("category") not in MARKET_EVIDENCE_CATEGORIES:
            raise ContractError("market evidence category is unsupported")
        evidence_id = validate_identifier(item.get("evidence_id"), "market evidence_id")
        if evidence_id in evidence_ids:
            raise ContractError(f"duplicate market evidence_id: {evidence_id}")
        evidence_ids.add(evidence_id)
        if parse_datetime(item.get("retrieved_at"), "market evidence retrieved_at") > retrieved:
            raise ContractError(
                "market evidence retrieved_at must not exceed snapshot retrieved_at"
            )
        if parse_datetime(item.get("as_of"), "market evidence as_of") > snapshot_as_of:
            raise ContractError("market evidence as_of must not exceed seed.as_of")
    close_by_security: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if fact.get("metric") != "close_price":
            raise ContractError("market financial_facts may only contain close_price")
        security_id = validate_identifier(fact.get("security_id"), "market fact security_id")
        if security_id not in security_ids:
            raise ContractError(f"market close_price references unknown security_id: {security_id}")
        if security_id in close_by_security:
            raise ContractError(f"market JSON contains duplicate close_price for {security_id}")
        evidence_ref = fact.get("evidence_ref")
        evidence_item = next(
            (item for item in evidence if item["evidence_id"] == evidence_ref), None
        )
        if evidence_item is None or evidence_item.get("category") != "market_price":
            raise ContractError("close_price must reference structured market_price evidence")
        value = fact.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ContractError("close_price value must be a finite positive number")
        if fact.get("unit") != "USD/share":
            raise ContractError("close_price unit must be USD/share")
        fact_available = parse_datetime(fact.get("available_at"), "close_price.available_at")
        if fact_available > retrieved:
            raise ContractError("close_price.available_at must not exceed collection start")
        evidence_available = parse_datetime(
            evidence_item.get("available_at"), "market evidence available_at"
        )
        if fact_available < evidence_available:
            raise ContractError(
                "close_price.available_at must not precede cited evidence.available_at"
            )
        period_end = _parse_date(fact.get("period_end"), "close_price.period_end")
        evidence_as_of = parse_datetime(evidence_item.get("as_of"), "market evidence as_of")
        if period_end != evidence_as_of.date():
            raise ContractError("close_price.period_end must match cited evidence.as_of date")
        close_by_security[security_id] = fact
    missing = sorted(security_ids - set(close_by_security))
    if missing:
        raise ContractError("market JSON is missing close_price for: " + ", ".join(missing))
    return evidence, facts, context


def _unknown_market_context() -> dict[str, Any]:
    return {
        "regime": "UNKNOWN",
        "breadth": "UNKNOWN",
        "rates": "UNKNOWN",
        "liquidity": "UNKNOWN",
        "calculation_note": (
            "No licensed structured market JSON supplied. SEC filing availability uses a three-minute estimate; actual public availability can be later, so near-filing strict PIT use remains UNKNOWN."
        ),
        "evidence_refs": [],
    }


def _has_newer_interim(
    filings: Mapping[str, Mapping[str, Any]], annual_period_end: date | None
) -> bool:
    return annual_period_end is not None and any(
        _base_form(filing["form"]) in INTERIM_BASE_FORMS
        and filing["report_date"] > annual_period_end
        for filing in filings.values()
    )


def _base_form(form: str) -> str:
    return form.removesuffix("/A")


def _filing_is_superseded(
    filing: Mapping[str, Any], filings: Mapping[str, Mapping[str, Any]]
) -> bool:
    return any(
        other["accn"] != filing["accn"]
        and _base_form(other["form"]) == _base_form(filing["form"])
        and other["report_date"] == filing["report_date"]
        and other["acceptance"] > filing["acceptance"]
        for other in filings.values()
    )


def _same_period_and_accession(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        left.get("accn") == right.get("accn")
        and left.get("start") == right.get("start")
        and left.get("end") == right.get("end")
    )


def _publish_snapshot(workspace: Path, snapshot_id: str, snapshot: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ContractError(f"snapshot exceeds MAX_SNAPSHOT_BYTES={MAX_SNAPSHOT_BYTES}")
    workspace_root = workspace.expanduser().resolve()
    root = workspace_root
    for component in ("data", "normalized", "us"):
        root = root / component
        try:
            root.mkdir(mode=0o755)
        except FileExistsError:
            metadata = root.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ContractError("normalized snapshot root is unsafe")
    target = root / snapshot_id
    try:
        target.mkdir(mode=0o755)
    except FileExistsError as exc:
        try:
            metadata = target.lstat()
        except OSError:
            metadata = None
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise ContractError(f"snapshot target is unsafe: {snapshot_id}") from exc
        raise ContractError(f"snapshot already exists: {snapshot_id}") from exc

    temporary = target / ".snapshot.json.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target / "snapshot.json")
        directory_fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def _read_json_file(path: Path | None, field: str) -> dict[str, Any]:
    if path is None:
        raise ContractError(f"{field} path is required")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path.expanduser(), flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
            raise ContractError(f"{field} is not a bounded regular file")
        payload = os.read(descriptor, MAX_INPUT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_INPUT_BYTES:
        raise ContractError(f"{field} exceeds {MAX_INPUT_BYTES} bytes")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{field} must contain valid UTF-8 JSON") from exc
    return require_mapping(result, field)


def _urlopen_transport(request: Request, timeout: float) -> HttpResponse:
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_SEC_RESPONSE_BYTES + 1)
        headers = {key.casefold(): value for key, value in response.headers.items()}
    if len(body) > MAX_SEC_RESPONSE_BYTES:
        raise RuntimeError("SEC response exceeds the configured size limit")
    return HttpResponse(body=body, headers=headers)


def _decode_http_body(response: HttpResponse) -> bytes:
    headers = {key.casefold(): value for key, value in response.headers.items()}
    encoding = headers.get("content-encoding", "").casefold()
    try:
        if encoding == "gzip":
            return gzip.decompress(response.body)
        if encoding == "deflate":
            return zlib.decompress(response.body)
        return response.body
    except (gzip.BadGzipFile, zlib.error) as exc:
        raise RuntimeError("SEC returned invalid compressed data") from exc


def _normalize_cik(value: Any) -> str:
    raw = require_string(value, "cik", strip=False)
    if not raw.isascii() or not raw.isdigit() or not 1 <= len(raw) <= 10 or int(raw) <= 0:
        raise ContractError("cik must contain 1 to 10 ASCII digits")
    return raw.zfill(10)


def _require_symbol_ownership(seed: Mapping[str, Any], submissions: Mapping[str, Any]) -> None:
    symbol = validate_symbol(seed.get("symbol"), "candidate.symbol")
    expected_security_id = f"US.{symbol}"
    if seed.get("security_id") != expected_security_id:
        raise ContractError(
            f"candidate.security_id must equal {expected_security_id} for symbol {symbol}"
        )
    tickers = require_list(submissions.get("tickers"), "SEC submissions.tickers")
    normalized_tickers = {
        _normalize_ticker(item) for item in tickers if isinstance(item, str) and item.strip()
    }
    if _normalize_ticker(symbol) not in normalized_tickers:
        raise ContractError("candidate.symbol does not belong to the supplied SEC CIK")


def _normalize_ticker(value: str) -> str:
    return value.strip().upper().replace(".", "-")


def _require_matching_sec_cik(value: Any, expected: str, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ContractError(f"{field} is missing a valid CIK")
    actual = _normalize_cik(str(value))
    if actual != expected:
        raise ContractError(f"{field} CIK does not match the research seed")


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _read_clock(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("collection clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _valid_accession(value: str) -> bool:
    parts = value.split("-")
    return (
        len(parts) == 3
        and len(parts[0]) == 10
        and len(parts[1]) == 2
        and len(parts[2]) == 6
        and all(part.isascii() and part.isdigit() for part in parts)
    )


def _parse_sec_datetime(value: Any, field: str) -> datetime:
    raw = require_string(value, field, strip=False)
    if len(raw) == 10:
        parsed_date = _parse_date(raw, field)
        return datetime.combine(parsed_date, datetime_time.min, tzinfo=UTC)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return parse_datetime(raw, field).astimezone(UTC)


def _parse_date(value: Any, field: str) -> date:
    raw = require_string(value, field, strip=False)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO-8601 date") from exc


def _optional_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_at_utc(value: date) -> str:
    return datetime.combine(value, datetime_time.min, tzinfo=UTC).isoformat()


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def _string_list(value: Any, field: str) -> list[str]:
    items = require_list(value, field)
    return [require_string(item, f"{field}[]") for item in items]


def _unique_strings(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
