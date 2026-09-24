from __future__ import annotations

import json
import unittest

from a_share_research.core.contracts import DISCLAIMER, ContractError
from a_share_research.core.harness_review import (
    normalize_bear_review,
    normalize_final_judgment,
    render_opportunity_memo,
)

ARTIFACT_ID = "cn-artifact-review-test"
SNAPSHOT_ID = "cn-snapshot-review-test"
DECISION_AT = "2026-08-26T20:30:00+08:00"
EXPECTED_INTEGRITY = {
    "facts": {"content_sha256": "a" * 64, "total_chars": 10, "expected_pages": 1},
    "report": {"content_sha256": "a" * 64, "total_chars": 100, "expected_pages": 1},
    "packet": {"content_sha256": "b" * 64, "total_chars": 100, "expected_pages": 1},
}
EXPECTED_RESEARCH_HISTORY = {
    "theme-test:CN.SH.600000": {
        "run_count": 3,
        "consecutive_continue_research": 2,
        "stale_days": 7,
        "aging_action": "active",
    }
}


def _evidence(
    source_ref: str = "EV-1",
    *,
    source_type: str = "issuer",
) -> dict[str, str]:
    return {
        "source_ref": source_ref,
        "source_url": "https://www.cninfo.com.cn/test",
        "source_type": source_type,
        "published_at": "2026-08-25T18:00:00+08:00",
        "available_at": "2026-08-25T18:00:00+08:00",
        "summary": "公司披露了可核验的合成测试事项。",
    }


def _assessment(
    value: str,
    *,
    baseline: str | None = None,
    source_type: str = "issuer",
) -> dict[str, object]:
    result: dict[str, object] = {
        "assessment": value,
        "reason": "由正式来源支持的合成测试判断。",
        "evidence_refs": [_evidence(source_type=source_type)],
    }
    if baseline is not None:
        result["baseline"] = baseline
    return result


def _economic_assessment() -> dict[str, object]:
    return {
        **_assessment("medium"),
        "magnitude": {
            "status": "quantified",
            "basis": "revenue",
            "numerator": "6",
            "denominator": "10",
            "ratio": "0.6",
            "formula": "合成项目金额 / 合成历史收入",
        },
    }


def _final_payload() -> dict[str, object]:
    dimensions = {name: _assessment("medium") for name in ("big", "new", "many", "durable", "timely")}
    return {
        "artifact_id": ARTIFACT_ID,
        "snapshot_id": SNAPSHOT_ID,
        "decision_at": DECISION_AT,
        "integrity": {
            "report_content_sha256": "a" * 64,
            "packet_content_sha256": "b" * 64,
            "pages_read": 2,
        },
        "executive_summary": "一个候选进入深度研究层，主要约束是利润传导仍待验证。",
        "candidate_rankings": [
            {
                "rank": 1,
                "candidate_id": "theme-test:CN.SH.600000",
                "symbol": "600000",
                "name": "合成测试公司",
                "canonical_state": "continue_research",
                "attention_bucket": "deep_dive",
                "opportunity_view": "positive",
                "history_context": {
                    "candidate_id": "theme-test:CN.SH.600000",
                    "latest_decision": "continue_research",
                    "run_count": 3,
                    "consecutive_continue_research": 2,
                    "stale_days": 7,
                    "aging_action": "active",
                },
                "five_dimensions": dimensions,
                "new_information": _assessment("strong"),
                "economic_impact": _economic_assessment(),
                "expectation_gap": _assessment("positive", baseline="historical"),
                "market_pricing": _assessment(
                    "confirmed",
                    source_type="structured_market",
                ),
                "impact_chain": [
                    {"step": "正式事件", "status": "supported", "evidence_refs": [_evidence()]},
                    {"step": "产业需求", "status": "partial", "evidence_refs": [_evidence()]},
                    {"step": "公司现金流", "status": "unknown", "evidence_refs": []},
                ],
                "strongest_support": {"claim": "正式公告支持事件存在。", "evidence_refs": [_evidence()]},
                "strongest_counterevidence": {"claim": "利润确认仍为 UNKNOWN。", "evidence_refs": []},
                "next_catalyst": {
                    "description": "公告明确的项目节点",
                    "scheduled_at": "2026-09-20T09:00:00+08:00",
                    "verification_rule": "检查正式进展公告与回款。",
                    "evidence_refs": [_evidence()],
                },
                "invalidation_conditions": ["项目节点未兑现且无正式延期说明"],
                "unknowns": ["利润率"],
            }
        ],
        "new_opportunities": [],
        "collection_requests": [],
        "outcome_feedback": {"sample_count": 0, "summary": "尚无可用样本。"},
        "audit_notes": ["供应商为重建型时间语义。"],
    }


