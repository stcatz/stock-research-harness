from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from us_equity_research.core.contracts import ContractError
from us_equity_research.core.pipeline import run_research
from us_equity_research.core.snapshot import validate_snapshot
from us_equity_research.ingest.sec import (
    HttpResponse,
    SecClient,
    collect_sec_snapshot,
)

RETRIEVED_AT = "2026-08-18T12:00:00+00:00"
ACCESSION = "0000123456-26-000001"


def _seed() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "market": "US",
        "as_of": "2026-06-30T23:59:59+00:00",
        "themes": [
            {
                "theme_id": "theme-cloud-infrastructure",
                "name": "Cloud infrastructure",
                "event_type": "earnings_confirmation",
                "stage": "confirming",
                "dimensions": {
                    name: {
                        "assessment": "medium",
                        "reason": f"Researcher-authored {name} hypothesis.",
                    }
                    for name in (
                        "materiality",
                        "novelty",
                        "breadth",
                        "durability",
                        "timeliness",
                    )
                },
                "transmission_chain": [
                    "Demand expands.",
                    "Capacity utilization rises.",
                    "Issuer economics may improve.",
                ],
                "next_catalyst_at": "2026-10-20T20:00:00+00:00",
                "data_gaps": ["Market confirmation is not supplied by the SEC."],
                "candidates": [
                    {
                        "security_id": "US.TEST",
                        "symbol": "TEST",
                        "name": "Test Issuer",
                        "cik": "0000123456",
                        "role": "leader",
                        "thesis": "Cloud demand may improve issuer economics.",
                        "bull_case": "Revenue and operating income may expand.",
                        "bear_case": "Demand may be pulled forward.",
                        "risk_verdict": "The filing must be reviewed by a human.",
                        "invalidation_conditions": ["Revenue contracts year over year."],
                        "data_gaps": ["No licensed market snapshot supplied."],
                        "risk_flags": [],
                        "manual_review_items": ["Read the source filing."],
                    }
                ],
            }
        ],
    }


def _submissions() -> dict[str, object]:
    return {
        "cik": "0000123456",
        "name": "Test Issuer",
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION],
                "acceptanceDateTime": ["2026-02-10T21:15:30.000Z"],
                "filingDate": ["2026-02-10"],
                "reportDate": ["2025-12-31"],
                "form": ["10-K"],
                "primaryDocument": ["test-20251231.htm"],
            }
        },
    }


def _fact(
    value: float,
    *,
    unit: str = "USD",
    start: str | None = "2025-01-01",
    end: str = "2025-12-31",
) -> dict[str, object]:
    result: dict[str, object] = {
        "end": end,
        "val": value,
        "accn": ACCESSION,
        "fy": 2025,
        "fp": "FY",
        "form": "10-K",
        "filed": "2026-02-10",
    }
    if start is not None:
        result["start"] = start
    return result


def _companyfacts() -> dict[str, object]:
    facts: dict[str, object] = {}

    def add(concept: str, value: float, *, unit: str = "USD", instant: bool = False) -> None:
        facts[concept] = {
            "label": concept,
            "description": concept,
            "units": {
                unit: [
                    _fact(
                        value,
                        unit=unit,
                        start=None if instant else "2025-01-01",
                    )
                ]
            },
        }

    add("RevenueFromContractWithCustomerExcludingAssessedTax", 1_000_000_000)
    add("OperatingIncomeLoss", 200_000_000)
    add("NetCashProvidedByUsedInOperatingActivities", 180_000_000)
    add("PaymentsToAcquirePropertyPlantAndEquipment", 30_000_000)
    add("CashAndCashEquivalentsAtCarryingValue", 400_000_000, instant=True)
    add("LongTermDebtCurrent", 20_000_000, instant=True)
    add("LongTermDebtNoncurrent", 80_000_000, instant=True)
    add("WeightedAverageNumberOfDilutedSharesOutstanding", 50_000_000, unit="shares")
    return {
        "cik": 123456,
        "entityName": "Test Issuer",
        "facts": {"us-gaap": facts},
    }


class FakeSecTransport:
    def __init__(
        self,
        *,
        submissions: dict[str, object] | None = None,
        companyfacts: dict[str, object] | None = None,
    ) -> None:
        self.requests: list[object] = []
        self.submissions = submissions or _submissions()
        self.companyfacts = companyfacts or _companyfacts()

    def __call__(self, request: object, timeout: float) -> HttpResponse:
        self.requests.append(request)
        url = request.full_url
        payload = self.submissions if "/submissions/" in url else self.companyfacts
        return HttpResponse(
            body=gzip.compress(json.dumps(payload).encode("utf-8")),
            headers={"content-encoding": "gzip"},
        )


