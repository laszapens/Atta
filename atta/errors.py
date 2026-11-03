# -*- coding: utf-8 -*-
"""Error and envelope contracts shared by CLI, MCP, HTTP and the plugin.

Every failure is a *value*, never a traceback leaking through a protocol.
The structure is deliberately small::

    {"ok": false, "error": {"code", "message", "path", "fix", "details"}}

`fix` is the part that makes an LLM useful: a concrete patch, not an apology.
"""

from __future__ import annotations

import time
import traceback
from typing import Any


class AttaError(Exception):
    """A structured, actionable error."""

    def __init__(self, code: str, message: str, *, path: str | None = None,
                 fix: str | None = None, details: Any = None,
                 severity: str = "error"):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.path = path
        self.fix = fix
        self.details = details
        self.severity = severity

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message,
               "severity": self.severity}
        if self.path is not None:
            out["path"] = self.path
        if self.fix is not None:
            out["fix"] = self.fix
        if self.details is not None:
            out["details"] = self.details
        return out

    def __str__(self) -> str:  # pragma: no cover - display only
        extra = " (%s)" % self.path if self.path else ""
        return "%s: %s%s" % (self.code, self.message, extra)


def issue(code: str, message: str, *, severity: str = "error",
          path: str | None = None, fix: str | None = None,
          value: Any = None, expected: Any = None) -> dict:
    """Build one check/issue row in the public shape."""
    out = {"code": str(code), "severity": severity, "message": str(message)}
    if path is not None:
        out["path"] = path
    if fix is not None:
        out["fix"] = fix
    if value is not None:
        out["value"] = value
    if expected is not None:
        out["expected"] = expected
    return out


def ok(data: Any = None, *, op: str | None = None, ms: float | None = None,
       **extra) -> dict:
    out = {"ok": True, "data": data}
    if op is not None:
        out["op"] = op
    if ms is not None:
        out["ms"] = round(float(ms), 3)
    out.update(extra)
    return out


def fail(error: Any, *, op: str | None = None, ms: float | None = None,
         tb: str | None = None, **extra) -> dict:
    if isinstance(error, AttaError):
        payload = error.to_dict()
    elif isinstance(error, BaseException):
        payload = {"code": type(error).__name__, "message": str(error),
                   "severity": "error"}
    else:
        payload = error
    out = {"ok": False, "error": payload}
    if op is not None:
        out["op"] = op
    if ms is not None:
        out["ms"] = round(float(ms), 3)
    if tb:
        out["traceback"] = tb
    out.update(extra)
    return out


class Timer:
    """Tiny context manager used by tools to report honest elapsed time."""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.ms = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self.t0) * 1000.0
        return False

    def format_traceback(self) -> str:
        return traceback.format_exc()


__all__ = ["AttaError", "issue", "ok", "fail", "Timer"]
