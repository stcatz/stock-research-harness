from __future__ import annotations

import json
import unittest
from collections import defaultdict
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from a_share_research.core.snapshot import validate_snapshot
from a_share_research.ingest.hithink_enrichment import (
    HITHINK_FINANCIAL_ENDPOINTS,
    HITHINK_POOL_ENDPOINTS,
    HITHINK_PRICES_SNAPSHOT_ENDPOINT,
    HITHINK_VALUATIONS_ENDPOINT,
    HiThinkEnrichmentError,
    collect_hithink_enrichment,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 8, 21)
SESSION_TIMESTAMP_MS = int(
    datetime.combine(SESSION, time(15, 0), tzinfo=SHANGHAI_TZ).timestamp() * 1000
)
SESSION_DATE_MS = int(datetime.combine(SESSION, time.min, tzinfo=SHANGHAI_TZ).timestamp() * 1000)


class _QueueClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {endpoint: list(items) for endpoint, items in responses.items()}
        self.calls: list[tuple[str, dict[str, str | int]]] = []

    def get(self, endpoint: str, params: dict[str, str | int]) -> Any:
        self.calls.append((endpoint, dict(params)))
        try:
            return self.responses[endpoint].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"unexpected request: {endpoint} {params}") from exc


def _response(
    data: Any,
    *,
    request_id: str,
    minute: int,
    endpoint: str | None = None,
    sha: str | None = None,
    artifact_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        data=data,
        request_id=request_id,
        retrieved_at=datetime(2026, 8, 21, 8, minute, tzinfo=UTC),
        body_sha256=sha or (format(minute % 16, "x") * 64),
        raw_artifact_id=artifact_id or f"hithink-artifact-{request_id}",
        endpoint=endpoint,
        raw_body=b'{"secret":"must-not-leak"}',
    )


def _price(
    thscode: str,
    *,
    ratio: float,
    volume: int,
    turnover: int,
) -> dict[str, Any]:
    return {
        "thscode": thscode,
        "ticker": thscode[:6],
        "last_price": 10,
        "price_change": ratio / 10,
        "price_change_ratio_pct": ratio,
        "open_price": 9,
        "high_price": 11,
        "low_price": 8,
        "prev_price": 9,
        "volume": volume,
        "turnover": turnover,
    }


def _financial_row(
    thscode: str,
    period_end: datetime,
    report_date: datetime,
    *,
    statement: str,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "thscode": thscode,
        "ticker": thscode[:6],
        "period": "annual",
        "period_end_ms": int(period_end.timestamp() * 1000),
        "report_date_ms": int(report_date.timestamp() * 1000),
        "fiscal_year": period_end.year,
        "fiscal_period": "FY",
        "currency": "CNY",
    }
    if statement == "income":
        return {
            **common,
            "basic_eps": "-0.25",
            "operating_income": 1000,
            "operating_costs": 600,
            "operating_expenses": None,
            "operating_profit": 200,
            "profit_total": 180,
            "net_profit": -50,
            "parent_holder_net_profit": -55,
            "income_tax_expense": 10,
            "interest_expenses": None,
            "manage_fee": 20,
            "sales_fee": 30,
            "research_and_development_expenses": 40,
        }
    if statement == "balance":
        return {
            **common,
            "total_current_assets": 900,
            "non_current_nets_total": 1100,
            "assets_total": 2000,
            "total_debt": 800,
            "holder_equity_total": 1200,
            "cash": 300,
            "accounts_receivable": None,
        }
    return {
        **common,
        "act_cash_flow_net": -20,
        "invest_cash_flow_net": -100,
        "financing_cash_flow_net": 80,
        "cash_equivalents_net_addition": -40,
        "pay_dividends_profits_interest_cash": 5,
        "pay_fixed_assets_etc_cash": None,
    }


