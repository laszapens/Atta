# -*- coding: utf-8 -*-
"""Cross-process build lock.

Host plugins may spawn one Python process per tool call.  Two builds writing
the same workspace at once would still produce valid JSON (writes are atomic),
but the later record could silently win.  A single advisory lock makes
"one build at a time per workspace" explicit.

The lock is advisory and local to a workspace; it is not a distributed lock.
"""

from __future__ import annotations

import fcntl
import threading
import time
from contextlib import contextmanager

from . import state
from .errors import AttaError

_PROC_LOCK = threading.RLock()


@contextmanager
def project_lock(exclusive: bool = True, timeout: float = 600.0):
    path = state.atta_dir() / "project.lock"
    with _PROC_LOCK:
        fh = open(path, "a+", encoding="utf-8")
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + float(timeout)
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AttaError(
                            "lock_timeout",
                            "workspace is busy with another build",
                            path=str(path),
                            fix="wait for the other model call, or use a separate ATTA_HOME",
                        )
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


__all__ = ["project_lock"]
