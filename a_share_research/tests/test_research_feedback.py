from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from a_share_research.core.pipeline import read_artifact, run_research, verified_research_packet


class ReadAndFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = {"schema_version": "0.1", "workflow": "daily_report",
                        "decision_at": "2026-08-16T08:30:00+08:00",
                        "snapshot": {"selector": "demo"}}

    def test_pages_reassemble_exact_immutable_report(self):
        result = run_research(self.request, self.root)
        expected = (self.root / "artifacts/runs" / result["run_id"] / "report.md").read_text()
        parts, offset = [], 0
        while True:
            page = read_artifact({"artifact_id": result["artifact_id"], "section": "report",
                                  "max_chars": 500, "offset": offset}, self.root)
            parts.append(page["content"])
            if not page["truncated"]:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(parts), expected)

    def test_engine_version_changes_immutable_identity(self):
        from unittest.mock import patch
        from a_share_research.core import engine
        first = run_research(self.request, self.root)
        with patch.object(engine, "ENGINE_VERSION", "future-engine"):
            second = run_research(self.request, self.root)
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])

    def test_old_real_snapshot_cannot_reach_observe(self):
        from a_share_research.core.snapshot import load_snapshot
        from a_share_research.core.contracts import RunRequest
        data = copy.deepcopy(load_snapshot(self.root, RunRequest.from_dict(self.request)).data)
        data.update(data_mode="snapshot", pit_quality="RECONSTRUCTED_NON_PIT", snapshot_id="old")
        target = self.root / "data/normalized/old/snapshot.json"
        import json
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(data))
        result = run_research(self.request | {"snapshot": {"selector": "id", "snapshot_id": "old"},
                                              "decision_at": "2026-09-11T18:00:00+08:00"}, self.root)
        self.assertEqual(result["data_readiness"], "STALE")
        self.assertEqual(result["counts"]["observe"], 0)


    def test_future_context_is_hidden_and_candidate_price_session_is_checked(self):
        from a_share_research.core.snapshot import load_snapshot, ValidatedSnapshot
        from a_share_research.core.contracts import RunRequest
        from a_share_research.core.engine import build_research_packet
        from a_share_research.core.utils import sha256_value
        request = RunRequest.from_dict(self.request | {"decision_at":"2026-08-14T17:00:00+08:00"})
        old = load_snapshot(self.root, RunRequest.from_dict(self.request))
        data = copy.deepcopy(old.data)
        data.update(data_mode="snapshot",as_of="2026-08-14T15:00:00+08:00")
        data["market_context"]["evidence_refs"] = ["EV-FUTURE-001"]
        data["market_context"]["regime"] = "FUTURE CONTEXT MUST NOT LEAK"
        snapshot = ValidatedSnapshot(data=data, snapshot_hash=sha256_value(data),
                                     evidence_by_id={e['evidence_id']:e for e in data['evidence']},themes=data['themes'])
        packet = build_research_packet(snapshot,request,generated_at=request.decision_at)
        self.assertNotIn("FUTURE CONTEXT",str(packet["market_context"]))
        self.assertFalse(any(c["gates"]["market_freshness"] for c in packet["all_decisions"]))

    def test_discovery_uses_its_evidence_reference_and_hides_future_labels(self):
        from a_share_research.core.snapshot import load_snapshot, ValidatedSnapshot
        from a_share_research.core.contracts import RunRequest
        from a_share_research.core.engine import build_research_packet
        from a_share_research.core.utils import sha256_value
        request = RunRequest.from_dict(self.request)
        old = load_snapshot(self.root,request)
        data = copy.deepcopy(old.data)
        current = next(e['evidence_id'] for e in data['evidence'] if e['available_at'] < self.request['decision_at'])
        data['market_discoveries'] = [{'evidence_ref':current,'label':'CURRENT'},
                                      {'evidence_ref':'EV-FUTURE-001','label':'FUTURE'}]
        snapshot = ValidatedSnapshot(data=data,snapshot_hash=sha256_value(data),
                                     evidence_by_id=old.evidence_by_id,themes=old.themes)
        packet = build_research_packet(snapshot,request,generated_at=request.decision_at)
        self.assertEqual([r['label'] for r in packet['market_discoveries']],['CURRENT'])