def _valid_responses(
    *,
    candidates: tuple[str, ...] = ("600519.SH",),
) -> dict[str, list[Any]]:
    period_2024 = datetime(2024, 12, 31, tzinfo=UTC)
    period_2023 = datetime(2023, 12, 31, tzinfo=UTC)
    report_2024 = datetime(2025, 4, 20, tzinfo=UTC)
    report_2023 = datetime(2024, 4, 20, tzinfo=UTC)
    responses: dict[str, list[Any]] = defaultdict(list)
    responses[HITHINK_PRICES_SNAPSHOT_ENDPOINT] = [
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "total": 3,
                "item": [
                    _price("600519.SH", ratio=1.25, volume=10, turnover=100),
                    _price("000001.SZ", ratio=-0.5, volume=20, turnover=200),
                ],
            },
            request_id="prices-1",
            minute=1,
            endpoint=HITHINK_PRICES_SNAPSHOT_ENDPOINT,
        ),
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "total": 3,
                "item": [_price("300001.SZ", ratio=0, volume=0, turnover=0)],
            },
            request_id="prices-2",
            minute=2,
            endpoint=HITHINK_PRICES_SNAPSHOT_ENDPOINT,
        ),
    ]
    responses[HITHINK_POOL_ENDPOINTS["limit_up"]] = [
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "pagination": {"total": 3, "pages": 2, "size": 2, "page": 1},
                "item": [
                    {
                        "thscode": "600519.SH",
                        "ticker": "600519",
                        "name": "贵州茅台",
                        "is_st": False,
                        "is_new": False,
                        "last_price": 1500,
                        "price_change_ratio_pct": 10,
                        "limit_up_time": "14:20",
                        "limit_up_reason": "供应商归因，不是公告证据",
                        "continue_day_text": "首板",
                        "continue_day_cnt": 1,
                        "seal_money": 123,
                        "max_seal_money": 456,
                    },
                    {"thscode": "000002.SZ", "name": "其他甲"},
                ],
            },
            request_id="up-1",
            minute=3,
        ),
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "pagination": {"total": 3, "pages": 2, "size": 2, "page": 2},
                "item": [{"thscode": "000003.SZ", "name": "其他乙"}],
            },
            request_id="up-2",
            minute=4,
        ),
    ]
    responses[HITHINK_POOL_ENDPOINTS["limit_down"]] = [
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "pagination": {"total": 0, "pages": 0, "size": 2, "page": 1},
                "item": [],
            },
            request_id="down-1",
            minute=5,
        )
    ]
    responses[HITHINK_POOL_ENDPOINTS["limit_break"]] = [
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "pagination": {"total": 1, "pages": 1, "size": 2, "page": 1},
                "item": [
                    {
                        "thscode": "000001.SZ",
                        "name": "平安银行",
                        "price_change_ratio_pct": 6.2,
                        "open_times": 2,
                        "turnover_ratio_pct": 3.1,
                        "turnover": 900,
                    }
                ],
            },
            request_id="break-1",
            minute=6,
        )
    ]
    valuation_items = []
    for code in candidates:
        valuation_items.append(
            {
                "thscode": code,
                "ticker": code[:6],
                "name": "候选",
                "pe_ttm": -12.5,
                "pe_mrq": None,
                "pb_mrq": 3.25,
                "ps_ttm": 4,
                "pcf_ttm": None,
            }
        )
    responses[HITHINK_VALUATIONS_ENDPOINT] = [
        _response(
            {
                "timestamp": SESSION_TIMESTAMP_MS,
                "total": len(valuation_items),
                "item": valuation_items,
            },
            request_id="valuations-1",
            minute=7,
        )
    ]
    minute = 8
    for code in candidates:
        for statement, endpoint in HITHINK_FINANCIAL_ENDPOINTS.items():
            responses[endpoint].append(
                _response(
                    {
                        "timestamp": SESSION_TIMESTAMP_MS,
                        "item": [
                            _financial_row(code, period_2024, report_2024, statement=statement),
                            _financial_row(code, period_2023, report_2023, statement=statement),
                        ],
                    },
                    request_id=f"{statement}-{code}-{minute}",
                    minute=minute,
                )
            )
            minute += 1
    return dict(responses)


