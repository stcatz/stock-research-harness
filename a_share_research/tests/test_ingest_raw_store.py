from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from a_share_research.ingest.hithink_client import HiThinkResponse
from a_share_research.ingest.raw_store import (
    ImmutableRawStore,
    RawStoreCollisionError,
    RawStoreValidationError,
)


def _response(
    *,
    params: dict[str, str | int] | None = None,
    raw_body: bytes | None = None,
) -> HiThinkResponse:
    body = raw_body or (b'{"code":0,"message":"ok","request_id":"req-raw-1","data":{"rows":[]}}')
    return HiThinkResponse(
        data={"rows": []},
        request_id="req-raw-1",
        retrieved_at=datetime(2026, 8, 23, 2, 3, 4, 567890, tzinfo=UTC),
        raw_body=body,
        endpoint="/api/a-share/prices/historical",
        non_sensitive_params=params or {"thscode": "600519.SH", "limit": 20},
        provider="hithink-financial-api",
        provider_version="public-api-unversioned",
    )


class ImmutableRawStoreTests(unittest.TestCase):
    def test_publish_atomically_separates_body_and_non_sensitive_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ImmutableRawStore(Path(temporary_directory) / "provider_raw")
            response = _response()

            artifact = store.publish(response)

            self.assertTrue(artifact.directory.is_dir())
            self.assertEqual(artifact.body_path.read_bytes(), response.raw_body)
            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["artifact_id"], artifact.artifact_id)
            self.assertEqual(manifest["endpoint"], response.endpoint)
            self.assertEqual(manifest["params"], dict(response.non_sensitive_params))
            self.assertEqual(manifest["request_id"], response.request_id)
            self.assertEqual(manifest["retrieved_at"], "2026-08-23T02:03:04.567890+00:00")
            self.assertEqual(manifest["body_sha256"], hashlib.sha256(response.raw_body).hexdigest())
            self.assertEqual(manifest["body_bytes"], len(response.raw_body))
            self.assertEqual(
                manifest["provider"],
                {"name": "hithink-financial-api", "version": "public-api-unversioned"},
            )
            self.assertEqual(manifest["body_file"], "body.json")
            self.assertFalse(Path(manifest["body_file"]).is_absolute())
            self.assertNotIn(
                str(Path(temporary_directory).resolve()), artifact.manifest_path.read_text()
            )
            self.assertEqual(os.stat(artifact.body_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(artifact.manifest_path).st_mode & 0o777, 0o600)
            self.assertFalse(any(path.name.startswith(".tmp-") for path in store.root.iterdir()))

    def test_existing_artifact_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ImmutableRawStore(Path(temporary_directory) / "provider_raw")
            response = _response()
            first = store.publish(response)
            original_manifest = first.manifest_path.read_bytes()

            with self.assertRaises(RawStoreCollisionError):
                store.publish(response)

            self.assertEqual(first.manifest_path.read_bytes(), original_manifest)
            self.assertEqual(first.body_path.read_bytes(), response.raw_body)

    def test_manifest_rejects_sensitive_url_and_path_parameters(self) -> None:
        unsafe_params = (
            {"api_key": "secret"},
            {"presigned_url": "https://objects.example/signed"},
            {"redirect": "https://objects.example/signed"},
            {"redirect": "https%3A%2F%2Fobjects.example%2Fsigned"},
            {"output": "/Users/example/data.json"},
            {"windows_output": "C:\\Users\\example\\data.json"},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ImmutableRawStore(Path(temporary_directory) / "provider_raw")
            for params in unsafe_params:
                with (
                    self.subTest(params=params),
                    self.assertRaises(RawStoreValidationError),
                ):
                    store.publish(_response(params=params))

            self.assertEqual(list(store.root.iterdir()), [])

    def test_invalid_timestamp_endpoint_or_provider_fails_before_writing(self) -> None:
        base = _response()
        invalid_responses = (
            HiThinkResponse(
                data=base.data,
                request_id=base.request_id,
                retrieved_at=base.retrieved_at.replace(tzinfo=None),
                raw_body=base.raw_body,
                endpoint=base.endpoint,
                non_sensitive_params=base.non_sensitive_params,
                provider=base.provider,
                provider_version=base.provider_version,
            ),
            HiThinkResponse(
                data=base.data,
                request_id=base.request_id,
                retrieved_at=base.retrieved_at,
                raw_body=base.raw_body,
                endpoint="https://evil.example/api/data",
                non_sensitive_params=base.non_sensitive_params,
                provider=base.provider,
                provider_version=base.provider_version,
            ),
            HiThinkResponse(
                data=base.data,
                request_id=base.request_id,
                retrieved_at=base.retrieved_at,
                raw_body=base.raw_body,
                endpoint=base.endpoint,
                non_sensitive_params=base.non_sensitive_params,
                provider="",
                provider_version=base.provider_version,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ImmutableRawStore(Path(temporary_directory) / "provider_raw")
            for response in invalid_responses:
                with (
                    self.subTest(response=response),
                    self.assertRaises(RawStoreValidationError),
                ):
                    store.publish(response)

            self.assertEqual(list(store.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