def market_fixture():
    """Explicit synthetic calendar includes a non-weekend exchange closure."""
    days = ["2026-08-10", "2026-08-11", "2026-08-13", "2026-08-14"]
    cutoff = "2026-08-14T16:00:00+08:00"
    def evidence(eid, category):
        return {"evidence_id": eid, "source_level": "structured_market", "category": category,
                "title": "SYNTHETIC", "summary": "SYNTHETIC", "source_url": "https://fixture.example.invalid",
                "published_at": cutoff, "available_at": cutoff, "retrieved_at": cutoff,
                "effective_at": cutoff, "as_of": "2026-08-14T15:00:00+08:00"}
    records = []
    for code, changes in [("sh.000001", [100,100,100,100]), ("sh.600001", [100,110,90,120]),
                          ("sh.600002", [100,90,110,80])]:
        bars = [{"code":code,"date":day,"close":str(price),"preclose":"100",
                 "amount":"1000","trade_status":"1"} for day, price in zip(days,changes)]
        records.append(evidence(code, "market_data") | {
            "instrument":{"code":code,"kind":"benchmark" if code=="sh.000001" else "candidate"},
            "provider":{"frequency":"1d","adjustment":"none"},
            "latest":bars[-1],"calculation_window":bars})
    calendar = evidence("calendar", "trading_calendar") | {"calendar":{
        "market":"CN","exchanges":["SH","SZ","BJ"],"complete":True,
        "coverage_start":days[0],"coverage_end":days[-1],"sessions":days}}
    return {"snapshot_id":"synthetic", "as_of":"2026-08-14T15:00:00+08:00",
            "evidence":records+[calendar]}, datetime.fromisoformat(cutoff)


