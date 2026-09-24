"""Frozen, source-linked manual observation cards; never evaluate future outcomes."""
from __future__ import annotations

from datetime import date, datetime, time

from .feedback_metrics import SHANGHAI, available, calendar_record, number
from .utils import sha256_value

VERSION = 'cn-observation-plan-v1'
SOURCE_FIELDS = ('evidence_id', 'source_url', 'source_level', 'published_at',
                 'effective_at', 'available_at', 'retrieved_at', 'as_of')
CHECKPOINTS = (
    ('09:15', '盘前核验', '复核新正式披露、原催化、名单和行情日期；写下反方条件。'),
    ('09:35', '首份观察', '记录固定成员当期涨跌幅、市场宽度及来源时间，作为当日比较起点。'),
    ('10:00', '第二次观察', '与 09:35 相比，检查改善是否持续、是否扩展到其他成员。'),
    ('13:30', '午后复核', '检查上午解释是否仍成立；相反证据出现时撤回对应解释。'),
    ('15:10', '收盘记录', '核收盘数据是否已到；逐卡记录支持、反对或未知，保留原计划。'),
)


def _sources(items, cutoff):
    indexed = {}
    for e in items:
        if available(e, cutoff) and all(k in e for k in SOURCE_FIELDS):
            indexed[e['evidence_id']] = {k:e[k] for k in SOURCE_FIELDS}
    return [indexed[key] for key in sorted(indexed)]


def _next_session(data, cutoff):
    cal = calendar_record(data, cutoff)
    today = cutoff.astimezone(SHANGHAI).date().isoformat()
    if not cal or not {'SH', 'SZ'}.issubset(cal['exchanges']):
        return None, None
    if not cal['coverage_start'] <= today <= cal['coverage_end']:
        return None, cal
    return next((day for day in cal['sessions'] if day > today), None), cal


def _base_card(kind, title, question):
    return {'kind':kind, 'title':title, 'question':question, 'members':[],
            'observed_result':'PENDING_MANUAL_OBSERVATION', 'sources':[],
            'checkpoints':['09:35', '10:00', '13:30', '15:10']}


