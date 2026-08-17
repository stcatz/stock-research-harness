from __future__ import annotations

import copy
import unittest
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from a_share_research.ingest import (
    BENCHMARK_SYMBOLS,
    DAILY_FIELDS,
    CollectionError,
    collect_cn_market_data,
    normalize_baostock_symbol,
)


class FakeProvider:
    name = "fake-baostock"
    version = "test-1"
    source_url = "https://www.baostock.com/"

    def __init__(self, rows_by_code: Mapping[str, Sequence[Mapping[str, str]]]) -> None:
        self.rows_by_code = rows_by_code
        self.queries: list[dict[str, Any]] = []
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
        self.queries.append(
            {
                "code": code,
                "fields": fields,
                "start_date": start_date,
                "end_date": end_date,
                "adjustflag": adjustflag,
            }
        )
        if code not in self.rows_by_code:
            raise RuntimeError("fixture has no rows")
        return copy.deepcopy(self.rows_by_code[code])


class MarketDataCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.retrieved_at = datetime(2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.candidate = "sh.600000"
        self.session_dates = _weekdays(date(2026, 7, 30), count=12)
        self.assertEqual(self.session_dates[-1], date(2026, 8, 14))
        self.rows_by_code = {
            code: _daily_rows(code, self.session_dates, benchmark=code in BENCHMARK_SYMBOLS)
            for code in (*BENCHMARK_SYMBOLS, self.candidate)
        }

    def test_collects_unadjusted_series_and_emits_honest_fragments(self) -> None:
        provider = FakeProvider(self.rows_by_code)

        result = collect_cn_market_data(
            ["600000.SH", "sh.600000"],
            provider=provider,
            retrieved_at=self.retrieved_at,
        )

        self.assertTrue(provider.login_called)
        self.assertTrue(provider.logout_called)
        self.assertEqual(
            [query["code"] for query in provider.queries],
            [*BENCHMARK_SYMBOLS, self.candidate],
        )
        self.assertTrue(all(query["fields"] == DAILY_FIELDS for query in provider.queries))
        self.assertTrue(all(query["adjustflag"] == "3" for query in provider.queries))
        self.assertEqual(result.latest_session, date(2026, 8, 14))

        candidate = next(item for item in result.instruments if item.code == self.candidate)
        self.assertEqual(candidate.to_dict()["derived"]["return_10_sessions_pct"], "10.0000")
        self.assertEqual(candidate.to_dict()["derived"]["average_amount_5_sessions"], "900.00")
        self.assertEqual(candidate.latest.to_dict()["turn"], "1.2")

        fragment = next(
            item
            for item in result.evidence_fragments()
            if item["instrument"]["code"] == self.candidate
        )
        self.assertEqual(fragment["source_level"], "structured_market")
        self.assertEqual(fragment["pit_quality"], "RECONSTRUCTED_NON_PIT")
        self.assertEqual(fragment["available_at"], self.retrieved_at.isoformat())
        self.assertEqual(fragment["as_of"], "2026-08-14T15:00:00+08:00")
        self.assertEqual(len(fragment["calculation_window"]), 11)
        self.assertIn(
            "does not provide an immutable first-seen timestamp", fragment["non_pit_notice"]
        )

        context = result.market_context_fragment()
        self.assertIn("UNKNOWN", context["breadth"])
        self.assertIn("not full-market breadth", context["calculation_note"])
        self.assertEqual(len(context["evidence_refs"]), 3)

    def test_benchmark_turnover_may_be_blank_but_candidate_turnover_may_not(self) -> None:
        provider = FakeProvider(self.rows_by_code)
        result = collect_cn_market_data(
            [self.candidate], provider=provider, retrieved_at=self.retrieved_at
        )
        benchmark = next(item for item in result.instruments if item.code == "sh.000001")
        self.assertIsNone(benchmark.latest.turn)

        invalid = copy.deepcopy(self.rows_by_code)
        invalid[self.candidate][-1]["turn"] = ""
        with self.assertRaisesRegex(CollectionError, "missing turn"):
            collect_cn_market_data(
                [self.candidate],
                provider=FakeProvider(invalid),
                retrieved_at=self.retrieved_at,
            )

    def test_missing_or_suspended_latest_session_fails_closed(self) -> None:
        missing = copy.deepcopy(self.rows_by_code)
        missing[self.candidate] = missing[self.candidate][:-1]
        provider = FakeProvider(missing)
        with self.assertRaisesRegex(CollectionError, "missing the latest market session"):
            collect_cn_market_data(
                [self.candidate], provider=provider, retrieved_at=self.retrieved_at
            )
        self.assertTrue(provider.logout_called)

        suspended = copy.deepcopy(self.rows_by_code)
        suspended[self.candidate][-1]["tradestatus"] = "0"
        with self.assertRaisesRegex(CollectionError, "tradestatus=0"):
            collect_cn_market_data(
                [self.candidate],
                provider=FakeProvider(suspended),
                retrieved_at=self.retrieved_at,
            )

    def test_query_failure_is_contextual_and_logs_out(self) -> None:
        rows = {code: value for code, value in self.rows_by_code.items() if code != self.candidate}
        provider = FakeProvider(rows)

        with self.assertRaisesRegex(CollectionError, "daily query failed for sh.600000"):
            collect_cn_market_data(
                [self.candidate], provider=provider, retrieved_at=self.retrieved_at
            )

        self.assertTrue(provider.logout_called)

    def test_open_same_day_data_is_rejected_as_incomplete(self) -> None:
        retrieved_at = datetime(2026, 8, 14, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        with self.assertRaisesRegex(CollectionError, "is not closed"):
            collect_cn_market_data(
                [self.candidate],
                provider=FakeProvider(self.rows_by_code),
                retrieved_at=retrieved_at,
            )

    def test_adjusted_row_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.rows_by_code)
        invalid[self.candidate][0]["adjustflag"] = "2"
        with self.assertRaisesRegex(CollectionError, "only unadjusted data"):
            collect_cn_market_data(
                [self.candidate],
                provider=FakeProvider(invalid),
                retrieved_at=self.retrieved_at,
            )


class SymbolNormalizationTests(unittest.TestCase):
    def test_normalizes_common_exchange_formats(self) -> None:
        self.assertEqual(normalize_baostock_symbol("600000"), "sh.600000")
        self.assertEqual(normalize_baostock_symbol("000001.SZ"), "sz.000001")
        self.assertEqual(normalize_baostock_symbol("BJ.830799"), "bj.830799")

    def test_ambiguous_or_invalid_symbol_requires_exchange(self) -> None:
        with self.assertRaisesRegex(ValueError, "exchange is ambiguous"):
            normalize_baostock_symbol("510300")
        with self.assertRaisesRegex(ValueError, "unsupported BaoStock symbol"):
            normalize_baostock_symbol("not-a-symbol")


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
                "amount": str(index * 100),
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
