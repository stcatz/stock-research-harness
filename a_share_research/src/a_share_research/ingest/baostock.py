from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from datetime import date
from importlib import metadata
from typing import Any

from .market_data import CollectionError, ProviderUnavailableError


class BaoStockProvider:
    """Thin adapter around the optional official ``baostock`` Python client."""

    name = "baostock"
    source_url = "https://www.baostock.com/"

    def __init__(self, client: Any | None = None, *, version: str | None = None) -> None:
        self._client = client or self._import_client()
        self.version = version or self._detect_version(self._client)
        self._logged_in = False

    def login(self) -> None:
        try:
            result = self._client.login()
        except Exception as exc:
            raise CollectionError(f"BaoStock login raised an exception: {exc}") from exc
        self._require_success(result, "login")
        self._logged_in = True

    def logout(self) -> None:
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            result = self._client.logout()
        except Exception as exc:
            raise CollectionError(f"BaoStock logout raised an exception: {exc}") from exc
        # Older clients do not document a logout return contract.  Validate a status when one
        # is supplied, while accepting ``None`` after the socket-close call completed.
        if result is not None:
            self._require_success(result, "logout")

    def query_daily(
        self,
        code: str,
        *,
        fields: tuple[str, ...],
        start_date: date,
        end_date: date,
        adjustflag: str,
    ) -> Sequence[Mapping[str, str]]:
        if not self._logged_in:
            raise CollectionError("BaoStock query attempted before a successful login")
        try:
            result = self._client.query_history_k_data_plus(
                code,
                ",".join(fields),
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                frequency="d",
                adjustflag=adjustflag,
            )
        except Exception as exc:
            raise CollectionError(f"BaoStock query raised an exception for {code}: {exc}") from exc
        self._require_success(result, f"query {code}")

        response_fields = getattr(result, "fields", None)
        if not isinstance(response_fields, (list, tuple)):
            raise CollectionError(f"BaoStock query {code} returned no field metadata")
        normalized_fields = tuple(str(field) for field in response_fields)
        missing_fields = sorted(set(fields) - set(normalized_fields))
        if missing_fields:
            raise CollectionError(
                f"BaoStock query {code} omitted fields: {', '.join(missing_fields)}"
            )

        rows: list[dict[str, str]] = []
        while True:
            try:
                has_next = result.next()
            except Exception as exc:
                raise CollectionError(
                    f"BaoStock result iteration failed for {code}: {exc}"
                ) from exc
            self._require_success(result, f"query iteration {code}")
            if not has_next:
                break
            try:
                values = result.get_row_data()
            except Exception as exc:
                raise CollectionError(
                    f"BaoStock row decoding failed for {code}: {exc}"
                ) from exc
            if not isinstance(values, (list, tuple)) or len(values) != len(normalized_fields):
                raise CollectionError(f"BaoStock query {code} returned a malformed row")
            rows.append(dict(zip(normalized_fields, (str(value) for value in values), strict=True)))
        return rows

    @staticmethod
    def _import_client() -> Any:
        try:
            return importlib.import_module("baostock")
        except ModuleNotFoundError as exc:
            if exc.name != "baostock":
                raise
            raise ProviderUnavailableError(
                "BaoStock support is optional. Install it into the A-share environment with "
                "`uv pip install baostock`, then retry the collection."
            ) from exc

    @staticmethod
    def _detect_version(client: Any) -> str:
        try:
            return metadata.version("baostock")
        except metadata.PackageNotFoundError:
            client_version = getattr(client, "__version__", None)
            if isinstance(client_version, str) and client_version.strip():
                return client_version.strip()
            return "unknown"

    @staticmethod
    def _require_success(result: Any, operation: str) -> None:
        code = getattr(result, "error_code", None)
        message = getattr(result, "error_msg", "")
        if str(code) != "0":
            raise CollectionError(
                f"BaoStock {operation} failed: error_code={code!r}, error_msg={message!r}"
            )
