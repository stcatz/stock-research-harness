"""Small, fail-closed client for the public HiThink Financial API.

The module intentionally does not depend on HiThink's SDK.  It keeps credentials at the
collection boundary, preserves the exact successful response for audit storage, and exposes
only decoded ``data`` plus collection metadata to higher-level normalizers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Any, Protocol

HITHINK_BASE_URL = "https://fuyao.aicubes.cn"
HITHINK_API_KEY_ENV = "HITHINK_FINANCE_API_KEY"
HITHINK_PROVIDER_NAME = "hithink-financial-api"
HITHINK_PROVIDER_VERSION = "public-api-unversioned"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
MAX_ATTEMPTS_LIMIT = 6
MAX_RETRY_DELAY_SECONDS = 8.0
MAX_QUERY_LENGTH = 16 * 1024

_ENDPOINT_PATTERN = re.compile(r"/api/[A-Za-z0-9_/-]+\Z")
_PARAMETER_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SENSITIVE_PARAMETER_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "key",
    "password",
    "secret",
    "token",
    "x_api_key",
}
_TRANSIENT_BUSINESS_CODES = {4001}


class HiThinkError(RuntimeError):
    """Base error for the provider boundary."""


class HiThinkConfigurationError(HiThinkError):
    """Raised when local client configuration is unsafe or incomplete."""


class HiThinkTransportError(HiThinkError):
    """Raised after transient network failures exhaust the retry budget."""


class HiThinkProtocolError(HiThinkError):
    """Raised for HTTP or response-envelope contract violations."""


class HiThinkAPIError(HiThinkError):
    """A fail-closed upstream business error without provider text or credentials."""

    def __init__(self, code: int, request_id: str) -> None:
        super().__init__(f"HiThink API rejected the request: code={code}, request_id={request_id}")
        self.code = code
        self.request_id = request_id


class _NetworkFailure(Exception):
    """Internal marker used by the standard-library transport."""


class _DuplicateJsonKey(ValueError):
    """Internal marker for ambiguous JSON objects."""


class _InvalidJsonConstant(ValueError):
    """Internal marker for non-standard NaN and infinity values."""


@dataclass(frozen=True)
class HiThinkRequest:
    """Input to an injectable HTTP transport."""

    url: str
    headers: Mapping[str, str]
    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True)
class HiThinkHttpResponse:
    """Minimal response returned by an injectable HTTP transport."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class HiThinkResponse:
    """Successful, decoded response plus the exact bytes needed for audit storage."""

    data: Any
    request_id: str
    retrieved_at: datetime
    raw_body: bytes
    endpoint: str
    non_sensitive_params: Mapping[str, str | int]
    provider: str = HITHINK_PROVIDER_NAME
    provider_version: str = HITHINK_PROVIDER_VERSION
    raw_artifact_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_body", bytes(self.raw_body))
        object.__setattr__(
            self,
            "non_sensitive_params",
            MappingProxyType(dict(self.non_sensitive_params)),
        )

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.raw_body).hexdigest()


class HiThinkTransport(Protocol):
    def __call__(self, request: HiThinkRequest) -> HiThinkHttpResponse: ...


class RawResponseStore(Protocol):
    def publish(self, response: HiThinkResponse) -> Any: ...


class UrllibHiThinkTransport:
    """Bounded standard-library GET transport used in production by default."""

    def __init__(self) -> None:
        # urllib follows redirects by default and may copy headers to the redirected request.
        # A provider credential must never leave the one fixed API origin.
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def __call__(self, request: HiThinkRequest) -> HiThinkHttpResponse:
        http_request = urllib.request.Request(
            request.url,
            headers=dict(request.headers),
            method="GET",
        )
        try:
            with self._opener.open(http_request, timeout=request.timeout_seconds) as response:
                return _read_http_response(response, request.max_response_bytes)
        except urllib.error.HTTPError as error:
            try:
                with error:
                    return _read_http_response(error, request.max_response_bytes)
            except HiThinkProtocolError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                raise _NetworkFailure from None
        except HiThinkProtocolError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise _NetworkFailure from None