class HarnessReviewTests(unittest.TestCase):
    def test_bear_parser_accepts_prose_wrapper_but_normalizes_only_the_contract(self) -> None:
        payload = {
            "artifact_id": ARTIFACT_ID,
            "decision_at": DECISION_AT,
            "review_mode": "independent_bear",
            "integrity": {"content_sha256": "a" * 64, "total_chars": 10, "pages_read": 1},
            "candidates": [
                {
                    "candidate_id": "theme-test:CN.SH.600000",
                    "symbol": "600000",
                    "contradicting_evidence": [],
                    "alternative_explanations": [],
                    "invalidation_tests": [],
                    "unknowns": ["UNKNOWN"],
                    "bear_confidence": 0.4,
                }
            ],
            "global_data_risks": [],
        }
        raw = "已完成检查。\n" + json.dumps(payload, ensure_ascii=False) + "\n请复核。"
        self.assertEqual(
            normalize_bear_review(
                raw,
                artifact_id=ARTIFACT_ID,
                decision_at=DECISION_AT,
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
            ),
            payload,
        )

    def test_bear_parser_tolerates_braces_in_the_surrounding_prose(self) -> None:
        # Regression guard for the machine-local harness fix that located the review payload
        # with "first { .. last }". That span breaks as soon as the narration itself contains
        # a brace, and the whole run then exits 3. Scanning for the single object that actually
        # satisfies the contract is what makes both cases work.
        payload = {
            "artifact_id": ARTIFACT_ID,
            "decision_at": DECISION_AT,
            "review_mode": "independent_bear",
            "integrity": {"content_sha256": "a" * 64, "total_chars": 10, "pages_read": 1},
            "candidates": [
                {
                    "candidate_id": "theme-test:CN.SH.600000",
                    "symbol": "600000",
                    "contradicting_evidence": [],
                    "alternative_explanations": [],
                    "invalidation_tests": [],
                    "unknowns": ["UNKNOWN"],
                    "bear_confidence": 0.4,
                }
            ],
            "global_data_risks": [],
        }
        raw = "检查完成 {草稿}，正式结果如下：\n" + json.dumps(payload, ensure_ascii=False) + "\n{待复核}"
        self.assertEqual(
            normalize_bear_review(
                raw,
                artifact_id=ARTIFACT_ID,
                decision_at=DECISION_AT,
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
            ),
            payload,
        )

    def test_final_contract_renders_decision_first_memo(self) -> None:
        payload = _final_payload()
        normalized = normalize_final_judgment(
            "说明\n" + json.dumps(payload, ensure_ascii=False),
            artifact_id=ARTIFACT_ID,
            snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            bear_candidates={"theme-test:CN.SH.600000": "600000"},
            canonical_candidates={
                "theme-test:CN.SH.600000": ("600000", "continue_research")
            },
            expected_integrity=EXPECTED_INTEGRITY,
            expected_research_history=EXPECTED_RESEARCH_HISTORY,
            expected_outcome_sample_count=0,
        )
        memo = render_opportunity_memo(
            normalized,
            {
                "status": "ok",
                "material_change_count": 0,
                "severity_counts": {"critical": 0, "high": 0, "info": 12},
            },
        )
        self.assertIn("今天最值得花时间的候选", memo)
        self.assertIn("题材五维", memo)
        self.assertIn("预期差", memo)
        self.assertNotIn("代码更新", memo)
        self.assertEqual(memo.strip().splitlines()[-1], DISCLAIMER)

    def test_scheduled_catalyst_without_formal_evidence_is_rejected(self) -> None:
        payload = _final_payload()
        payload["candidate_rankings"][0]["next_catalyst"]["evidence_refs"] = []  # type: ignore[index]
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_bear_cannot_omit_a_canonical_candidate(self) -> None:
        payload = {
            "artifact_id": ARTIFACT_ID,
            "decision_at": DECISION_AT,
            "review_mode": "independent_bear",
            "integrity": {"content_sha256": "a" * 64, "total_chars": 10, "pages_read": 1},
            "candidates": [],
            "global_data_risks": [],
        }
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_bear_review(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                decision_at=DECISION_AT,
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
            )

    def test_final_model_cannot_rewrite_the_canonical_state(self) -> None:
        payload = _final_payload()
        payload["candidate_rankings"][0]["canonical_state"] = "observe"  # type: ignore[index]
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_secondary_media_cannot_prove_market_pricing(self) -> None:
        payload = _final_payload()
        payload["candidate_rankings"][0]["market_pricing"] = _assessment(  # type: ignore[index]
            "confirmed",
            source_type="secondary",
        )
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_final_economic_magnitude_cannot_use_an_inconsistent_ratio(self) -> None:
        payload = _final_payload()
        magnitude = payload["candidate_rankings"][0]["economic_impact"]["magnitude"]  # type: ignore[index]
        magnitude["ratio"] = "0.9"
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_final_cannot_invent_research_history(self) -> None:
        payload = _final_payload()
        history = payload["candidate_rankings"][0]["history_context"]  # type: ignore[index]
        history["run_count"] = 99
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_final_cannot_invent_outcome_sample_count(self) -> None:
        payload = _final_payload()
        payload["outcome_feedback"]["sample_count"] = 12  # type: ignore[index]
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_deep_dive_requires_a_positive_evidence_contract(self) -> None:
        payload = _final_payload()
        payload["candidate_rankings"][0]["opportunity_view"] = "unclear"  # type: ignore[index]
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )

    def test_memo_does_not_force_a_top_candidate_without_deep_dive_evidence(self) -> None:
        payload = _final_payload()
        candidate = payload["candidate_rankings"][0]  # type: ignore[index]
        candidate["attention_bucket"] = "backlog"
        candidate["opportunity_view"] = "unclear"

        memo = render_opportunity_memo(payload, {"status": "ok"})

        self.assertIn("本次没有满足结构化裁判合同的冻结候选", memo)
        self.assertNotIn("### 1. 合成测试公司", memo)
        self.assertIn("| 1 | 合成测试公司", memo)

    def test_bear_cannot_claim_the_wrong_facts_hash(self) -> None:
        payload = {
            "artifact_id": ARTIFACT_ID,
            "decision_at": DECISION_AT,
            "review_mode": "independent_bear",
            "integrity": {"content_sha256": "b" * 64, "total_chars": 10, "pages_read": 1},
            "candidates": [
                {
                    "candidate_id": "theme-test:CN.SH.600000",
                    "symbol": "600000",
                    "contradicting_evidence": [],
                    "alternative_explanations": [],
                    "invalidation_tests": [],
                    "unknowns": [],
                    "bear_confidence": 0.4,
                }
            ],
            "global_data_risks": [],
        }
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_bear_review(
                json.dumps(payload, ensure_ascii=False),
                artifact_id=ARTIFACT_ID,
                decision_at=DECISION_AT,
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
            )

    def test_two_valid_contract_objects_are_rejected(self) -> None:
        payload = _final_payload()
        raw = json.dumps(payload, ensure_ascii=False) + "\n" + json.dumps(payload, ensure_ascii=False)
        with self.assertRaisesRegex(ContractError, "exactly one valid contract"):
            normalize_final_judgment(
                raw,
                artifact_id=ARTIFACT_ID,
                snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                bear_candidates={"theme-test:CN.SH.600000": "600000"},
                canonical_candidates={
                    "theme-test:CN.SH.600000": ("600000", "continue_research")
                },
                expected_integrity=EXPECTED_INTEGRITY,
                expected_research_history=EXPECTED_RESEARCH_HISTORY,
                expected_outcome_sample_count=0,
            )


if __name__ == "__main__":
    unittest.main()
