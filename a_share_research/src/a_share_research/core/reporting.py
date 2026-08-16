from __future__ import annotations

from typing import Any

from .contracts import DIMENSION_LABELS, DISCLAIMER

ASSESSMENT_LABELS = {
    "strong": "强",
    "medium": "中",
    "weak": "弱",
    "unknown": "未知",
}
ROLE_LABELS = {
    "core": "核心",
    "midcap": "中军",
    "elastic": "弹性",
    "follower": "跟风",
}
PROHIBITED_REPORT_PHRASES = ("建议买入", "建议卖出", "目标价", "止损价", "保证上涨")


def render_report(packet: dict[str, Any]) -> str:
    data_status = packet["data_status"]
    lines = [
        "# A股题材研究报告",
        "",
        f"- 运行ID：`{packet['run_id']}`",
        f"- 生成时间：{packet['generated_at']}",
        f"- 研究时点：{packet['decision_at']}",
        f"- 快照：`{packet['snapshot_id']}`（{packet['data_mode']} / {packet['pit_quality']}）",
        f"- 最新行情适用时间：{data_status['latest_market_as_of'] or '未知'}",
        f"- 最新官方证据可用时间：{data_status['latest_official_available_at'] or '未知'}",
        "",
    ]
    if packet["data_mode"] == "fixture":
        lines.extend(
            [
                "> **安装测试数据**：本报告只使用合成 fixture，不含任何真实股票推荐或真实市场事实。",
                "",
            ]
        )

    lines.extend(
        [
            "## 数据状态",
            "",
            f"- 使用证据数量：{data_status['used_evidence_count']}",
            f"- 输入快照时间：{data_status['snapshot_as_of']}",
            f"- 抓取时间：{data_status['snapshot_retrieved_at']}",
            f"- 缺失或过期数据：{_join_or_unknown(packet['data_gaps'])}",
            "",
        ]
    )
    if packet["warnings"]:
        lines.append("### 警告")
        lines.append("")
        lines.extend(f"- {warning}" for warning in packet["warnings"])
        lines.append("")

    context = packet["market_context"]
    lines.extend(
        [
            "## 市场与情绪背景",
            "",
            f"- 指数环境：{context['regime']}",
            f"- 板块宽度：{context['breadth']}",
            f"- 流动性：{context['liquidity']}",
            f"- 计算说明：{context['calculation_note']}",
            "",
            "## 题材总表",
            "",
            "| 题材 | 类型 | 大 | 新 | 多 | 久 | 准 | 周期阶段 | 观察 | 继续研究 | 排除 |",
            "|---|---|---|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for theme in packet["themes"]:
        assessments = [
            ASSESSMENT_LABELS[theme["dimensions"][key]["assessment"]] for key in DIMENSION_LABELS
        ]
        counts = theme["candidate_counts"]
        lines.append(
            f"| {theme['name']} | {theme['event_type']} | "
            + " | ".join(assessments)
            + f" | {theme['stage']} | {counts['observe']} | {counts['continue_research']} | {counts['exclude']} |"
        )
    if not packet["themes"]:
        lines.append(
            "| 未匹配到题材 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 0 | 0 | 0 |"
        )
    lines.append("")

    lines.extend(["## 研究优先候选", ""])
    if not packet["focus"]:
        lines.extend(["没有候选满足继续研究或观察门槛。", ""])
    for index, candidate in enumerate(packet["focus"], start=1):
        lines.extend(_candidate_card(index, candidate))

    lines.extend(["## 被排除的题材与标的", ""])
    if not packet["excluded"]:
        lines.extend(["本次运行没有被排除的候选。", ""])
    for candidate in packet["excluded"]:
        lines.append(
            f"- **{candidate['name']}（{candidate['symbol']}）**："
            + "；".join(candidate["reasons"])
        )
    lines.append("")

    lines.extend(
        [
            "## 明日需要补充的数据",
            "",
        ]
    )
    review_items = list(
        dict.fromkeys(
            item
            for candidate in packet["focus"]
            for item in [*candidate["manual_review_items"], *candidate["data_gaps"]]
        )
    )
    if review_items:
        lines.extend(f"- {item}" for item in review_items)
    else:
        lines.append("- 未知：需要人工复核输入快照与正式来源。")
    lines.extend(["", DISCLAIMER, ""])
    report = "\n".join(lines)
    _assert_report_policy(report)
    return report


def _candidate_card(index: int, candidate: dict[str, Any]) -> list[str]:
    lines = [
        f"### {index}. {candidate['name']}（{candidate['symbol']}）",
        "",
        f"- 处理结果：**{candidate['decision_label']}**",
        f"- 角色：{ROLE_LABELS[candidate['role']]}",
        f"- 周期阶段：{candidate['stage']}",
        f"- 风险标志：{_join_or_unknown(candidate['risk_flags'])}",
        f"- 下一催化或观察日期：{candidate['next_catalyst_at']}",
        "",
        "#### 事实",
        "",
    ]
    for evidence in candidate["evidence"]:
        lines.append(
            f"- [{evidence['title']}]({evidence['source_url']}) — "
            f"等级 `{evidence['source_level']}`；可用时间 {evidence['available_at']}；"
            f"适用时间 {evidence['as_of']}；{evidence['summary']}"
        )
    if not candidate["evidence"]:
        lines.append("- 未知：没有研究时点前可用的证据。")

    passed = [key for key, value in candidate["gates"].items() if value]
    failed = [key for key, value in candidate["gates"].items() if not value]
    lines.extend(
        [
            "",
            "#### 计算结果",
            "",
            f"- 已通过门槛：{_join_or_unknown(passed)}",
            f"- 未通过门槛：{_join_or_unknown(failed)}",
            "",
            "#### 模型/规则推断",
            "",
            f"- 受益传导链：{' -> '.join(candidate['transmission_chain'])}",
            f"- 当前研究假设：{candidate['thesis']}",
            f"- 反方解释：{candidate['counter_thesis']}",
            f"- 证伪条件：{_join_or_unknown(candidate['invalidation_conditions'])}",
            f"- 数据缺口：{_join_or_unknown(candidate['data_gaps'])}",
            f"- 待人工复核：{_join_or_unknown(candidate['manual_review_items'])}",
            f"- 处理理由：{'；'.join(candidate['reasons'])}",
            "",
        ]
    )
    return lines


def _join_or_unknown(values: list[str]) -> str:
    return "；".join(values) if values else "未知"


def _assert_report_policy(report: str) -> None:
    found = [phrase for phrase in PROHIBITED_REPORT_PHRASES if phrase in report]
    if found:
        raise ValueError("report contains prohibited trading language: " + ", ".join(found))
