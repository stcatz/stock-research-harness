from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from importlib.resources import files
from pathlib import Path

from a_share_research.core.outcomes import settle_outcomes_from_snapshot, summarize_outcomes
from a_share_research.core.pipeline import run_research
from a_share_research.ingest.snapshot_builder import _unsettled_outcome_watchlist


def _weekdays(start: date, count: int) -> list[date]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _bars(dates: list[date], base_date: date, base_close: int, daily_step: int) -> list[dict[str, str]]:
    base_index = dates.index(base_date)
    return [
        {
            "date": day.isoformat(),
            "close": str(base_close + (index - base_index) * daily_step),
        }
        for index, day in enumerate(dates)
    ]


class OutcomeSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        raw = json.loads(
            files("a_share_research.fixtures")
            .joinpath("demo_snapshot.json")
            .read_text(encoding="utf-8")
        )
        base_date = date(2026, 8, 14)
        all_dates = _weekdays(date(2026, 7, 31), 36)
        self.assertIn(base_date, all_dates)
        base_index = all_dates.index(base_date)
        self.window = all_dates[max(0, base_index - 10) : base_index + 26]

        base = copy.deepcopy(raw)
        base.update(
            snapshot_id="cn-settlement-base",
            data_mode="snapshot",
            pit_quality="RECONSTRUCTED_NON_PIT",
        )
        symbols = ("600001", "600002", "600003")
        for candidate, symbol in zip(
            [
                base["themes"][0]["candidates"][0],
                base["themes"][0]["candidates"][1],
                base["themes"][1]["candidates"][0],
            ],
            symbols,
            strict=True,
        ):
            candidate["symbol"] = symbol

        market = next(item for item in base["evidence"] if item["evidence_id"] == "EV-MARKET-001")
        candidate_bars = _bars(self.window[: base_index + 1], base_date, 100, 1)
        market.update(
            instrument={"code": "sh.600001", "kind": "candidate"},
            latest=candidate_bars[-1],
            derived={"return_10_sessions_pct": "0", "average_amount_5_sessions": "1"},
            calculation_window=candidate_bars,
        )
        benchmark = copy.deepcopy(market)
        benchmark.update(
            evidence_id="EV-BENCH-800",
            title="DEMO：中证800合成日线",
            instrument={"code": "sh.000906", "kind": "benchmark"},
        )
        benchmark_bars = _bars(self.window[: base_index + 1], base_date, 200, 1)
        benchmark["latest"] = benchmark_bars[-1]
        benchmark["calculation_window"] = benchmark_bars
        base["evidence"].append(benchmark)
        base["market_context"]["evidence_refs"].append("EV-BENCH-800")

        current = copy.deepcopy(base)
        current.update(
            snapshot_id="cn-settlement-current",
            as_of="2026-09-30T15:00:00+08:00",
            retrieved_at="2026-09-30T16:00:00+08:00",
        )
        current_market = next(
            item for item in current["evidence"] if item["evidence_id"] == "EV-MARKET-001"
        )
        current_benchmark = next(
            item for item in current["evidence"] if item["evidence_id"] == "EV-BENCH-800"
        )
        for evidence, close, code in (
            (current_market, 100, "sh.600001"),
            (current_benchmark, 200, "sh.000906"),
        ):
            window = _bars(self.window, base_date, close, 1)
            evidence.update(
                published_at="2026-09-30T15:01:00+08:00",
                effective_at="2026-09-30T15:00:00+08:00",
                available_at="2026-09-30T15:01:00+08:00",
                retrieved_at="2026-09-30T16:00:00+08:00",
                as_of="2026-09-30T15:00:00+08:00",
                instrument={"code": code, "kind": "benchmark" if code.endswith("000906") else "candidate"},
                latest=window[-1],
                calculation_window=window,
            )
        for snapshot in (base, current):
            path = self.workspace / "data" / "normalized" / snapshot["snapshot_id"] / "snapshot.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_frozen_snapshot_settles_exact_fifth_and_twentieth_sessions(self) -> None:
        run = run_research(
            {
                "schema_version": "0.1",
                "market": "CN",
                "workflow": "daily_report",
                "decision_at": "2026-08-16T08:30:00+08:00",
                "snapshot": {"selector": "id", "snapshot_id": "cn-settlement-base"},
                "top_n": 5,
            },
            self.workspace,
        )
        watchlist_before = _unsettled_outcome_watchlist(
            self.workspace,
            reference_time=datetime.fromisoformat("2026-09-30T16:00:00+08:00"),
        )
        self.assertIn("sh.600001", watchlist_before)
        settlement = settle_outcomes_from_snapshot(
            {
                "schema_version": "0.1",
                "market": "CN",
                "snapshot_id": "cn-settlement-current",
                "evaluation_at": "2026-09-30T16:00:00+08:00",
            },
            self.workspace,
        )
        observations = [item for item in settlement["recorded"] if item["symbol"] == "600001"]
        self.assertEqual(len(observations), 2)
        fifth = next(item for item in observations if item["horizon_trading_days"] == 5)
        twentieth = next(item for item in observations if item["horizon_trading_days"] == 20)
        self.assertAlmostEqual(fifth["candidate_return"], 0.05)
        self.assertAlmostEqual(fifth["benchmark_return"], 0.025)
        self.assertAlmostEqual(twentieth["candidate_return"], 0.20)
        self.assertAlmostEqual(twentieth["benchmark_return"], 0.10)

        summary = summarize_outcomes(
            {
                "schema_version": "0.1",
                "market": "CN",
                "run_id": run["run_id"],
                "evaluation_at": "2026-09-30T16:01:00+08:00",
            },
            self.workspace,
        )
        self.assertEqual(summary["observation_count"], 2)
        watchlist_after = _unsettled_outcome_watchlist(
            self.workspace,
            reference_time=datetime.fromisoformat("2026-09-30T16:00:00+08:00"),
        )
        self.assertNotIn("sh.600001", watchlist_after)

        repeated = settle_outcomes_from_snapshot(
            {
                "schema_version": "0.1",
                "market": "CN",
                "snapshot_id": "cn-settlement-current",
                "evaluation_at": "2026-09-30T16:00:00+08:00",
            },
            self.workspace,
        )
        self.assertEqual(repeated["recorded_count"], 0)

    def test_unsettled_slots_expire_after_the_bounded_watchlist_window(self) -> None:
        run_research(
            {
                "schema_version": "0.1",
                "market": "CN",
                "workflow": "daily_report",
                "decision_at": "2026-08-16T08:30:00+08:00",
                "snapshot": {"selector": "id", "snapshot_id": "cn-settlement-base"},
                "top_n": 5,
            },
            self.workspace,
        )

        settlement = settle_outcomes_from_snapshot(
            {
                "schema_version": "0.1",
                "market": "CN",
                "snapshot_id": "cn-settlement-current",
                "evaluation_at": "2026-12-31T16:00:00+08:00",
            },
            self.workspace,
        )

        self.assertEqual(settlement["recorded_count"], 0)
        self.assertEqual(settlement["pending_count"], 0)
        self.assertEqual(settlement["expired_unsettled_count"], 6)
        self.assertEqual(
            _unsettled_outcome_watchlist(
                self.workspace,
                reference_time=datetime.fromisoformat("2026-12-31T16:00:00+08:00"),
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
