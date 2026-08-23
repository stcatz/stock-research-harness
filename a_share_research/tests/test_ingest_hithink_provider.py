from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from a_share_research.ingest import CollectionError, HiThinkProvider

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class _Response:
    data: dict[str, Any]
    request_id: str
    retrieved_at: datetime
    body_sha256: str
    endpoint: str
    non_sensitive_params: dict[str, Any]


class _Client:
    def __init__(self, histories: dict[str, list[dict[str, Any]]]) -> None:
        self.histories = histories
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.snapshot_overrides: dict[str, dict[str, Any]] = {}
        self.snapshot_items_overrides: dict[str, list[dict[str, Any]]] = {}
        self.retrieved_times = {
            "historical": datetime(2026, 8, 17, 17, 59, tzinfo=SHANGHAI),
            "snapshot": datetime(2026, 8, 17, 18, 0, tzinfo=SHANGHAI),
        }

    def get(self, endpoint: str, params: dict[str, Any]) -> _Response:
        self.calls.append((endpoint, dict(params)))
        thscode = str(params["thscode"] if "thscode" in params else params["thscodes"])
        if endpoint.endswith("/historical"):
            retrieved_at = self.retrieved_times["historical"]
            data = {
                "timestamp": (
                    self.histories[thscode][-1]["date_ms"] if self.histories[thscode] else None
                ),
                "item": self.histories[thscode],
            }
        elif endpoint.endswith("/snapshot"):
            retrieved_at = self.retrieved_times["snapshot"]
            latest = self.histories[thscode][-1]
            previous = self.histories[thscode][-2]
            item = {
                "thscode": thscode,
                "ticker": thscode.split(".")[0],
                "last_price": latest["close_price"],
                "price_change": latest["close_price"] - previous["close_price"],
                "price_change_ratio_pct": ((latest["close_price"] / previous["close_price"]) - 1)
                * 100,
                "open_price": latest["open_price"],
                "high_price": latest["high_price"],
                "low_price": latest["low_price"],
                "prev_price": previous["close_price"],
                "volume": latest["volume"],
                "turnover": latest["turnover"],
            }
            item.update(self.snapshot_overrides.get(thscode, {}))
            items = self.snapshot_items_overrides.get(thscode, [item])
            data = {"timestamp": None, "total": len(items), "item": items}
        else:  # pragma: no cover - assertion aid
            raise AssertionError(f"unexpected endpoint {endpoint}")
        return _Response(
            data=data,
            request_id=f"request-{len(self.calls)}",
            retrieved_at=retrieved_at,
            body_sha256=str(len(self.calls)) * 64,
            endpoint=endpoint,
            non_sensitive_params=dict(params),
        )


class HiThinkProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = _weekdays(date(2026, 7, 31), count=12)
        self.histories = {
            "600000.SH": _history(self.sessions),
            "000001.SH": _history(self.sessions),
        }
        self.client = _Client(self.histories)
        self.provider = HiThinkProvider(self.client, version="api-2026.08")

    def test_stock_history_is_unadjusted_and_normalized_without_inventing_fields(self) -> None:
        series = self.provider.fetch_daily_series(
            "sh.600000",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 17),
            is_benchmark=False,
        )

        history_endpoint, history_params = self.client.calls[0]
        self.assertEqual(history_endpoint, "/api/a-share/prices/historical")
        self.assertEqual(history_params["thscode"], "600000.SH")
        self.assertEqual(history_params["interval"], "1d")
        self.assertEqual(history_params["adjust"], "none")
        self.assertIsInstance(history_params["start"], int)
        self.assertIsInstance(history_params["end"], int)

        snapshot_endpoint, snapshot_params = self.client.calls[1]
        self.assertEqual(snapshot_endpoint, "/api/a-share/prices/snapshot")
        self.assertEqual(snapshot_params, {"thscodes": "600000.SH"})

        self.assertEqual(series.code, "sh.600000")
        self.assertEqual(len(series.bars), 12)
        self.assertEqual(series.retrieved_at, self.client.retrieved_times["snapshot"])
        latest = series.bars[-1]
        self.assertEqual(latest.trade_status, "UNKNOWN")
        self.assertIsNone(latest.turn)
        self.assertIsNone(latest.is_st)
        self.assertEqual(latest.preclose, Decimal(110))
        self.assertEqual(
            latest.field_provenance["preclose"],
            "reported_snapshot_cross_validated",
        )
        self.assertIsNone(series.bars[-2].preclose)
        self.assertIsNone(series.bars[-2].pct_chg)
        self.assertEqual(series.bars[-2].field_provenance["pct_chg"], "UNKNOWN")
        self.assertEqual(series.metadata["snapshot_cross_check"]["status"], "MATCHED")
        self.assertEqual(len(series.metadata["raw_artifacts"]), 2)
        self.assertEqual(series.metadata["raw_artifacts"][0]["sha256"], "1" * 64)
        self.assertNotIn("raw_body", series.metadata)

    def test_index_uses_index_endpoints_without_adjust_parameter(self) -> None:
        series = self.provider.fetch_daily_series(
            "sh.000001",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 17),
            is_benchmark=True,
        )

        self.assertEqual(self.client.calls[0][0], "/api/a-share-index/prices/historical")
        self.assertEqual(self.client.calls[0][1]["thscode"], "000001.SH")
        self.assertNotIn("adjust", self.client.calls[0][1])
        self.assertEqual(self.client.calls[1][0], "/api/a-share-index/prices/snapshot")
        self.assertEqual(series.code, "sh.000001")

    def test_series_retrieved_at_is_maximum_of_both_provider_responses(self) -> None:
        self.client.retrieved_times["historical"] = datetime(2026, 8, 17, 18, 1, tzinfo=SHANGHAI)
        self.client.retrieved_times["snapshot"] = datetime(2026, 8, 17, 18, 0, tzinfo=SHANGHAI)

        series = self.provider.fetch_daily_series(
            "sh.600000",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 17),
            is_benchmark=False,
        )

        self.assertEqual(series.retrieved_at, self.client.retrieved_times["historical"])

    def test_snapshot_mismatch_fails_closed(self) -> None:
        self.client.snapshot_overrides["600000.SH"] = {
            "last_price": 999,
            "prev_price": 998,
            "price_change_ratio_pct": 0.1,
        }

        with self.assertRaisesRegex(CollectionError, "snapshot mismatch.*close"):
            self.provider.fetch_daily_series(
                "sh.600000",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 8, 17),
                is_benchmark=False,
            )

    def test_snapshot_unavailable_fails_closed(self) -> None:
        self.client.snapshot_items_overrides["600000.SH"] = []

        with self.assertRaisesRegex(CollectionError, "no matching entry"):
            self.provider.fetch_daily_series(
                "sh.600000",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 8, 17),
                is_benchmark=False,
            )

    def test_snapshot_preclose_may_differ_from_adjacent_raw_close_on_corporate_action(self) -> None:
        # An ex-rights reference price is an exchange-reported value, not necessarily the prior
        # unadjusted close. Only the explicit snapshot may supply it.
        self.client.snapshot_overrides["600000.SH"] = {
            "prev_price": "95",
            "price_change_ratio_pct": str((Decimal(111) / Decimal(95) - 1) * Decimal(100)),
        }

        series = self.provider.fetch_daily_series(
            "sh.600000",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 17),
            is_benchmark=False,
        )

        self.assertEqual(series.bars[-2].close, Decimal(110))
        self.assertIsNone(series.bars[-2].preclose)
        self.assertEqual(series.bars[-1].preclose, Decimal(95))
        self.assertEqual(
            series.bars[-1].field_provenance["preclose"],
            "reported_snapshot_cross_validated",
        )

    def test_snapshot_internally_inconsistent_change_ratio_fails_closed(self) -> None:
        self.client.snapshot_overrides["600000.SH"] = {
            "prev_price": 110,
            "price_change_ratio_pct": 99,
        }

        with self.assertRaisesRegex(CollectionError, "snapshot mismatch.*pct_chg"):
            self.provider.fetch_daily_series(
                "sh.600000",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 8, 17),
                is_benchmark=False,
            )

    def test_empty_or_malformed_history_fails_closed(self) -> None:
        self.client.histories["600000.SH"] = []
        with self.assertRaisesRegex(CollectionError, "no historical daily bars"):
            self.provider.fetch_daily_series(
                "sh.600000",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 8, 17),
                is_benchmark=False,
            )

    def test_complete_exchange_mapping_is_strict(self) -> None:
        self.assertEqual(self.provider.to_hithink_symbol("sz.000001"), "000001.SZ")
        self.assertEqual(self.provider.to_hithink_symbol("bj.830799"), "830799.BJ")
        with self.assertRaisesRegex(ValueError, "unsupported canonical CN symbol"):
            self.provider.to_hithink_symbol("hk.00700")


def _history(session_dates: list[date]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, session_date in enumerate(session_dates):
        close = 100 + index
        rows.append(
            {
                "date_ms": int(
                    datetime.combine(session_date, time.min, tzinfo=SHANGHAI).timestamp() * 1000
                ),
                "open_price": close - 1,
                "high_price": close + 1,
                "low_price": close - 2,
                "close_price": close,
                "volume": 1000 + index,
                "turnover": 100_000 + index,
            }
        )
    return rows


def _weekdays(start: date, *, count: int) -> list[date]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


if __name__ == "__main__":
    unittest.main()
