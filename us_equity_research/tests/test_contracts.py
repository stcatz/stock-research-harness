from __future__ import annotations

import json
import unittest
from pathlib import Path

from us_equity_research.core.contracts import ArtifactReadRequest, ContractError, RunRequest


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

        self.assertEqual(schema["properties"]["schema_version"]["const"], "0.1")
        self.assertEqual(schema["properties"]["market"]["const"], "US")
        self.assertEqual(
            schema["properties"]["artifact_id"]["pattern"],
            "^[A-Za-z0-9._-]{1,128}$",
        )


if __name__ == "__main__":
    unittest.main()