class HiThinkEnrichmentTests(unittest.TestCase):
    def test_collects_strict_market_breadth_and_evidence_ready_candidate_data(self) -> None:
        client = _QueueClient(_valid_responses())

        result = collect_hithink_enrichment(
            client,
            latest_session=SESSION,
            candidate_thscodes=["600519.sh", "600519.SH"],
            page_size=2,
        )

        self.assertEqual(result.latest_session, SESSION)
        self.assertEqual(result.retrieved_at, datetime(2026, 8, 21, 8, 10, tzinfo=UTC))
        self.assertEqual(result.breadth.total, 3)
        self.assertEqual(result.breadth.advancing, 1)
        self.assertEqual(result.breadth.declining, 1)
        self.assertEqual(result.breadth.unchanged, 0)
        self.assertEqual(result.breadth.no_trade, 1)
        self.assertEqual(str(result.breadth.turnover_total), "300")
        self.assertEqual(
            result.pool_summary.counts, {"limit_up": 3, "limit_down": 0, "limit_break": 1}
        )
        self.assertEqual(len(result.candidate_pool_records), 1)
        pool_record = result.candidate_pool_records[0]
        self.assertEqual(pool_record.thscode, "600519.SH")
        self.assertEqual(pool_record.pool, "limit_up")
        self.assertEqual(pool_record.provider_reason, "供应商归因，不是公告证据")
        self.assertFalse(pool_record.is_official_evidence)

        valuation = result.valuations[0]
        valuation_facts = {fact.metric: fact for fact in valuation.facts}
        self.assertEqual(valuation_facts["pe_ttm"].value, "-12.5")
        self.assertEqual(valuation_facts["pe_ttm"].status, "observed")
        self.assertIsNone(valuation_facts["pe_mrq"].value)
        self.assertEqual(valuation_facts["pe_mrq"].status, "unknown")

        self.assertEqual(len(result.financial_periods), 2)
        latest_financial = result.financial_periods[0]
        financial_facts = {fact.metric: fact for fact in latest_financial.facts}
        self.assertEqual(financial_facts["income.net_profit"].value, "-50")
        self.assertEqual(financial_facts["income.operating_expenses"].status, "unknown")
        self.assertEqual(
            latest_financial.published_date_hints["income"],
            "2025-04-20T00:00:00+00:00",
        )

        context = result.market_context_fragment()
        self.assertIn("上涨 1", context["breadth"])
        self.assertIn("300", context["liquidity"])
        self.assertEqual(len(context["evidence_refs"]), 2)
        fragments = result.evidence_fragments()
        self.assertTrue(fragments)
        self.assertTrue(all(item["source_level"] == "structured_market" for item in fragments))
        self.assertTrue(all("effective_at" in item for item in fragments))
        refs = result.candidate_evidence_refs()["600519.SH"]
        self.assertTrue(any("VALUATION" in ref for ref in refs))
        self.assertTrue(any("FINANCIAL" in ref for ref in refs))
        self.assertTrue(any("LIMIT-UP" in ref for ref in refs))

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("raw_body", serialized)
        self.assertNotIn("api_key", serialized.casefold())
        self.assertNotIn("presigned", serialized.casefold())
        self.assertNotIn("/Users/", serialized)
        provider_sources = fragments[0]["provider"]["responses"]
        self.assertEqual(provider_sources[0]["endpoint"], HITHINK_PRICES_SNAPSHOT_ENDPOINT)
        self.assertEqual(provider_sources[0]["request_id"], "prices-1")
        self.assertEqual(len(provider_sources[0]["raw_artifact"]["sha256"]), 64)

        validated = validate_snapshot(
            {
                "schema_version": "0.1",
                "market": "CN",
                "snapshot_id": "hithink-enrichment-contract-test",
                "data_mode": "snapshot",
                "pit_quality": "RECONSTRUCTED_NON_PIT",
                "as_of": datetime.combine(SESSION, time(15, 0), tzinfo=SHANGHAI_TZ).isoformat(),
                "retrieved_at": result.retrieved_at.isoformat(),
                "market_context": context,
                "evidence": fragments,
                "themes": [],
            }
        )
        self.assertEqual(len(validated.evidence_by_id), len(fragments))

        price_calls = [
            params
            for endpoint, params in client.calls
            if endpoint == HITHINK_PRICES_SNAPSHOT_ENDPOINT
        ]
        self.assertEqual(price_calls, [{"limit": 2, "offset": 0}, {"limit": 2, "offset": 2}])
        pool_calls = [
            params
            for endpoint, params in client.calls
            if endpoint in HITHINK_POOL_ENDPOINTS.values()
        ]
        self.assertTrue(all(params["date_ms"] == SESSION_DATE_MS for params in pool_calls))
        self.assertTrue(all("date" not in params for params in pool_calls))

    def test_market_snapshot_fails_closed_on_incomplete_duplicate_or_inconsistent_pages(
        self,
    ) -> None:
        mutations = ("incomplete", "duplicate", "timestamp")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                responses = _valid_responses()
                second = responses[HITHINK_PRICES_SNAPSHOT_ENDPOINT][1]
                if mutation == "incomplete":
                    second.data["item"] = []
                elif mutation == "duplicate":
                    second.data["item"] = [_price("600519.SH", ratio=0, volume=1, turnover=1)]
                else:
                    second.data["timestamp"] += 1
                client = _QueueClient(responses)

                with self.assertRaises(HiThinkEnrichmentError):
                    collect_hithink_enrichment(
                        client,
                        latest_session=SESSION,
                        candidate_thscodes=["600519.SH"],
                        page_size=2,
                    )

                self.assertEqual(
                    len(
                        [
                            call
                            for call in client.calls
                            if call[0] == HITHINK_PRICES_SNAPSHOT_ENDPOINT
                        ]
                    ),
                    2,
                )

    def test_pool_pagination_metadata_and_candidate_duplicates_fail_closed(self) -> None:
        for mutation in ("wrong_page", "duplicate"):
            with self.subTest(mutation=mutation):
                responses = _valid_responses()
                second = responses[HITHINK_POOL_ENDPOINTS["limit_up"]][1]
                if mutation == "wrong_page":
                    second.data["pagination"]["page"] = 1
                else:
                    second.data["item"] = [{"thscode": "600519.SH", "name": "重复"}]
                client = _QueueClient(responses)

                with self.assertRaises(HiThinkEnrichmentError):
                    collect_hithink_enrichment(
                        client,
                        latest_session=SESSION,
                        candidate_thscodes=["600519.SH"],
                        page_size=2,
                    )

    def test_missing_valuation_is_unknown_and_unmatched_financial_period_is_only_a_gap(
        self,
    ) -> None:
        responses = _valid_responses(candidates=("600519.SH", "000001.SZ"))
        valuation_response = responses[HITHINK_VALUATIONS_ENDPOINT][0]
        valuation_response.data["item"] = valuation_response.data["item"][:1]
        valuation_response.data["total"] = 1
        cashflow_responses = responses[HITHINK_FINANCIAL_ENDPOINTS["cashflow"]]
        # The second candidate has no matching 2024 cash-flow statement.  No cross-period join
        # may be fabricated from its 2023 statement.
        cashflow_responses[1].data["item"] = cashflow_responses[1].data["item"][1:]
        client = _QueueClient(responses)

        result = collect_hithink_enrichment(
            client,
            latest_session=SESSION,
            candidate_thscodes=["600519.SH", "000001.SZ"],
            page_size=2,
        )

        missing_valuation = next(item for item in result.valuations if item.thscode == "000001.SZ")
        self.assertTrue(all(fact.status == "unknown" for fact in missing_valuation.facts))
        self.assertTrue(any("valuation row" in gap for gap in result.data_gaps["000001.SZ"]))
        candidate_periods = [
            item.period_end for item in result.financial_periods if item.thscode == "000001.SZ"
        ]
        self.assertEqual(candidate_periods, [datetime(2023, 12, 31, tzinfo=UTC)])
        self.assertTrue(
            any("not present in all three" in gap for gap in result.data_gaps["000001.SZ"])
        )

    def test_financial_report_date_after_collection_fails_closed(self) -> None:
        responses = _valid_responses()
        income = responses[HITHINK_FINANCIAL_ENDPOINTS["income"]][0]
        income.data["item"][0]["report_date_ms"] = int(
            datetime(2027, 1, 1, tzinfo=UTC).timestamp() * 1000
        )

        with self.assertRaisesRegex(HiThinkEnrichmentError, "report_date_ms"):
            collect_hithink_enrichment(
                _QueueClient(responses),
                latest_session=SESSION,
                candidate_thscodes=["600519.SH"],
                page_size=2,
            )

    def test_data_ready_timestamps_are_provenance_not_trading_session_boundaries(self) -> None:
        responses = _valid_responses()
        data_ready_ms = SESSION_TIMESTAMP_MS - 24 * 60 * 60 * 1000
        for response in responses[HITHINK_POOL_ENDPOINTS["limit_up"]]:
            response.data["timestamp"] = data_ready_ms
        responses[HITHINK_VALUATIONS_ENDPOINT][0].data["timestamp"] = data_ready_ms

        result = collect_hithink_enrichment(
            _QueueClient(responses),
            latest_session=SESSION,
            candidate_thscodes=["600519.SH"],
            page_size=2,
        )

        self.assertTrue(
            all(
                source.upstream_timestamp_ms == data_ready_ms
                for source in result.pool_summary.sources
                if source.endpoint == HITHINK_POOL_ENDPOINTS["limit_up"]
            )
        )
        valuation = result.valuations[0]
        self.assertEqual(valuation.source.upstream_timestamp_ms, data_ready_ms)
        self.assertTrue(
            all(fact.as_of == valuation.source.retrieved_at for fact in valuation.facts)
        )
        valuation_evidence = next(
            item
            for item in result.evidence_fragments()
            if item["evidence_id"] == valuation.evidence_id
        )
        self.assertEqual(valuation_evidence["as_of"], valuation.source.retrieved_at.isoformat())
        self.assertEqual(
            valuation_evidence["provider"]["upstream_timestamp_scope"],
            "response_global_not_per_row",
        )
        pool_record = result.candidate_pool_records[0]
        expected_pool_as_of = datetime.combine(SESSION, time(15, 0), tzinfo=SHANGHAI_TZ)
        self.assertTrue(all(fact.as_of == expected_pool_as_of for fact in pool_record.facts))
        pool_evidence = next(
            item
            for item in result.evidence_fragments()
            if item["evidence_id"] == pool_record.evidence_id
        )
        self.assertEqual(pool_evidence["as_of"], expected_pool_as_of.isoformat())
        self.assertIn("data-ready time", pool_evidence["provider"]["upstream_timestamp_semantics"])

    def test_null_valuation_timestamp_is_preserved_without_fabricating_provenance(self) -> None:
        responses = _valid_responses()
        responses[HITHINK_VALUATIONS_ENDPOINT][0].data["timestamp"] = None

        result = collect_hithink_enrichment(
            _QueueClient(responses),
            latest_session=SESSION,
            candidate_thscodes=["600519.SH"],
            page_size=2,
        )

        valuation = result.valuations[0]
        self.assertIsNone(valuation.source.upstream_timestamp_ms)
        facts = {fact.metric: fact for fact in valuation.facts}
        self.assertEqual(facts["pe_ttm"].status, "observed")
        self.assertEqual(facts["pe_ttm"].as_of, valuation.source.retrieved_at)
        self.assertEqual(facts["pe_mrq"].status, "unknown")
        self.assertEqual(facts["pe_mrq"].as_of, valuation.source.retrieved_at)

    def test_missing_valuation_timestamp_fails_closed(self) -> None:
        responses = _valid_responses()
        responses[HITHINK_VALUATIONS_ENDPOINT][0].data.pop("timestamp")

        with self.assertRaisesRegex(HiThinkEnrichmentError, "timestamp is missing"):
            collect_hithink_enrichment(
                _QueueClient(responses),
                latest_session=SESSION,
                candidate_thscodes=["600519.SH"],
                page_size=2,
            )

    def test_every_non_null_upstream_timestamp_must_not_be_in_the_future(self) -> None:
        endpoint_mutations = (
            HITHINK_PRICES_SNAPSHOT_ENDPOINT,
            HITHINK_POOL_ENDPOINTS["limit_up"],
            HITHINK_VALUATIONS_ENDPOINT,
            HITHINK_FINANCIAL_ENDPOINTS["income"],
        )
        for endpoint in endpoint_mutations:
            with self.subTest(endpoint=endpoint):
                responses = _valid_responses()
                response = responses[endpoint][0]
                response.data["timestamp"] = int(
                    (response.retrieved_at.timestamp() + 1) * 1000
                )

                with self.assertRaisesRegex(HiThinkEnrichmentError, "later than retrieval"):
                    collect_hithink_enrichment(
                        _QueueClient(responses),
                        latest_session=SESSION,
                        candidate_thscodes=["600519.SH"],
                        page_size=2,
                    )

    def test_full_market_snapshot_uses_local_observation_time_not_assumed_eod(self) -> None:
        responses = _valid_responses()
        # The upstream value is a latest-valid-data time, not a guarantee that the
        # snapshot is an official 15:00 close for ``latest_session``.
        upstream_ms = SESSION_TIMESTAMP_MS - 24 * 60 * 60 * 1000
        for response in responses[HITHINK_PRICES_SNAPSHOT_ENDPOINT]:
            response.data["timestamp"] = upstream_ms

        result = collect_hithink_enrichment(
            _QueueClient(responses),
            latest_session=SESSION,
            candidate_thscodes=["600519.SH"],
            page_size=2,
        )

        breadth_evidence = next(
            item
            for item in result.evidence_fragments()
            if item["evidence_id"] == result.breadth.evidence_id
        )
        observed_at = max(source.retrieved_at for source in result.breadth.sources)
        self.assertEqual(breadth_evidence["as_of"], observed_at.isoformat())
        self.assertEqual(
            breadth_evidence["provider"]["upstream_timestamp_semantics"],
            "response-level latest valid data time; not a trading-session or EOD boundary",
        )

    def test_response_metadata_extractor_supports_mapping_fakes(self) -> None:
        responses = _valid_responses()
        first = responses[HITHINK_PRICES_SNAPSHOT_ENDPOINT][0]
        responses[HITHINK_PRICES_SNAPSHOT_ENDPOINT][0] = {
            "data": first.data,
            "request_id": first.request_id,
            "retrieved_at": first.retrieved_at,
            "body_sha256": first.body_sha256,
            "raw_artifact_id": first.raw_artifact_id,
        }

        result = collect_hithink_enrichment(
            _QueueClient(responses),
            latest_session=SESSION,
            candidate_thscodes=["600519.SH"],
            page_size=2,
        )

        self.assertEqual(result.breadth.total, 3)


if __name__ == "__main__":
    unittest.main()
