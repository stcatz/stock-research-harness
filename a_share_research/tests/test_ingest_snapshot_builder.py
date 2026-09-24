from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from a_share_research.core.snapshot import validate_snapshot
from a_share_research.ingest import (
    BENCHMARK_SYMBOLS,
    CollectionError,
    collect_cn_market_data,
    collect_cn_snapshot,
    probe_hithink_provider,
    validate_research_seed,
)

_GENERATED_FIELDS = {
    "as_of",
    "data_mode",
    "market_context",
    "pit_quality",
    "retrieved_at",
    "snapshot_id",
}


class FakeProvider:
    name = "fake-baostock"
    version = "test-1"
    source_url = "https://www.baostock.com/"

    def __init__(self, rows_by_code: Mapping[str, Sequence[Mapping[str, str]]]) -> None:
        self.rows_by_code = rows_by_code
        self.login_called = False
        self.logout_called = False

    def login(self) -> None:
        self.login_called = True

    def logout(self) -> None:
        self.logout_called = True

    def query_daily(
        self,
        code: str,
        *,
        fields: tuple[str, ...],
        start_date: date,
        end_date: date,
        adjustflag: str,
    ) -> Sequence[Mapping[str, str]]:
        del fields, start_date, end_date, adjustflag
        return copy.deepcopy(self.rows_by_code[code])


class FakeEnrichment:
    def __init__(self, retrieved_at: datetime) -> None:
        self.retrieved_at = retrieved_at
        self.latest_session = date(2026, 8, 17)
        self.theme_discoveries: tuple[Any, ...] = ()
        self.data_gaps = {"600000.SH": ("pe_mrq is UNKNOWN",)}

    def evidence_fragments(self) -> list[dict[str, Any]]:
        retrieved_at = self.retrieved_at.isoformat()
        as_of = "2026-08-17T15:00:00+08:00"
        return [
            {
                "evidence_id": "MKT-HITHINK-VALUATION-600000-SH-20260817",
                "category": "market_data",
                "source_level": "structured_market",
                "title": "同花顺最新估值快照：600000.SH",
                "source_url": "https://fuyao.aicubes.cn/docs/",
                "published_at": retrieved_at,
                "effective_at": as_of,
                "available_at": retrieved_at,
                "retrieved_at": retrieved_at,
                "as_of": as_of,
                "summary": "估值事实，其中缺失项保持 UNKNOWN。",
                "facts": [
                    {
                        "fact_id": "FACT-HITHINK-600000-SH-PE-MRQ-20260817",
                        "metric": "pe_mrq",
                        "value": None,
                        "unit": "ratio",
                        "status": "unknown",
                        "as_of": as_of,
                        "effective_at": as_of,
                        "available_at": retrieved_at,
                        "source_field": "pe_mrq",
                    }
                ],
            },
            {
                "evidence_id": "MKT-HITHINK-BREADTH-20260817",
                "category": "market_data",
                "source_level": "structured_market",
                "title": "同花顺全市场行情宽度",
                "source_url": "https://fuyao.aicubes.cn/docs/",
                "published_at": retrieved_at,
                "effective_at": as_of,
                "available_at": retrieved_at,
                "retrieved_at": retrieved_at,
                "as_of": as_of,
                "summary": "全市场宽度。",
                "facts": [],
            },
        ]

    def candidate_evidence_refs(self) -> dict[str, tuple[str, ...]]:
        return {"600000.SH": ("MKT-HITHINK-VALUATION-600000-SH-20260817",)}

    def market_context_fragment(self) -> dict[str, Any]:
        return {
            "regime": "UNKNOWN（未配置确定性的市场状态判定规则）",
            "breadth": "全市场上涨 3000、下跌 2000。",
            "liquidity": "全市场成交额 100000000 元。",
            "calculation_note": "HiThink 全市场快照。",
            "evidence_refs": ["MKT-HITHINK-BREADTH-20260817"],
        }


class SnapshotBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.raw_temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.raw_root = Path(self.raw_temporary.name)
        self.retrieved_at = datetime(2026, 8, 17, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        sessions = _weekdays(date(2026, 7, 31), count=12)
        self.assertEqual(sessions[-1], date(2026, 8, 17))
        self.rows = {
            code: _daily_rows(code, sessions, benchmark=code in BENCHMARK_SYMBOLS)
            for code in (*BENCHMARK_SYMBOLS, "sh.600000")
        }

    def tearDown(self) -> None:
        self.raw_temporary.cleanup()
        self.temporary.cleanup()

    def test_builds_valid_snapshot_and_injects_candidate_market_evidence(self) -> None:
        seed = _research_seed()
        original_seed = copy.deepcopy(seed)
        provider = FakeProvider(self.rows)

        result = collect_cn_snapshot(
            seed,
            workspace=self.workspace,
            snapshot_id="cn-20260817-test",
            retrieved_at=self.retrieved_at,
            provider=provider,
        )

        self.assertTrue(provider.login_called)
        self.assertTrue(provider.logout_called)
        self.assertEqual(seed, original_seed)
        self.assertEqual(result.snapshot_id, "cn-20260817-test")
        self.assertTrue(result.created)
        self.assertEqual(result.relative_path, "data/normalized/cn-20260817-test/snapshot.json")

        snapshot_path = self.workspace / result.relative_path
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
        validated = validate_snapshot(raw)
        self.assertEqual(validated.snapshot_hash, result.snapshot_hash)
        self.assertEqual(raw["data_mode"], "snapshot")
        self.assertEqual(raw["pit_quality"], "RECONSTRUCTED_NON_PIT")
        self.assertEqual(raw["as_of"], "2026-08-17T15:00:00+08:00")
        self.assertEqual(raw["retrieved_at"], self.retrieved_at.isoformat())

        candidate = raw["themes"][0]["candidates"][0]
        market_ref = "MKT-BAOSTOCK-SH-600000-20260817"
        self.assertEqual(candidate["market_evidence_refs"], [market_ref])
        evidence = {item["evidence_id"]: item for item in raw["evidence"]}
        self.assertEqual(evidence[market_ref]["source_level"], "structured_market")
        self.assertEqual(len(raw["market_context"]["evidence_refs"]), 3)
        self.assertTrue(all(ref in evidence for ref in raw["market_context"]["evidence_refs"]))

    def test_hithink_uses_one_client_and_merges_enrichment_without_changing_gates(self) -> None:
        daily = collect_cn_market_data(
            ("sh.600000",),
            provider=FakeProvider(self.rows),
            retrieved_at=self.retrieved_at,
        )
        daily = replace(
            daily,
            provider_name="hithink-financial-api",
            provider_version="public-api-unversioned",
            provider_source_url="https://fuyao.aicubes.cn/docs/",
        )
        enrichment = FakeEnrichment(self.retrieved_at + timedelta(minutes=5))
        client = object()
        raw_store = object()
        hithink_provider = object()
        raw_root = self.raw_root / "integration"

        with (
            patch(
                "a_share_research.ingest.snapshot_builder.ImmutableRawStore",
                return_value=raw_store,
            ) as raw_store_factory,
            patch(
                "a_share_research.ingest.snapshot_builder.HiThinkClient",
                return_value=client,
            ) as client_factory,
            patch(
                "a_share_research.ingest.snapshot_builder.HiThinkProvider",
                return_value=hithink_provider,
            ) as provider_factory,
            patch(
                "a_share_research.ingest.snapshot_builder.collect_cn_market_data",
                return_value=daily,
            ) as daily_collect,
            patch(
                "a_share_research.ingest.snapshot_builder.collect_hithink_enrichment",
                return_value=enrichment,
            ) as enrich,
        ):
            result = collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="cn-hithink-integration",
                provider_kind="hithink",
                raw_store_root=raw_root,
            )

        raw_store_factory.assert_called_once_with(raw_root.resolve())
        client_factory.assert_called_once_with(raw_store=raw_store)
        provider_factory.assert_called_once_with(client)
        self.assertIs(daily_collect.call_args.kwargs["provider"], hithink_provider)
        enrich.assert_called_once_with(
            client,
            latest_session=date(2026, 8, 17),
            candidate_thscodes=("600000.SH",),
            full_market_limit=6000,
        )
        self.assertEqual(result.provider_name, "hithink-financial-api")

        raw = json.loads((self.workspace / result.relative_path).read_text(encoding="utf-8"))
        self.assertEqual(raw["retrieved_at"], enrichment.retrieved_at.isoformat())
        candidate = raw["themes"][0]["candidates"][0]
        self.assertEqual(
            candidate["market_evidence_refs"],
            ["MKT-HITHINK-SH-600000-20260817"],
        )
        self.assertIn("MKT-HITHINK-VALUATION-600000-SH-20260817", candidate["evidence_refs"])
        self.assertNotIn(
            "MKT-HITHINK-VALUATION-600000-SH-20260817",
            candidate["market_evidence_refs"],
        )
        self.assertEqual(
            candidate["data_gaps"],
            _research_seed()["themes"][0]["candidates"][0]["data_gaps"],
        )
        self.assertEqual(raw["provider_data_gaps"], {"600000.SH": ["pe_mrq is UNKNOWN"]})
        self.assertEqual(raw["market_discoveries"], [])
        self.assertIn("MKT-HITHINK-BREADTH-20260817", raw["market_context"]["evidence_refs"])
        self.assertIn(
            "MKT-HITHINK-SH-000001-20260817",
            raw["market_context"]["evidence_refs"],
        )
        validate_snapshot(raw)

    def test_invalid_seed_is_fully_rejected_before_hithink_client_or_network(self) -> None:
        seed = _research_seed()
        seed["evidence"][0].pop("effective_at")

        with (
            patch("a_share_research.ingest.snapshot_builder.ImmutableRawStore") as raw_store,
            patch("a_share_research.ingest.snapshot_builder.HiThinkClient") as client,
            self.assertRaisesRegex(Exception, "effective_at"),
        ):
            collect_cn_snapshot(
                seed,
                workspace=self.workspace,
                snapshot_id="invalid-before-network",
                provider_kind="hithink",
                raw_store_root=self.raw_root / "invalid-seed",
            )

        raw_store.assert_not_called()
        client.assert_not_called()

    def test_hithink_failure_does_not_fallback_to_baostock(self) -> None:
        with (
            patch(
                "a_share_research.ingest.snapshot_builder.HiThinkClient",
                side_effect=RuntimeError("provider unavailable"),
            ),
            patch("a_share_research.ingest.snapshot_builder.BaoStockProvider") as baostock,
            self.assertRaisesRegex(RuntimeError, "provider unavailable"),
        ):
            collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="no-fallback",
                provider_kind="hithink",
                raw_store_root=self.raw_root / "no-fallback",
            )

        baostock.assert_not_called()

    def test_hithink_raw_store_cannot_be_placed_inside_repository(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        with (
            patch("a_share_research.ingest.snapshot_builder.HiThinkClient") as client,
            self.assertRaisesRegex(CollectionError, "outside the source repository"),
        ):
            collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="unsafe-raw-root",
                provider_kind="hithink",
                raw_store_root=repository_root / "raw-provider-data",
            )
        client.assert_not_called()

    def test_hithink_raw_store_cannot_be_placed_inside_runtime_workspace(self) -> None:
        with (
            patch("a_share_research.ingest.snapshot_builder.HiThinkClient") as client,
            self.assertRaisesRegex(CollectionError, "outside the runtime workspace"),
        ):
            collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="unsafe-workspace-raw-root",
                provider_kind="hithink",
                raw_store_root=self.workspace / "raw-provider-data",
            )
        client.assert_not_called()

    def test_provider_probe_returns_only_safe_receipt_metadata(self) -> None:
        response = Mock(
            endpoint="/api/a-share/prices/snapshot",
            request_id="request-123",
            retrieved_at=self.retrieved_at,
            body_sha256="a" * 64,
            raw_artifact_id="hithink-artifact-123",
            data={"item": [{"thscode": "600000.SH", "last_price": 99}]},
            raw_body=b'{"secret":"must-not-escape"}',
        )
        client = Mock()
        client.get.return_value = response
        raw_store = object()

        with (
            patch(
                "a_share_research.ingest.snapshot_builder.ImmutableRawStore",
                return_value=raw_store,
            ),
            patch(
                "a_share_research.ingest.snapshot_builder.HiThinkClient",
                return_value=client,
            ),
        ):
            result = probe_hithink_provider(
                "600000.SH",
                workspace=self.workspace,
                raw_store_root=self.raw_root / "probe",
            )

        client.get.assert_called_once_with(
            "/api/a-share/prices/snapshot", {"thscodes": "600000.SH"}
        )
        serialized = json.dumps(result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["request_id"], "request-123")
        self.assertNotIn("last_price", serialized)
        self.assertNotIn("raw_body", serialized)
        self.assertNotIn("must-not-escape", serialized)

    def test_provider_probe_uses_only_the_documented_raw_store_environment_override(self) -> None:
        response = Mock(
            endpoint="/api/a-share/prices/snapshot",
            request_id="request-env",
            retrieved_at=self.retrieved_at,
            body_sha256="b" * 64,
            raw_artifact_id="hithink-artifact-env",
        )
        client = Mock()
        client.get.return_value = response
        environment_root = self.raw_root / "environment-raw"

        with (
            patch.dict(
                os.environ,
                {"STOCK_RESEARCH_RAW_STORE": str(environment_root)},
            ),
            patch(
                "a_share_research.ingest.snapshot_builder.ImmutableRawStore",
                return_value=object(),
            ) as raw_store,
            patch(
                "a_share_research.ingest.snapshot_builder.HiThinkClient",
                return_value=client,
            ),
        ):
            probe_hithink_provider("600000.SH", workspace=self.workspace)

        raw_store.assert_called_once_with(environment_root.resolve())

    def test_baostock_rejects_hithink_raw_store_argument_or_environment(self) -> None:
        for snapshot_id, raw_root, environment in (
            ("baostock-raw-argument", self.raw_root / "explicit", None),
            ("baostock-raw-environment", None, str(self.raw_root / "environment")),
        ):
            with self.subTest(snapshot_id=snapshot_id):
                context = (
                    patch.dict(
                        os.environ,
                        {"STOCK_RESEARCH_RAW_STORE": environment},
                    )
                    if environment is not None
                    else patch.dict(os.environ, {}, clear=False)
                )
                with (
                    context,
                    patch("a_share_research.ingest.snapshot_builder.BaoStockProvider") as provider,
                    self.assertRaisesRegex(CollectionError, "provider_kind=hithink"),
                ):
                    if environment is None:
                        os.environ.pop("STOCK_RESEARCH_RAW_STORE", None)
                    collect_cn_snapshot(
                        _research_seed(),
                        workspace=self.workspace,
                        snapshot_id=snapshot_id,
                        provider_kind="baostock",
                        raw_store_root=raw_root,
                    )
                provider.assert_not_called()

    def test_same_content_is_idempotent_but_different_content_cannot_overwrite(self) -> None:
        seed = _research_seed()
        first = collect_cn_snapshot(
            seed,
            workspace=self.workspace,
            snapshot_id="fixed-id",
            retrieved_at=self.retrieved_at,
            provider=FakeProvider(self.rows),
        )
        original = (self.workspace / first.relative_path).read_bytes()

        repeated = collect_cn_snapshot(
            seed,
            workspace=self.workspace,
            snapshot_id="fixed-id",
            retrieved_at=self.retrieved_at,
            provider=FakeProvider(self.rows),
        )
        self.assertFalse(repeated.created)

        changed = _research_seed()
        changed["themes"][0]["name"] = "changed research seed"
        with self.assertRaisesRegex(CollectionError, "different content"):
            collect_cn_snapshot(
                changed,
                workspace=self.workspace,
                snapshot_id="fixed-id",
                retrieved_at=self.retrieved_at,
                provider=FakeProvider(self.rows),
            )
        self.assertEqual((self.workspace / first.relative_path).read_bytes(), original)

    def test_rejects_retrieved_at_override_without_explicit_provider(self) -> None:
        with self.assertRaisesRegex(
            CollectionError, "retrieved_at override requires an explicitly injected provider"
        ):
            collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="forged-live-time",
                retrieved_at=self.retrieved_at,
            )

    def test_rejects_market_data_in_seed_before_calling_provider(self) -> None:
        seed = _research_seed()
        seed["evidence"].append(
            {
                **seed["evidence"][0],
                "evidence_id": "PRECOMPUTED-MARKET",
                "source_level": "structured_market",
                "category": "market_data",
            }
        )
        provider = FakeProvider(self.rows)

        with self.assertRaisesRegex(CollectionError, "must not contain market data"):
            collect_cn_snapshot(
                seed,
                workspace=self.workspace,
                snapshot_id="rejected",
                retrieved_at=self.retrieved_at,
                provider=provider,
            )

        self.assertFalse(provider.login_called)
        self.assertFalse(
            (self.workspace / "data" / "normalized" / "rejected" / "snapshot.json").exists()
        )

    def test_rejects_example_or_fixture_seed_before_calling_provider(self) -> None:
        for marker in ("example_notice", "fixture_notice"):
            with self.subTest(marker=marker):
                seed = _research_seed()
                seed[marker] = "synthetic content"
                provider = FakeProvider(self.rows)

                with self.assertRaisesRegex(CollectionError, "synthetic|example|fixture"):
                    collect_cn_snapshot(
                        seed,
                        workspace=self.workspace,
                        snapshot_id=f"rejected-{marker}",
                        retrieved_at=self.retrieved_at,
                        provider=provider,
                    )

                self.assertFalse(provider.login_called)

    def test_rejects_nested_synthetic_markers_even_if_top_level_notice_is_removed(self) -> None:
        example_path = Path(__file__).parents[1] / "config" / "research_seed.example.json"
        seed = json.loads(example_path.read_text(encoding="utf-8"))
        seed.pop("example_notice")
        for evidence in seed["evidence"]:
            evidence["source_url"] = "https://www.cninfo.com.cn/operator-must-replace"
        provider = FakeProvider(self.rows)

        with self.assertRaisesRegex(CollectionError, "synthetic example content"):
            collect_cn_snapshot(
                seed,
                workspace=self.workspace,
                snapshot_id="renamed-example",
                retrieved_at=self.retrieved_at,
                provider=provider,
            )

        self.assertFalse(provider.login_called)

    def test_rejects_reserved_invalid_source_urls_before_calling_provider(self) -> None:
        seed = _research_seed()
        seed["evidence"][0]["source_url"] = "https://policy.example.invalid/synthetic"
        provider = FakeProvider(self.rows)

        with self.assertRaisesRegex(CollectionError, "synthetic|invalid source"):
            collect_cn_snapshot(
                seed,
                workspace=self.workspace,
                snapshot_id="rejected-invalid-source",
                retrieved_at=self.retrieved_at,
                provider=provider,
            )

        self.assertFalse(provider.login_called)

    def test_rejects_security_id_that_does_not_match_candidate_symbol(self) -> None:
        seed = _research_seed()
        seed["themes"][0]["candidates"][0]["security_id"] = "CN.SH.999999"
        provider = FakeProvider(self.rows)

        with self.assertRaisesRegex(CollectionError, "security_id must be CN.SH.600000"):
            collect_cn_snapshot(
                seed,
                workspace=self.workspace,
                snapshot_id="rejected-identity-mismatch",
                retrieved_at=self.retrieved_at,
                provider=provider,
            )

        self.assertFalse(provider.login_called)

    def test_validation_failure_does_not_leave_a_latest_candidate(self) -> None:
        seed = _research_seed()
        seed["themes"][0]["evidence_refs"] = ["UNKNOWN-EVIDENCE"]

        with self.assertRaisesRegex(Exception, "unknown evidence"):
            collect_cn_snapshot(
                seed,
                workspace=self.workspace,
                snapshot_id="invalid-output",
                retrieved_at=self.retrieved_at,
                provider=FakeProvider(self.rows),
            )

        normalized = self.workspace / "data" / "normalized"
        self.assertEqual(list(normalized.glob("*/snapshot.json")), [])

    def test_publish_failure_removes_snapshot_from_latest_candidates(self) -> None:
        with (
            patch(
                "a_share_research.ingest.snapshot_builder._fsync_directory",
                side_effect=OSError("simulated fsync failure"),
            ),
            self.assertRaisesRegex(CollectionError, "filesystem operation failed"),
        ):
            collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="failed-publish",
                retrieved_at=self.retrieved_at,
                provider=FakeProvider(self.rows),
            )

        normalized = self.workspace / "data" / "normalized"
        self.assertEqual(list(normalized.glob("*/snapshot.json")), [])

    def test_rejects_symlink_snapshot_target_without_touching_destination(self) -> None:
        normalized = self.workspace / "data" / "normalized"
        normalized.mkdir(parents=True)
        outside = self.workspace / "outside"
        outside.mkdir()
        target = normalized / "escaped"
        target.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(CollectionError, "symbolic link"):
            collect_cn_snapshot(
                _research_seed(),
                workspace=self.workspace,
                snapshot_id="escaped",
                retrieved_at=self.retrieved_at,
                provider=FakeProvider(self.rows),
            )

        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_path_navigation_snapshot_ids_before_calling_provider(self) -> None:
        for snapshot_id in (".", "..", "../escaped", "nested/escaped"):
            with self.subTest(snapshot_id=snapshot_id):
                provider = FakeProvider(self.rows)
                with self.assertRaisesRegex(CollectionError, "snapshot_id"):
                    collect_cn_snapshot(
                        _research_seed(),
                        workspace=self.workspace,
                        snapshot_id=snapshot_id,
                        retrieved_at=self.retrieved_at,
                        provider=provider,
                    )
                self.assertFalse(provider.login_called)

    def test_tracked_example_is_a_valid_seed_but_contains_no_snapshot_or_market_data(self) -> None:
        example_path = Path(__file__).parents[1] / "config" / "research_seed.example.json"
        example = json.loads(example_path.read_text(encoding="utf-8"))

        normalized = validate_research_seed(example)

        self.assertIn("SYNTHETIC FORMAT EXAMPLE ONLY", normalized["example_notice"])
        self.assertTrue(
            _GENERATED_FIELDS.isdisjoint(normalized),
            "example seed must not masquerade as a generated snapshot",
        )
        self.assertTrue(
            all(item["source_level"] != "structured_market" for item in normalized["evidence"])
        )
        self.assertTrue(
            all(
                item["source_url"].startswith("https://example.invalid/")
                for item in normalized["evidence"]
            )
        )
        self.assertTrue(
            all(
                candidate.get("market_evidence_refs", []) == []
                for theme in normalized["themes"]
                for candidate in theme["candidates"]
            )
        )


