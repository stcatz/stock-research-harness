from __future__ import annotations

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def run_lock(workspace: Path, run_id: str, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Serialize writers for one deterministic run id on POSIX hosts."""

    lock_directory = workspace.resolve() / "artifacts" / ".locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock_path = lock_directory / f"{run_id}.lock"
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for research run lock: {run_id}")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