class HiThinkClient:
    """Credential-isolated, read-only client for ``https://fuyao.aicubes.cn``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: HiThinkTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        raw_store: RawResponseStore | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._api_key = _resolve_api_key(api_key)
        self._transport = transport or UrllibHiThinkTransport()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or time.sleep
        self._raw_store = raw_store
        self._timeout_seconds = _positive_number(timeout_seconds, "timeout_seconds")
        self._max_response_bytes = _positive_integer(max_response_bytes, "max_response_bytes")
        self._max_attempts = _bounded_attempt_count(max_attempts)
        self._retry_backoff_seconds = _nonnegative_number(
            retry_backoff_seconds, "retry_backoff_seconds"
        )

    def get(
        self,
        endpoint: str,
        params: Mapping[str, str | int] | None = None,
    ) -> HiThinkResponse:
        """Issue a bounded GET and optionally publish the successful raw response.

        Business ``code`` is checked even when HTTP status is 200.  Network failures, HTTP
        5xx, rate-limit code 4001 and provider 5xxx codes are retried; every other violation
        fails closed immediately.
        """

        safe_endpoint = _validate_endpoint(endpoint)
        safe_params = _copy_non_sensitive_params(params or {}, api_key=self._api_key)
        query = urllib.parse.urlencode(sorted(safe_params.items()))
        url = f"{HITHINK_BASE_URL}{safe_endpoint}"
        if query:
            url = f"{url}?{query}"
        if len(url) > MAX_QUERY_LENGTH:
            raise ValueError("HiThink request URL exceeds the configured safety limit")
        request = HiThinkRequest(
            url=url,
            headers=MappingProxyType({"Accept": "application/json", "X-api-key": self._api_key}),
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )

        for attempt in range(1, self._max_attempts + 1):
            try:
                http_response = self._transport(request)
            except (_NetworkFailure, TimeoutError, OSError):
                if attempt == self._max_attempts:
                    raise HiThinkTransportError(
                        f"HiThink request failed after {self._max_attempts} attempts"
                    ) from None
                self._sleep_before_retry(attempt)
                continue

            _validate_http_response_shape(http_response)
            if http_response.status != 200:
                if 500 <= http_response.status <= 599 and attempt < self._max_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise HiThinkProtocolError(
                    f"HiThink returned unexpected HTTP status {http_response.status}"
                )

            if len(http_response.body) > self._max_response_bytes:
                raise HiThinkProtocolError("HiThink response exceeded the configured size limit")
            _require_json_content_type(http_response.headers)
            if self._api_key.encode("utf-8") in http_response.body:
                raise HiThinkProtocolError(
                    "HiThink response contained credential material and was rejected"
                )
            envelope = _decode_envelope(http_response.body)
            code = envelope["code"]
            request_id = envelope["request_id"]
            if code != 0:
                if _is_transient_business_code(code) and attempt < self._max_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise HiThinkAPIError(code, request_id)

            response = HiThinkResponse(
                data=envelope["data"],
                request_id=request_id,
                retrieved_at=_read_clock(self._clock),
                raw_body=http_response.body,
                endpoint=safe_endpoint,
                non_sensitive_params=safe_params,
            )
            if self._raw_store is not None:
                artifact = self._raw_store.publish(response)
                artifact_id = getattr(artifact, "artifact_id", None)
                if not isinstance(artifact_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9._-]{1,160}", artifact_id
                ):
                    raise HiThinkProtocolError("raw store returned an invalid artifact identifier")
                response = replace(response, raw_artifact_id=artifact_id)
            return response

        raise AssertionError("bounded retry loop exited unexpectedly")

    def _sleep_before_retry(self, failed_attempt: int) -> None:
        delay = min(
            self._retry_backoff_seconds * (2 ** (failed_attempt - 1)),
            MAX_RETRY_DELAY_SECONDS,
        )
        self._sleeper(delay)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _read_http_response(response: Any, max_response_bytes: int) -> HiThinkHttpResponse:
    raw_headers = getattr(response, "headers", {})
    headers = {str(name): str(value) for name, value in raw_headers.items()}
    content_length = _header_value(headers, "content-length")
    if content_length is not None:
        try:
            advertised_length = int(content_length)
        except ValueError:
            raise HiThinkProtocolError("HiThink returned an invalid Content-Length") from None
        if advertised_length < 0:
            raise HiThinkProtocolError("HiThink returned an invalid Content-Length")
        if advertised_length > max_response_bytes:
            raise HiThinkProtocolError("HiThink response exceeded the configured size limit")
    body = response.read(max_response_bytes + 1)
    if not isinstance(body, bytes):
        raise HiThinkProtocolError("HiThink transport returned a non-bytes response body")
    if len(body) > max_response_bytes:
        raise HiThinkProtocolError("HiThink response exceeded the configured size limit")
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return HiThinkHttpResponse(status=int(status), headers=headers, body=body)


def _resolve_api_key(injected: str | None) -> str:
    candidate = os.environ.get(HITHINK_API_KEY_ENV) if injected is None else injected
    if not isinstance(candidate, str) or not candidate.strip():
        raise HiThinkConfigurationError(
            f"configure {HITHINK_API_KEY_ENV} or inject an API key into HiThinkClient"
        )
    normalized = candidate.strip()
    if "\r" in normalized or "\n" in normalized:
        raise HiThinkConfigurationError("HiThink API key contains invalid control characters")
    if len(normalized) > 4096:
        raise HiThinkConfigurationError("HiThink API key exceeds the supported length")
    return normalized


def _validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not _ENDPOINT_PATTERN.fullmatch(endpoint):
        raise ValueError("endpoint must be a relative /api/... path without query or fragment")
    if "//" in endpoint or "/../" in endpoint or endpoint.endswith("/.."):
        raise ValueError("endpoint contains an unsafe path segment")
    return endpoint


def _copy_non_sensitive_params(
    params: Mapping[str, str | int], *, api_key: str
) -> dict[str, str | int]:
    if not isinstance(params, Mapping):
        raise TypeError("params must be a mapping")
    normalized: dict[str, str | int] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not _PARAMETER_NAME_PATTERN.fullmatch(name):
            raise ValueError("parameter names must use letters, numbers and underscores")
        lowered = name.casefold()
        if _is_sensitive_parameter_name(lowered):
            raise HiThinkConfigurationError("sensitive values cannot be supplied as query params")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"parameter {name} must be a string or integer")
        if isinstance(value, str):
            if "\r" in value or "\n" in value or "\x00" in value:
                raise ValueError(f"parameter {name} contains invalid control characters")
            if value == api_key:
                raise HiThinkConfigurationError(
                    "API credentials cannot be supplied as query parameter values"
                )
            if _looks_like_url_or_absolute_path(value):
                raise HiThinkConfigurationError(
                    f"parameter {name} cannot contain a URL or absolute path"
                )
        normalized[name] = value
    return normalized


def _is_sensitive_parameter_name(lowered: str) -> bool:
    if lowered in _SENSITIVE_PARAMETER_NAMES:
        return True
    return lowered.endswith(("_api_key", "_key", "_token", "_secret", "_password", "_url", "_path"))


def _looks_like_url_or_absolute_path(value: str) -> bool:
    decoded = urllib.parse.unquote(value)
    parsed = urllib.parse.urlsplit(decoded)
    if parsed.scheme.casefold() in {"http", "https", "file"} or parsed.netloc:
        return True
    return decoded.startswith(("/", "~/")) or PureWindowsPath(decoded).is_absolute()


def _validate_http_response_shape(response: Any) -> None:
    if not isinstance(response, HiThinkHttpResponse):
        raise HiThinkProtocolError("HiThink transport returned an invalid response object")
    if isinstance(response.status, bool) or not isinstance(response.status, int):
        raise HiThinkProtocolError("HiThink transport returned an invalid HTTP status")
    if not isinstance(response.headers, Mapping):
        raise HiThinkProtocolError("HiThink transport returned invalid headers")
    if not isinstance(response.body, bytes):
        raise HiThinkProtocolError("HiThink transport returned a non-bytes response body")


def _require_json_content_type(headers: Mapping[str, str]) -> None:
    value = _header_value(headers, "content-type")
    if value is not None and "application/json" not in value.casefold():
        raise HiThinkProtocolError("HiThink returned a non-JSON Content-Type")


def _header_value(headers: Mapping[str, str], expected_name: str) -> str | None:
    for name, value in headers.items():
        if str(name).casefold() == expected_name:
            return str(value)
    return None


def _decode_envelope(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _InvalidJsonConstant,
        RecursionError,
    ):
        raise HiThinkProtocolError("HiThink returned malformed JSON") from None
    if not isinstance(decoded, dict):
        raise HiThinkProtocolError("HiThink response envelope must be a JSON object")
    required = {"code", "message", "request_id", "data"}
    if not required.issubset(decoded):
        raise HiThinkProtocolError("HiThink response envelope omitted required fields")
    code = decoded["code"]
    if isinstance(code, bool) or not isinstance(code, int):
        raise HiThinkProtocolError("HiThink response code must be an integer")
    if not isinstance(decoded["message"], str):
        raise HiThinkProtocolError("HiThink response message must be a string")
    request_id = decoded["request_id"]
    if not isinstance(request_id, str) or not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise HiThinkProtocolError("HiThink response request_id is invalid")
    return decoded


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateJsonKey
        result[name] = value
    return result


def _reject_json_constant(_: str) -> Any:
    raise _InvalidJsonConstant


def _is_transient_business_code(code: int) -> bool:
    return code in _TRANSIENT_BUSINESS_CODES or 5000 <= code <= 5999


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HiThinkConfigurationError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _positive_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _bounded_attempt_count(value: int) -> int:
    normalized = _positive_integer(value, "max_attempts")
    if normalized > MAX_ATTEMPTS_LIMIT:
        raise ValueError(f"max_attempts cannot exceed {MAX_ATTEMPTS_LIMIT}")
    return normalized


def _positive_number(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return float(value)


def _nonnegative_number(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return float(value)