def _research_seed() -> dict[str, Any]:
    resource = files("a_share_research.fixtures").joinpath("demo_snapshot.json")
    with resource.open("r", encoding="utf-8") as handle:
        demo = json.load(handle)
    evidence = [
        item
        for item in demo["evidence"]
        if item["evidence_id"] in {"EV-POLICY-001", "EV-COMPANY-001", "EV-INDUSTRY-001"}
    ]
    source_urls = {
        "EV-POLICY-001": "https://www.ndrc.gov.cn/test-policy",
        "EV-COMPANY-001": "https://www.cninfo.com.cn/test-company",
        "EV-INDUSTRY-001": "https://www.stats.gov.cn/test-industry",
    }
    for item in evidence:
        item["source_url"] = source_urls[item["evidence_id"]]
    theme = copy.deepcopy(demo["themes"][0])
    theme["evidence_refs"] = ["EV-POLICY-001", "EV-INDUSTRY-001"]
    theme["dimensions"]["many"]["evidence_refs"] = ["EV-INDUSTRY-001"]
    theme["dimensions"]["timely"]["evidence_refs"] = ["EV-POLICY-001"]
    candidate = theme["candidates"][0]
    candidate.update(
        {
            "security_id": "CN.SH.600000",
            "symbol": "600000",
            "name": "研究候选甲",
            "market_evidence_refs": [],
            "risk_flags": [],
        }
    )
    theme["candidates"] = [candidate]
    return {
        "schema_version": "0.1",
        "market": "CN",
        "evidence": evidence,
        "themes": [theme],
    }


def _weekdays(start: date, *, count: int) -> list[date]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _daily_rows(
    code: str, session_dates: Sequence[date], *, benchmark: bool
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, session_date in enumerate(session_dates):
        close = 99 + index
        rows.append(
            {
                "date": session_date.isoformat(),
                "code": code,
                "open": str(close - 1),
                "high": str(close + 1),
                "low": str(close - 2),
                "close": str(close),
                "preclose": str(close - 1),
                "volume": str(1000 + index),
                "amount": str(1000 + index * 100),
                "adjustflag": "3",
                "turn": "" if benchmark else "1.2",
                "tradestatus": "1",
                "pctChg": "1.01",
                "isST": "" if benchmark else "0",
            }
        )
    return rows


if __name__ == "__main__":
    unittest.main()
