"""Deterministic, bounded research observations. No network or trading model."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, parse_datetime
from .utils import sha256_value

VERSION = "cn-cohort-feedback-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
BENCHMARK = "sh.000001"
HORIZONS = (1, 5, 20)
TIME_FIELDS = ("published_at", "available_at", "retrieved_at", "as_of")


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".4f")


def available(evidence: dict, cutoff: datetime) -> bool:
    try:
        return all(parse_datetime(evidence[key], key) <= cutoff for key in TIME_FIELDS)
    except (KeyError, ValueError, TypeError):
        return False


def calendar_record(data: dict, cutoff: datetime) -> dict | None:
    calendars = []
    for evidence in data["evidence"]:
        if evidence.get("category") != "trading_calendar" or not available(evidence, cutoff):
            continue
        if evidence.get("source_level") not in {"official", "structured_market"}:
            continue
        raw = evidence.get("calendar", {})
        if not isinstance(raw,dict):
            raise ContractError("calendar must be an object")
        if raw.get("market") != "CN" or raw.get("complete") is not True:
            continue
        days = raw.get("sessions")
        if not isinstance(days, list) or not days or len(days) > 4000:
            raise ContractError("calendar sessions must be a non-empty bounded array")
        try:
            start, end = date.fromisoformat(raw["coverage_start"]), date.fromisoformat(raw["coverage_end"])
            parsed = [date.fromisoformat(day) for day in days]
        except (ValueError, KeyError, TypeError) as exc:
            raise ContractError("invalid calendar date") from exc
        if [d.isoformat() for d in parsed] != days or parsed != sorted(set(parsed)) or parsed[0] < start or parsed[-1] > end:
            raise ContractError("calendar dates must be unique, ordered and inside coverage")
        if not isinstance(raw.get("exchanges"),list) or not raw["exchanges"] or any(x not in {"SH","SZ","BJ"} for x in raw["exchanges"]):
            raise ContractError("unsupported calendar exchanges")
        calendars.append(raw | {"evidence_ref": evidence["evidence_id"]})
    if not calendars:
        return None
    # Multiple conflicting calendars are never silently resolved by order.
    if any({k:v for k,v in c.items() if k != "evidence_ref"} !=
           {k:v for k,v in calendars[0].items() if k != "evidence_ref"} for c in calendars[1:]):
        return None
    return calendars[0]


def expected_session(data: dict, cutoff: datetime) -> str | None:
    cal = calendar_record(data, cutoff)
    local = cutoff.astimezone(SHANGHAI)
    if not cal or "SH" not in cal["exchanges"] or not cal["coverage_start"] <= local.date().isoformat() <= cal["coverage_end"]:
        return None
    closed = [day for day in cal["sessions"] if
              datetime.combine(date.fromisoformat(day), time(15), SHANGHAI) <= cutoff]
    return closed[-1] if closed else None


def _bars(datasets: list[dict], cutoff: datetime) -> dict:
    result: dict[tuple[str, str], dict] = {}
    for data in datasets:
        for e in data["evidence"]:
            if e.get("source_level") != "structured_market" or not available(e, cutoff):
                continue
            provider = e.get("provider", {})
            if provider.get("frequency") != "1d" or provider.get("adjustment") != "none":
                continue
            code = e.get("instrument", {}).get("code")
            as_of = parse_datetime(e["as_of"], "as_of")
            for bar in [*e.get("calculation_window", []), e.get("latest", {})]:
                day = bar.get("date")
                if not code or bar.get("code") != code or not isinstance(day, str):
                    continue
                try:
                    close_at = datetime.combine(date.fromisoformat(day), time(15), SHANGHAI)
                except ValueError:
                    continue
                if close_at > as_of or close_at > cutoff:
                    continue
                key = (code, day)
                fields = {k:bar.get(k) for k in ("close", "preclose", "amount", "trade_status")}
                ref = f"{data['snapshot_id']}:{e['evidence_id']}"
                previous = result.get(key)
                if previous is None:
                    result[key] = {"fields": fields, "refs": [ref], "conflict": False}
                else:
                    # Missing older reference prices may be completed by a later receipt.
                    for field, value in fields.items():
                        old = previous["fields"][field]
                        if old in (None, "UNKNOWN"):
                            previous["fields"][field] = value
                        elif value not in (None, "UNKNOWN") and (
                            number(old) != number(value) if field != "trade_status" else old != value
                        ):
                            previous["conflict"] = True
                    if ref not in previous["refs"]:
                        previous["refs"].append(ref)
    return result


def _change(point: dict | None) -> Decimal | None:
    if not point or point["conflict"] or point["fields"].get("trade_status") != "1":
        return None
    close, preclose = (number(point["fields"].get(k)) for k in ("close", "preclose"))
    if close is None or preclose is None or close <= 0 or preclose <= 0:
        return None
    return close / preclose - 1


def _stats(values: list[Decimal | None]) -> dict:
    observed = [v for v in values if v is not None]
    return {
        "status": "UNKNOWN" if not observed else "OK" if len(observed) == len(values) else "PARTIAL",
        "observed": len(observed), "total": len(values),
        "median_excess_pct": fmt(median(observed)) if observed else None,
        "minimum_excess_pct": fmt(min(observed)) if observed else None,
        "positive_fraction": fmt(Decimal(sum(v > 0 for v in observed)) / len(observed)) if observed else None,
    }


def _cohorts(codes: list[str], day: str | None, bars: dict) -> dict:
    values = [(code, _change(bars.get((code, day)))) for code in sorted(set(codes))]
    valid = sorted([(c,v) for c,v in values if v is not None], key=lambda x: (-x[1], x[0]))
    size = min(20, len(valid) // 2)
    return {
        "strong": [c for c,_ in valid[:size]],
        "weak": [c for c,_ in valid[-size:]] if size else [],
        "unclassified": [c for c,v in values if v is None],
        "observed": len(valid), "total": len(values), "selection_session": day,
        "rule": "daily reference-price change, disjoint top/bottom min(20, floor(valid_n/2))",
    }


def build_feedback(data: dict, decisions: list[dict], cutoff: datetime, top_n: int) -> dict:
    bars = _bars([data], cutoff)
    anchor = parse_datetime(data["as_of"], "as_of").astimezone(SHANGHAI).date().isoformat()
    codes = {c["candidate_id"]: security_code(c["security_id"]) for c in decisions}
    all_codes = sorted(set(codes.values()))
    groups = _cohorts(all_codes, anchor, bars)
    groups["cohort_id"] = "cn-cohort-" + sha256_value({"snapshot":sha256_value(data), "groups":groups,
                                                     "version":VERSION})[:20]
    cal = calendar_record(data, cutoff)
    previous = None
    if cal and anchor in cal["sessions"] and cal["sessions"].index(anchor) > 0:
        previous = cal["sessions"][cal["sessions"].index(anchor)-1]
    benchmark_change = _change(bars.get((BENCHMARK, anchor)))
    themes = []
    for theme_id in dict.fromkeys(c["theme_id"] for c in decisions):
        members = sorted({codes[c["candidate_id"]] for c in decisions if c["theme_id"] == theme_id})
        prior = _cohorts(members, previous, bars)
        prior["membership_semantics"] = "previous-session ranking reconstructed from current frozen snapshot"
        changes = {code:_change(bars.get((code, anchor))) for code in members}
        excess = {code:(v-benchmark_change)*100 if v is not None and benchmark_change is not None
                  else None for code,v in changes.items()}
        amounts = [number(bars.get((code,anchor), {}).get("fields", {}).get("amount"))
                   if not bars.get((code,anchor), {}).get("conflict") else None for code in members]
        complete = bool(amounts) and all(v is not None and v >= 0 for v in amounts)
        total = sum(amounts, Decimal(0)) if complete else Decimal(0)
        themes.append({
            "theme_id":theme_id, "members":members, "membership_scope":"frozen_research_members_only",
            "diffusion":_stats(list(excess.values())), "prior_cohorts":prior,
            "strong_feedback":_stats([excess[c] for c in prior["strong"]]),
            "weak_feedback":_stats([excess[c] for c in prior["weak"]]),
            "turnover_hhi":fmt(sum((v/total)**2 for v in amounts)) if total > 0 else None,
            "stage":"UNKNOWN", "stage_note":"No calibrated automatic phase classifier",
        })
    baseline = [c["candidate_id"] for c in decisions if c["decision"] != "exclude"]
    priorities = {t["theme_id"]:number(t["strong_feedback"]["median_excess_pct"])
                  if t["strong_feedback"]["status"] == "OK" and not t["prior_cohorts"]["unclassified"]
                  else None for t in themes}
    eligible = [c for c in decisions if c["decision"] != "exclude"]
    ready = bool(eligible) and all(priorities[c["theme_id"]] is not None for c in eligible)
    ordered = sorted(eligible, key=lambda c: (
        {"observe":0,"continue_research":1}[c["decision"]],
        {"core":0,"midcap":1,"elastic":2,"follower":3}[c["role"]],
        -priorities[c["theme_id"]], baseline.index(c["candidate_id"]),
    )) if ready else eligible
    change_values = [_change(bars.get((c,anchor))) for c in all_codes]
    up, down = sum(v is not None and v > 0 for v in change_values), sum(v is not None and v < 0 for v in change_values)
    return {
        "version":VERSION, "anchor_session":anchor, "benchmark_code":BENCHMARK,
        "scope":"candidate_universe_not_full_market", "calendar_status":"VERIFIED_SOURCE" if cal else "UNKNOWN",
        "cohorts":groups, "themes":themes,
        "breadth":{"advancing":up,"declining":down,"unchanged":sum(v == 0 for v in change_values),
                   "unknown":sum(v is None for v in change_values),"total":len(change_values),
                   "advance_fraction":fmt(Decimal(up)/(up+down)) if up+down else None},
        "experiment":{"id":"cn-r0-r1-strong-feedback-v1", "mode":"shadow", "formal_sample":False,
                      "status":"COMPUTED_NOT_VALIDATED" if ready else "UNKNOWN_NO_REORDER",
                      "R0":baseline, "R1":[c["candidate_id"] for c in ordered], "top_n":top_n,
                      "single_change":"prior frozen theme strong-cohort feedback at current close",
                      "efficacy":"UNKNOWN", "note":"Current baseline is partly categorical; not proof of unique sentiment information"},
        "input_evidence_refs":sorted({r for point in bars.values() for r in point["refs"]} |
                                     ({f"{data['snapshot_id']}:{cal['evidence_ref']}"} if cal else set())),
        "limitations":["Price feedback is not investor P&L", "No intraday inference",
                       "Theme membership is editorial, not a complete sector census",
                       "Missing preclose, active status or calendar remains UNKNOWN"],
    }


def security_code(security_id: str) -> str:
    pieces = security_id.split(".")
    if not pieces or pieces[0] != "CN":
        raise ContractError("feedback requires a canonical CN security ID")
    if len(pieces) != 3 or pieces[1] not in {"SH","SZ","BJ"}:
        return "UNKNOWN/" + security_id
    return pieces[1].lower() + "." + pieces[2]


def evaluate_horizons(datasets: list[dict], anchor: str, codes: list[str], benchmark: str,
                      horizons: list[int], cutoff: datetime) -> dict:
    merged = {"evidence":[e for data in datasets for e in data["evidence"]]}
    cal = calendar_record(merged, cutoff)
    bars = _bars(datasets, cutoff)
    outcomes = []
    for code in sorted(set(codes)):
        for horizon in horizons:
            row = {"code":code,"horizon_sessions":horizon,"anchor_session":anchor,
                   "target_session":None,"status":"UNKNOWN","reason":"CALENDAR_UNAVAILABLE",
                   "change_pct":None,"benchmark_change_pct":None,"excess_pct":None,
                   "input_observations":[],"semantic_confirmation":"UNKNOWN"}
            if (cal and anchor in cal["sessions"] and code[:2].upper() in cal["exchanges"]
                    and benchmark[:2].upper() in cal["exchanges"]):
                i = cal["sessions"].index(anchor)
                window = cal["sessions"][i+1:i+1+horizon]
                if len(window) == horizon:
                    row["target_session"] = window[-1]
                    if datetime.combine(date.fromisoformat(window[-1]),time(15),SHANGHAI) > cutoff:
                        row.update(status="PENDING", reason="WINDOW_NOT_MATURE")
                    else:
                        returns = []
                        for instrument in (code, benchmark):
                            chain = Decimal(1)
                            for day in window:
                                point = bars.get((instrument,day))
                                change = _change(point)
                                row["input_observations"].append({"code":instrument,"session":day,
                                                                  "observation":point})
                                if change is None:
                                    chain = None
                                elif chain is not None:
                                    chain *= 1+change
                            returns.append((chain-1)*100 if chain is not None else None)
                        row.update(change_pct=fmt(returns[0]),benchmark_change_pct=fmt(returns[1]),
                                   reason="MISSING_CONFLICTING_OR_INACTIVE_OBSERVATION")
                        if all(v is not None for v in returns):
                            row.update(status="OK",reason="COMPOUNDED_REFERENCE_PRICE_CHANGE",
                                       excess_pct=fmt(returns[0]-returns[1]))
            outcomes.append(row)
    return {"outcomes":outcomes,"calendar_evidence_ref":cal["evidence_ref"] if cal else None,
            "formula":"(product(close / provider_reference_preclose) - 1) * 100; excess = security - benchmark",
            "interpretation":"Research price feedback, not total return, executable return or thesis validation"}