def build_observation_plan(data, decisions, diagnostics, discoveries, cutoff):
    session, cal = _next_session(data, cutoff)
    market = diagnostics['current_market']
    dist = market['distribution']
    cohorts = market.get('cohorts', {})
    cards = []
    card = _base_card('market_feedback', '先验证市场反馈能否扩展',
                      '原强势群体能否延续，原弱势群体是否停止恶化？')
    card.update(
        baseline=f"本轮报价有效 {dist['observed']}/{dist['total']}；上涨 {dist['advancing']}、下跌 {dist['declining']}、平盘 {dist['unchanged']}；中位数 {dist['median_change_pct'] or 'UNKNOWN'}%。",
        members=[{'group':key, 'codes':list(cohorts.get(key, []))}
                 for key in ('strong', 'weak')],
        before_open='核名单、证券状态、代码组、当日行情参考价；未取得合格日期前，不将本轮现价榜称为上一交易日收盘榜。',
        measure='09:35 与 10:00 分别记录市场上涨比例/中位数、固定强势组中位数、弱势组中位数/10%分位、各组有效/全部成员。',
        support_condition='同日同成员、覆盖完整可比时：10:00 市场上涨比例和中位数均高于 09:35；强势组中位数大于 0%；弱势组中位数及10%分位均不低于 09:35。',
        counter_condition='强势组中位数不大于 0%，且弱势组中位数或10%分位比 09:35 更低；仅少数股票改善时，只记局部响应。',
        on_support='记录“群体改善假设获支持”，优先核验有新正式事实的题材；不据此自动改变任何候选状态。',
        on_counter='撤回本卡的广泛改善解释，保留局部线索；13:30 重新检查，不把指数表现替代群体表现。',
        on_unknown='缺任一比较输入或覆盖不可比：记录 UNKNOWN 和缺失成员，等待下一检查点，不补零、不重选强弱组。',
        invalidation='午后不再满足同一组支持条件，或新证据否定行情口径时，撤回上午解释。',
        data_gaps=['盘中时点序列未自动接入', '现价横截面没有逐股日期', '代码组制度未统一；按组复核，不据总榜判定龙头'],
        sources=_sources(market['sources'], cutoff),
    )
    cards.append(card)

    # A bounded discovery task does not admit its members as issuer research candidates.
    evidence = {e['evidence_id']:e for e in data['evidence']}
    valid_discoveries = [d for d in discoveries if d.get('evidence_ref') in evidence
                         and available(evidence[d['evidence_ref']], cutoff)
                         and isinstance(d.get('members'), list) and d['members']
                         and all(isinstance(m, dict) and isinstance(m.get('thscode'), str)
                                 for m in d['members'])]
    for discovery in sorted(valid_discoveries, key=lambda d:(-d.get('member_count', 0), d['label']))[:1]:
        n = len(discovery['members'])
        required = n // 2 + 1
        card = _base_card('discovery_verification', f"核验新线索：{discovery['label']}",
                          '供应商标签能否得到公司正式事实和多个固定成员的共同响应？')
        card.update(
            baseline=f"供应商标签聚类 {discovery.get('member_count', n)} 个成员；分类 provider_derived_unverified，未通过公司事实准入。",
            members=[{'name':m.get('name') or m['thscode'], 'code':m['thscode']} for m in discovery['members']],
            before_open='逐公司查交易所/公司最新公告：记录标题、链接、公开时间、业务对应段落；没有新事实就写未核实，不把标签当成订单。',
            measure='先补公司事实；再对同一组记录 09:35/10:00 每名成员的当期涨跌幅、正值成员数和中位数。',
            support_condition=f"一级事实映射核验后，10:00 全部 {n} 名成员报价有效，至少 {required}/{n} 为正，且组中位数高于 09:35。" if n else 'UNKNOWN：没有固定成员。',
            counter_condition='只有单个成员突出、其余没有响应，或公司正式披露不支持供应商业务标签。',
            on_support='形成附官方证据的新研究输入，按原全部门槛重新评估；本卡不自动新增正式候选。',
            on_counter='撤回本卡的群体响应或业务映射解释，保留失败成员和原标签记录。',
            on_unknown='无官方事实或任一成员缺有效报价：继续线索核验，不把其他成员删去后宣布成功。',
            invalidation='后续公告否定业务映射，或午后多数成员不再响应时，撤回相应解释。',
            data_gaps=['供应商标签未获一级事实确认', '盘中序列待人工记录'],
            sources=_sources([evidence[discovery['evidence_ref']]], cutoff),
        )
        cards.append(card)

    # Choose at most two distinct themes using current reported change, not R0 or seed role.
    by_id = {c['candidate_id']:c for c in decisions}
    eligible = []
    for view in diagnostics['candidate_views']:
        c = by_id[view['candidate_id']]
        gates = c['gates']
        if (c['decision'] != 'exclude' and number(view['reported_change_pct']) is not None
                and all(gates.get(k) for k in ('official_event', 'structured_market', 'market_freshness'))):
            eligible.append(view)
    eligible.sort(key=lambda v:(-number(v['reported_change_pct']), v['code'], v['candidate_id']))
    chosen_themes = set()
    for view in eligible:
        if view['theme_id'] in chosen_themes:
            continue
        if len(chosen_themes) == 2:
            break
        chosen_themes.add(view['theme_id'])
        c = by_id[view['candidate_id']]
        peers = [v for v in diagnostics['candidate_views'] if v['theme_id'] == view['theme_id']]
        n = len(peers)
        required = n // 2 + 1
        members = [{'candidate_id':v['candidate_id'], 'name':by_id[v['candidate_id']]['name'],
                    'code':v['code'], 'baseline_change_pct':v['reported_change_pct'],
                    'formal_decision':by_id[v['candidate_id']]['decision']} for v in peers]
        card = _base_card('candidate_comparison', f"{c['name']}：验证相对韧性与同组响应", '本轮相对表现较强能否延续，并扩展到其他固定成员？')
        card.update(
            candidate_id=c['candidate_id'], theme_id=c['theme_id'], theme_name=c['theme_name'],
            formal_decision=c['decision'], members=members,
            baseline=f"{c['name']}报告涨跌幅 {view['reported_change_pct']}%；在已有合格研究候选内按本轮涨跌幅选作比较对象，不认定为龙头。",
            original_catalyst_at=view['original_catalyst_at'], risk_flags=c.get('risk_flags', []),
            before_open=(f"原催化 {view['original_catalyst_at']} 已到期，必须补新的正式披露/具体事件日期；找不到就维持原研究状态。"
                         if view['research_lifecycle']=='REVALIDATE_EXPIRED_CATALYST' else
                         f"复核原催化 {view['original_catalyst_at']} 是否仍有效，记录新披露及相反事实。"),
            measure=f"记录 {c['name']}及全部同组成员在09:35、10:00的涨跌幅；比较对象是否高于其他成员中位数、组内正值成员数、组中位数变化。",
            support_condition=(f"10:00 全部 {n} 名成员有效，{c['name']}涨跌幅高于其他成员中位数，至少 {required}/{n} 为正，且组中位数高于09:35；同时新增官方事实和有效催化核验通过。"
                               if n > 1 else '只有一个成员，群体响应 UNKNOWN；须先建立有依据的可比成员组。'),
            counter_condition=f"{c['name']}表现不高于其他成员中位数，或只有该股改善；这反对相对韧性/群体响应解释，不自动否定经营事实。",
            on_support='记录本卡观察支持，带新增证据按全部原门槛重新评估；仍有硬缺口就保持 continue_research，不能仅凭价格升级 observe。',
            on_counter='撤回本卡对应的相对强势或共同响应解释；保留原候选和反例，研究状态只由完整证据重新评估。',
            on_unknown='催化未更新、成员报价缺失、缺比较时点：继续补证，不能宣称本卡通过。',
            invalidation='；'.join(c.get('invalidation_conditions', [])) or 'UNKNOWN：需补原研究的证伪条件。',
            counter_thesis=c.get('counter_thesis', 'UNKNOWN'),
            data_gaps=list(c.get('data_gaps', [])) + ['盘中比较尚待人工观察；两次观测必须使用同一批成员'],
            sources=_sources([*[e for e in c.get('evidence', []) if e.get('source_level') == 'official'],
                              *view['market_sources']], cutoff),
        )
        cards.append(card)

    for i, card in enumerate(cards, 1):
        card['card_id'] = f'C{i}'
    checkpoints = []
    for clock, label, task in CHECKPOINTS:
        scheduled = datetime.combine(date.fromisoformat(session), time.fromisoformat(clock), SHANGHAI).isoformat() if session else None
        checkpoints.append({'local_time':clock, 'label':label, 'task':task, 'scheduled_at':scheduled})
    covered = cal['exchanges'] if cal else []
    all_ids = {c.get('candidate_id') for c in cards}
    result = {
        'version':VERSION, 'next_session':session, 'calendar_exchanges':covered,
        'calendar_sources':_sources([evidence[cal['evidence_ref']]], cutoff) if cal else [],
        'execution_mode':'MANUAL_CHECKLIST_NOT_SCHEDULED', 'checkpoints':checkpoints,
        'selection_rule':'最多1个线索验证组、2个不同题材的已有候选比较卡；现价涨跌幅降序，仅分配研究注意力，正式R0不变',
        'comparison_contract':'09:35/10:00为本版本人工记录约定，非已验证信号。比较须同一交易日、同成员、同涨跌幅口径、全部有效；盘中数据尚未自动接入。',
        'calendar_scope_note':'具体日期只适用于日历声明交易所；其他代码组待核日历，不自动使用这些时间。',
        'cards':cards,
        'not_prioritized':[{'candidate_id':c['candidate_id'],'name':c['name'],'decision':c['decision'],
                            'reason':'原规则排除，保留原因见候选卡' if c['decision']=='exclude' else
                                     '本轮最多两组比较卡；未入选不等于研究排除，原记录保留'}
                           for c in decisions if c['candidate_id'] not in all_ids],
        'record_fields':['card_id','scheduled_at','actual_observed_at','source_url','source_available_at',
                         'quote_session','valid_members/total_members','metrics','support/counter/unknown',
                         'evidence_refs','research_action','withdrawal_reason'],
    }
    result['plan_id'] = 'cn-plan-' + sha256_value({'version':VERSION,'snapshot':data['snapshot_id'],
                                                'cutoff':cutoff.isoformat(),'plan':result})[:20]
    return result
