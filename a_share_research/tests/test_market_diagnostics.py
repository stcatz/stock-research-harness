from __future__ import annotations

import copy
import unittest
from datetime import datetime

from test_ingest_hithink_enrichment import _QueueClient, _valid_responses, SESSION
from a_share_research.ingest.hithink_enrichment import collect_hithink_enrichment
from a_share_research.core.contracts import ContractError


class MarketDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        result=collect_hithink_enrichment(_QueueClient(_valid_responses()),latest_session=SESSION,
                                         candidate_thscodes=['600519.SH'],page_size=2)
        self.data={'snapshot_id':'synthetic-diagnostics','evidence':result.evidence_fragments()}
        self.cutoff=datetime.fromisoformat('2026-08-21T18:00:00+08:00')
        self.decisions=[{'candidate_id':'theme:CN.SH.600519','security_id':'CN.SH.600519','name':'合成研究公司',
                         'symbol':'600519','theme_id':'theme','theme_name':'合成主题','decision':'continue_research',
                         'next_catalyst_at':'2026-08-20T00:00:00+08:00','stage':'warming','gates':{'official_event':True,'structured_market':True,'next_catalyst':False},
                         'evidence':[],'data_gaps':['field missing'],'manual_review_items':['复核公告']}]

    def build(self):
        from a_share_research.core.market_diagnostics import build_market_diagnostics
        return build_market_diagnostics(self.data,self.decisions,self.cutoff)

    def test_current_observation_does_not_require_unknown_execution_status(self):
        result=self.build()
        self.assertEqual(result['current_market']['distribution']['observed'],2)
        self.assertEqual(result['current_market']['distribution']['total'],3)
        self.assertEqual(result['candidate_views'][0]['reported_change_pct'],'1.2500')
        self.assertEqual(result['capabilities']['current_candidate_observation']['observed'],1)
        self.assertEqual(result['capabilities']['historical_reference_feedback']['observed'],0)
        self.assertEqual(result['current_market']['dated_eod_status'],'UNKNOWN')

    def test_frozen_full_market_groups_include_non_seed_stock_and_keep_missing(self):
        result=self.build()['current_market']
        self.assertEqual(result['cohorts']['strong'],['sh.600519'])
        self.assertEqual(result['cohorts']['weak'],['sz.000001'])
        self.assertEqual(result['cohorts']['unclassified'],['sz.300001'])
        self.assertEqual(result['distribution']['positive_fraction'],'0.5000')

    def test_expired_catalyst_creates_specific_revalidation_task(self):
        result=self.build()
        self.assertEqual(result['candidate_views'][0]['research_lifecycle'],'REVALIDATE_EXPIRED_CATALYST')
        self.assertTrue(any(t['kind']=='refresh_official_catalyst' for t in result['research_queue']))
        self.assertEqual(result['candidate_views'][0]['decision'],'continue_research')
        self.assertEqual(result['automatic_cycle_label'],'UNKNOWN')
        self.assertEqual(result['capabilities']['unexpired_research_window']['status'],'NOT_READY')

    def test_future_cross_section_cannot_enter_market_interpretation(self):
        cross=next(e for e in self.data['evidence'] if 'cross_section' in e)
        cross['available_at']='2099-01-01T00:00:00+08:00'
        result=self.build()
        self.assertEqual(result['current_market']['status'],'UNKNOWN')
        self.assertIsNone(result['candidate_views'][0]['reported_change_pct'])

    def test_cross_section_duplicates_and_false_coverage_are_rejected(self):
        cross=next(e for e in self.data['evidence'] if 'cross_section' in e)['cross_section']
        cross['observations'][1]=copy.deepcopy(cross['observations'][0])
        with self.assertRaises(ContractError): self.build()

    def test_pool_ladder_uses_complete_provider_universe(self):
        result=self.build()['special_pools']
        self.assertEqual(result['counts']['limit_up'],3)
        self.assertEqual(sum(r['members'] for r in result['ladder']),3)
        self.assertEqual(result['rule_version'],'UNKNOWN')
        self.assertEqual(result['intraday_paths'],'UNKNOWN')

    def test_declared_complete_cross_section_cannot_hide_missing_members(self):
        cross=next(e for e in self.data['evidence'] if 'cross_section' in e)['cross_section']
        cross['observations'].pop()
        with self.assertRaises(ContractError): self.build()

    def test_negative_price_is_a_review_question_not_business_falsification(self):
        self.decisions[0]['security_id']='CN.SZ.000001'
        result=self.build()
        candidate=result['candidate_views'][0]
        self.assertEqual(candidate['evidence_market_relation'],'OFFICIAL_FACT_PRICE_DIVERGENCE')
        self.assertEqual(candidate['decision'],'continue_research')
        self.assertTrue(any(t['kind']=='review_fact_price_divergence' for t in result['research_queue']))

    def test_break_pool_fraction_counts_union_not_double_counted_members(self):
        pool=next(e for e in self.data['evidence'] if 'pool_membership' in e)['pool_membership']
        member=copy.deepcopy(next(r for r in pool['records'] if r['pool']=='limit_up'))
        member['pool']='limit_break'
        pool['records']=[r for r in pool['records'] if r['pool']!='limit_break']+[member]
        pool['counts']['limit_break']=1
        self.assertEqual(self.build()['special_pools']['break_pool_fraction'],'0.3333')


if __name__=='__main__':
    unittest.main()
