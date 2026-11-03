# -*- coding: utf-8 -*-
"""Cross-process build lock.

Host plugins may spawn one Python process per tool call.  Two builds writing
the same workspace at once would still produce valid JSON (writes are atomic),
but the later record could silently win.  A single advisory lock makes
"one build at a time per workspace" explicit.

The lock is advisory and local to a workspace; it is not a distributed lock.

Portability
-----------
There is no portable cross-process file lock in the standard library, so this
module picks the best primitive the platform actually has:

* POSIX  -- ``fcntl.flock`` (advisory, released by the kernel if we die).
* Windows -- ``msvcrt.locking`` on a reserved byte.  Windows has no ``flock``;
  importing ``fcntl`` there raises ``ImportError``, which used to make *every*
  ``atta_model`` call fail at the import line rather than at the lock.
* Neither available (exotic build, restricted sandbox) -- an in-process only
  lock.  Degrading to "serialised within this process" is honest and still
  strictly better than no lock; the contract we guarantee everywhere is
  re-entrancy plus mutual exclusion, and both hold.

The lock file is opened in text mode with an explicit ``utf-8`` encoding so a
non-ASCII ``ATTA_HOME`` behaves the same on Windows (cp1252 default) as on
Linux.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import state
from .errors import AttaError

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    _fcntl = None

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

#: Windows locks a byte range; byte 0 of the lock file is reserved for it.
_LOCK_BYTES = 1

_PROC_LOCK = threading.RLock()
#: thread id -> (depth, path) so a nested ``project_lock`` in the same thread
#: does not deadlock on a non-reentrant OS lock.
_HELD: dict[int, list] = {}


def backend() -> str:
    """Which primitive this interpreter will use: ``fcntl`` | ``msvcrt`` | ``none``."""
    if _fcntl is not None:
        return "fcntl"
    if _msvcrt is not None:
        return "msvcrt"
    return "none"


def _is_lock_busy(exc: OSError) -> bool:
    """True when ``exc`` means "someone else holds it", not a real failure."""
    if isinstance(exc, BlockingIOError):
        return True
    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                     errno.EDEADLK, errno.EBUSY):
        return True
    # msvcrt raises a plain OSError with errno 13 (EACCES) on contention, and
    # on some WinPython builds the errno is unavailable -- treat "locked" as
    # busy when the message says so.
    return "lock" in str(exc).lower() and "fail" in str(exc).lower()


def _try_lock(fh, exclusive: bool) -> bool:
    """Non-blocking attempt; returns True when acquired."""
    kind = backend()
    if kind == "fcntl":
        mode = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
        try:
            _fcntl.flock(fh.fileno(), mode | _fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if _is_lock_busy(exc):
                return False
            raise
    if kind == "msvcrt":  # pragma: no cover - Windows only
        # msvcrt has no shared lock; an exclusive lock is a strict superset
        # and keeps the same mutual-exclusion contract on Windows.
        try:
            fh.seek(0)
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, _LOCK_BYTES)
            return True
        except OSError as exc:
            if _is_lock_busy(exc):
                return False
            raise
    return True  # in-process lock only (see _PROC_LOCK)


def _unlock(fh) -> None:
    kind = backend()
    try:
        if kind == "fcntl":
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        elif kind == "msvcrt":  # pragma: no cover - Windows only
            fh.seek(0)
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, _LOCK_BYTES)
    except OSError:
        pass


def _open_lock_file(path: Path):
    """Open the lock file so Windows can also lock a zero-length file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        # msvcrt.locking() cannot lock past EOF; make sure byte 0 exists.
        if backend() == "msvcrt":  # pragma: no cover - Windows only
            fh.seek(0, os.SEEK_END)
            if fh.tell() < _LOCK_BYTES:
                fh.write("\0")
                fh.flush()
    except OSError:
        pass
    return fh


@contextmanager
def project_lock(exclusive: bool = True, timeout: float = 600.0):
    """Serialise builds that write the same workspace.

    Re-entrant: taking the lock twice in one thread is a no-op the second time
    (the inner block is already protected by the outer one).
    """
    path = state.atta_dir() / "project.lock"
    depth = _HELD.get(threading.get_ident(), [0])
    if depth[0] > 0:
        depth[0] += 1
        try:
            yield
        finally:
            depth[0] -= 1
        return

    with _PROC_LOCK:
        fh = _open_lock_file(path)
        deadline = time.monotonic() + float(timeout)
        acquired = False
        depth[0] = 1
        _HELD[threading.get_ident()] = depth
        try:
            while True:
                if _try_lock(fh, exclusive):
                    acquired = True
                    break
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
            depth[0] = 0
            _HELD.pop(threading.get_ident(), None)
            if acquired:
                _unlock(fh)
            fh.close()


__all__ = ["backend", "project_lock"]
