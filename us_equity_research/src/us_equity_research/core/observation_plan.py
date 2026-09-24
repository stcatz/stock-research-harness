"""Evidence-linked research attention plans, never execution instructions."""

from __future__ import annotations


def pct(value):
    return "UNKNOWN" if value is None else f"{value:.2%}"


def build_plan(metrics, sector_leaders, sector_positive, sector_count, checks, latest_session):
    def ret(symbol, window):
        return metrics.get(symbol, {}).get("returns", {}).get(str(window))

    def diff(a, b, window):
        x, y = ret(a, window), ret(b, window)
        return None if x is None or y is None else x - y

    spy = [ret("US.SPY", n) for n in (1, 5, 20)]
    if any(v is None for v in spy):
        regime, interpretation = "UNKNOWN", "市场窗口不全，先补齐 SPY 数据，不能判断反弹是否延续。"
    elif spy[0] > 0 and spy[1] < 0 and spy[2] < 0:
        regime = "短期反弹，中期修复待确认"
        interpretation = "SPY 当日上涨，但五日和二十日仍下跌。下一次观察的核心是上涨能否持续、是否有更多风格参与，不能由一天上涨认定趋势反转。"
    elif spy[0] > 0 and spy[1] > 0 and spy[2] > 0:
        regime = "多窗口同向上涨"
        interpretation = (
            "SPY 三个窗口同向上涨；继续核查等权、小盘和行业参与，不能据指数表现推断所有个股走强。"
        )
    elif spy[0] <= 0 and spy[1] < 0:
        regime = "短期走弱"
        interpretation = "SPY 当日与五日均未改善；先核查相对强势样本是否也转弱。"
    else:
        regime = "窗口分化"
        interpretation = "SPY 不同窗口方向分化；先观察下一完整收盘，不强行命名情绪周期。"
    cards = []
    cards.append(
        {
            "object": "SPY / QQQ / RSP / IWM",
            "priority": "先看环境",
            "state": "continue_research",
            "question": "指数上涨是否从少数权重扩散到等权和小盘？",
            "baseline": f"SPY 1/5/20日={pct(spy[0])}/{pct(spy[1])}/{pct(spy[2])}；RSP与IWM二十日相对SPY分别={pct(diff('US.RSP', 'US.SPY', 20))}/{pct(diff('US.IWM', 'US.SPY', 20))}。",
            "confirm": "连续两个完整收盘，SPY当日回报>0、RSP与IWM当日回报均超过SPY，且11只行业ETF中至少6只上涨，才将‘参与扩散’列为得到支持。缺任一输入则UNKNOWN。",
            "invalidate": "SPY当日回报≤0，同时RSP与IWM均落后SPY：本轮扩散未获支持；若只满足部分条件，保留‘分化’，不硬选方向。",
            "follow_up": "扩散条件成立：增加行业成分核查优先级；反证成立：先复核指数与原强势行业；混合结果：维持当前研究顺序。",
            "refs": [
                "metrics.US.SPY",
                "metrics.US.QQQ",
                "metrics.US.RSP",
                "metrics.US.IWM",
                "sector_positive",
            ],
            "gap": "等权、小盘仅为风格代理；真实涨跌家数、VIX与盘中情绪UNKNOWN。",
        }
    )
    for rank, m in enumerate(sector_leaders, 1):
        symbol = m["symbol"]
        r5, r20, relative = m["returns"]["5"], m["returns"]["20"], m["relative_20d"]
        sustained = r5 is not None and r20 is not None and r5 > 0 and r20 > 0
        theme = "强势延续" if sustained else "相对抗跌能否转为持续上涨"
        cards.append(
            {
                "object": symbol,
                "priority": f"行业核查第{rank}顺位",
                "state": "continue_research",
                "question": theme + "？",
                "baseline": f"5日={pct(r5)}，20日={pct(r20)}，20日相对SPY={pct(relative)}；单日上涨不是持续性证据。",
                "confirm": "接下来两个完整收盘，该ETF当日回报均>0且均超过SPY；最新五日回报>0、二十日相对SPY>0。满足后优先核查其成分公司及一级催化，不能直接指定龙头。",
                "invalidate": "最新二十日相对SPY≤0，推翻这一窗口的相对强势；若连续两个完整收盘均落后SPY，则降低其‘延续’研究优先级。",
                "follow_up": "条件成立：补授权成分股集合，区分权重贡献，再找具有正式披露的公司；条件未成立：只保留ETF比较，不展开龙头结论。",
                "refs": [f"metrics.{symbol}", "metrics.US.SPY"],
                "gap": "成分股贡献、具体经营催化、下一催化日期UNKNOWN；ETF不是个股候选。",
            }
        )
    if "US.MSFT" in metrics:
        relative5 = diff("US.MSFT", "US.QQQ", 5)
        vr = metrics["US.MSFT"].get("volume_ratio")
        cards.append(
            {
                "object": "MSFT / QQQ",
                "priority": "公司待核查项",
                "state": "exclude",
                "question": "微软是否开始跟上科技指数，且公司证据能解释这种变化？",
                "baseline": f"MSFT五日={pct(ret('US.MSFT', 5))}，QQQ五日={pct(ret('US.QQQ', 5))}，差值={pct(relative5)}；MSFT完整日量比={'UNKNOWN' if vr is None else f'{vr:.2f}'}。",
                "confirm": "连续两个完整收盘MSFT当日回报超过QQQ，并且最新五日相对QQQ>0；另行核查最新SEC/IR披露是否支持收入与现金流命题。价格条件与经营证据必须分别记录。",
                "invalidate": "五日相对QQQ继续≤0：‘跟上科技’未获支持；新官方披露削弱收入/现金流命题：记录具体事实并重审命题，不能用股价上涨覆盖反证。",
                "follow_up": "优先读最新8-K涉及的事件，并核查10-K资本开支、现金流与债务口径；满足价格条件仅增加人工复核优先级，正式证据未齐仍维持exclude。",
                "refs": [
                    "metrics.US.MSFT",
                    "metrics.US.QQQ",
                    "sec.evidence",
                    "fundamentals",
                    "filing_bodies",
                ],
                "gap": "当前只有一个公司种子，无法识别美股个股龙头；一致预期、确认催化日期与正式行情时间语义UNKNOWN。",
            }
        )
    return {
        "version": "us-observation-plan-1",
        "baseline_session": latest_session,
        "regime": regime,
        "interpretation": interpretation,
        "sector_participation": f"{sector_positive if sector_positive is not None else 'UNKNOWN'}/{sector_count}",
        "cards": cards,
        "checks": checks,
        "rule_basis": "两次收盘及多数行业条件是预先写明的研究规则，尚未回测；不代表胜率或收益承诺。",
        "expiry": "下一完整交易日收盘后更新基线；两个收盘的连续条件仅从本计划之后计数，单次盘中信号不能代替。",
        "intraday": "人工同源核验；未采集，当前结果UNKNOWN",
    }


