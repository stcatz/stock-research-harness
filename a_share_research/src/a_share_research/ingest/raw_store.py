"""Immutable on-disk audit store for exact HiThink API responses."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path, PureWindowsPath
from typing import Any

from .hithink_client import HiThinkResponse

_ENDPOINT_PATTERN = re.compile(r"/api/[A-Za-z0-9_/-]+\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PARAMETER_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_SENSITIVE_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "key",
    "password",
    "presigned_url",
    "secret",
    "token",
    "x_api_key",
}


class RawStoreError(RuntimeError):
    """Base error for immutable raw-response storage."""


class RawStoreValidationError(RawStoreError):
    """Raised before publication when metadata is unsafe or malformed."""


class RawStoreCollisionError(RawStoreError):
    """Raised when publication would replace an existing artifact."""


@dataclass(frozen=True)
class RawArtifact:
    artifact_id: str
    directory: Path
    body_path: Path
    manifest_path: Path


class ImmutableRawStore:
    """Atomically publish ``body.json`` and a credential-free manifest.

    Each artifact is built in a private temporary directory and exposed with one directory
    rename.  The final artifact identifier is content- and time-addressed; an existing target
    is treated as a collision and is never replaced.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise RawStoreValidationError("raw store root must be a directory")

    def publish(self, response: HiThinkResponse) -> RawArtifact:
        metadata = _validated_metadata(response)
        digest = response.body_sha256
        retrieved_utc = response.retrieved_at.astimezone(UTC)
        timestamp = retrieved_utc.strftime("%Y%m%dT%H%M%S%fZ")
        identity_material = json.dumps(
            {
                "body_sha256": digest,
                "endpoint": metadata["endpoint"],
                "params": metadata["params"],
                "request_id": metadata["request_id"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        identity_digest = hashlib.sha256(identity_material).hexdigest()
        artifact_id = f"hithink-{timestamp}-{identity_digest[:16]}"
        final_directory = self.root / artifact_id
        if final_directory.exists():
            raise RawStoreCollisionError("raw response artifact already exists")

        manifest = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "provider": {
                "name": metadata["provider"],
                "version": metadata["provider_version"],
            },
            "endpoint": metadata["endpoint"],
            "params": metadata["params"],
            "request_id": metadata["request_id"],
            "retrieved_at": retrieved_utc.isoformat(),
            "body_sha256": digest,
            "body_bytes": len(response.raw_body),
            "body_file": "body.json",
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        temporary_directory = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.root))
        try:
            body_path = temporary_directory / "body.json"
            manifest_path = temporary_directory / "manifest.json"
            _write_exclusive(body_path, response.raw_body)
            _write_exclusive(manifest_path, manifest_bytes)
            _fsync_directory(temporary_directory)
            try:
                os.rename(temporary_directory, final_directory)
            except OSError as exc:
                if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise RawStoreCollisionError("raw response artifact already exists") from None
                raise
            _fsync_directory(self.root)
        except Exception:
            if temporary_directory.exists():
                shutil.rmtree(temporary_directory)
            raise

        return RawArtifact(
            artifact_id=artifact_id,
            directory=final_directory,
            body_path=final_directory / "body.json",
            manifest_path=final_directory / "manifest.json",
        )


def _validated_metadata(response: HiThinkResponse) -> dict[str, Any]:
    if not isinstance(response, HiThinkResponse):
        raise RawStoreValidationError("raw store accepts only HiThinkResponse values")
    if not isinstance(response.raw_body, bytes) or not response.raw_body:
        raise RawStoreValidationError("raw response body must be non-empty bytes")
    if response.retrieved_at.tzinfo is None or response.retrieved_at.utcoffset() is None:
        raise RawStoreValidationError("retrieved_at must be timezone-aware")
    if not _ENDPOINT_PATTERN.fullmatch(response.endpoint) or "//" in response.endpoint:
        raise RawStoreValidationError("endpoint must be a safe relative /api/... path")
    if not _IDENTIFIER_PATTERN.fullmatch(response.request_id):
        raise RawStoreValidationError("request_id is invalid")
    if not _IDENTIFIER_PATTERN.fullmatch(response.provider):
        raise RawStoreValidationError("provider is invalid")
    if not _IDENTIFIER_PATTERN.fullmatch(response.provider_version):
        raise RawStoreValidationError("provider_version is invalid")
    params = _validated_params(response.non_sensitive_params)
    return {
        "provider": response.provider,
        "provider_version": response.provider_version,
        "endpoint": response.endpoint,
        "params": params,
        "request_id": response.request_id,
    }


def _validated_params(params: Mapping[str, str | int]) -> dict[str, str | int]:
    if not isinstance(params, Mapping):
        raise RawStoreValidationError("params must be a mapping")
    result: dict[str, str | int] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not _PARAMETER_NAME_PATTERN.fullmatch(name):
            raise RawStoreValidationError("parameter name is invalid")
        lowered = name.casefold()
        if _is_sensitive_name(lowered):
            raise RawStoreValidationError("sensitive parameters cannot enter the manifest")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise RawStoreValidationError("manifest parameter values must be strings or integers")
        if isinstance(value, str):
            if "\r" in value or "\n" in value or "\x00" in value:
                raise RawStoreValidationError("manifest parameter contains control characters")
            if _looks_like_url_or_absolute_path(value):
                raise RawStoreValidationError("URLs and absolute paths cannot enter the manifest")
        result[name] = value
    return dict(sorted(result.items()))


def _is_sensitive_name(lowered: str) -> bool:
    if lowered in _SENSITIVE_NAMES:
        return True
    return lowered.endswith(("_api_key", "_key", "_token", "_secret", "_password", "_url", "_path"))


def _looks_like_url_or_absolute_path(value: str) -> bool:
    decoded = urllib.parse.unquote(value)
    parsed = urllib.parse.urlsplit(decoded)
    if parsed.scheme.casefold() in {"http", "https", "file"} or parsed.netloc:
        return True
    return decoded.startswith(("/", "~/")) or PureWindowsPath(decoded).is_absolute()


def _write_exclusive(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
