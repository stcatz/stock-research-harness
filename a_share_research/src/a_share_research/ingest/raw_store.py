"""Immutable on-disk audit store for exact HiThink API responses."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
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
        expanded = Path(root).expanduser()
        self.root = Path(os.path.abspath(os.fspath(expanded)))
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise RawStoreValidationError("raw store root could not be created securely") from None
        root_descriptor = _open_directory_path(self.root)
        try:
            os.fchmod(root_descriptor, 0o700)
            root_stat = os.fstat(root_descriptor)
            _verify_directory_identity(self.root, root_stat)
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        except RawStoreValidationError:
            raise
        except OSError:
            raise RawStoreValidationError("raw store root could not be secured") from None
        finally:
            os.close(root_descriptor)

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

        root_descriptor = self._open_verified_root()
        temporary_name: str | None = None
        temporary_descriptor: int | None = None
        published = False
        try:
            if _entry_exists(root_descriptor, artifact_id):
                raise RawStoreCollisionError("raw response artifact already exists")
            temporary_name = _make_private_temporary_directory(root_descriptor)
            temporary_descriptor = _open_directory_at(root_descriptor, temporary_name)
            os.fchmod(temporary_descriptor, 0o700)
            _write_exclusive_at(temporary_descriptor, "body.json", response.raw_body)
            _write_exclusive_at(temporary_descriptor, "manifest.json", manifest_bytes)
            os.fsync(temporary_descriptor)
            if _entry_exists(root_descriptor, artifact_id):
                raise RawStoreCollisionError("raw response artifact already exists")
            try:
                os.rename(
                    temporary_name,
                    artifact_id,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=root_descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR} and _entry_exists(
                    root_descriptor, artifact_id
                ):
                    raise RawStoreCollisionError("raw response artifact already exists") from None
                raise
            published = True
            os.fsync(root_descriptor)
            self._verify_open_root(root_descriptor)
        except (RawStoreCollisionError, RawStoreValidationError):
            raise
        except OSError:
            raise RawStoreError("raw response artifact could not be published securely") from None
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_name is not None and not published:
                _remove_private_temporary_directory(root_descriptor, temporary_name)
            os.close(root_descriptor)

        final_directory = self.root / artifact_id

        return RawArtifact(
            artifact_id=artifact_id,
            directory=final_directory,
            body_path=final_directory / "body.json",
            manifest_path=final_directory / "manifest.json",
        )

    def _open_verified_root(self) -> int:
        descriptor = _open_directory_path(self.root)
        try:
            self._verify_open_root(descriptor)
            os.fchmod(descriptor, 0o700)
        except RawStoreValidationError:
            os.close(descriptor)
            raise
        except OSError:
            os.close(descriptor)
            raise RawStoreValidationError("raw store root could not be secured") from None
        return descriptor

    def _verify_open_root(self, descriptor: int) -> None:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != self._root_identity:
            raise RawStoreValidationError("raw store root identity changed")
        _verify_directory_identity(self.root, opened)


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


def _open_directory_path(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RawStoreValidationError("raw store root must be a non-symlink directory") from None
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise RawStoreValidationError("raw store root must be a non-symlink directory")
    return descriptor


def _verify_directory_identity(path: Path, opened: os.stat_result) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise RawStoreValidationError("raw store root identity changed") from None
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise RawStoreValidationError("raw store root identity changed")


def _open_directory_at(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _entry_exists(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _make_private_temporary_directory(root_descriptor: int) -> str:
    for _ in range(32):
        name = f".tmp-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
        except FileExistsError:
            continue
        return name
    raise RawStoreError("raw response temporary artifact name could not be allocated")


def _write_exclusive_at(directory_descriptor: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "short raw artifact write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_private_temporary_directory(root_descriptor: int, name: str) -> None:
    try:
        temporary_descriptor = _open_directory_at(root_descriptor, name)
    except OSError:
        return
    try:
        for child in ("body.json", "manifest.json"):
            try:
                os.unlink(child, dir_fd=temporary_descriptor)
            except OSError:
                pass
    finally:
        os.close(temporary_descriptor)
    try:
        os.rmdir(name, dir_fd=root_descriptor)
    except OSError:
        pass
