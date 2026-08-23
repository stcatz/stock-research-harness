from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from a_share_research.ingest.hithink_client import (
    HITHINK_BASE_URL,
    HiThinkAPIError,
    HiThinkClient,
    HiThinkConfigurationError,
    HiThinkHttpResponse,
    HiThinkProtocolError,
    HiThinkRequest,
    HiThinkTransportError,
    UrllibHiThinkTransport,
)


class _QueueTransport:
    def __init__(self, *items: HiThinkHttpResponse | BaseException) -> None:
        self._items = list(items)
        self.requests: list[HiThinkRequest] = []

    def __call__(self, request: HiThinkRequest) -> HiThinkHttpResponse:
        self.requests.append(request)
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _RecordingStore:
    def __init__(self) -> None:
        self.responses: list[Any] = []

    def publish(self, response: Any) -> Any:
        self.responses.append(response)
        return type("Artifact", (), {"artifact_id": "raw-test-artifact"})()


def _body(
    *,
    code: int = 0,
    message: str = "ok",
    request_id: str = "req-123",
    data: Any = None,
) -> bytes:
    return json.dumps(
        {"code": code, "message": message, "request_id": request_id, "data": data},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _response(body: bytes, *, status: int = 200) -> HiThinkHttpResponse:
    return HiThinkHttpResponse(
        status=status,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=body,
    )


class HiThinkClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api_key = "test-secret-api-key"
        self.now = datetime(2026, 8, 23, 2, 3, 4, 567890, tzinfo=UTC)

    def test_get_uses_fixed_origin_and_returns_auditable_response(self) -> None:
        response_body = _body(data={"rows": [{"thscode": "600519.SH"}]})
        transport = _QueueTransport(_response(response_body))
        raw_store = _RecordingStore()
        client = HiThinkClient(
            api_key=self.api_key,
            transport=transport,
            clock=lambda: self.now,
            sleeper=lambda _: None,
            raw_store=raw_store,
        )

        result = client.get(
            "/api/a-share/prices/historical",
            {"thscode": "600519.SH", "start_date": "2026-08-01", "limit": 20},
        )

        request = transport.requests[0]
        self.assertTrue(request.url.startswith(f"{HITHINK_BASE_URL}/api/"))
        self.assertIn("limit=20", request.url)
        self.assertIn("start_date=2026-08-01", request.url)
        self.assertIn("thscode=600519.SH", request.url)
        self.assertEqual(request.headers["X-api-key"], self.api_key)
        self.assertNotIn(self.api_key, request.url)
        self.assertEqual(result.data["rows"][0]["thscode"], "600519.SH")
        self.assertEqual(result.request_id, "req-123")
        self.assertEqual(result.retrieved_at, self.now)
        self.assertEqual(result.raw_body, response_body)
        self.assertEqual(len(result.body_sha256), 64)
        self.assertEqual(result.endpoint, "/api/a-share/prices/historical")
        self.assertEqual(result.non_sensitive_params["limit"], 20)
        self.assertEqual(result.raw_artifact_id, "raw-test-artifact")
        self.assertEqual(raw_store.responses[0].raw_artifact_id, None)

    def test_key_must_come_from_injection_or_the_single_supported_environment_name(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(HiThinkConfigurationError, "HITHINK_FINANCE_API_KEY"),
        ):
            HiThinkClient(transport=_QueueTransport())

        transport = _QueueTransport(_response(_body(data={})))
        with patch.dict(
            os.environ,
            {
                "HITHINK_FINANCE_API_KEY": self.api_key,
                "FUYAO_TOKEN": "must-not-be-used",
                "API_KEY": "must-not-be-used-either",
            },
            clear=True,
        ):
            HiThinkClient(
                transport=transport,
                clock=lambda: self.now,
                sleeper=lambda _: None,
            ).get("/api/meta/tickers", {"query": "茅台"})

        self.assertEqual(transport.requests[0].headers["X-api-key"], self.api_key)

    def test_transient_network_and_business_failures_retry_with_bounded_backoff(self) -> None:
        transport = _QueueTransport(
            OSError(f"connection failed with {self.api_key}"),
            _response(_body(code=4001, message="rate limited", data=None)),
            _response(_body(code=5002, message="upstream busy", data=None)),
            _response(_body(data={"rows": []})),
        )
        sleeps: list[float] = []
        client = HiThinkClient(
            api_key=self.api_key,
            transport=transport,
            clock=lambda: self.now,
            sleeper=sleeps.append,
            max_attempts=4,
            retry_backoff_seconds=0.25,
        )

        result = client.get("/api/meta/tickers", {"query": "银行"})

        self.assertEqual(result.data, {"rows": []})
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(sleeps, [0.25, 0.5, 1.0])

    def test_non_transient_business_error_fails_closed_without_leaking_secret(self) -> None:
        transport = _QueueTransport(
            _response(_body(code=2002, message="credential rejected", data=None))
        )
        client = HiThinkClient(
            api_key=self.api_key,
            transport=transport,
            clock=lambda: self.now,
            sleeper=lambda _: self.fail("non-transient errors must not sleep"),
        )

        with self.assertRaises(HiThinkAPIError) as raised:
            client.get("/api/meta/tickers", {"query": "银行"})

        self.assertEqual(raised.exception.code, 2002)
        self.assertEqual(raised.exception.request_id, "req-123")
        self.assertNotIn(self.api_key, str(raised.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_transport_failure_exhaustion_does_not_chain_or_render_secret(self) -> None:
        transport = _QueueTransport(
            TimeoutError(self.api_key), TimeoutError(self.api_key), TimeoutError(self.api_key)
        )
        client = HiThinkClient(
            api_key=self.api_key,
            transport=transport,
            clock=lambda: self.now,
            sleeper=lambda _: None,
            max_attempts=3,
        )

        with self.assertRaises(HiThinkTransportError) as raised:
            client.get("/api/meta/tickers")

        self.assertNotIn(self.api_key, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(len(transport.requests), 3)

    def test_http_200_still_requires_valid_json_envelope_and_types(self) -> None:
        invalid_bodies = (
            b"not-json",
            b'[{"code":0}]',
            b'{"code":"0","message":"ok","request_id":"req","data":{}}',
            b'{"code":0,"message":"ok","request_id":"","data":{}}',
            b'{"code":0,"message":"ok","request_id":"req","data":{},"data":[]}',
            b'{"code":0,"message":"ok","request_id":"req","data":NaN}',
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                client = HiThinkClient(
                    api_key=self.api_key,
                    transport=_QueueTransport(_response(body)),
                    clock=lambda: self.now,
                    sleeper=lambda _: None,
                )
                with self.assertRaises(HiThinkProtocolError):
                    client.get("/api/meta/tickers")

    def test_response_size_and_content_type_are_checked_before_publication(self) -> None:
        store = _RecordingStore()
        oversized = _body(data={"payload": "x" * 300})
        client = HiThinkClient(
            api_key=self.api_key,
            transport=_QueueTransport(_response(oversized)),
            clock=lambda: self.now,
            sleeper=lambda _: None,
            raw_store=store,
            max_response_bytes=128,
        )
        with self.assertRaisesRegex(HiThinkProtocolError, "size limit"):
            client.get("/api/meta/tickers")
        self.assertEqual(store.responses, [])

        wrong_type = HiThinkHttpResponse(
            status=200,
            headers={"Content-Type": "text/html"},
            body=_body(data={}),
        )
        with self.assertRaisesRegex(HiThinkProtocolError, "Content-Type"):
            HiThinkClient(
                api_key=self.api_key,
                transport=_QueueTransport(wrong_type),
                clock=lambda: self.now,
                sleeper=lambda _: None,
            ).get("/api/meta/tickers")

    def test_endpoint_and_parameters_cannot_escape_origin_or_persist_secrets(self) -> None:
        client = HiThinkClient(
            api_key=self.api_key,
            transport=_QueueTransport(),
            clock=lambda: self.now,
            sleeper=lambda _: None,
        )
        invalid_calls = (
            ("https://evil.example/api/meta/tickers", {}),
            ("/api/meta/tickers?api_key=secret", {}),
            ("/api/../admin", {}),
            ("/api/meta/tickers", {"api_key": "secret"}),
            ("/api/meta/tickers", {"redirect_url": "https://evil.example/presigned"}),
            ("/api/meta/tickers", {"redirect": "https%3A%2F%2Fevil.example%2Fsigned"}),
            ("/api/meta/tickers", {"output_path": "/tmp/result.json"}),
            ("/api/meta/tickers", {"query": self.api_key}),
        )
        for endpoint, params in invalid_calls:
            with (
                self.subTest(endpoint=endpoint, params=params),
                self.assertRaises((ValueError, HiThinkConfigurationError)),
            ):
                client.get(endpoint, params)

    def test_default_transport_disables_redirects_that_could_forward_the_key(self) -> None:
        transport = UrllibHiThinkTransport()
        redirect_handler = next(
            handler
            for handler in transport._opener.handlers
            if type(handler).__name__ == "_NoRedirectHandler"
        )

        redirected = redirect_handler.redirect_request(
            object(), None, 302, "Found", {}, "https://evil.example/collect"
        )

        self.assertIsNone(redirected)

    def test_retry_budget_is_strictly_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            HiThinkClient(api_key=self.api_key, max_attempts=7)

    def test_success_body_that_echoes_the_api_key_is_never_published(self) -> None:
        store = _RecordingStore()
        client = HiThinkClient(
            api_key=self.api_key,
            transport=_QueueTransport(_response(_body(data={"echo": self.api_key}))),
            clock=lambda: self.now,
            sleeper=lambda _: None,
            raw_store=store,
        )

        with self.assertRaisesRegex(HiThinkProtocolError, "credential material"):
            client.get("/api/meta/tickers")
        self.assertEqual(store.responses, [])


if __name__ == "__main__":
    unittest.main()
