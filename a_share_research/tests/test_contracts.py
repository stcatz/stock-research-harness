from __future__ import annotations

import copy
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from a_share_research.core.contracts import ContractError, RunRequest
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

    def test_unknown_evidence_reference_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["themes"][0]["evidence_refs"].append("EV-NOT-FOUND")
        with self.assertRaisesRegex(ContractError, "unknown evidence"):
            validate_snapshot(invalid)

    def test_all_evidence_requires_a_timezone(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["available_at"] = "2026-08-15T15:20:00"
        with self.assertRaisesRegex(ContractError, "timezone"):
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