class SecClientTests(unittest.TestCase):
    def test_user_agent_is_mandatory(self) -> None:
        with self.assertRaisesRegex(ValueError, "SEC_USER_AGENT"):
            SecClient(user_agent="")
        with self.assertRaisesRegex(ValueError, "contact email"):
            SecClient(user_agent="Stock Research")

    def test_requests_are_gzip_decoded_and_rate_limited_below_ten_rps(self) -> None:
        now = [0.0]
        sleeps: list[float] = []
        transport = FakeSecTransport()

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        client = SecClient(
            user_agent="Stock Research test@example.com",
            transport=transport,
            monotonic=lambda: now[0],
            sleep=sleep,
        )
        first = client.get_submissions("0000123456")
        second = client.get_companyfacts("0000123456")

        self.assertEqual(first["name"], "Test Issuer")
        self.assertEqual(second["entityName"], "Test Issuer")
        self.assertEqual(len(transport.requests), 2)
        self.assertGreaterEqual(sum(sleeps), 0.1)
        for request in transport.requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["user-agent"], "Stock Research test@example.com")
            self.assertIn("gzip", headers["accept-encoding"])


class SecSnapshotCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.seed_path = self.workspace / "seed.json"
        self.seed_path.write_text(json.dumps(_seed()), encoding="utf-8")
        self.transport = FakeSecTransport()
        self.client = SecClient(
            user_agent="Stock Research test@example.com",
            transport=self.transport,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_collects_official_evidence_and_deterministic_financial_facts(self) -> None:
        result = collect_sec_snapshot(
            workspace=self.workspace,
            seed_path=self.seed_path,
            snapshot_id="us-sec-golden-1",
            retrieved_at=RETRIEVED_AT,
            client=self.client,
        )

        self.assertEqual(result["status"], "published")
        self.assertEqual(
            result["relative_path"], "data/normalized/us/us-sec-golden-1/snapshot.json"
        )
        snapshot_path = self.workspace / result["relative_path"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        validated = validate_snapshot(snapshot)
        self.assertEqual(validated.snapshot_id, "us-sec-golden-1")
        self.assertEqual(snapshot["data_mode"], "snapshot")
        self.assertEqual(snapshot["pit_quality"], "P2")
        self.assertNotIn("fixture_notice", snapshot)

        evidence = snapshot["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["source_document_id"], "sec-000012345626000001")
        self.assertEqual(evidence[0]["available_at"], "2026-02-10T21:18:30+00:00")
        self.assertIn(ACCESSION, evidence[0]["summary"])
        self.assertIn("/Archives/edgar/data/123456/000012345626000001/", evidence[0]["source_url"])

        metrics = {fact["metric"]: fact["value"] for fact in snapshot["financial_facts"]}
        self.assertEqual(metrics["revenue_ttm"], 1_000_000_000)
        self.assertEqual(metrics["operating_income_ttm"], 200_000_000)
        self.assertEqual(metrics["free_cash_flow_ttm"], 150_000_000)
        self.assertEqual(metrics["cash_and_equivalents"], 400_000_000)
        self.assertEqual(metrics["total_debt"], 100_000_000)
        self.assertEqual(metrics["diluted_shares"], 50_000_000)
        self.assertNotIn("close_price", metrics)

        candidate = snapshot["themes"][0]["candidates"][0]
        self.assertEqual(candidate["market_evidence_refs"], [])
        self.assertIn("No licensed market snapshot supplied.", candidate["data_gaps"])
        self.assertIn("NARRATIVE_REQUIRES_HUMAN_VERIFICATION", candidate["risk_flags"])

        run = run_research(
            {
                "schema_version": "0.1",
                "market": "US",
                "workflow": "daily_report",
                "decision_at": "2026-08-18T12:01:00+00:00",
                "snapshot": {"selector": "id", "snapshot_id": "us-sec-golden-1"},
                "top_n": 5,
            },
            self.workspace,
        )
        self.assertEqual(run["counts"], {"observe": 0, "continue_research": 0, "exclude": 1})

    def test_refuses_to_overwrite_an_existing_snapshot(self) -> None:
        kwargs = {
            "workspace": self.workspace,
            "seed_path": self.seed_path,
            "snapshot_id": "immutable",
            "retrieved_at": RETRIEVED_AT,
            "client": self.client,
        }
        collect_sec_snapshot(**kwargs)
        with self.assertRaisesRegex(ContractError, "already exists"):
            collect_sec_snapshot(**kwargs)

    def test_rejects_path_escape_and_symlink_snapshot_target(self) -> None:
        with self.assertRaises(ContractError):
            collect_sec_snapshot(
                workspace=self.workspace,
                seed_path=self.seed_path,
                snapshot_id="../escape",
                retrieved_at=RETRIEVED_AT,
                client=self.client,
            )

        root = self.workspace / "data" / "normalized" / "us"
        root.mkdir(parents=True)
        outside = self.workspace / "outside"
        outside.mkdir()
        (root / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "unsafe|already exists"):
            collect_sec_snapshot(
                workspace=self.workspace,
                seed_path=self.seed_path,
                snapshot_id="linked",
                retrieved_at=RETRIEVED_AT,
                client=self.client,
            )

    def test_market_json_requires_explicit_license_attestation(self) -> None:
        market_path = self.workspace / "market.json"
        market_path.write_text(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "market": "US",
                    "provider": "Example Licensed Provider",
                    "evidence": [],
                    "financial_facts": [],
                    "market_context": {
                        "regime": "UNKNOWN",
                        "breadth": "UNKNOWN",
                        "rates": "UNKNOWN",
                        "liquidity": "UNKNOWN",
                        "calculation_note": "No data.",
                        "evidence_refs": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContractError, "license_attestation"):
            collect_sec_snapshot(
                workspace=self.workspace,
                seed_path=self.seed_path,
                snapshot_id="market-no-license",
                retrieved_at=RETRIEVED_AT,
                market_path=market_path,
                client=self.client,
            )

    def test_licensed_market_json_adds_only_validated_close_evidence(self) -> None:
        market_path = self.workspace / "market.json"
        market_path.write_text(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "market": "US",
                    "provider": "Example Licensed Provider",
                    "license_attestation": "authorized_for_local_research_snapshot",
                    "evidence": [
                        {
                            "evidence_id": "EV-MARKET-TEST-20260817",
                            "source_level": "structured_market",
                            "category": "market_price",
                            "title": "TEST licensed close",
                            "summary": "Provider-normalized official close supplied by the user.",
                            "source_url": "https://licensed.example.com/TEST/2026-08-17",
                            "source_document_id": "licensed-test-20260817",
                            "published_at": "2026-08-17T20:05:00+00:00",
                            "effective_at": "2026-08-17T20:00:00+00:00",
                            "available_at": "2026-08-17T20:05:00+00:00",
                            "retrieved_at": "2026-08-18T11:00:00+00:00",
                            "as_of": "2026-08-17T20:00:00+00:00",
                        }
                    ],
                    "financial_facts": [
                        {
                            "fact_id": "FACT-TEST-CLOSE-20260817",
                            "security_id": "US.TEST",
                            "metric": "close_price",
                            "value": 25.0,
                            "unit": "USD/share",
                            "period_end": "2026-08-17",
                            "available_at": "2026-08-17T20:05:00+00:00",
                            "evidence_ref": "EV-MARKET-TEST-20260817",
                        }
                    ],
                    "market_context": {
                        "regime": "UNKNOWN",
                        "breadth": "UNKNOWN",
                        "rates": "UNKNOWN",
                        "liquidity": "UNKNOWN",
                        "calculation_note": "Only a licensed close was supplied.",
                        "evidence_refs": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        result = collect_sec_snapshot(
            workspace=self.workspace,
            seed_path=self.seed_path,
            snapshot_id="licensed-market",
            retrieved_at=RETRIEVED_AT,
            market_path=market_path,
            client=self.client,
        )
        snapshot = json.loads(
            (self.workspace / result["relative_path"]).read_text(encoding="utf-8")
        )
        candidate = snapshot["themes"][0]["candidates"][0]
        self.assertEqual(candidate["market_evidence_refs"], ["EV-MARKET-TEST-20260817"])
        self.assertIn("FACT-TEST-CLOSE-20260817", candidate["financial_fact_refs"])
        self.assertNotIn(
            "No licensed structured market snapshot was supplied.", candidate["data_gaps"]
        )
        validate_snapshot(snapshot)

    def test_newer_interim_filing_marks_annual_ttm_metrics_stale(self) -> None:
        submissions = _submissions()
        recent = submissions["filings"]["recent"]
        additions = {
            "accessionNumber": "0000123456-26-000002",
            "acceptanceDateTime": "2026-08-01T20:30:00.000Z",
            "filingDate": "2026-08-01",
            "reportDate": "2026-06-30",
            "form": "10-Q",
            "primaryDocument": "test-20260630.htm",
        }
        for column, value in additions.items():
            recent[column].append(value)
        client = SecClient(
            user_agent="Stock Research test@example.com",
            transport=FakeSecTransport(submissions=submissions),
        )
        result = collect_sec_snapshot(
            workspace=self.workspace,
            seed_path=self.seed_path,
            snapshot_id="stale-annual",
            retrieved_at=RETRIEVED_AT,
            client=client,
        )
        snapshot = json.loads(
            (self.workspace / result["relative_path"]).read_text(encoding="utf-8")
        )
        gaps = snapshot["themes"][0]["candidates"][0]["data_gaps"]
        self.assertTrue(any("TTM roll-forward is not implemented" in gap for gap in gaps))


if __name__ == "__main__":
    unittest.main()
