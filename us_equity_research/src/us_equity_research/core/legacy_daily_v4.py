"""Reproducible daily review of staged quotes and separately validated SEC inputs.

This does not promote staged quotes into the canonical evidence contract.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..ingest.futu_quotes import validate_plan
from .calculations import build_calculation_bundle
from .contracts import ContractError, parse_datetime
from .research_diagnostics import validate_session_calendar
from .snapshot import validate_snapshot

NY = ZoneInfo("America/New_York")
SH = ZoneInfo("Asia/Shanghai")
SECTORS = {
    "US.XLC": "通信服务",
    "US.XLY": "可选消费",
    "US.XLP": "必需消费",
    "US.XLE": "能源",
    "US.XLF": "金融",
    "US.XLV": "医疗",
    "US.XLI": "工业",
    "US.XLB": "材料",
    "US.XLRE": "房地产",
    "US.XLK": "科技",
    "US.XLU": "公用事业",
}
VERSION = "us-daily-review-4"
DISCLAIMER = "本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。"


def analyze(bundle, calendar, sec_snapshot, decision_at, fundamentals=None, filing_bodies=None):
    cutoff = parse_datetime(decision_at, "decision_at")
    validate_plan(bundle["plan"])
    if bundle.get("schema_version") != "futu-stage-1" or bundle.get("market") != "US":
        raise ContractError("Expected real US Futu staging bundle")
    if bundle.get("provider") not in {"futu_mcp", "futu_rest"}:
        raise ContractError("Unsupported quote provider")
    received = parse_datetime(bundle["retrieved_at"], "retrieved_at")
    if received > cutoff:
        raise ContractError("Quote collection exceeds decision time")
    cal, ce = calendar["session_calendar"], calendar["evidence"]
    validate_session_calendar({"session_calendar": cal}, {ce["evidence_id"]: ce})
    if parse_datetime(ce["available_at"], "calendar.available_at") > cutoff:
        raise ContractError("Calendar unavailable at decision time")
    reviewed = parse_datetime(ce.get("retrieved_at", ce["available_at"]), "calendar.retrieved_at")
    if not timedelta(0) <= cutoff - reviewed <= timedelta(days=7):
        raise ContractError("Calendar review older than 7 days; refresh official schedule")
    if not cal["coverage_start"] <= cutoff.astimezone(NY).date().isoformat() <= cal["coverage_end"]:
        raise ContractError("Calendar coverage expired; review exchange schedule before running")
    expected = [
        s["date"] for s in cal["sessions"] if parse_datetime(s["close_at"], "close") <= cutoff
    ]
    if not expected:
        raise ContractError("No completed calendar session")
    latest = expected[-1]
    session_seconds = {
        s["date"]: (
            parse_datetime(s["close_at"], "close") - parse_datetime(s["open_at"], "open")
        ).total_seconds()
        for s in cal["sessions"]
    }
    securities = bundle["securities"]
    symbols = [s["symbol"] for s in securities]
    if len(set(symbols)) != len(symbols) or set(symbols) != set(bundle["plan"]["symbols"]):
        raise ContractError("Quote universe does not match frozen plan")
    metrics = {}
    for security in securities:
        series = {}
        for bar in security["split_adjusted"]:
            day = bar["date"]
            if day in series:
                raise ContractError("Duplicate daily bar")
            if not bundle["plan"]["start"] <= day <= bundle["plan"]["end"]:
                raise ContractError("Bar outside frozen interval")
            available = parse_datetime(bar["available_at"], "bar.available_at")
            retrieved = parse_datetime(bar["retrieved_at"], "bar.retrieved_at")
            if available > retrieved or retrieved > received:
                raise ContractError("Bar time exceeds collection boundary")
            for key in ("close", "volume"):
                value = bar[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    or (key == "close" and value == 0)
                ):
                    raise ContractError("Invalid daily calculation input")
            series[day] = bar
        item = {
            "symbol": security["symbol"],
            "source_url": security["source_url"],
            "last_date": max(series, default=None),
            "missing_sessions": [],
            "returns": {},
        }
        item["missing_sessions"] = [
            d for d in expected if d >= bundle["plan"]["start"] and d not in series
        ]
        for k in (1, 5, 20, 60):
            window = expected[-k - 1 :]
            value = None
            if len(window) == k + 1 and all(d in series for d in window):
                value = series[latest]["close"] / series[window[0]]["close"] - 1
            item["returns"][str(k)] = value
        window = expected[-21:]
        item["volume_ratio"] = None
        if len(window) == 21 and all(d in series and session_seconds[d] == 23400 for d in window):
            average = sum(series[d]["volume"] for d in window[:-1]) / 20
            if average > 0:
                item["volume_ratio"] = series[latest]["volume"] / average
        metrics[security["symbol"]] = item
    benchmark = bundle["plan"]["benchmark"]
    base = metrics[benchmark]["returns"]["20"]
    for item in metrics.values():
        value = item["returns"]["20"]
        item["relative_20d"] = value - base if value is not None and base is not None else None
    sector_rows = [
        metrics[s] for s in SECTORS if s in metrics and metrics[s]["relative_20d"] is not None
    ]
    leaders = sorted(
        (m for m in sector_rows if m["relative_20d"] > 0),
        key=lambda x: (-x["relative_20d"], x["symbol"]),
    )[:3]
    sector_positive = sum(
        m["returns"]["1"] > 0 for m in sector_rows if m["returns"]["1"] is not None
    )
    sector_complete = len(sector_rows) == len(SECTORS) and all(
        m["returns"]["1"] is not None for m in sector_rows
    )
    sec = {
        "status": "MISSING",
        "retrieved_at": "UNKNOWN",
        "as_of": "UNKNOWN",
        "evidence": [],
        "candidates": [],
    }
    if sec_snapshot is not None:
        validated = validate_snapshot(sec_snapshot)
        if sec_snapshot.get("data_mode") == "fixture":
            raise ContractError("Fixture SEC inputs cannot enter a real daily review")
        stamp = parse_datetime(sec_snapshot["retrieved_at"], "SEC.retrieved_at")
        if stamp > cutoff:
            raise ContractError("SEC snapshot exceeds decision time")
        sec.update(
            status="CURRENT_COLLECTION"
            if stamp.astimezone(NY).date() == cutoff.astimezone(NY).date()
            else "OLDER_COLLECTION",
            retrieved_at=sec_snapshot["retrieved_at"],
            as_of=sec_snapshot["as_of"],
        )
        sec["evidence"] = [
            e
            for e in sec_snapshot["evidence"]
            if e["source_level"] == "official"
            and parse_datetime(e["available_at"], "evidence.available_at") <= cutoff
        ]
        for theme in sec_snapshot["themes"]:
            for c in theme["candidates"]:
                sec["candidates"].append(
                    {
                        "symbol": c["symbol"],
                        "financials": build_calculation_bundle(validated, c, cutoff),
                        "state": "exclude",
                        "reason": "行情原始时间语义未齐全，不能将待验行情提升为正式候选证据；正文与经营预期尚待复核。",
                        "stage": theme["stage"],
                        "invalidation_conditions": c["invalidation_conditions"],
                        "data_gaps": c["data_gaps"],
                        "manual_review_items": c["manual_review_items"],
                        "next_catalyst": "UNKNOWN（种子日期不视为已确认日程）",
                    }
                )
    future = next(
        (s for s in cal["sessions"] if parse_datetime(s["open_at"], "open") > cutoff), None
    )
    checks = []
    if future:
        opening = parse_datetime(future["open_at"], "open")
        closing = parse_datetime(future["close_at"], "close")
        for title, stamp in [
            ("盘前核验", opening - timedelta(minutes=30)),
            ("开盘后核验", opening + timedelta(minutes=30)),
            ("收盘复核", closing + timedelta(minutes=15)),
        ]:
            if stamp > cutoff:
                checks.append(
                    {
                        "title": title,
                        "new_york": stamp.astimezone(NY).isoformat(),
                        "shanghai": stamp.astimezone(SH).isoformat(),
                    }
                )
    fundamental_results = []
    if fundamentals is not None:
        from ..ingest.fundamentals import analyze_fundamentals

        if (
            fundamentals.get("schema_version") != "us-fundamentals-1"
            or fundamentals.get("market") != "US"
        ):
            raise ContractError("Unsupported financial input bundle")
        if parse_datetime(fundamentals["retrieved_at"], "fundamentals.retrieved_at") > cutoff:
            raise ContractError("Financial collection exceeds decision time")
        for company in fundamentals["companies"]:
            if any(
                r["symbol"] != company["symbol"]
                or parse_datetime(r["retrieved_at"], "financial.retrieved_at")
                > parse_datetime(fundamentals["retrieved_at"], "fundamentals.retrieved_at")
                for r in company["facts"]
            ):
                raise ContractError("Financial input ownership or collection time mismatch")
            fundamental_results.append(
                {
                    "symbol": company["symbol"],
                    "analysis": analyze_fundamentals(company["facts"], decision_at),
                }
            )
    from ..ingest.filing_body import analyze_bodies

    return {
        "filing_bodies": analyze_bodies(filing_bodies, fundamentals, decision_at),
        "fundamentals": fundamental_results,
        "version": VERSION,
        "decision_at": decision_at,
        "quote_retrieved_at": bundle["retrieved_at"],
        "latest_session": latest,
        "benchmark": benchmark,
        "metrics": metrics,
        "sector_leaders": leaders,
        "sector_positive": sector_positive if sector_complete else None,
        "sector_count": len(SECTORS),
        "sec": sec,
        "checks": checks,
        "status": "DEGRADED_RESEARCH_ONLY",
        "formal_market_eligible": False,
        "calendar_evidence": ce,
    }


def pct(v):
    return "UNKNOWN" if v is None else f"{v:.2%}"


def render(packet):
    p = packet
    lines = [
        "# 美股当日研究报告（数据能力受限）",
        "",
        f"生成/研究截止时间：{p['decision_at']}。行情取得时间：{p['quote_retrieved_at']}；最近完整交易日：{p['latest_session']}。",
        f"SEC 采集状态：{p['sec']['status']}；取得时间：{p['sec']['retrieved_at']}；输入 as_of：{p['sec']['as_of']}。",
        "",
        "## 当前判断与使用范围",
        "",
        "事实：已接入结构化历史日线。计算：下列回报、量比和行业排名均从冻结输入计算。推断：行业相对强弱仅为进一步研究线索，不能据此确认情绪周期、基本面催化或个股龙头。",
        "本次没有通过正式证据门槛的最终候选。待验行情的发布时间、有效时点和原始 as_of 尚未核实；数据接入成功不等于严格历史可得性成立。",
        "",
        "## 能力检查与缺口处理",
        "",
        "| 能力 | 本次状态 | 解决路径 |",
        "|---|---|---|",
        "| 日线与行业比较 | 已计算，见完整性检查 | 保留同源日期、复权版本和缺失窗口；按交易日核对 |",
        f"| 公司披露更新 | {p['sec']['status']} | 配置 SEC_USER_AGENT 后采集新快照；旧输入不改称今日更新 |",
        "| 正式市场证据 | 未通过 | 核验供应商各时间字段；无法提供时维持待验，不能编造发布时间 |",
        "| 财报正文与经营验证 | 缺失 | 增加带段落引用的 SEC/IR 正文提取、人工核验和命题证伪 |",
        "| 事前收入/EPS一致预期 | 缺失 | 接入获许可、带预测期间和采集版本的预期源；从现在开始留存，不能回填历史预期 |",
        "| TTM、时点股数、债务与估值 | 不完整 | 增加同口径 FY+本期YTD-上年YTD 桥接、股数和债务组件校验后再计算 |",
        "| 全市场广度、VIX、盘中确认 | 缺失 | 行业ETF只能做代理；需另补成分股集合、波动率与带延迟标记的盘中源 |",
        "| 催化日历与复盘结果 | 未闭环 | 核公司IR事件时间；追加保存计划、后续证据和结果，待足够样本后评估规则 |",
        "",
        "## 市场与行业观察（计算结果）",
        "",
        f"行业ETF上涨数量：{p['sector_positive'] if p['sector_positive'] is not None else 'UNKNOWN'} / {p['sector_count']}。这不是上涨股票占比。",
        "行业ETF按20日回报减SPY回报排序；回报为当前复权价格回报，不含股息，排名不代表预测收益。",
        "",
        "| 标的/行业 | 1日 | 5日 | 20日 | 60日 | 20日相对SPY | 量/此前20日均量 | 缺失交易日数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ordered = sorted(
        p["metrics"].values(),
        key=lambda m: (
            m["symbol"] in SECTORS,
            -(m["relative_20d"] if m["relative_20d"] is not None else -999),
            m["symbol"],
        ),
    )
    for m in ordered:
        vals = [pct(m["returns"][str(k)]) for k in (1, 5, 20, 60)]
        vals += [
            pct(m["relative_20d"]),
            "UNKNOWN" if m["volume_ratio"] is None else f"{m['volume_ratio']:.2f}",
            str(len(m["missing_sessions"])),
        ]
        lines.append(
            "| "
            + m["symbol"]
            + " "
            + SECTORS.get(m["symbol"], "")
            + " | "
            + " | ".join(vals)
            + " |"
        )
    leaders = (
        ", ".join(m["symbol"] + " " + SECTORS[m["symbol"]] for m in p["sector_leaders"])
        or "UNKNOWN"
    )
    lines += [
        "",
        "## 下一交易日重点观察与计划",
        "",
        f"优先复核的行业相对强势样本：{leaders}。先检查是否仅由少数权重股贡献，再查行业与公司一级披露。尚未采集成分股，不能把这些ETF叫作个股龙头。",
        "检查基线：比较 MSFT 相对 QQQ、QQQ 相对 SPY、RSP 相对 SPY、IWM 相对 SPY 的同一时段表现；后两项只作风格与权重分化线索。盘中数据未接入，开盘后检查当前是人工任务。",
        "",
        "| 检查点 | 纽约时间 | 北京时间 |",
        "|---|---|---|",
    ]
    for c in p["checks"]:
        lines.append(f"| {c['title']} | {c['new_york']} | {c['shanghai']} |")
    if not p["checks"]:
        lines.append("| UNKNOWN：日历无可用未来检查点 | UNKNOWN | UNKNOWN |")
    lines += [
        "",
        "1. 盘前：检查本次截止后 SEC/IR 新披露与已确认催化，保存原始链接和可得时间；未找到证据则记未知。",
        "2. 开盘后：人工记录上述样本同一时点的价格、延迟标记和相对涨跌；不将盘前、盘中和收盘价格混用。没有盘中输入就不填判断。",
        "3. 收盘后：待新日线到齐，重算行业排名、20日相对回报和量比；对照本次基线保存变化。",
        "证伪标准：某行业20日相对SPY回报降至零或以下，则该窗口的相对强势判断失效；缺少任何所需交易日则该指标直接UNKNOWN。量比只能对完整日比较，不能拿半日成交量对全天均量。",
        "反例：行业ETF走强可能只反映权重集中、风格切换或价格反弹，不能证明需求、收入或利润改善。以上阈值是明确的观察定义，尚未经历史效果评估。",
        "",
        "## 排除对象与公司证据",
        "",
    ]
    for c in p["sec"]["candidates"]:
        lines += [
            f"- {c['symbol']}：{c['state']}；阶段：{c['stage']}（研究种子标签，未重新验证）；{c['reason']}",
            f"  下一催化：{c['next_catalyst']}。反证检查：{'；'.join(c['invalidation_conditions'])}",
            f"  待人工复核：{'；'.join(c['manual_review_items'])}",
        ]
        lines += ["", "| 财务指标 | 结果 | 公式 | 输入事实 |", "|---|---:|---|---|"]
        for calculation in c["financials"]["calculations"]:
            label = {
                "revenue_growth": "年度收入增长率",
                "operating_margin": "年度营业利润率",
                "free_cash_flow_margin": "年度自由现金流率",
                "market_cap": "市值",
                "net_cash": "净现金",
                "enterprise_value": "企业价值",
                "ev_to_revenue": "企业价值/收入",
                "free_cash_flow_yield": "自由现金流收益率",
            }[calculation["metric"]]
            value = "UNKNOWN"
            if calculation["status"] == "OK":
                value = (
                    pct(calculation["value"])
                    if calculation["unit"] == "ratio"
                    else str(calculation["value"])
                )
            lines.append(
                f"| {label} | {value} | {calculation['formula']} | {', '.join(calculation['input_fact_ids'])} |"
            )
        lines.append(
            "财务计算基于最新可提取的报表期间，不等于实时经营数据；单位和期间、原始事实与来源关联见冻结计算包。缺时点股数、总债务与严格价格证据时不计算估值。"
        )
    if not p["sec"]["candidates"]:
        lines.append("- 公司证据缺失，无法形成正式候选。")
    lines += ["- ETF样本仅为市场比较对象，不作为公司经营命题候选。"]
    lines += [
        "- SEC 当前可得性仍为 P2：available_at 使用受理时间后3分钟估计，实际公开可得时间未被独立证明；不能用于临近公告时刻的严格回放。"
    ]
    for e in p["sec"]["evidence"]:
        lines.append(
            f"- [{e['title']}]({e['source_url']})；等级 {e['source_level']}；published_at={e['published_at']}；effective_at={e['effective_at']}；available_at={e['available_at']}；retrieved_at={e['retrieved_at']}；as_of={e['as_of']}。"
        )
    if p["fundamentals"]:
        lines += [
            "",
            "## TTM 桥接与时点财务组件",
            "",
            "计算只使用同一概念、同一币种且期间匹配的 FY/YTD 输入。若最新披露刚好为完整财年，则 FY 等于截至该期末的 TTM；这不代表已经观测到下一季度经营数据。",
            "",
        ]
        for company in p["fundamentals"]:
            lines += [
                f"### {company['symbol']}",
                "",
                "| 指标 | 结果 | 期间终点 | 计算路径/缺口 |",
                "|---|---:|---|---|",
            ]
            for name, item in company["analysis"]["calculations"].items():
                value = "UNKNOWN"
                if item["status"] == "OK":
                    value = (
                        pct(item["value"])
                        if item.get("unit") == "ratio"
                        else f"{item['value']:,.2f} USD"
                    )
                lines.append(
                    f"| {name} | {value} | {item.get('period_end', 'UNKNOWN')} | {item.get('formula', item.get('reason'))} |"
                )
            lines += [
                "",
                "时点股数与债务组件（不将期末普通股数称为稀释股数，不将可能重叠的债务项目直接相加）：",
                "",
            ]
            for name, fact in company["analysis"]["instant_components"].items():
                lines.append(
                    f"- {name}："
                    + (
                        f"{fact['value']:,.0f} {fact['unit']}，截至 {fact['period_end']}；[SEC 来源]({fact['source_url']})"
                        if fact
                        else "UNKNOWN"
                    )
                )
            lines += [
                "总债务与当前估值仍为 UNKNOWN，待核短债、商业票据重叠及价格和股数口径。全部输入事实 ID、来源及六项时间字段见冻结 inputs.json。"
            ]
    lines += [
        "",
        "## 财报正文交叉核验与剩余缺口",
        "",
        "正文只提取合并口径的白名单 XBRL 数字；不将分部数据与合并数据混用。数字一致不等于管理层叙述、业务前景或催化日期已核实。",
    ]
    for body in p["filing_bodies"]:
        counts = {
            k: sum(c["status"] == k for c in body["checks"])
            for k in ("MATCH", "CONFLICT", "BODY_ONLY")
        }
        lines += [
            f"- {body['symbol']}：[SEC 原文]({body['source_url']})，正文采集 {body['retrieved_at']}；状态 {body['status']}。",
            f"- 与同一申报文件的 Company Facts 核验：一致 {counts['MATCH']} 项，冲突 {counts['CONFLICT']} 项，仅正文提取 {counts['BODY_ONLY']} 项。历史比较期也包含在计数内；逐项期间、事实 ID 与六项时间见 inputs.json。",
        ]
        value = body["long_term_debt_including_current"]
        lines += [
            f"- 长期债务（含一年内到期部分）：{f'{value:,.0f} USD' if value is not None else 'UNKNOWN'}，截至 {body['period_end']}；公式=LongTermDebtCurrent+LongTermDebtNoncurrent；与正文 LongTermDebt 标签核对={body['debt_reconciliation']}。",
            "- 总债务、企业价值与估值倍数：UNKNOWN。长期债务小计不包含未经核实的短债、商业票据和租赁调整；叙述语义仍待人工复核。",
        ]
    if not p["filing_bodies"]:
        lines.append("- 财报正文：UNKNOWN，本次未获得可验证正文输入。")
    lines += [
        "- 一致预期：UNKNOWN；用户当前没有额外财务数据授权。评级/目标价不替代收入和 EPS 预期，也不输出预期差。",
        "- 全市场广度与分时观察：UNKNOWN；现有数据只支持样本 ETF 和日线研究。富途新增只读接口仍待可用响应验证。",
        "- 复盘效果：UNKNOWN；计划已逐次留档，尚无到期人工结果，不能统计命中率。",
        "- 下一步观察：先核实最新 SEC 披露中的业务与催化；下一交易日按报告检查时点观察样本行业与基准相对变化。盘中接口不可用时，分时确认项保持 UNKNOWN，不升级研究状态。",
    ]
    ce = p["calendar_evidence"]
    lines += [
        "",
        "## 来源、口径与审计",
        "",
        f"- [交易所日历]({ce['source_url']})；published_at={ce.get('published_at', 'UNKNOWN')}；effective_at={ce.get('effective_at', 'UNKNOWN')}；available_at={ce['available_at']}；retrieved_at={ce.get('retrieved_at', 'UNKNOWN')}；as_of={ce.get('as_of', 'UNKNOWN')}。原始时间不全，因此不据此伪造严格历史证据。",
        "- [行业ETF名称与范围](https://www.sec.gov/Archives/edgar/data/1064641/000119312526027312/d15107d485bpos.htm)仅用于定义研究样本，不用基金名称证明行业结论。",
        "- 回报=末日复权收盘/窗口起日复权收盘-1；相对回报=标的回报-基准回报；量比=末日量/此前20个完整交易日均量。全部窗口按显式日历检验。",
    ]
    for m in p["metrics"].values():
        lines.append(
            f"- {m['symbol']}：[行情来源]({m['source_url']})；等级 structured_market（待验）；published_at/effective_at/as_of=UNKNOWN；逐条 available_at、retrieved_at 见冻结行情输入。"
        )
    lines += [
        "",
        "行情权益、成交量覆盖与拆股/分红口径待人工复核；不宣称实时或全市场覆盖。报告、计算包和冻结输入的校验值写入manifest，可离线复算。",
        "",
        DISCLAIMER,
        "",
    ]
    return "\n".join(lines)


def write_review(
    bundle, calendar, sec_snapshot, decision_at, output_dir, fundamentals=None, filing_bodies=None
):
    packet = analyze(bundle, calendar, sec_snapshot, decision_at, fundamentals, filing_bodies)
    report = render(packet)
    files = {
        "inputs.json": json.dumps(
            {
                "bundle": bundle,
                "fundamentals": fundamentals,
                "filing_bodies": filing_bodies,
                "calendar": calendar,
                "sec_snapshot": sec_snapshot,
                "decision_at": decision_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
        "review.json": json.dumps(packet, ensure_ascii=False, sort_keys=True, allow_nan=False),
        "report.md": report,
    }
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, text in files.items():
        with (directory / name).open("x", encoding="utf-8") as f:
            f.write(text)
        hashes[name] = hashlib.sha256(text.encode()).hexdigest()
    manifest = {
        "version": VERSION,
        "status": packet["status"],
        "decision_at": decision_at,
        "files": hashes,
    }
    with (directory / "manifest.json").open("x") as f:
        json.dump(manifest, f, sort_keys=True)
    verify_review(directory)
    return manifest


def verify_review(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("version") == "us-daily-review-3":
        from .legacy_daily_v3 import verify_review as verify_v3

        return verify_v3(directory)
    if manifest.get("version") == "us-daily-review-2":
        from .legacy_daily_v2 import verify_review as verify_v2

        return verify_v2(directory)
    if manifest.get("version") != VERSION or set(manifest["files"]) != {
        "inputs.json",
        "review.json",
        "report.md",
    }:
        raise ContractError("Unsupported daily review manifest")
    for name, checksum in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ContractError("Daily review integrity mismatch")
    inputs = json.loads((directory / "inputs.json").read_text())
    packet = analyze(**inputs)
    if (
        manifest.get("status") != packet["status"]
        or manifest.get("decision_at") != packet["decision_at"]
    ):
        raise ContractError("Daily review manifest metadata mismatch")
    if (
        packet != json.loads((directory / "review.json").read_text())
        or render(packet) != (directory / "report.md").read_text()
    ):
        raise ContractError("Daily review does not match frozen inputs")
    return {"status": "VERIFIED", "version": VERSION, "decision_at": packet["decision_at"]}
