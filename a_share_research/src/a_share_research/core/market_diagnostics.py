"""Research capability diagnosis and current-market observations, separate from EOD returns."""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from statistics import median

from .contracts import ContractError, parse_datetime
from .feedback_metrics import _bars, _change, available, fmt, number, security_code
from .utils import sha256_value

VERSION = "cn-market-observation-v2"
CODE = re.compile(r"(?:sh|sz|bj)\.[0-9]{6}\Z")
SOURCE_FIELDS = ("evidence_id", "source_url", "source_level", "published_at", "effective_at",
                 "available_at", "retrieved_at", "as_of")


def _source(evidence):
    return {key:evidence[key] for key in SOURCE_FIELDS}


def _distribution(values, total=None):
    valid = sorted(v for v in values if v is not None)
    total = len(values) if total is None else total
    up, down = sum(v > 0 for v in valid), sum(v < 0 for v in valid)
    return {"observed":len(valid),"total":total,"unknown":total-len(valid),
            "advancing":up,"declining":down,"unchanged":len(valid)-up-down,
            "advance_fraction":fmt(Decimal(up)/(up+down)) if up+down else None,
            "positive_fraction":fmt(Decimal(up)/len(valid)) if valid else None,
            "median_change_pct":fmt(median(valid)) if valid else None,
            "minimum_change_pct":fmt(valid[0]) if valid else None,
            "p10_change_pct":fmt(valid[(len(valid)-1)//10]) if valid else None,
            "maximum_change_pct":fmt(valid[-1]) if valid else None}


def _capability(observed, total, reason, remedy):
    return {"status":"UNKNOWN" if not observed else "READY" if observed==total else "PARTIAL",
            "observed":observed,"total":total,"reason":reason,"remedy":remedy}


def _prefix_group(code):
    # A grouping key, not a legal assertion of board or limit applicability.
    if code.startswith('sh.68'):
        return 'SH_68x'
    if code.startswith(('sz.30',)):
        return 'SZ_30x'
    return code[:2].upper()+'_other'


def _current_market(data, cutoff):
    evidence = [e for e in data['evidence'] if e.get('source_level')=='structured_market'
                and 'cross_section' in e and available(e,cutoff)]
    if len(evidence)!=1:
        return {"status":"UNKNOWN","reason":"CURRENT_CROSS_SECTION_MISSING_OR_AMBIGUOUS",
                "dated_eod_status":"UNKNOWN","distribution":_distribution([]),"cohorts":{},
                "sources":[],"temporal_transition":"UNKNOWN_SINGLE_OBSERVATION"}, {}
    e = evidence[0]
    cross = e['cross_section']
    if not isinstance(cross,dict) or cross.get('schema_version')!='cn-current-quotes-v1' or cross.get('time_semantics')!='CURRENT_OBSERVATION_NOT_DATED_EOD':
        raise ContractError('unsupported current cross-section semantics')
    rows, expected = cross.get('observations'),cross.get('expected_count')
    if not isinstance(rows,list) or isinstance(expected,bool) or not isinstance(expected,int) or not 1<=expected<=20000 or len(rows)>expected:
        raise ContractError('invalid bounded cross-section coverage')
    if cross.get('complete') is True and len(rows)!=expected:
        raise ContractError('cross-section complete flag contradicts coverage')
    indexed, values = {}, {}
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('code'),str) or not CODE.fullmatch(row['code']) or row['code'] in indexed:
            raise ContractError('invalid or duplicate current cross-section code')
        code = row['code']
        indexed[code] = row
        ratio, volume = number(row.get('reported_change_pct')),number(row.get('volume'))
        values[code] = ratio if row.get('observation_status')=='OBSERVED' and volume is not None and volume>0 else None
    ordered = sorted((c for c,v in values.items() if v is not None),key=lambda c:(-values[c],c))
    size = min(20,len(ordered)//2)
    cohorts = {"strong":ordered[:size],"weak":ordered[-size:] if size else [],
               "unclassified":sorted(c for c,v in values.items() if v is None),
               "universe_complete":cross.get('complete') is True,
               "rule":"provider-reported percentage; disjoint top/bottom min(20,floor(n/2)); ties by code"}
    cohorts['cohort_id'] = 'cn-market-cohort-'+sha256_value({'snapshot':data['snapshot_id'],
                                                          'evidence':sha256_value(e),'cohorts':cohorts,'version':VERSION})[:20]
    dist = _distribution(list(values.values()),expected)
    balance = ('ADVANCING_MAJORITY' if dist['advancing']>dist['declining'] else
               'DECLINING_MAJORITY' if dist['declining']>dist['advancing'] else 'BALANCED') if dist['observed'] else 'UNKNOWN'
    groups = {group:_distribution([values[c] for c in values if _prefix_group(c)==group])
              for group in sorted({_prefix_group(c) for c in values})}
    return {"status":"READY" if cross.get('complete') and dist['observed'] else 'PARTIAL' if dist['observed'] else 'UNKNOWN',
            "scope":"provider_full_a_share_list" if cross.get('complete') else 'incomplete_observed_subset',
            "dated_eod_status":"UNKNOWN","observed_at":e['as_of'],"sources":[_source(e)],
            "distribution":dist,"breadth_balance":balance,"cohorts":cohorts,"code_prefix_groups":groups,
            "temporal_transition":"UNKNOWN_SINGLE_OBSERVATION",
            "note":"Provider-reported current price change; no individual session timestamp, suspension inference or cumulative-return substitution"}, {
                code:row | {'usable_change':values[code]} for code,row in indexed.items()}


def _special_pools(data, cutoff):
    evidence = [e for e in data['evidence'] if e.get('source_level')=='structured_market'
                and 'pool_membership' in e and available(e,cutoff)]
    result = {"status":"UNKNOWN","counts":{},"ladder":[],"sources":[],
              "rule_version":"UNKNOWN","intraday_paths":"UNKNOWN"}
    if len(evidence)!=1:
        return result
    e = evidence[0]
    pool = e['pool_membership']
    if not isinstance(pool,dict) or pool.get('schema_version')!='cn-dated-special-pools-v1':
        raise ContractError('unsupported special-pool contract')
    rows, counts = pool.get('records'),pool.get('counts')
    if not isinstance(rows,list) or len(rows)>20000 or not isinstance(counts,dict):
        raise ContractError('invalid special-pool coverage')
    if set(counts)!={'limit_up','limit_down','limit_break'} or any(isinstance(n,bool) or not isinstance(n,int) or n<0 for n in counts.values()):
        raise ContractError('invalid special-pool counts')
    seen, ladder = set(),Counter()
    for row in rows:
        if not isinstance(row,dict) or not CODE.fullmatch(str(row.get('code',''))) or row.get('pool') not in counts:
            raise ContractError('invalid special-pool member')
        key = (row['code'],row['pool'])
        if key in seen:
            raise ContractError('duplicate special-pool member')
        seen.add(key)
        if row['pool']=='limit_up':
            streak = number(row.get('reported_streak'))
            level = str(int(streak)) if streak is not None and streak>=1 and streak==int(streak) else 'UNKNOWN'
            ladder[(_prefix_group(row['code']),level)]+=1
    complete = pool.get('complete') is True
    if complete and any(sum(p==kind for _,p in seen)!=count for kind,count in counts.items()):
        raise ContractError('special-pool complete flag contradicts coverage')
    union = {c for c,p in seen if p in {'limit_up','limit_break'}}
    broken = {c for c,p in seen if p=='limit_break'}
    return result | {"status":"READY" if complete else 'PARTIAL',"counts":counts,
                     "session":pool['session'],"sources":[_source(e)],
                     "ladder":[{'code_prefix_group':g,'provider_reported_streak':n,'members':v}
                               for (g,n),v in sorted(ladder.items())],
                     "break_pool_fraction":fmt(Decimal(len(broken))/len(union)) if complete and union else None,
                     "fraction_definition":"unique break-pool members / union(up-pool,break-pool); not intraday event frequency",
                     "note":"Vendor-reported streaks grouped by code prefix; board, ST and IPO limit rules not validated"}


def build_market_diagnostics(data, decisions, cutoff):
    market, quotes = _current_market(data,cutoff)
    pools = _special_pools(data,cutoff)
    daily = _bars([data],cutoff)
    views, queue, historical = [],[],0
    for c in decisions:
        code = security_code(c['security_id'])
        quote = quotes.get(code,{})
        change = quote.get('usable_change')
        own_bars = [point for (instrument,_),point in daily.items() if instrument==code]
        history_ok = bool(own_bars) and all(_change(point) is not None for point in own_bars)
        historical += history_ok
        # ``next_catalyst_at`` comes from the evidence-qualified opportunity profile and is
        # None when no catalyst cleared the evidence gate. A missing schedule is UNKNOWN,
        # not "expired": only a dated catalyst that is already past is expired.
        catalyst_at = c.get('next_catalyst_at')
        expired = (
            catalyst_at is not None
            and parse_datetime(catalyst_at, 'next_catalyst_at') <= cutoff
        )
        official = c['gates'].get('official_event',False)
        response = 'UNKNOWN' if change is None else 'POSITIVE' if change>0 else 'NEGATIVE' if change<0 else 'FLAT'
        state = ('OFFICIAL_EVIDENCE_MISSING' if not official else
                 'OFFICIAL_FACT_PRICE_DIVERGENCE' if response=='NEGATIVE' else
                 'OFFICIAL_FACT_PRICE_POSITIVE_NO_CAUSAL_PROOF' if response=='POSITIVE' else
                 'OFFICIAL_FACT_MARKET_FEEDBACK_UNRESOLVED')
        views.append({'candidate_id':c['candidate_id'],'code':code,'name':c['name'],
                      'theme_id':c['theme_id'],'decision':c['decision'],
                      'reported_change_pct':fmt(change),'price_response':response,'evidence_market_relation':state,
                      'research_lifecycle':'REVALIDATE_EXPIRED_CATALYST' if expired else 'ACTIVE_RESEARCH_WINDOW',
                      'original_catalyst_at':c['next_catalyst_at'],'seed_stage':c['stage'],
                      'current_cycle_stage':'UNKNOWN','historical_reference_feedback':history_ok,
                      'market_sources':market['sources'] if quote else [],
                      'official_sources':[e for e in c['evidence'] if e.get('source_level')=='official']})
        if expired:
            queue.append({'kind':'refresh_official_catalyst','candidate_id':c['candidate_id'],
                          'reason':'原催化日期已过；旧阶段不能自动续期',
                          'required_evidence':'新的公司披露或正式事件日期、原业务映射的变化与反例；缺失则保留未知'})
        if not history_ok:
            queue.append({'kind':'complete_historical_price_basis','candidate_id':c['candidate_id'],
                          'reason':'缺少完整的逐日参考前收或有效交易状态',
                          'required_evidence':'有日期、公司行动口径及可得时间的历史序列；当前快照不得回填历史'})
        if state=='OFFICIAL_FACT_PRICE_DIVERGENCE':
            queue.append({'kind':'review_fact_price_divergence','candidate_id':c['candidate_id'],
                          'reason':'存在官方事实，但当前供应商涨跌幅为负；这不是经营事实已经被证伪',
                          'required_evidence':'事前预期、同行对照、同期基准与新增公告'})
    themes = []
    for tid in dict.fromkeys(c['theme_id'] for c in decisions):
        members = sorted({security_code(c['security_id']) for c in decisions if c['theme_id']==tid})
        changes = [quotes.get(code,{}).get('usable_change') for code in members]
        amounts = [number(quotes.get(code,{}).get('amount')) for code in members]
        total = sum(amounts,Decimal(0)) if amounts and all(v is not None and v>=0 for v in amounts) else None
        themes.append({'theme_id':tid,'members':members,'membership_scope':'frozen_editorial_members',
                       'distribution':_distribution(changes),
                       'turnover_hhi':fmt(sum((v/total)**2 for v in amounts)) if total else None,
                       'amount_observed':sum(v is not None for v in amounts),'total':len(members),
                       'historical_expansion_or_contraction':'UNKNOWN_NO_PRIOR_MEMBERSHIP_OBSERVATION'})
    n = len(decisions)
    current = sum(v['reported_change_pct'] is not None for v in views)
    active = sum(v['research_lifecycle']=='ACTIVE_RESEARCH_WINDOW' for v in views)
    return {'version':VERSION,'current_market':market,'special_pools':pools,'candidate_views':views,'themes':themes,
            'automatic_cycle_label':'UNKNOWN','research_queue':queue,
            'capabilities':{
                'current_candidate_observation':_capability(current,n,'当前横截面观测；不要求可执行交易状态','保留全市场个股快照及其真实观测时间'),
                'historical_reference_feedback':_capability(historical,n,'累计反馈需要历史参考价及状态；与现价观测分开','显式接入合格的历史数据，验证除权及交易日历'),
                'unexpired_research_window':_capability(active,n,'催化未过期仅是必要条件，不等于逻辑有效','更新正式披露和具体事件窗口，保留旧版本') | {
                    'status':'NOT_READY' if n and active==0 else 'READY' if n and active==n else 'PARTIAL' if active else 'UNKNOWN'},
            },
            'interpretation_contract':{
                'version':'cn-evidence-interpretation-v2',
                'questions':['当前可观察的市场分布是什么，覆盖和时间口径是什么？',
                             '哪些原研究判断已到期，哪些事实与价格出现矛盾？',
                             '与前一次冻结观察相比是否变化？没有可比输入就写 UNKNOWN。',
                             '分别给出支持解释、反方解释、证伪条件和下一条所需证据。'],
                'rules':['每个数字引用计算字段和证据 ID；事实、计算、推断分开。',
                         '现价横截面不得冒充指定交易日收盘，不得替代多日累计反馈。',
                         '价格上涨不能确认业务因果；单日下跌不能自动否定经营假设。',
                         '过期催化不能改个日期继续沿用；没有外部事实就保留待复核。',
                         '不得把代码前缀、供应商连板标签当成已验证的交易制度或资金身份。'],
                'model_execution':'NOT_RUN_BY_DETERMINISTIC_ENGINE',
            }}
