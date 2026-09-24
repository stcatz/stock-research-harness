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
        f"- 候选关联的最新官方证据可用时间：{data_status['latest_official_available_at'] or '未知'}",
        f"- 全部可用输入最新可得 / 抓取时间：{data_status.get('latest_input_available_at') or 'UNKNOWN'} / {data_status.get('latest_input_retrieved_at') or 'UNKNOWN'}",
        f"- 有日期的最新日线截至：{data_status.get('latest_daily_market_as_of') or 'UNKNOWN'}",
        "",
    ]
    if packet["data_mode"] == "fixture":
        lines.extend(
            [
                "> **安装测试数据**：本报告只使用合成 fixture，不含任何真实股票推荐或真实市场事实。",
                "",
            ]
        )

    if packet.get('observation_plan'):
        lines.extend(render_observation_plan(packet['observation_plan']))

    if packet.get('market_diagnostics'):
        lines.extend(_market_brief(packet['market_diagnostics']))

    lines.extend(
        [
            "## 数据状态",
            "",
            f"- 行情可用状态：{data_status.get('readiness', 'UNKNOWN')}（近期不等于已核最新交易日）",
            f"- 使用证据数量：{data_status['used_evidence_count']}",
            f"- 输入快照时间：{data_status['snapshot_as_of']}",
            f"- 抓取时间：{data_status['snapshot_retrieved_at']}",
            f"- 最新日线截至：{data_status.get('latest_daily_market_as_of') or 'UNKNOWN'}",
            f"- 全部可用输入的最新可得时间：{data_status.get('latest_input_available_at') or 'UNKNOWN'}",
            f"- 全部可用输入的最新抓取时间：{data_status.get('latest_input_retrieved_at') or 'UNKNOWN'}",
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
    feedback = packet.get("research_feedback")
    if feedback:
        breadth = feedback["breadth"]
        lines.extend([
            "## 日线累计反馈的冻结样本", "",
            "以下采用历史参考价和状态口径，与前面的当前横截面观测分别计算；仅覆盖研究成员。",
            f"- 观察日：{feedback['anchor_session']}；日历状态：{feedback['calendar_status']}",
            f"- 样本：{breadth['total']}；上涨 {breadth['advancing']}，下跌 {breadth['declining']}，"
            f"平盘 {breadth['unchanged']}，未知 {breadth['unknown']}",
            f"- 固定强势组：{_join_or_unknown(feedback['cohorts']['strong'])}",
            f"- 固定弱势组：{_join_or_unknown(feedback['cohorts']['weak'])}",
            "- 阶段来自研究输入，尚未建立经验证的自动情绪阶段模型。", "",
            "| 题材 | 成员覆盖 | 相对表现中位数% | 前期强势组反馈% | 前期弱势组反馈% | 成交集中度 |",
            "|---|---|---:|---:|---:|---:|",
        ])
        for theme in feedback["themes"]:
            diffusion = theme["diffusion"]
            cells = [theme["theme_id"], f"{diffusion['observed']}/{diffusion['total']}",
                     diffusion["median_excess_pct"],theme["strong_feedback"]["median_excess_pct"],
                     theme["weak_feedback"]["median_excess_pct"], theme["turnover_hhi"]]
            lines.append("| " + " | ".join(str(x) if x is not None else "UNKNOWN" for x in cells) + " |")
        experiment = feedback["experiment"]
        lines.extend(["", f"- 单条件排序对照：{experiment['status']}；效果 UNKNOWN；当前正式候选排序保持 R0。",
                      "- R0：" + _join_or_unknown(experiment["R0"][:experiment["top_n"]]),
                      "- R1（影子）：" + _join_or_unknown(experiment["R1"][:experiment["top_n"]]),
                      "- 预期差：未登记事前预期时保持 UNKNOWN；事实证伪与价格反馈分别复核。", ""])
    lines.extend(
        [
            "## 市场与情绪背景",
            "",
            f"- 指数环境：{context['regime']}",
            f"- 板块宽度：{context['breadth']}",
            f"- 流动性：{context['liquidity']}",
            f"- 计算说明：{context['calculation_note']}",
            "",
        ]
    )

    discoveries = packet.get("market_discoveries", [])
    lines.extend(["## 市场新题材线索", ""])
    if discoveries:
        lines.extend(
            [
                "> 以下线索由供应商涨停原因的重复标签自动聚类，只能标记为 `continue_research`；供应商标签未经官方核验，不能替代公司公告或监管事实。",
                "",
                "| 标签 | 涨停成员数 | 成员 | 研究状态 | 证据分类 |",
                "|---|---:|---|---|---|",
            ]
        )
        for discovery in discoveries:
            members = "、".join(
                f"{item['name'] or item['thscode']}（{item['thscode'][:6]}）"
                for item in discovery["members"]
            )
            lines.append(
                f"| {discovery['label']} | {discovery['member_count']} | {members} | "
                f"`{discovery['status']}` | `{discovery['classification']}` |"
            )
    else:
        lines.append("UNKNOWN：线索证据超出研究时点，已隐藏。" if packet.get("market_discovery_input_count")
                     else "没有可用的重复供应商标签线索；不等于全市场没有新题材。")
    lines.extend(
        [
            "",
            "## 题材总表",
            "",
            "| 题材 | 类型 | 大 | 新 | 多 | 久 | 准 | 原种子阶段（待复核） | 观察 | 继续研究 | 排除 |",
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
            f"- 处理理由：{'；'.join(candidate['reasons'])}",
            "",
        ]
    )
    return lines


def _join_or_unknown(values: list[str]) -> str:
    return "；".join(values) if values else "未知"


def render_observation_plan(plan: dict) -> list[str]:
    """The same frozen plan is available in the packet and the front of every report."""
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')

    lines = ['## 下一交易日计划卡', '',
             f"- 观察日：**{plan['next_session'] or 'UNKNOWN（须补合格日历）'}**；上海时间。计划 ID：`{plan['plan_id']}`。",
             '- 执行方式：人工观察与记录；未启动自动监控，尚未接入盘中数据。未来结果均待观察。',
             f"- {plan['selection_rule']}。", f"- {plan['comparison_contract']}",
             f"- {plan['calendar_scope_note']}", '',
             '| 检查点 | 做什么 |', '|---|---|']
    for point in plan['checkpoints']:
        stamp = point['scheduled_at'] or f"日期未知 / {point['local_time']}"
        lines.append(f"| {stamp}（{point['label']}） | {point['task']} |")
    lines.extend(['', '### 本轮重点与优先顺序', '',
                  '| 卡片 | 具体问题 |', '|---|---|'])
    for card in plan['cards']:
        lines.append(f"| {card['card_id']} · {cell(card['title'])} | {cell(card['question'])} |")
    labels = [('baseline', '已知基线'), ('before_open', '盘前先完成'), ('measure', '现场记录'),
              ('support_condition', '支持条件'), ('counter_condition', '反方条件'),
              ('on_support', '满足后做什么'), ('on_counter', '失败后做什么'),
              ('on_unknown', '数据不足时'), ('invalidation', '撤回条件')]
    for card in plan['cards']:
        lines.extend(['', f"### {card['card_id']} · {card['title']}", ''])
        if card['kind'] == 'market_feedback':
            for group in card['members']:
                label = '冻结强势组' if group['group'] == 'strong' else '冻结弱势组'
                lines.append(f"- {label}：{_join_or_unknown(group['codes'])}。名单来自现价观测，不冒充昨日收盘榜。")
        else:
            members = []
            for member in card['members']:
                text = f"{member['name']}（{member['code']}）"
                if 'baseline_change_pct' in member:
                    text += f"：{member['baseline_change_pct'] or 'UNKNOWN'}% / {member['formal_decision']}"
                members.append(text)
            lines.append('- 固定对照成员：' + '；'.join(members))
        if card.get('formal_decision'):
            lines.extend([f"- 原研究状态：`{card['formal_decision']}`；计划卡不会自动改变它。",
                          f"- 原催化：{card['original_catalyst_at']}；风险：{_join_or_unknown(card['risk_flags'])}。",
                          f"- 原研究反例：{card['counter_thesis']}"])
        lines.extend(['', '| 项目 | 具体安排 |', '|---|---|'])
        lines.extend(f"| {label} | {cell(card[key])} |" for key, label in labels)
        lines.extend(['', '- 数据缺口：' + _join_or_unknown(card['data_gaps']),
                      '- 实际结果：**待人工观察**；支持 / 反对 / UNKNOWN 尚未填写。'])
        for source in card['sources']:
            lines.append(f"- 依据：[{source['evidence_id']}]({source['source_url']})（{source['source_level']}；"
                         f"适用 {source['as_of']}；可得 {source['available_at']}）。其余时间字段保存在同一 packet 的计划卡来源中。")
    lines.extend(['', '### 每个检查点如何留记录', '',
                  '逐卡填写：卡片编号、实际观察时间、行情所属日期、来源链接和可得时间、有效/全部成员、指标值、支持/反对/UNKNOWN、后续研究动作及撤回理由。',
                  '先完成09:35记录，再填10:00比较；缺任何必要输入，不把未来结果预填为成功。15:10若收盘数据未到，记录延迟。', '',
                  '### 未列为本轮优先比较对象', '',
                  '| 原候选 | 原状态 | 原因 |', '|---|---|---|'])
    for row in plan['not_prioritized']:
        lines.append(f"| {cell(row['name'])} | {row['decision']} | {row['reason']} |")
    if not plan['not_prioritized']:
        lines.append('| 无 | — | 原候选均已列入比较卡或没有候选 |')
    lines.append('')
    return lines


def _market_brief(diagnostics: dict) -> list[str]:
    market = diagnostics['current_market']
    dist = market['distribution']
    pools = diagnostics['special_pools']
    caps = diagnostics['capabilities']
    active = caps['unexpired_research_window']
    lines = ['## 市场观察与研究任务', '',
             f"研究窗口仍有效：{active['observed']}/{active['total']}；其余须重新核验催化。当前周期阶段 UNKNOWN。",
             '', '| 能力 | 有效/全部 | 状态 | 缺口处理 |', '|---|---|---|---|']
    labels = {'current_candidate_observation':'候选当前价格观察','historical_reference_feedback':'完整历史参考价反馈',
              'unexpired_research_window':'未过期的催化窗口'}
    for key,label in labels.items():
        cap = caps[key]
        lines.append(f"| {label} | {cap['observed']}/{cap['total']} | {cap['status']} | {cap['remedy']} |")
    lines.extend(['', '### 当前全市场分布（计算结果）', '',
                  f"- 本地观测于：{market.get('observed_at','UNKNOWN')}；有效 {dist['observed']}/{dist['total']}，未知 {dist['unknown']}。",
                  '- 当前快照没有逐股日期，不能当成指定交易日收盘；无成交不等于已经核实停牌。',
                  f"- 上涨 {dist['advancing']}，下跌 {dist['declining']}，平盘 {dist['unchanged']}；上涨占涨跌家数比例 {dist['advance_fraction'] or 'UNKNOWN'}。",
                  f"- 供应商涨跌幅中位数 {dist['median_change_pct'] or 'UNKNOWN'}%；10% 分位 {dist['p10_change_pct'] or 'UNKNOWN'}%。",
                  '- 前后状态变化：UNKNOWN（单份横截面不能判定情绪转折）。'])
    for source in market['sources']:
        lines.append(f"- 来源：[{source['evidence_id']}]({source['source_url']})；等级 {source['source_level']}；可得时间 {source['available_at']}。")
    if market.get('cohorts'):
        groups = market['cohorts']
        lines.extend([f"- 冻结强势观测组：{_join_or_unknown(groups['strong'])}",
                      f"- 冻结弱势观测组：{_join_or_unknown(groups['weak'])}",
                      '- 上述是行情观测样本，不自动进入有官方事实门槛的研究候选；不同代码组、ST 和新股规则尚未统一。'])
    lines.extend(['', '### 供应商特色池与梯队', '',
                  f"- 日期：{pools.get('session','UNKNOWN')}；状态 {pools['status']}；涨停 {pools['counts'].get('limit_up','UNKNOWN')}，"
                  f"跌停 {pools['counts'].get('limit_down','UNKNOWN')}，炸板池 {pools['counts'].get('limit_break','UNKNOWN')}。",
                  f"- 炸板池成员占涨停/炸板并集的比例：{pools.get('break_pool_fraction') or 'UNKNOWN'}；不代表盘中炸板频次。",
                  '- 连板数是供应商标签；按代码前缀分组展示，具体涨跌幅制度和盘中路径 UNKNOWN。', '',
                  '| 代码组 | 供应商连板数 | 成员数 |', '|---|---:|---:|'])
    for row in pools['ladder']:
        lines.append(f"| {row['code_prefix_group']} | {row['provider_reported_streak']} | {row['members']} |")
    for source in pools['sources']:
        lines.append(f"- 来源：[{source['evidence_id']}]({source['source_url']})；等级 {source['source_level']}；可得时间 {source['available_at']}。")
    lines.extend(['', '### 固定题材成员的当前表现', '',
                  '| 题材 | 有效/全部 | 涨幅中位数% | 正值比例 | 成交额覆盖 | 成交集中度 |', '|---|---|---:|---:|---|---:|'])
    for theme in diagnostics['themes']:
        d = theme['distribution']
        lines.append(f"| {theme['theme_id']} | {d['observed']}/{d['total']} | {d['median_change_pct'] or 'UNKNOWN'} | "
                     f"{d['positive_fraction'] or 'UNKNOWN'} | {theme['amount_observed']}/{theme['total']} | {theme['turnover_hhi'] or 'UNKNOWN'} |")
    lines.extend(['', '只表示现有研究成员的分布，不等于完整行业扩散；没有前一次可比成员观察时不宣布扩散或收缩。', '',
                  '### 原研究与当前反馈的对照', '',
                  '| 候选 | 研究状态 | 当前供应商涨跌幅% | 事实与价格关系 | 催化窗口 |', '|---|---|---:|---|---|'])
    relation_labels = {'OFFICIAL_EVIDENCE_MISSING':'官方证据不足',
                      'OFFICIAL_FACT_PRICE_DIVERGENCE':'事实存在、价格负反馈；经营证伪待核',
                      'OFFICIAL_FACT_PRICE_POSITIVE_NO_CAUSAL_PROOF':'事实存在、价格正反馈；因果未确认',
                      'OFFICIAL_FACT_MARKET_FEEDBACK_UNRESOLVED':'市场确认尚不明确'}
    for c in diagnostics['candidate_views']:
        lifecycle = '已过期，重新核验' if c['research_lifecycle']=='REVALIDATE_EXPIRED_CATALYST' else '未到期，继续核验'
        lines.append(f"| {c['name']}（{c['code']}） | {c['decision']} | {c['reported_change_pct'] or 'UNKNOWN'} | "
                     f"{relation_labels[c['evidence_market_relation']]} | {lifecycle} |")
    counts = {}
    for task in diagnostics['research_queue']:
        counts.setdefault(task['kind'],[]).append(task)
    lines.extend(['', '### 本轮需要补证的事项', ''])
    for kind,tasks in counts.items():
        lines.append(f"- {tasks[0]['reason']}（{len(tasks)} 个候选）：{tasks[0]['required_evidence']}。")
    lines.extend(['', '判读要求：每个数字引用证据；给出支持与反方解释；事前预期、比较时点和证伪条件缺失时保持 UNKNOWN。',
                  '以上为确定性计算与检查结果；独立语义判读尚未自动执行。', ''])
    return lines


def _assert_report_policy(report: str) -> None:
    found = [phrase for phrase in PROHIBITED_REPORT_PHRASES if phrase in report]
    if found:
        raise ValueError("report contains prohibited trading language: " + ", ".join(found))