class FeedbackMetricTests(unittest.TestCase):
    def test_five_and_twenty_session_windows_mature_independently(self):
        from datetime import date,timedelta
        from decimal import Decimal
        from a_share_research.core.feedback_metrics import evaluate_horizons
        data,_ = market_fixture()
        # Synthetic explicit exchange calendar; no runtime weekday inference.
        start=date(2026,7,1)
        days=[(start+timedelta(days=i)).isoformat() for i in range(32)
              if (start+timedelta(days=i)).weekday()<5][:21]
        data['as_of']=days[-1]+'T15:00:00+08:00'
        for e in data['evidence']:
            for k in ('published_at','available_at','retrieved_at','as_of'):
                e[k]=days[-1]+'T16:00:00+08:00'
            if 'instrument' in e:
                code=e['instrument']['code']
                e['calculation_window']=[{'code':code,'date':d,'preclose':'100','close':'100' if code=='sh.000001' else '101',
                                          'trade_status':'1','amount':'1000'} for d in days]
                e['latest']=e['calculation_window'][-1]
        data['evidence'][-1]['calendar'].update(coverage_start=days[0],coverage_end=days[-1],sessions=days)
        result=evaluate_horizons([data],days[0],['sh.600001'],'sh.000001',[5,20],datetime.fromisoformat(days[-1]+'T17:00:00+08:00'))
        for row,h in zip(result['outcomes'],[5,20]):
            self.assertEqual(row['target_session'],days[h])
            self.assertEqual(row['excess_pct'],format((Decimal('1.01')**h-1)*100,'.4f'))
        # Calendar is known early but prices are only available after receipt.
        cal=data['evidence'][-1]
        for k in ('published_at','available_at','retrieved_at','as_of'):
            cal[k]=days[0]+'T16:00:00+08:00'
        result=evaluate_horizons([data],days[0],['sh.600001'],'sh.000001',[5,20],datetime.fromisoformat(days[5]+'T17:00:00+08:00'))
        self.assertEqual([r['status'] for r in result['outcomes']],['UNKNOWN','PENDING'])

    def test_r1_reorders_only_with_complete_prior_group_feedback(self):
        from a_share_research.core.feedback_metrics import build_feedback
        data, cutoff = market_fixture()
        candidates=[]
        for i in range(4):
            code=f"sh.60000{i+1}"
            if i>=2:
                e=copy.deepcopy(data['evidence'][i-1])
                e['evidence_id']=code
                e['instrument']['code']=code
                for b in e['calculation_window']:
                    b['code']=code
                e['latest']['code']=code
                # Reverse the last session outcome, retaining the previous-session order.
                e['latest']['close']=e['calculation_window'][-1]['close']='80' if i==2 else '120'
                data['evidence'].append(e)
            candidates.append({'candidate_id':str(i),'security_id':'CN.SH.'+code[3:],
                               'theme_id':'A' if i<2 else 'B','decision':'observe','role':'core'})
        result=build_feedback(data,candidates,cutoff,2)
        self.assertEqual(result['experiment']['R0'],['0','1','2','3'])
        self.assertEqual(result['experiment']['R1'],['2','3','0','1'])
        self.assertFalse(result['experiment']['formal_sample'])

    def test_shadow_ranking_has_one_prior_cohort_change_and_no_future_inputs(self):
        from a_share_research.core.feedback_metrics import build_feedback
        data, cutoff = market_fixture()
        # Previous session's leader 600002 now weakens; the fixed group cannot be relabeled.
        candidates = [{"candidate_id":str(i),"security_id":"CN.SH."+code,
                       "theme_id":"theme","decision":"observe","role":"core"}
                      for i,code in enumerate(["600001","600002"])]
        result = build_feedback(data,candidates,cutoff,1)
        self.assertEqual(result["themes"][0]["prior_cohorts"]["strong"],["sh.600002"])
        self.assertEqual(result["themes"][0]["strong_feedback"]["median_excess_pct"],"-20.0000")
        self.assertEqual(result["experiment"]["status"],"COMPUTED_NOT_VALIDATED")
        data["evidence"][2]["available_at"] = "2099-01-01T00:00:00+08:00"
        result = build_feedback(data,candidates,cutoff,1)
        self.assertEqual(result["experiment"]["status"],"UNKNOWN_NO_REORDER")
        self.assertEqual(result["experiment"]["R0"],result["experiment"]["R1"])

    def test_horizon_uses_calendar_and_reference_returns_not_raw_close_ratio(self):
        from a_share_research.core.feedback_metrics import evaluate_horizons
        data, cutoff = market_fixture()
        result = evaluate_horizons([data], "2026-08-11", ["sh.600001"], "sh.000001", [1], cutoff)
        row = result["outcomes"][0]
        self.assertEqual(row["target_session"], "2026-08-13")
        self.assertEqual(row["excess_pct"], "-10.0000")

    def test_missing_calendar_does_not_guess_weekdays(self):
        from a_share_research.core.feedback_metrics import evaluate_horizons
        data, cutoff = market_fixture()
        data["evidence"].pop()
        result = evaluate_horizons([data], "2026-08-11", ["sh.600001"], "sh.000001", [1], cutoff)
        self.assertEqual(result["outcomes"][0]["status"], "UNKNOWN")

    def test_future_evidence_and_missing_or_suspended_members_remain_visible(self):
        from a_share_research.core.feedback_metrics import evaluate_horizons
        data, cutoff = market_fixture()
        for field, value in [("preclose", None), ("trade_status", "0")]:
            broken = copy.deepcopy(data)
            broken["evidence"][1]["calculation_window"][2][field] = value
            row = evaluate_horizons([broken], "2026-08-11", ["sh.600001","sh.600099"],
                                    "sh.000001", [1], cutoff)["outcomes"]
            self.assertEqual(len(row), 2)
            self.assertTrue(all(x["status"] == "UNKNOWN" for x in row))
        data["evidence"][1]["available_at"] = "2099-01-01T00:00:00+08:00"
        row = evaluate_horizons([data], "2026-08-11", ["sh.600001"], "sh.000001", [1], cutoff)
        self.assertEqual(row["outcomes"][0]["status"], "UNKNOWN")


    def test_conflicting_observations_are_not_overwritten(self):
        from a_share_research.core.feedback_metrics import evaluate_horizons
        data, cutoff = market_fixture()
        other = copy.deepcopy(data)
        other["snapshot_id"] = "revision"
        other["evidence"][1]["calculation_window"][2]["close"] = "300"
        row = evaluate_horizons([data,other], "2026-08-11", ["sh.600001"], "sh.000001", [1], cutoff)
        self.assertEqual(row["outcomes"][0]["status"], "UNKNOWN")


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        ReadAndFreshnessTests.setUp(self)
        from a_share_research.core.snapshot import load_snapshot
        from a_share_research.core.contracts import RunRequest
        self.result = run_research(self.request,self.root)
        self.packet = verified_research_packet(self.root,self.result["artifact_id"])
        data = copy.deepcopy(load_snapshot(self.root,RunRequest.from_dict(self.request)).data)
        data["snapshot_id"] = "follow-up"
        target = self.root / "data/normalized/follow-up/snapshot.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(data))
        self.review_request = {"artifact_id":self.result["artifact_id"],"snapshot_ids":["follow-up"]}

    def test_review_retains_exclusions_deduplicates_and_recovers_missing_report(self):
        from a_share_research.core.feedback import review_feedback, read_feedback
        first = review_feedback(self.review_request,self.root)
        report = self.root / first["report_path"]
        original = report.read_text()
        report.unlink()
        again = review_feedback(self.review_request,self.root)
        self.assertTrue(again["reused"])
        self.assertEqual(report.read_text(),original)
        parts,offset = [],0
        while True:
            page = read_feedback({"record_id":first["record_id"],"max_chars":500,"offset":offset},self.root)
            parts.append(page["content"])
            if not page["truncated"]:
                break
            offset = page["next_offset"]
        record = json.loads("".join(parts))
        self.assertEqual(len(record["candidate_results"]),len(self.packet["all_decisions"]))
        self.assertIn("exclude",[c["original_decision"] for c in record["candidate_results"]])
        self.assertFalse(record["formal_sample"])
        report.write_text(original + "tamper")
        with self.assertRaisesRegex(RuntimeError,"feedback report integrity"):
            read_feedback({"record_id":first["record_id"]},self.root)

    def test_existing_report_is_verified_independently_of_new_renderer(self):
        from a_share_research.core.feedback import review_feedback, read_feedback
        first = review_feedback(self.review_request,self.root)
        report = self.root / first['report_path']
        original = report.read_text()
        with patch('a_share_research.core.feedback._report', return_value='new renderer version'):
            read_feedback({'record_id':first['record_id']},self.root)
            self.assertEqual(report.read_text(),original)
            report.unlink()
            with self.assertRaisesRegex(RuntimeError,'feedback report integrity'):
                read_feedback({'record_id':first['record_id']},self.root)
            self.assertFalse(report.exists())

    def test_hypothesis_revision_is_append_only_and_float_horizon_is_rejected(self):
        from a_share_research.core.feedback import register_hypothesis, read_feedback
        from a_share_research.core.contracts import ContractError
        request = {"artifact_id":self.result["artifact_id"],"candidate_id":self.packet["all_decisions"][0]["candidate_id"],
                   "kind":"price_feedback","horizon_sessions":1,"expectation":"事前研究假设",
                   "counterexample":"反例","invalidation_condition":"证伪条件"}
        with self.assertRaises(ContractError):
            register_hypothesis(request | {"horizon_sessions":1.0},self.root)
        first = register_hypothesis(request,self.root)
        second = register_hypothesis(request | {"expectation":"修订研究假设","supersedes_id":first["record_id"],
                                              "revision_reason":"新增证据待复核"},self.root)
        self.assertNotEqual(first["record_id"],second["record_id"])
        record = json.loads(read_feedback({"record_id":second["record_id"]},self.root)["content"])
        self.assertEqual(record["supersedes_id"],first["record_id"])
        self.assertEqual(record["semantic_result"],"UNKNOWN")
        with sqlite3.connect(self.root / "data/stock_research.sqlite3") as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError,"append-only"):
                db.execute("DELETE FROM cn_feedback_records")

    def test_review_refuses_mixed_fixture_and_real_inputs(self):
        from a_share_research.core.feedback import review_feedback
        from a_share_research.core.contracts import ContractError
        target = self.root / "data/normalized/follow-up/snapshot.json"
        data = json.loads(target.read_text())
        data.update(data_mode="snapshot",pit_quality="RECONSTRUCTED_NON_PIT")
        target.write_text(json.dumps(data))
        with self.assertRaisesRegex(ContractError,"mix fixture and real"):
            review_feedback(self.review_request,self.root)


if __name__ == "__main__":
    unittest.main()
