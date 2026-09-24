"""US-only capability checks and conditional research cards, with no live data access."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .contracts import ContractError, parse_datetime, reject_unknown_fields

NEW_YORK = ZoneInfo("America/New_York")
SHANGHAI = ZoneInfo("Asia/Shanghai")
VERSION = "us-research-plan-v1"
SOURCE_FIELDS = (
    "evidence_id",
    "source_url",
    "source_level",
    "published_at",
    "effective_at",
    "available_at",
    "retrieved_at",
    "as_of",
)


def validate_session_calendar(data, evidence):
    cal = data.get("session_calendar")
    if cal is None:
        return
    if not isinstance(cal, dict):
        raise ContractError("session_calendar must be an object")
    reject_unknown_fields(
        cal,
        {"scope", "source_evidence_ref", "coverage_start", "coverage_end", "complete", "sessions"},
        "session_calendar",
    )
    if cal.get("scope") != "US_CORE_EQUITIES" or cal.get("complete") is not True:
        raise ContractError("session_calendar requires complete US_CORE_EQUITIES scope")
    ref = cal.get("source_evidence_ref")
    if ref not in evidence or evidence[ref]["source_level"] != "official":
        raise ContractError("session_calendar requires official source evidence")
    start, end = date.fromisoformat(cal["coverage_start"]), date.fromisoformat(cal["coverage_end"])
    rows = cal.get("sessions")
    if start > end or not isinstance(rows, list) or not 1 <= len(rows) <= 4000:
        raise ContractError("invalid session_calendar coverage")
    previous = None
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("session_calendar session must be an object")
        reject_unknown_fields(row, {"date", "open_at", "close_at"}, "session_calendar.session")
        day = date.fromisoformat(row["date"])
        opening = parse_datetime(row["open_at"], "open_at").astimezone(NEW_YORK)
        closing = parse_datetime(row["close_at"], "close_at").astimezone(NEW_YORK)
        if (
            not start <= day <= end
            or (previous and day <= previous)
            or opening >= closing
            or opening.date() != day
            or closing.date() != day
        ):
            raise ContractError("invalid or duplicate session_calendar session")
        previous = day


def usable_calendar(snapshot, cutoff):
    cal = snapshot.data.get("session_calendar")
    if not cal:
        return None
    e = snapshot.evidence_by_id[cal["source_evidence_ref"]]
    today = cutoff.astimezone(NEW_YORK).date().isoformat()
    if parse_datetime(e["available_at"], "available_at") > cutoff:
        return None
    return cal if cal["coverage_start"] <= today <= cal["coverage_end"] else None


def price_readiness(snapshot, candidate, cutoff):
    facts = [snapshot.financial_facts_by_id[f] for f in candidate.get("financial_fact_refs", [])]
    prices = []
    for f in facts:
        e = snapshot.evidence_by_id[f["evidence_ref"]]
        if (
            f["metric"] == "close_price"
            and f["security_id"] == candidate["security_id"]
            and f["unit"] == "USD/share"
            and f["value"] > 0
            and e["source_level"] == "structured_market"
            and e["category"] == "market_price"
            and parse_datetime(f["available_at"], "available_at") <= cutoff
            and parse_datetime(e["available_at"], "available_at") <= cutoff
        ):
            prices.append((f, e))
    if not prices:
        return {"status": "UNKNOWN_NO_PRICE", "fact_id": None, "as_of": None}
    prices.sort(
        key=lambda pair: (
            parse_datetime(pair[1]["as_of"], "as_of"),
            parse_datetime(pair[0]["available_at"], "available_at"),
            pair[0]["fact_id"],
        ),
        reverse=True,
    )
    f, e = prices[0]
    observed = parse_datetime(e["as_of"], "as_of")
    status = "UNKNOWN_CALENDAR"
    if snapshot.data_mode == "fixture":
        status = "FIXTURE"
    elif cutoff - observed > timedelta(hours=96):
        status = "STALE"
    else:
        cal = usable_calendar(snapshot, cutoff)
        if cal:
            closed = [
                row
                for row in cal["sessions"]
                if parse_datetime(row["close_at"], "close_at") <= cutoff
            ]
            if closed:
                expected = closed[-1]
                status = (
                    "CURRENT_CORE_SESSION"
                    if f["period_end"] == expected["date"]
                    and observed == parse_datetime(expected["close_at"], "close_at")
                    else "SESSION_MISMATCH"
                )
    return {
        "status": status,
        "fact_id": f["fact_id"],
        "as_of": e["as_of"],
        "source_evidence_ref": e["evidence_id"],
        "value": f["value"],
        "unit": f["unit"],
    }


def _cap(count, total, missing_reason):
    return {
        "observed": count,
        "total": total,
        "status": "READY" if total and count == total else "PARTIAL" if count else "UNKNOWN",
        "missing_reason": missing_reason,
    }


def build_diagnostics(snapshot, decisions, cutoff):
    source_candidates = {
        f"{t['theme_id']}:{c['security_id']}": c for t in snapshot.themes for c in t["candidates"]
    }
    rows, cards = [], []
    for c in decisions:
        price = price_readiness(snapshot, source_candidates[c["candidate_id"]], cutoff)
        expired = parse_datetime(c["next_catalyst_at"], "next_catalyst_at") <= cutoff
        calc = c["calculations"]
        known = sum(x["status"] != "UNKNOWN" for x in calc)
        peers = sorted(
            {
                d["symbol"]
                for d in decisions
                if d["theme_id"] == c["theme_id"] and d["symbol"] != c["symbol"]
            }
        )
        rows.append(
            {
                "candidate_id": c["candidate_id"],
                "symbol": c["symbol"],
                "decision": c["decision"],
                "price_status": price["status"],
                "price": price,
                "expired_catalyst": expired,
                "catalyst_verification": "UNKNOWN_SEED_DATE_NOT_INDEPENDENTLY_CONFIRMED",
                "calculation_coverage": {"observed": known, "total": len(calc)},
                "expectations": "UNKNOWN_NO_VERSIONED_CONSENSUS",
                "peer_members": peers,
                "peer_scope": "frozen_research_members_not_verified_industry_peers",
            }
        )
        baseline = "; ".join(f"{x['metric']}={x.get('value')} ({x['status']})" for x in calc)
        purpose = "EVIDENCE_REPAIR" if c["decision"] == "exclude" else "CONDITIONAL_RESEARCH"
        sources = [{k: e[k] for k in SOURCE_FIELDS} for e in c["evidence"]]
        cards.append(
            {
                "card_id": f"US-C{len(cards) + 1}",
                "candidate_id": c["candidate_id"],
                "symbol": c["symbol"],
                "formal_decision": c["decision"],
                "purpose": purpose,
                "question": f"{c['symbol']} 的新披露能否支持原经营假设，且获得可比同行和价格证据？",
                "baseline": baseline,
                "price_status": price["status"],
                "peer_members": peers,
                "original_catalyst_at": c["next_catalyst_at"],
                "catalyst_new_york": parse_datetime(c["next_catalyst_at"], "next_catalyst_at")
                .astimezone(NEW_YORK)
                .isoformat(),
                "catalyst_shanghai": parse_datetime(c["next_catalyst_at"], "next_catalyst_at")
                .astimezone(SHANGHAI)
                .isoformat(),
                "catalyst_label": "已过期，重新核验"
                if expired
                else "种子日期待公司IR/正式公告确认；不是已验证财报日",
                "before_event": "先查最新10-K/10-Q及相关8-K/公司IR材料，固定accession/版本和公开时间；核修订、财年/季度、GAAP与非GAAP口径。",
                "record_before": "逐项记录原预期及来源版本：收入/经营利润率/现金流、业务指标、管理层指引范围。缺少事前一致预期时，禁止把同比增长称为超预期。",
                "measure_after": "披露后按同一期间和会计口径对照实际值、原指引及事前预期；为收入、利润率、现金流分别标注支持/反对/UNKNOWN。",
                "market_check": "先补本股票有日期且获授权的常规时段收盘与同口径基准；盘前/盘后报价单列。同行名单须核业务可比性，不能把本研究的同题材成员当作完整同行。",
                "support_condition": "新原始披露支持原命题，关键经营指标口径可比，主要反方解释被新证据回应；另有同一研究时点可得的合格市场证据。",
                "counter_condition": "正式披露否定业务映射或原证伪条件触发；收入改善但利润率/现金流恶化须分别保留，不用股价上涨覆盖经营反证。",
                "on_support": "形成新证据版本并按全部原门槛重新运行；仍缺市场、观点支持或关键计算时不升级observe。",
                "on_counter": "撤回被反证的具体命题，保留原报告及反方证据；依据完整新输入重评exclude/continue_research/observe。",
                "on_unknown": "按缺口逐项补证；禁止用旧报价补新时点、用FY冒充TTM、用事后预期回填事前版本。",
                "invalidation_conditions": c["invalidation_conditions"],
                "risk_flags": c["risk_flags"],
                "data_gaps": c["data_gaps"],
                "sources": sources,
                "observed_result": "PENDING_MANUAL_REVIEW",
            }
        )
    n = len(rows)
    valid = [
        e
        for e in snapshot.data["evidence"]
        if parse_datetime(e["available_at"], "available_at") <= cutoff
    ]
    cal = usable_calendar(snapshot, cutoff)
    upcoming = (
        next((s for s in cal["sessions"] if parse_datetime(s["open_at"], "open_at") > cutoff), None)
        if cal
        else None
    )
    checks = []
    if upcoming:
        opening = parse_datetime(upcoming["open_at"], "open_at")
        closing = parse_datetime(upcoming["close_at"], "close_at")
        for label, stamp in [
            ("盘前补证", opening - timedelta(minutes=30)),
            ("常规时段观察", opening + timedelta(minutes=30)),
            ("收盘核验", closing + timedelta(minutes=15)),
        ]:
            if stamp <= cutoff:
                continue
            checks.append(
                {
                    "label": label,
                    "new_york": stamp.astimezone(NEW_YORK).isoformat(),
                    "shanghai": stamp.astimezone(SHANGHAI).isoformat(),
                }
            )
    return {
        "version": VERSION,
        "snapshot_as_of": snapshot.data["as_of"],
        "snapshot_retrieved_at": snapshot.data["retrieved_at"],
        "latest_input_available_at": max(
            (e["available_at"] for e in valid),
            key=lambda x: parse_datetime(x, "available_at"),
            default=None,
        ),
        "latest_input_retrieved_at": max(
            (e["retrieved_at"] for e in valid),
            key=lambda x: parse_datetime(x, "retrieved_at"),
            default=None,
        ),
        "capabilities": {
            "issuer_official": _cap(
                sum(c["gates"]["issuer_specific_official"] for c in decisions), n, "缺公司原始披露"
            ),
            "current_core_price": _cap(
                sum(r["price_status"] == "CURRENT_CORE_SESSION" for r in rows),
                n,
                "需授权价格、明确日期及常规时段日历；fixture不算真实覆盖",
            ),
            "complete_calculations": _cap(
                sum(
                    r["calculation_coverage"]["observed"] == r["calculation_coverage"]["total"]
                    for r in rows
                ),
                n,
                "补齐同口径财务与估值输入；NOT_MEANINGFUL不等于有数值",
            ),
            "versioned_expectations": _cap(
                0, n, "尚未接入事前一致预期或指引版本，不能自动判超预期"
            ),
        },
        "candidate_readiness": rows,
        "plan_cards": cards,
        "next_core_session": upcoming["date"] if upcoming else None,
        "checkpoints": checks,
        "calendar_evidence_ref": cal["source_evidence_ref"] if cal else None,
        "execution_mode": "MANUAL_RESEARCH_NOT_SCHEDULED",
        "semantic_model_execution": "NOT_INVOKED",
        "interpretation_contract": [
            "先核披露正文与会计期间，再核预期版本，最后检查同行与同口径市场反馈。",
            "每个数字引用fact_id/计算输入，支持、反方、风险分开。",
            "缺少逐时点预期或市场证据时，未知不能被模型语气覆盖。",
        ],
    }
