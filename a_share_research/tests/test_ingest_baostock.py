from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest.mock import patch

from a_share_research.ingest import (
    DAILY_FIELDS,
    BaoStockProvider,
    CollectionError,
    ProviderUnavailableError,
)


class _Status:
    def __init__(self, error_code: str = "0", error_msg: str = "") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


class _QueryResult(_Status):
    def __init__(self, rows: list[list[str]]) -> None:
        super().__init__()
        self.fields = list(DAILY_FIELDS)
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


class _Client:
    def __init__(self, *, login_status: _Status | None = None, noisy: bool = False) -> None:
        self.login_status = login_status or _Status()
        self.noisy = noisy
        self.query_arguments: tuple[object, ...] | None = None
        self.query_keywords: dict[str, str] | None = None

    def login(self) -> _Status:
        if self.noisy:
            print("third-party login noise")
        return self.login_status

    def logout(self) -> None:
        if self.noisy:
            print("third-party logout noise", file=__import__("sys").stderr)

    def query_history_k_data_plus(self, *args: object, **kwargs: str) -> _QueryResult:
        if self.noisy:
            print("third-party query noise")
        self.query_arguments = args
        self.query_keywords = kwargs
        row = [
            "2026-08-14",
            "sh.600000",
            "10",
            "11",
            "9",
            "10.5",
            "10",
            "1000",
            "10500",
            "3",
            "1.2",
            "1",
            "5.0",
            "0",
        ]
        return _QueryResult([row])


class BaoStockProviderTests(unittest.TestCase):
    def test_adapter_suppresses_third_party_console_output(self) -> None:
        provider = BaoStockProvider(_Client(noisy=True), version="test-1")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            provider.login()
            provider.query_daily(
                "sh.600000",
                fields=DAILY_FIELDS,
                start_date=date(2026, 7, 1),
                end_date=date(2026, 8, 14),
                adjustflag="3",
            )
            provider.logout()

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_adapter_uses_daily_unadjusted_query_and_maps_rows(self) -> None:
        client = _Client()
        provider = BaoStockProvider(client, version="test-1")

        provider.login()
        rows = provider.query_daily(
            "sh.600000",
            fields=DAILY_FIELDS,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 14),
            adjustflag="3",
        )
        provider.logout()

        self.assertEqual(client.query_arguments, ("sh.600000", ",".join(DAILY_FIELDS)))
        self.assertEqual(
            client.query_keywords,
            {
                "start_date": "2026-07-01",
                "end_date": "2026-08-14",
                "frequency": "d",
                "adjustflag": "3",
            },
        )
        self.assertEqual(rows[0]["close"], "10.5")
        self.assertEqual(rows[0]["tradestatus"], "1")

    def test_login_error_fails_closed(self) -> None:
        provider = BaoStockProvider(
            _Client(login_status=_Status("1001", "login rejected")), version="test-1"
        )
        with self.assertRaisesRegex(CollectionError, "error_code='1001'"):
            provider.login()

    def test_missing_optional_package_has_actionable_error(self) -> None:
        missing = ModuleNotFoundError("No module named 'baostock'", name="baostock")
        with (
            patch("a_share_research.ingest.baostock.importlib.import_module", side_effect=missing),
            self.assertRaisesRegex(ProviderUnavailableError, "uv pip install baostock"),
        ):
            BaoStockProvider()


if __name__ == "__main__":
    unittest.main()