def render_plan(plan):
    lines = [
        "",
        "## 先看这一页：下一交易日观察计划",
        "",
        f"**当前研究判断：{plan['regime']}。** {plan['interpretation']}",
        f"基线交易日：{plan['baseline_session']}；行业样本上涨数：{plan['sector_participation']}。以下为模型推断和待检验计划，不是已发生的确认结果。",
        "",
        "**观察顺序：市场参与 → 行业延续 → 公司证据。** 先回答环境是否改善，再决定把研究时间放在哪个对象上。",
        "",
    ]
    for card in plan["cards"]:
        lines += [
            f"### {card['priority']}：{card['object']}",
            "",
            f"**要回答：{card['question']}**",
            "",
            f"- 当前基线（计算）：{card['baseline']}",
            f"- 确认条件：{card['confirm']}",
            f"- 反证/未确认条件：{card['invalidate']}",
            f"- 对应研究动作：{card['follow_up']}",
            f"- 当前研究状态：{card['state']}；缺口：{card['gap']}",
            f"- 证据索引：{', '.join(card['refs'])}；对应来源和六项时间见后文与冻结输入。",
            "",
        ]
    lines += ["### 按时点执行观察", "", "| 时点 | 北京时间 | 完成什么 |", "|---|---|---|"]
    tasks = [
        "检查截止后新增SEC/IR披露；逐条记链接与时间。没有确认日程就写未知。",
        "仅在有带时点和延迟标记的同源行情时，记录同一时刻的SPY、RSP、IWM及重点ETF涨跌；当前分时未接入，留UNKNOWN，等待收盘核查。",
        "等完整日线到齐，重算每张卡的条件，分别填‘支持/不支持/未知’，保存证据并更新研究优先级。",
    ]
    for i, check in enumerate(plan["checks"]):
        lines.append(f"| {check['title']} | {check['shanghai']} | {tasks[min(i, 2)]} |")
    if not plan["checks"]:
        lines.append("| UNKNOWN | UNKNOWN | 日历未覆盖下一交易日，先补日历。 |")
    lines += [
        "",
        "记录格式：观察时间｜对象｜同源涨跌及比较差值｜规则是否满足｜证据链接｜下一次复核。",
        "盘中涨跌统一用同一供应商前一常规收盘为基准；盘前、盘后与常规时段分开。量比只比较完整交易日，不用半小时成交量除以全天均量。",
        plan["expiry"],
        plan["rule_basis"],
        "这些动作管理观察与研究顺序。正式候选仍受证据门槛约束；本卡不生成下单、仓位或止损指令。",
        "",
    ]
    return lines
