from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import MARKET

FIXTURE_BANNER = (
    "> **FIXTURE / 合成测试数据**：本报告只使用虚构公司与合成快照，不包含真实发行人事实或投资推荐。"
)
DISCLAIMER = "本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。"
PROHIBITED_REPORT_PHRASES = (
    "建议买入",
    "建议卖出",
    "目标价",
    "止损价",
    "自动下单",
    "代客下单",
    "保证上涨",
    "仓位建议",
    "price target",
    "strong buy",
    "strong sell",
)


def render_report(packet: dict[str, Any]) -> str:
    if packet.get("market") != MARKET:
        raise ValueError("report writer only accepts market US")
    if packet.get("writer_mode") != "engine":
        raise ValueError("report writer requires canonical engine output")

    status = packet["data_status"]
    lines = [
        "# 美股投研报告（研究用途）",
        "",
        f"- 运行 ID：`{_cell(packet['run_id'])}`",
        f"- 工作流：`{_cell(packet['workflow'])}`",
        f"- 研究时点：{_cell(packet['decision_at'])}",
        f"- 生成时间：{_cell(packet['generated_at'])}",
        f"- 快照：`{_cell(packet['snapshot_id'])}`",
        "",
    ]
    if packet["data_mode"] == "fixture":
        lines.extend([FIXTURE_BANNER, ""])

    lines.extend(
        [
            "## 数据与 PIT 状态",
            "",
            f"- 数据模式：`{_cell(packet['data_mode'])}`",
            f"- PIT 质量：`{_cell(packet['pit_quality'])}`",
            f"- 处理状态：`{_cell(status['status'])}`",
            f"- 纳入判断的候选数：{_cell(status['decision_count'])}",
            (
                "- 最新可用官方原始证据："
                f"{_cell(status['official_primary_latest_available_at'] or 'UNKNOWN')}"
            ),
            f"- 最新市场数据适用日：{_cell(status['market_latest_as_of'] or 'UNKNOWN')}",
            "- 口径：事实来自冻结快照；计算由确定性函数完成；观点属于规则化研究推断。",
            "",
        ]
    )
    if packet["warnings"]:
        lines.extend(["### 数据警告", ""])
        lines.extend(f"- {_text(warning)}" for warning in packet["warnings"])
        lines.append("")

    context = packet["market_context"]
    lines.extend(
        [
            "## 宏观与市场背景",
            "",
            f"- 市场状态：{_text(context['regime'])}",
            f"- 市场宽度：{_text(context['breadth'])}",
            f"- 利率环境：{_text(context['rates'])}",
            f"- 流动性：{_text(context['liquidity'])}",
            f"- 计算口径说明：{_text(context['calculation_note'])}",
            f"- 证据引用：{_join(context['evidence_refs'])}",
            "",
            "## 主题与催化剂",
            "",
            "| theme_id | theme_name | event_type | stage | next_catalyst_at | observe | continue_research | exclude |",
            "|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    if packet["themes"]:
        for theme in packet["themes"]:
            counts = theme["candidate_counts"]
            lines.append(
                "| "
                + " | ".join(
                    (
                        _cell(theme["theme_id"]),
                        _cell(theme["theme_name"]),
                        _cell(theme["event_type"]),
                        _cell(theme["stage"]),
                        _cell(theme["next_catalyst_at"]),
                        _cell(counts["observe"]),
                        _cell(counts["continue_research"]),
                        _cell(counts["exclude"]),
                    )
                )
                + " |"
            )
    else:
        lines.append("| UNKNOWN | 未匹配主题 | UNKNOWN | UNKNOWN | UNKNOWN | 0 | 0 | 0 |")
    lines.extend(["", "### 主题研究维度（不合成为收益分数）", ""])
    for theme in packet["themes"]:
        lines.append(f"- **{_text(theme['theme_name'])}**")
        for dimension, assessment in theme["dimensions"].items():
            lines.append(
                f"  - `{_cell(dimension)}` = `{_cell(assessment['assessment'])}`："
                f"{_text(assessment['reason'])}（证据：{_join(assessment['evidence_refs'])}）"
            )
    if not packet["themes"]:
        lines.append("- UNKNOWN：请求未匹配任何主题。")

    lines.extend(["", "## 重点研究标的", ""])
    if not packet["focus"]:
        lines.extend(["本次没有进入观察或继续研究清单的候选。", ""])
    for index, candidate in enumerate(packet["focus"], start=1):
        lines.extend(_candidate_section(index, candidate))

    lines.extend(["## 排除的候选", ""])
    if not packet["excluded"]:
        lines.append("本次没有被排除的候选。")
    for candidate in packet["excluded"]:
        lines.extend(
            [
                (
                    f"- **{_text(candidate['name'])}（`{_cell(candidate['symbol'])}`）**："
                    f"{_join(candidate['reasons'])}"
                ),
                f"  - 风险标志：{_join(candidate['risk_flags'])}",
                f"  - 可用证据：{_join(candidate['usable_evidence_refs'])}",
                f"  - 截止时点后证据：{_join(candidate['time_leak_evidence_refs'])}",
            ]
        )

    all_gaps = _unique(
        [
            *packet["data_gaps"],
            *[gap for candidate in packet["all_decisions"] for gap in candidate["data_gaps"]],
        ]
    )
    all_review_items = _unique(
        [item for candidate in packet["all_decisions"] for item in candidate["manual_review_items"]]
    )
    lines.extend(["", "## 数据缺口与人工复核", "", "### 数据缺口", ""])
    lines.extend(f"- {_text(item)}" for item in all_gaps)
    if not all_gaps:
        lines.append("- 无已声明缺口；正式使用前仍须人工核对原始文件。")
    lines.extend(["", "### 人工复核", ""])
    lines.extend(f"- {_text(item)}" for item in all_review_items)
    if not all_review_items:
        lines.append("- 核对发行人原始披露、市场数据授权与研究时点。")

    lines.extend(["", DISCLAIMER, ""])
    report = "\n".join(lines)
    _assert_report_policy(report)
    return report


def _candidate_section(index: int, candidate: Mapping[str, Any]) -> list[str]:
    lines = [
        f"### {index}. {_text(candidate['name'])}（`{_cell(candidate['symbol'])}`）",
        "",
        f"- 研究状态：`{_cell(candidate['decision'])}`（{_text(candidate['decision_label'])}）",
        f"- 主题 / 角色：{_text(candidate['theme_name'])} / `{_cell(candidate['role'])}`",
        f"- 研究命题（推断）：{_text(candidate['thesis'])}",
        f"- 规则门槛：{_gate_summary(candidate['gates'])}",
        f"- 处理理由：{_join(candidate['reasons'])}",
        "",
        "#### 事实（输入）",
        "",
        "| metric | value | unit | period_end | available_at | evidence_ref |",
        "|---|---:|---|---|---|---|",
    ]
    if candidate["facts"]:
        for fact in candidate["facts"]:
            lines.append(
                "| "
                + " | ".join(
                    _cell(fact[key])
                    for key in (
                        "metric",
                        "value",
                        "unit",
                        "period_end",
                        "available_at",
                        "evidence_ref",
                    )
                )
                + " |"
            )
    else:
        lines.append("| UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |")

    lines.extend(["", "##### 事实来源", ""])
    if candidate["evidence"]:
        for evidence in candidate["evidence"]:
            lines.append(
                f"- `{_cell(evidence['evidence_id'])}` {_text(evidence['title'])} — "
                f"<{evidence['source_url']}>；source_level=`{_cell(evidence['source_level'])}`；"
                f"published_at={_cell(evidence['published_at'])}；"
                f"available_at={_cell(evidence['available_at'])}；"
                f"as_of={_cell(evidence['as_of'])}。{_text(evidence['summary'])}"
            )
    else:
        lines.append("- UNKNOWN：研究时点前没有可用证据。")

    lines.extend(
        [
            "",
            "#### 确定性计算（规则输出）",
            "",
            "| metric | status | value | unit | period_end | price_as_of | formula | input_fact_ids | evidence_refs |",
            "|---|---|---:|---|---|---|---|---|---|",
        ]
    )
    for calculation in candidate["calculations"]:
        cutoff_passed = calculation["available_at_cutoff_passed"] is True
        status = calculation["status"] if cutoff_passed else "UNKNOWN"
        value = calculation["value"] if cutoff_passed else None
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(calculation["metric"]),
                    _cell(status),
                    _cell(value if value is not None else "UNKNOWN"),
                    _cell(calculation["unit"]),
                    _cell(calculation["period_end"] or "UNKNOWN"),
                    _cell(calculation["price_as_of"] or "UNKNOWN"),
                    _cell(calculation["formula"]),
                    _join(calculation["input_fact_ids"]),
                    _join(calculation["evidence_refs"]),
                )
            )
            + " |"
        )

    bull = candidate["bull_case"]
    bear = candidate["bear_case"]
    risk = candidate["risk_verdict"]
    lines.extend(
        [
            "",
            "#### 多空与风险推断",
            "",
            f"- 多方假设：{_text(bull['text'])}（证据：{_join(bull['evidence_refs'])}）",
            f"- 反方假设：{_text(bear['text'])}（证据：{_join(bear['evidence_refs'])}）",
            f"- 风险结论：{_text(risk['text'])}（证据：{_join(risk['evidence_refs'])}）",
            f"- 风险标志：{_join(candidate['risk_flags'])}",
            "",
            "#### 催化剂与证伪条件",
            "",
            f"- 传导链：{_join(candidate['transmission_chain'], separator=' → ')}",
            f"- 下一催化时点：{_cell(candidate['next_catalyst_at'])}",
            f"- 证伪条件：{_join(candidate['invalidation_conditions'])}",
            f"- 数据缺口：{_join(candidate['data_gaps'])}",
            f"- 人工复核：{_join(candidate['manual_review_items'])}",
            "",
        ]
    )
    return lines


def _gate_summary(gates: Mapping[str, bool]) -> str:
    return "；".join(
        f"{_cell(name)}={'PASS' if passed else 'FAIL'}" for name, passed in gates.items()
    )


def _join(values: Iterable[Any], *, separator: str = "；") -> str:
    rendered = [_cell(value) for value in values]
    return separator.join(rendered) if rendered else "UNKNOWN"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _cell(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _text(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _assert_report_policy(report: str) -> None:
    normalized = report.casefold()
    found = [phrase for phrase in PROHIBITED_REPORT_PHRASES if phrase.casefold() in normalized]
    if found:
        raise ValueError("report contains prohibited trading language: " + ", ".join(found))
