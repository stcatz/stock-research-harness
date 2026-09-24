from __future__ import annotations

import copy
import unittest

from test_research_feedback import market_fixture


class ObservationPlanTests(unittest.TestCase):
    def setUp(self):
        self.data, self.cutoff = market_fixture()
        cal = self.data['evidence'][-1]['calendar']
        cal['coverage_end'] = '2026-08-19'
        cal['sessions'] += ['2026-08-19']  # Explicit synthetic closure, not weekday arithmetic.
        source = self.data['evidence'][0]
        self.decisions, self.views = [], []
        for i, change in enumerate(['-1', '-2', '-3', '0', '-4', '-5']):
            tid = 'a' if i < 3 else 'b'
            code = f'sh.60000{i+1}'
            cid = f'{tid}:CN.SH.60000{i+1}'
            self.decisions.append({'candidate_id':cid, 'theme_id':tid, 'theme_name':tid,
                'name':f'合成公司{i}', 'symbol':code[3:], 'decision':'continue_research',
                'gates':{'official_event':True, 'structured_market':True, 'market_freshness':True},
                'evidence':[source | {'source_level':'official'}], 'risk_flags':['SYNTHETIC'],
                'data_gaps':['需要核验新公告'], 'counter_thesis':'样本无法代表业务',
                'invalidation_conditions':['正式公告否定业务映射']})
            self.views.append({'candidate_id':cid, 'theme_id':tid, 'code':code,
                'reported_change_pct':change, 'original_catalyst_at':'2026-08-13T15:00:00+08:00',
                'research_lifecycle':'REVALIDATE_EXPIRED_CATALYST','market_sources':[source]})
        self.diag = {'candidate_views':self.views, 'current_market':{
            'distribution':{'observed':6,'total':6,'advancing':0,'declining':5,'unchanged':1,
                            'median_change_pct':'-2.5','advance_fraction':'0'},
            'sources':[source], 'cohorts':{'strong':['sh.600001'],'weak':['sh.600006'],'unclassified':[]}},
            'special_pools':{'counts':{'limit_up':2,'limit_down':1,'limit_break':1},'sources':[source]}}

    def build(self):
        from a_share_research.core.observation_plan import build_observation_plan
        return build_observation_plan(self.data,self.decisions,self.diag,[],self.cutoff)

    def test_calendar_skips_explicit_closed_weekdays(self):
        plan = self.build()
        self.assertEqual(plan['next_session'],'2026-08-19')
        self.assertTrue(plan['checkpoints'][1]['scheduled_at'].startswith('2026-08-19T09:35'))

    def test_no_calendar_never_guesses_date_or_claims_monitoring(self):
        self.data['evidence'].pop()
        plan = self.build()
        self.assertIsNone(plan['next_session'])
        self.assertTrue(all(p['scheduled_at'] is None for p in plan['checkpoints']))
        self.assertEqual(plan['execution_mode'],'MANUAL_CHECKLIST_NOT_SCHEDULED')

    def test_two_comparison_cards_use_current_strength_not_r0_role(self):
        cards = [c for c in self.build()['cards'] if c['kind']=='candidate_comparison']
        self.assertEqual([c['candidate_id'] for c in cards],['b:CN.SH.600004','a:CN.SH.600001'])
        self.assertEqual(len(cards[0]['members']),3)
        self.assertIn('2/3',cards[0]['support_condition'])
        self.assertIn('催化',cards[0]['before_open'])
        self.assertIn('全部',cards[0]['on_support'])
        self.assertEqual(cards[0]['formal_decision'],'continue_research')

    def test_excluded_stale_or_unproven_candidates_cannot_be_priorities(self):
        self.decisions[3]['decision']='exclude'
        self.decisions[0]['gates']['market_freshness']=False
        self.decisions[1]['gates']['official_event']=False
        ids=[c.get('candidate_id') for c in self.build()['cards']]
        for index in [0,1,3]: self.assertNotIn(self.decisions[index]['candidate_id'],ids)

    def test_plan_does_not_mutate_source_decisions_or_observe_the_future(self):
        before=copy.deepcopy(self.decisions)
        p=self.build()
        self.assertEqual(before,self.decisions)
        self.assertTrue(all(c['observed_result']=='PENDING_MANUAL_OBSERVATION' for c in p['cards']))
        self.assertEqual(p['plan_id'],self.build()['plan_id'])

    def test_future_or_unreferenced_discoveries_cannot_enter_card(self):
        from a_share_research.core.observation_plan import build_observation_plan
        discovery={'label':'不应出现','evidence_ref':'no-such-evidence','members':[]}
        p=build_observation_plan(self.data,self.decisions,self.diag,[discovery],self.cutoff)
        self.assertFalse(any(c['kind']=='discovery_verification' for c in p['cards']))

    def test_missing_quotes_does_not_select_fake_leader(self):
        for row in self.views: row['reported_change_pct']=None
        self.assertFalse(any(c['kind']=='candidate_comparison' for c in self.build()['cards']))

    def test_calendar_coverage_end_and_future_calendar_do_not_create_dates(self):
        cal=self.data['evidence'][-1]
        cal['calendar']['coverage_end']='2026-08-14'
        cal['calendar']['sessions'].pop()
        self.assertIsNone(self.build()['next_session'])
        cal['available_at']='2099-01-01T00:00:00+00:00'
        self.assertIsNone(self.build()['next_session'])

    def test_discovery_card_stays_a_verification_task(self):
        from a_share_research.core.observation_plan import build_observation_plan
        discovery={'label':'合成线索','evidence_ref':self.data['evidence'][0]['evidence_id'],
                   'member_count':2,'members':[{'thscode':'600001.SH','name':'甲'}, {'thscode':'600002.SH','name':'乙'}]}
        p=build_observation_plan(self.data,self.decisions,self.diag,[discovery],self.cutoff)
        card=next(c for c in p['cards'] if c['kind']=='discovery_verification')
        self.assertIn('一级事实',card['support_condition'])
        self.assertNotIn('candidate_id',card)
        self.data['evidence'][0]['available_at']='2099-01-01T00:00:00+00:00'
        p=build_observation_plan(self.data,self.decisions,self.diag,[discovery],self.cutoff)
        self.assertFalse(any(c['kind']=='discovery_verification' for c in p['cards']))

    def test_rendered_plan_has_specific_members_conditions_and_actions(self):
        from a_share_research.core.reporting import render_observation_plan
        report='\n'.join(render_observation_plan(self.build()))
        for text in ['合成公司3','sh.600004','2/3','09:35','10:00','满足后做什么','失败后做什么','待人工观察']:
            self.assertIn(text,report)


if __name__ == '__main__':
    unittest.main()
