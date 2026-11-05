"""Cross-process lock so a scheduled cycle and a Desk-triggered scan never
write the same databases/report files at the same time."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent / ".cycle.lock"


class LockBusy(RuntimeError):
    pass


@contextmanager
def cycle_lock(label: str, path: Path = LOCK_PATH):
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip() or "another run"
            raise LockBusy(f"busy: {holder}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{label} (pid {os.getpid()})")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
