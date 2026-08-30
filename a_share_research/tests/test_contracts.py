from __future__ import annotations

import copy
import json
import tempfile
import tomllib
import unittest
from importlib.resources import files
from pathlib import Path

from a_share_research.core.contracts import ContractError, RunRequest
from a_share_research.core.engine import METHOD_ID
from a_share_research.core.snapshot import load_snapshot, validate_snapshot


class RunRequestTests(unittest.TestCase):
    def test_market_is_fixed_to_cn(self) -> None:
        with self.assertRaisesRegex(ContractError, "permanently bound"):
            RunRequest.from_dict(
                {
                    "schema_version": "0.1",
                    "market": "US",
                    "workflow": "daily_report",
                    "decision_at": "2026-08-16T08:30:00+08:00",
                    "snapshot": {"selector": "demo"},
                }
            )

    def test_decision_at_requires_timezone(self) -> None:
        with self.assertRaisesRegex(ContractError, "timezone"):
            RunRequest.from_dict(
                {
                    "schema_version": "0.1",
                    "workflow": "daily_report",
                    "decision_at": "2026-08-16T08:30:00",
                    "snapshot": {"selector": "demo"},
                }
            )

    def test_theme_and_stock_workflows_require_subject(self) -> None:
        base = {
            "schema_version": "0.1",
            "decision_at": "2026-08-16T08:30:00+08:00",
            "snapshot": {"selector": "demo"},
        }
        with self.assertRaisesRegex(ContractError, "subject is required"):
            RunRequest.from_dict({**base, "workflow": "theme_research"})
        with self.assertRaisesRegex(ContractError, "symbol is required"):
            RunRequest.from_dict({**base, "workflow": "stock_research"})

    def test_irrelevant_or_unknown_inputs_are_rejected(self) -> None:
        base = {
            "schema_version": "0.1",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00+08:00",
            "snapshot": {"selector": "demo"},
        }
        with self.assertRaisesRegex(ContractError, "subject is only allowed"):
            RunRequest.from_dict({**base, "subject": "ignored"})
        with self.assertRaisesRegex(ContractError, "unsupported fields"):
            RunRequest.from_dict({**base, "output_path": "/tmp/report.md"})
        with self.assertRaisesRegex(ContractError, "only allowed when selector=id"):
            RunRequest.from_dict(
                {**base, "snapshot": {"selector": "demo", "snapshot_id": "ignored"}}
            )

    def test_artifact_result_schema_matches_the_paginated_reader(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "artifact-result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("facts", schema["properties"]["section"]["enum"])
        self.assertTrue(
            {
                "cursor",
                "next_cursor",
                "total_chars",
                "content_sha256",
            }.issubset(schema["required"])
        )
        self.assertEqual(schema["properties"]["market"]["const"], "CN")

    def test_v2_method_card_matches_the_canonical_engine_identity(self) -> None:
        method_path = (
            Path(__file__).resolve().parents[1] / "methods" / "a_share_theme_v2.toml"
        )
        with method_path.open("rb") as handle:
            method = tomllib.load(handle)

        self.assertEqual(method["id"], METHOD_ID)
        self.assertEqual(method["evaluation"]["horizons_trading_days"], [5, 20])
        self.assertTrue(
            method["source_policy"]["economic_impact_requires_frozen_primary_content"]
        )


class SnapshotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        resource = files("a_share_research.fixtures").joinpath("demo_snapshot.json")
        with resource.open("r", encoding="utf-8") as handle:
            cls.demo = json.load(handle)

    def test_demo_snapshot_is_valid_and_explicitly_fixture(self) -> None:
        snapshot = validate_snapshot(self.demo)
        self.assertEqual(snapshot.data_mode, "fixture")
        self.assertEqual(snapshot.pit_quality, "FIXTURE")
        self.assertIn("EV-FUTURE-001", snapshot.evidence_by_id)
        self.assertEqual(
            snapshot.evidence_by_id["EV-COMPANY-001"]["document"]["source_document_id"],
            "DEMO-COMPANY-ANNOUNCEMENT-001",
        )

    def test_frozen_document_text_requires_a_matching_hash(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][2]["document"]["text_sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "does not match text"):
            validate_snapshot(invalid)

    def test_unknown_evidence_reference_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["themes"][0]["evidence_refs"].append("EV-NOT-FOUND")
        with self.assertRaisesRegex(ContractError, "unknown evidence"):
            validate_snapshot(invalid)

    def test_v2_opportunity_profile_is_complete_and_legacy_snapshot_stays_replayable(self) -> None:
        invalid = copy.deepcopy(self.demo)
        del invalid["themes"][0]["candidates"][0]["opportunity_profile"]["economic_impact"]
        with self.assertRaisesRegex(ContractError, "missing required fields"):
            validate_snapshot(invalid)

        short_chain = copy.deepcopy(self.demo)
        short_chain["themes"][0]["candidates"][0]["impact_chain"] = short_chain["themes"][0][
            "candidates"
        ][0]["impact_chain"][:2]
        with self.assertRaisesRegex(ContractError, "at least three"):
            validate_snapshot(short_chain)

        legacy = copy.deepcopy(self.demo)
        legacy_candidate = legacy["themes"][0]["candidates"][0]
        del legacy_candidate["opportunity_profile"]
        del legacy_candidate["impact_chain"]
        validated = validate_snapshot(legacy)
        self.assertNotIn("opportunity_profile", validated.themes[0]["candidates"][0])

    def test_economic_magnitude_ratio_must_match_frozen_inputs(self) -> None:
        invalid = copy.deepcopy(self.demo)
        magnitude = invalid["themes"][0]["candidates"][0]["opportunity_profile"][
            "economic_impact"
        ]["magnitude"]
        magnitude["ratio"] = "0.9"
        with self.assertRaisesRegex(ContractError, "ratio does not match"):
            validate_snapshot(invalid)

    def test_all_evidence_requires_a_timezone(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["available_at"] = "2026-08-15T15:20:00"
        with self.assertRaisesRegex(ContractError, "timezone"):
            validate_snapshot(invalid)

    def test_all_evidence_requires_an_explicit_effective_time(self) -> None:
        invalid = copy.deepcopy(self.demo)
        del invalid["evidence"][0]["effective_at"]
        with self.assertRaisesRegex(ContractError, "effective_at"):
            validate_snapshot(invalid)

    def test_announced_future_effective_event_remains_valid_known_evidence(self) -> None:
        announced = copy.deepcopy(self.demo)
        announced["evidence"][0]["effective_at"] = "2026-09-01T00:00:00+08:00"

        validated = validate_snapshot(announced)

        self.assertEqual(
            validated.data["evidence"][0]["effective_at"],
            "2026-09-01T00:00:00+08:00",
        )

    def test_structured_facts_preserve_unknown_and_reject_fabricated_values(self) -> None:
        valid = copy.deepcopy(self.demo)
        valid["evidence"][0]["facts"] = [
            {
                "fact_id": "FACT-TURNOVER-UNKNOWN",
                "metric": "turnover_rate",
                "value": None,
                "unit": "percent",
                "status": "unknown",
                "as_of": "2026-08-15T15:00:00+08:00",
                "effective_at": "2026-08-15T15:00:00+08:00",
                "available_at": "2026-08-15T15:20:00+08:00",
                "source_field": "turnover_rate",
            }
        ]
        self.assertIsNone(validate_snapshot(valid).data["evidence"][0]["facts"][0]["value"])

        invalid = copy.deepcopy(valid)
        invalid["evidence"][0]["facts"][0]["value"] = "0"
        with self.assertRaisesRegex(ContractError, "must be null"):
            validate_snapshot(invalid)

    def test_structured_fact_cannot_be_available_after_its_evidence(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["facts"] = [
            {
                "fact_id": "FACT-LATE",
                "metric": "pe_ttm",
                "value": "12.4",
                "unit": "ratio",
                "status": "observed",
                "as_of": "2026-08-15T15:00:00+08:00",
                "effective_at": "2026-08-15T15:00:00+08:00",
                "available_at": "2026-08-16T08:00:00+08:00",
                "source_field": "pe_ttm",
            }
        ]
        with self.assertRaisesRegex(ContractError, "its evidence available_at"):
            validate_snapshot(invalid)

    def test_snapshot_cannot_claim_data_retrieved_after_its_own_capture(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["retrieved_at"] = "2026-08-16T07:30:00+08:00"
        with self.assertRaisesRegex(ContractError, "snapshot.retrieved_at"):
            validate_snapshot(invalid)

    def test_evidence_as_of_cannot_be_after_it_was_available(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["as_of"] = "2026-08-17T15:00:00+08:00"
        with self.assertRaisesRegex(ContractError, "as_of must not be later"):
            validate_snapshot(invalid)

    def test_latest_snapshot_uses_retrieval_time_not_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            older = copy.deepcopy(self.demo)
            older["snapshot_id"] = "old-snapshot"
            newer = copy.deepcopy(self.demo)
            newer["snapshot_id"] = "new-snapshot"
            newer["retrieved_at"] = "2026-08-16T11:05:00+08:00"
            for directory, snapshot in (("z-old", older), ("a-new", newer)):
                path = workspace / "data" / "normalized" / directory / "snapshot.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(snapshot), encoding="utf-8")

            request = RunRequest.from_dict(
                {
                    "schema_version": "0.1",
                    "workflow": "daily_report",
                    "decision_at": "2026-08-16T12:00:00+08:00",
                    "snapshot": {"selector": "latest"},
                }
            )
            self.assertEqual(load_snapshot(workspace, request).snapshot_id, "new-snapshot")


if __name__ == "__main__":
    unittest.main()
