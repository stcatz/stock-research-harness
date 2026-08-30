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

    lines.extend(["## 可执行研究队列", ""])
    if packet["research_queue"]:
        for item in packet["research_queue"]:
            lines.append(
                f"- **{item['name']}（{item['symbol']}）**：优先级 "
                f"`{item['research_priority']}/100`，缺口 {len(item['gaps'])} 项，"
                "独立反方复核待完成。"
            )
            for gap in item["gaps"]:
                lines.append(
                    f"  - `{gap['gap_id']}` [{gap['state']}] 责任：`{gap['resolver']}`；"
                    f"转换条件：{gap['transition_condition']}；截止：{gap['expires_at']}"
                )
    else:
        lines.append("- 本次没有待处理研究缺口。")
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
        f"- 研究优先级：`{candidate['research_priority']}/100`",
        f"- 证据状态：`{candidate['evidence_state']}`",
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
            f"生效时间 {evidence['effective_at']}；适用时间 {evidence['as_of']}；"
            f"{evidence['summary']}"
        )
        for fact in evidence.get("facts", []):
            value = fact["value"] if fact["status"] == "observed" else "UNKNOWN"
            lines.append(
                f"  - `{fact['metric']}` = {value} {fact['unit']}；"
                f"状态 `{fact['status']}`；适用时间 {fact['as_of']}"
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
            "- 独立证伪：待联网 Harness 仅基于事实层完成，且不向反方展示多方结论。",
            f"- 处理理由：{'；'.join(candidate['reasons'])}",
            "",
            "#### 估值画像（确定性计算）",
            "",
            "| 指标 | 状态 | 数值 | 快照候选集分位 | 样本数 | 行业分位 | 自身历史分位 |",
            "|---|---|---:|---:|---:|---|---|",
        ]
    )
    for metric in candidate["valuation_profile"]["metrics"]:
        lines.append(
            f"| {metric['metric']} | {metric['status']} | "
            f"{metric['value'] if metric['value'] is not None else 'UNKNOWN'} | "
            f"{metric['snapshot_candidate_percentile'] if metric['snapshot_candidate_percentile'] is not None else 'UNKNOWN'} | "
            f"{metric['sample_size']} | UNKNOWN | UNKNOWN |"
        )
    lines.extend(
        [
            "",
            "> 分位范围仅为本次快照候选集，不是行业可比公司；行业与自身历史分位缺数时保持 UNKNOWN。",
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
