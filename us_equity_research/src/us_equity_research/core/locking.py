from __future__ import annotations

import fcntl
import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def run_lock(workspace: Path, run_id: str, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Serialize materialization of one deterministic run on POSIX systems."""

    with _advisory_lock(workspace, f"run:{run_id}", timeout_seconds=timeout_seconds):
        yield


@contextmanager
def database_lock(workspace: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Serialize schema changes and audit writes to the shared US database."""

    with _advisory_lock(workspace, "database:us", timeout_seconds=timeout_seconds):
        yield


@contextmanager
def _advisory_lock(
    workspace: Path,
    key: str,
    *,
    timeout_seconds: float,
) -> Iterator[None]:
    lock_directory = workspace.resolve() / "artifacts" / "us" / ".locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
    lock_path = lock_directory / lock_name
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for US research lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
