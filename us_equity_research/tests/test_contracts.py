from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from us_equity_research.core.contracts import ArtifactReadRequest, ContractError, RunRequest
from us_equity_research.core.snapshot import load_snapshot, validate_snapshot


class RunRequestTests(unittest.TestCase):
    def test_market_is_fixed_to_us(self) -> None:
        with self.assertRaisesRegex(ContractError, "permanently bound"):
            RunRequest.from_dict(
                {
                    "schema_version": "0.1",
                    "market": "CN",
                    "workflow": "daily_report",
                    "decision_at": "2026-08-16T08:30:00-04:00",
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

    def test_daily_report_workflow_contract(self) -> None:
        base = {
            "schema_version": "0.1",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00-04:00",
            "snapshot": {"selector": "latest"},
        }
        request = RunRequest.from_dict(base)
        self.assertEqual(request.to_dict()["market"], "US")

        with self.assertRaisesRegex(ContractError, "subject is not allowed"):
            RunRequest.from_dict({**base, "subject": "AI"})
        with self.assertRaisesRegex(ContractError, "symbol is not allowed"):
            RunRequest.from_dict({**base, "symbol": "AAPL"})

    def test_theme_research_workflow_contract(self) -> None:
        base = {
            "schema_version": "0.1",
            "workflow": "theme_research",
            "decision_at": "2026-08-16T08:30:00-04:00",
            "snapshot": {"selector": "demo"},
            "subject": "cloud",
        }
        request = RunRequest.from_dict(base)
        self.assertEqual(request.subject, "cloud")

        with self.assertRaisesRegex(ContractError, "subject is required"):
            RunRequest.from_dict({k: v for k, v in base.items() if k != "subject"})
        with self.assertRaisesRegex(ContractError, "symbol is not allowed"):
            RunRequest.from_dict({**base, "symbol": "AAPL"})

    def test_stock_research_workflow_contract(self) -> None:
        base = {
            "schema_version": "0.1",
            "workflow": "stock_research",
            "decision_at": "2026-08-16T08:30:00-04:00",
            "snapshot": {"selector": "id", "snapshot_id": "us-example-20260816"},
            "symbol": "BRK.B",
        }
        request = RunRequest.from_dict(base)
        self.assertEqual(request.symbol, "BRK.B")

        with self.assertRaisesRegex(ContractError, "symbol is required"):
            RunRequest.from_dict({k: v for k, v in base.items() if k != "symbol"})
        with self.assertRaisesRegex(ContractError, "subject is not allowed"):
            RunRequest.from_dict({**base, "subject": "banks"})

    def test_snapshot_selector_rules_and_unknown_fields(self) -> None:
        base = {
            "schema_version": "0.1",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00-04:00",
        }
        self.assertEqual(
            RunRequest.from_dict(
                {
                    **base,
                    "snapshot": {"selector": "id", "snapshot_id": "us-example-20260816"},
                }
            ).snapshot_id,
            "us-example-20260816",
        )

        with self.assertRaisesRegex(ContractError, "snapshot.snapshot_id is required"):
            RunRequest.from_dict({**base, "snapshot": {"selector": "id"}})
        with self.assertRaisesRegex(ContractError, "only allowed when selector=id"):
            RunRequest.from_dict(
                {
                    **base,
                    "snapshot": {"selector": "latest", "snapshot_id": "us-example-20260816"},
                }
            )
        with self.assertRaisesRegex(ContractError, "unsupported fields"):
            RunRequest.from_dict(
                {
                    **base,
                    "snapshot": {"selector": "demo"},
                    "unexpected": "value",
                }
            )

    def test_top_n_and_symbol_validation_are_strict(self) -> None:
        base = {
            "schema_version": "0.1",
            "workflow": "stock_research",
            "decision_at": "2026-08-16T08:30:00-04:00",
            "snapshot": {"selector": "demo"},
        }
        for symbol in ("AAPL", "BRK.B", "RDS-A"):
            request = RunRequest.from_dict({**base, "symbol": symbol})
            self.assertEqual(request.symbol, symbol)

        invalid_symbols = ("aapl", "BRK.B ", "RDS/A", "../AAPL", "ABCDEFGHIJKLMNOQ")
        for symbol in invalid_symbols:
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ContractError, "symbol"):
                RunRequest.from_dict({**base, "symbol": symbol})

        for top_n in (0, 21, True):
            with self.subTest(top_n=top_n), self.assertRaisesRegex(ContractError, "top_n"):
                RunRequest.from_dict({**base, "symbol": "AAPL", "top_n": top_n})

    def test_run_request_schema_matches_contract(self) -> None:
        schema = json.loads(
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("schemas", "run-request.schema.json")
            .read_text(encoding="utf-8")
        )

        self.assertEqual(schema["properties"]["schema_version"]["const"], "0.1")
        self.assertEqual(schema["properties"]["market"]["const"], "US")
        self.assertEqual(
            schema["properties"]["symbol"]["pattern"],
            "^[A-Z][A-Z0-9.-]{0,14}$",
        )
        self.assertEqual(
            schema["properties"]["snapshot"]["properties"]["selector"]["enum"],
            ["demo", "latest", "id"],
        )


class ArtifactReadRequestTests(unittest.TestCase):
    def test_artifact_id_must_be_safe(self) -> None:
        request = ArtifactReadRequest.from_dict(
            {
                "artifact_id": "us-artifact-demo_01",
                "section": "summary",
            }
        )
        self.assertEqual(request.artifact_id, "us-artifact-demo_01")

        for artifact_id in ("../secret", "has/slash", "", "a" * 129):
            with (
                self.subTest(artifact_id=artifact_id),
                self.assertRaisesRegex(ContractError, "artifact_id"),
            ):
                ArtifactReadRequest.from_dict(
                    {
                        "artifact_id": artifact_id,
                        "section": "summary",
                    }
                )

    def test_artifact_result_schema_matches_market_and_identifier_policy(self) -> None:
        schema = json.loads(
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("schemas", "artifact-result.schema.json")
            .read_text(encoding="utf-8")
        )

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "0.1")
        self.assertEqual(schema["properties"]["market"]["const"], "US")
        self.assertEqual(
            schema["properties"]["artifact_id"]["pattern"],
            "^(?!\\.)(?!.*\\.\\.)[A-Za-z0-9._-]{1,128}$",
        )


class SnapshotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        resource = files("us_equity_research.fixtures").joinpath("demo_snapshot.json")
        with resource.open("r", encoding="utf-8") as handle:
            cls.demo = json.load(handle)

    def test_demo_snapshot_is_valid_and_explicitly_fixture(self) -> None:
        snapshot = validate_snapshot(self.demo)
        self.assertEqual(snapshot.data_mode, "fixture")
        self.assertEqual(snapshot.pit_quality, "FIXTURE")
        self.assertEqual(snapshot.data["market"], "US")
        self.assertIn("EV-FUTURE-001", snapshot.evidence_by_id)
        decision_at = datetime.fromisoformat("2026-08-16T08:30:00-04:00")
        demoa_revenue_facts = [
            snapshot.financial_facts_by_id[fact_id]
            for fact_id in snapshot.data["themes"][0]["candidates"][0]["financial_fact_refs"]
            if snapshot.financial_facts_by_id[fact_id]["security_id"] == "US.DEMOA"
            and snapshot.financial_facts_by_id[fact_id]["metric"] == "revenue_ttm"
        ]
        self.assertGreaterEqual(len(demoa_revenue_facts), 2)
        self.assertEqual(
            len({fact["period_end"] for fact in demoa_revenue_facts}),
            len(demoa_revenue_facts),
        )
        self.assertTrue(
            all(
                datetime.fromisoformat(fact["available_at"]) <= decision_at
                for fact in demoa_revenue_facts
            )
        )
        self.assertTrue(all(fact["evidence_ref"] == "EV-SEC-001" for fact in demoa_revenue_facts))

    def test_snapshot_schema_matches_us_contract(self) -> None:
        schema = json.loads(
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("schemas", "snapshot.schema.json")
            .read_text(encoding="utf-8")
        )

        self.assertEqual(schema["properties"]["schema_version"]["const"], "0.1")
        self.assertEqual(schema["properties"]["market"]["const"], "US")
        self.assertEqual(
            schema["properties"]["pit_quality"]["enum"],
            ["P1", "P2", "P3", "RECONSTRUCTED_NON_PIT", "FIXTURE"],
        )
        self.assertEqual(
            schema["properties"]["financial_facts"]["items"]["properties"]["metric"]["enum"],
            [
                "revenue_ttm",
                "operating_income_ttm",
                "free_cash_flow_ttm",
                "cash_and_equivalents",
                "total_debt",
                "diluted_shares",
                "close_price",
            ],
        )

    def test_fixture_and_snapshot_pit_quality_rules_are_enforced(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["pit_quality"] = "P1"
        with self.assertRaisesRegex(
            ContractError, "fixture snapshots must use pit_quality=FIXTURE"
        ):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["data_mode"] = "snapshot"
        with self.assertRaisesRegex(
            ContractError, "non-fixture snapshots must not use pit_quality=FIXTURE"
        ):
            validate_snapshot(invalid)

    def test_unknown_references_and_duplicate_ids_are_rejected(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["themes"][0]["candidates"][0]["financial_fact_refs"].append("FACT-NOT-FOUND")
        with self.assertRaisesRegex(ContractError, "unknown financial fact"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["evidence"].append(copy.deepcopy(invalid["evidence"][0]))
        with self.assertRaisesRegex(ContractError, "must contain unique values"):
            validate_snapshot(invalid)

    def test_unknown_fields_are_rejected_at_every_snapshot_layer(self) -> None:
        scenarios = [
            ("snapshot", lambda payload: payload.update({"unexpected_root": True})),
            (
                "snapshot.evidence\\[0\\]",
                lambda payload: payload["evidence"][0].update({"unexpected_evidence": True}),
            ),
            (
                "snapshot.financial_facts\\[0\\]",
                lambda payload: payload["financial_facts"][0].update({"unexpected_fact": True}),
            ),
            (
                "snapshot.market_context",
                lambda payload: payload["market_context"].update(
                    {"unexpected_market_context": True}
                ),
            ),
            (
                "snapshot.themes\\[0\\]",
                lambda payload: payload["themes"][0].update({"unexpected_theme": True}),
            ),
            (
                "snapshot.themes\\[0\\]\\.dimensions\\.materiality",
                lambda payload: payload["themes"][0]["dimensions"]["materiality"].update(
                    {"unexpected_dimension": True}
                ),
            ),
            (
                "snapshot.themes\\[0\\]\\.candidates\\[0\\]",
                lambda payload: payload["themes"][0]["candidates"][0].update(
                    {"unexpected_candidate": True}
                ),
            ),
            (
                "snapshot.themes\\[0\\]\\.candidates\\[0\\]\\.bull_case",
                lambda payload: payload["themes"][0]["candidates"][0]["bull_case"].update(
                    {"unexpected_bull_case": True}
                ),
            ),
            (
                "snapshot.themes\\[0\\]\\.candidates\\[0\\]\\.bear_case",
                lambda payload: payload["themes"][0]["candidates"][0]["bear_case"].update(
                    {"unexpected_bear_case": True}
                ),
            ),
            (
                "snapshot.themes\\[0\\]\\.candidates\\[0\\]\\.risk_verdict",
                lambda payload: payload["themes"][0]["candidates"][0]["risk_verdict"].update(
                    {"unexpected_risk_verdict": True}
                ),
            ),
        ]

        for path_pattern, mutate in scenarios:
            invalid = copy.deepcopy(self.demo)
            mutate(invalid)
            with (
                self.subTest(path_pattern=path_pattern),
                self.assertRaisesRegex(ContractError, path_pattern),
            ):
                validate_snapshot(invalid)

    def test_evidence_timelines_and_required_fields_are_enforced(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["available_at"] = "2026-08-15T16:10:00"
        with self.assertRaisesRegex(ContractError, "timezone"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["source_level"] = "blog"
        with self.assertRaisesRegex(ContractError, "source_level must be one of"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["available_at"] = "2026-08-14T15:00:00-04:00"
        with self.assertRaisesRegex(
            ContractError, "published_at must not be later than available_at"
        ):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][1]["as_of"] = "2026-08-16T16:00:00-04:00"
        with self.assertRaisesRegex(ContractError, "as_of must not be later than available_at"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["evidence"][0]["retrieved_at"] = "2026-08-17T08:00:00-04:00"
        with self.assertRaisesRegex(ContractError, "snapshot.retrieved_at"):
            validate_snapshot(invalid)

    def test_financial_facts_enforce_metric_source_and_numeric_rules(self) -> None:
        invalid = copy.deepcopy(self.demo)
        invalid["financial_facts"][0]["value"] = True
        with self.assertRaisesRegex(ContractError, "must be a finite number"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["financial_facts"][0]["available_at"] = "2026-08-17T08:00:00-04:00"
        with self.assertRaisesRegex(ContractError, "financial_facts\\[0\\]\\.available_at"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["financial_facts"][0]["evidence_ref"] = "EV-MARKET-001"
        with self.assertRaisesRegex(ContractError, "must reference official evidence"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["financial_facts"][-1]["evidence_ref"] = "EV-SEC-001"
        with self.assertRaisesRegex(ContractError, "close_price must reference structured_market"):
            validate_snapshot(invalid)

    def test_market_context_requires_rates_and_valid_evidence(self) -> None:
        invalid = copy.deepcopy(self.demo)
        del invalid["market_context"]["rates"]
        with self.assertRaisesRegex(ContractError, "market_context.rates"):
            validate_snapshot(invalid)

        invalid = copy.deepcopy(self.demo)
        invalid["market_context"]["evidence_refs"] = ["EV-NOT-FOUND"]
        with self.assertRaisesRegex(ContractError, "unknown evidence"):
            validate_snapshot(invalid)

    def test_latest_snapshot_validates_each_candidate_and_orders_by_retrieved_at_then_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            base = workspace / "data" / "normalized" / "us"

            invalid = copy.deepcopy(self.demo)
            invalid["snapshot_id"] = "bad-latest"
            invalid["data_mode"] = "snapshot"
            invalid["pit_quality"] = "FIXTURE"
            bad_path = base / "zz-bad" / "snapshot.json"
            bad_path.parent.mkdir(parents=True)
            bad_path.write_text(json.dumps(invalid), encoding="utf-8")

            request = RunRequest.from_dict(
                {
                    "schema_version": "0.1",
                    "workflow": "daily_report",
                    "decision_at": "2026-08-16T12:00:00-04:00",
                    "snapshot": {"selector": "latest"},
                }
            )
            with self.assertRaisesRegex(
                ContractError, "non-fixture snapshots must not use pit_quality=FIXTURE"
            ):
                load_snapshot(workspace, request)
            bad_path.unlink()

            invalid = copy.deepcopy(self.demo)
            invalid["snapshot_id"] = "aa-same-time"
            invalid["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][0]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][1]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][2]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][3]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][4]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][5]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][6]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][7]["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            invalid["evidence"][8]["retrieved_at"] = "2026-08-16T10:05:00-04:00"

            winner = copy.deepcopy(self.demo)
            winner["snapshot_id"] = "zz-same-time"
            winner["retrieved_at"] = "2026-08-16T10:05:00-04:00"
            for evidence in winner["evidence"]:
                evidence["retrieved_at"] = "2026-08-16T10:05:00-04:00"

            for directory, snapshot in (("b-mid", invalid), ("a-top", winner)):
                path = base / directory / "snapshot.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(snapshot), encoding="utf-8")

            self.assertEqual(load_snapshot(workspace, request).snapshot_id, "zz-same-time")

    def test_snapshot_id_lookup_uses_safe_us_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            snapshot_path = (
                workspace
                / "data"
                / "normalized"
                / "us"
                / self.demo["snapshot_id"]
                / "snapshot.json"
            )
            snapshot_path.parent.mkdir(parents=True)
            snapshot_path.write_text(json.dumps(self.demo), encoding="utf-8")

            request = RunRequest.from_dict(
                {
                    "schema_version": "0.1",
                    "workflow": "daily_report",
                    "decision_at": "2026-08-16T12:00:00-04:00",
                    "snapshot": {"selector": "id", "snapshot_id": self.demo["snapshot_id"]},
                }
            )
            self.assertEqual(
                load_snapshot(workspace, request).snapshot_id, self.demo["snapshot_id"]
            )


if __name__ == "__main__":
    unittest.main()
