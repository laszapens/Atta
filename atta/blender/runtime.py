# -*- coding: utf-8 -*-
"""Host-side client for the resident Blender worker.

One Blender process per Python process, serialised requests, JSON lines over
stdin/stdout.  Blender stays alive between calls so a preview/verify/deliver
cycle pays the ~1 s startup once, not per render.

Wire protocol (one line per message, ``ATTA_JSON:`` prefix so Blender's own
stdout chatter can be skipped)::

    host -> worker  {"id": 7, "op": "render", "dir": "...", ...}\n
    worker -> host  ATTA_JSON:{"id": 7, "ok": true, ...}\n

Failures are values: :class:`BlenderError` carries a message plus the worker's
traceback.  A crashed or hung worker is killed and respawned on the next call.
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from .. import materials
from ..mesh import Mesh, canonical_hash
from .finish import normalize_finish

__all__ = [
    "BlenderError", "BlenderRuntime", "available", "blender_version",
    "find_blender", "runtime", "RENDER_VIEWS",
]

WORKER = Path(__file__).resolve().with_name("worker.py")
PROTOCOL_PREFIX = "ATTA_JSON:"
READY = "ATTA_READY"

#: Views the worker knows how to frame.  The interface contract guarantees
#: the first six; the rest are conveniences.
RENDER_VIEWS = ("iso", "front", "top", "left", "right", "back",
                "bottom", "iso_l", "iso_r", "hero", "hero_l", "hero_r")

EXPORT_FORMATS = ("glb", "obj", "stl", "blend")
RENDER_PASSES = ("beauty", "normal", "depth", "id", "curvature", "deviation")


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_passes(raw):
    out = []
    for value in (raw or ["beauty"]):
        name = str(value).strip().lower()
        seq = (["normal", "depth", "id", "curvature"]
               if name == "diagnostic" else [name])
        for item in seq:
            if item not in RENDER_PASSES:
                raise ValueError("unknown pass %r (use %s)" %
                                 (item, ", ".join(RENDER_PASSES)))
            if item not in out:
                out.append(item)
    return out or ["beauty"]

_WORKER_TIMEOUT = 120.0
_JOB_TIMEOUT = 300.0


class BlenderError(RuntimeError):
    """Blender backend failure with an actionable hint."""

    def __init__(self, message: str, *, code: str = "blender_error",
                 fix: str | None = None, details=None):
        super().__init__(message)
        self.code = code
        self.message = str(message)
        self.fix = fix
        self.details = details

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        if self.details is not None:
            out["details"] = self.details
        return out


# ----------------------------------------------------------------- discovery

#: Where each platform's installer actually puts Blender, since a GUI install
#: does not always update PATH (macOS especially, and Windows installer builds).
_PLATFORM_CANDIDATES = {
    "darwin": (
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "/Applications/Blender/Blender.app/Contents/MacOS/Blender",
    ),
    "win32": (
        r"C:\Program Files\Blender Foundation\Blender\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 3.6\blender.exe",
    ),
}


def _windows_versioned_candidates():
    """Blender installs to a version-named directory on Windows."""
    roots = [os.environ.get("ProgramFiles"),
             os.environ.get("ProgramFiles(x86)"),
             r"C:\Program Files"]
    names = []
    for major, minor in ((5, 2), (4, 5), (4, 4), (4, 3), (4, 2), (4, 1),
                         (4, 0), (3, 6), (3, 5), (3, 4), (3, 3), (3, 2)):
        names.append("Blender %d.%d" % (major, minor))
    out = []
    for root in filter(None, roots):
        for name in names:
            out.append(os.path.join(root, "Blender Foundation", name,
                                    "blender.exe"))
        out.append(os.path.join(root, "Blender Foundation", "Blender",
                                "blender.exe"))
    return out


def find_blender() -> str | None:
    """Locate the Blender executable.

    ``ATTA_BLENDER`` is authoritative when set: if it does not resolve, the
    result is ``None`` rather than silently using a different build.

    Discovery order: explicit override, PATH, then the per-platform install
    locations, because a normal macOS/Windows install does not put Blender on
    PATH and "not on PATH" must not mean "not installed".
    """
    override = os.environ.get("ATTA_BLENDER")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        return shutil.which(override)
    names = ("blender", "blender.exe") if os.name == "nt" else ("blender",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    known = list(_PLATFORM_CANDIDATES.get(sys.platform, ()))
    if os.name == "nt":
        known.extend(_windows_versioned_candidates())
    for path in known:
        try:
            if Path(path).is_file():
                return path
        except OSError:
            continue
    return None


#: Parsed ``blender --version`` cache keyed by executable path.
_VERSION_CACHE: dict[str, dict | None] = {}
_VERSION_LOCK = threading.Lock()


def blender_version(path: str | None = None, *, timeout: float = 20.0,
                    refresh: bool = False) -> dict | None:
    """``{"version": "4.2.1", "version_tuple": [4, 2, 1], "path": ...}``.

    Version drives behaviour (export operator names, EEVEE identity, shading
    APIs), so callers need the real number, not just "Blender exists".  The
    answer is cached per executable; ``--version`` costs ~0.16 s, which is too
    much to repeat on every tool call but cheap enough to pay once.
    """
    exe = path or find_blender()
    if not exe:
        return None
    key = str(exe)
    if not refresh:
        with _VERSION_LOCK:
            if key in _VERSION_CACHE:
                return _VERSION_CACHE[key]
    info = None
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, timeout=float(timeout),
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0))
        text = (proc.stdout or b"").decode("utf-8", "replace")
        first = text.strip().splitlines()[0] if text.strip() else ""
        # "Blender 4.2.1 LTS" / "Blender 3.6.5"
        parts = first.replace("Blender", "").split()
        nums = parts[0].split(".") if parts else []
        triple = [int(x) for x in nums[:3] if x.isdigit()]
        if triple:
            info = {"version": ".".join(str(n) for n in triple),
                    "version_tuple": triple, "path": key,
                    "raw": first.strip()}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        info = None
    with _VERSION_LOCK:
        _VERSION_CACHE[key] = info
    return info


def available() -> bool:
    return find_blender() is not None


# -------------------------------------------------------------------- runtime

class BlenderRuntime:
    """Persistent Blender subprocess used for finish/render/evaluate/export."""

    def __init__(self, root=None, *, timeout: float = _WORKER_TIMEOUT,
                 log_path=None):
        self.root = Path(root or os.getcwd()).expanduser().resolve()
        self.timeout = float(timeout)
        self.atta_dir = self.root / "_atta" / "blender"
        self.log_path = Path(log_path).expanduser() if log_path else (
            self.atta_dir / "blender.log")

        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue()
        self._seq = itertools.count(1)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._logfh = None
        self._log_lock = threading.Lock()
        self._ready = False
        self._stderr_tail: list[str] = []

    # ---------------------------------------------------------- lifecycle

    def available(self) -> bool:
        return find_blender() is not None

    def start(self):
        with self._lock:
            if self._alive():
                return self
            self._spawn()
            self._await_ready()
            return self

    def _alive(self) -> bool:
        return bool(self._ready and self._proc is not None
                    and self._proc.poll() is None)

    def _spawn(self):
        blender = find_blender()
        if not blender:
            raise BlenderError(
                "blender executable not found",
                code="blender_not_found",
                fix="install Blender or set ATTA_BLENDER=/path/to/blender")
        if not WORKER.is_file():
            raise BlenderError("worker script missing: %s" % WORKER,
                               code="blender_worker_missing")
        self._force_close()
        self._drain_queue()
        self.atta_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._logfh = open(self.log_path, "ab", buffering=0)
        cmd = [blender, "-b", "--factory-startup", "--python-exit-code", "1",
               "--python", str(WORKER), "--"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0, start_new_session=True)
        except OSError as exc:
            raise BlenderError("could not launch blender: %s" % exc,
                               code="blender_launch_failed",
                               fix="check that %r is executable" % blender) from exc
        self._ready = False
        self._reader = threading.Thread(target=self._read_loop,
                                        args=(self._proc,), daemon=True,
                                        name="atta-blender-reader")
        self._reader.start()

    def _read_loop(self, proc):
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                # Blender may emit a partial line (progress bars use \r) before
                # ours; look for the marker anywhere in the line, not just at
                # column zero.
                marker = line.rfind(PROTOCOL_PREFIX)
                if marker >= 0:
                    payload = line[marker + len(PROTOCOL_PREFIX):].strip()
                    try:
                        message = json.loads(payload)
                    except ValueError:
                        self._log("[atta] unparseable protocol line: %s" % line[:400])
                        continue
                    self._queue.put(message)
                    self._log("[atta] <- %s" % payload[:400])
                elif READY in line:
                    self._ready = True
                    self._queue.put({"__ready__": True})
                    self._log("[atta] worker ready")
                else:
                    self._log(line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._queue.put({"__eof__": True})
            self._log("[atta] worker stdout closed")

    def _log(self, line: str) -> None:
        tail = line[:2000]
        self._stderr_tail.append(tail)
        if len(self._stderr_tail) > 200:
            del self._stderr_tail[:-200]
        with self._log_lock:
            handle = self._logfh
            if handle is None:
                return
            try:
                handle.write((tail + "\n").encode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass

    def _log_tail(self, lines: int = 30) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
        except OSError:
            return "\n".join(self._stderr_tail[-lines:])

    def _await_ready(self):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._force_close()
                raise BlenderError(
                    "blender worker did not start within %.0fs" % self.timeout,
                    code="blender_start_timeout",
                    fix="check %s; raise BlenderRuntime(timeout=...)" % self.log_path,
                    details={"log": self._log_tail()})
            try:
                message = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._force_close()
                    raise BlenderError(
                        "blender exited during startup (code %s)" % code,
                        code="blender_start_failed",
                        fix="run `blender -b --factory-startup --python %s` "
                            "manually to see the error" % WORKER,
                        details={"log": self._log_tail()})
                continue
            if message.get("__ready__"):
                return
            if message.get("__eof__"):
                self._force_close()
                raise BlenderError("blender worker closed its stdout during startup",
                                   code="blender_start_failed",
                                   details={"log": self._log_tail()})

    def _drain_queue(self) -> None:
        """Drop stale responses/sentinels left by a previous worker."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _force_close(self):
        proc = self._proc
        self._ready = False
        self._proc = None
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        with self._log_lock:
            if self._logfh is not None:
                try:
                    self._logfh.close()
                except Exception:  # noqa: BLE001
                    pass
                self._logfh = None

    def close(self):
        with self._lock:
            proc = self._proc
            self._ready = False
            if proc is not None and proc.poll() is None:
                try:
                    self._send({"op": "shutdown", "id": 0})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            self._force_close()

    # ------------------------------------------------------------ transport

    def _send(self, payload: dict):
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise BlenderError("blender worker is not running",
                               code="blender_not_running")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BlenderError("blender worker pipe closed: %s" % exc,
                               code="blender_pipe_closed") from exc

    def _wait(self, request_id: int, timeout: float) -> dict:
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._force_close()
                raise BlenderError(
                    "blender request %d timed out after %.0fs" % (request_id, timeout),
                    code="blender_timeout",
                    fix="raise the timeout or reduce res/samples",
                    details={"log": self._log_tail()})
            try:
                message = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._force_close()
                    raise BlenderError(
                        "blender worker exited (code %s)" % code,
                        code="blender_crashed",
                        details={"log": self._log_tail()})
                continue
            if message.get("__eof__"):
                self._force_close()
                raise BlenderError("blender worker closed its stdout",
                                   code="blender_crashed",
                                   details={"log": self._log_tail()})
            if message.get("__ready__"):
                continue
            if message.get("id") != request_id:
                continue
            return message

    def request(self, req: dict, timeout: float | None = None) -> dict:
        """Send one request and return the worker's response dict.

        Raises :class:`BlenderError` when the worker reports ``ok: false``,
        times out, or dies.  A dead worker is restarted by the next call.
        """
        with self._lock:
            self.start()
            request_id = next(self._seq)
            payload = dict(req)
            payload["id"] = request_id
            try:
                self._send(payload)
                message = self._wait(request_id, float(timeout or self.timeout))
            except BlenderError:
                self._force_close()
                raise
            except Exception as exc:  # noqa: BLE001
                self._force_close()
                raise BlenderError("blender request failed: %s" % exc,
                                   code="blender_request_failed",
                                   details={"log": self._log_tail()}) from exc
            if not message.get("ok", False):
                raise BlenderError(str(message.get("error") or "blender op failed"),
                                   code="blender_op_failed",
                                   details={"worker": message,
                                            "traceback": message.get("traceback")})
            return message

    def ping(self) -> dict:
        return self.request({"op": "ping"}, timeout=30.0)

    # ---------------------------------------------------------------- jobs

    def _job_dir(self, workdir=None) -> Path:
        if workdir is not None:
            path = Path(workdir).expanduser().resolve()
        else:
            path = self.atta_dir / "jobs" / uuid.uuid4().hex[:12]
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_job(self, mesh: Mesh, finish: dict, workdir=None,
                   reuse_mesh: bool = False) -> Path:
        job = self._job_dir(workdir)
        mesh_path = job / "mesh.npz"
        if not (reuse_mesh and mesh_path.exists()):
            np.savez_compressed(
                mesh_path,
                verts=np.ascontiguousarray(mesh.verts, dtype=np.float64),
                faces=np.ascontiguousarray(mesh.faces, dtype=np.int64))
        (job / "finish.json").write_text(
            json.dumps(finish, sort_keys=True), encoding="utf-8")
        (job / "material.json").write_text(
            json.dumps(materials.resolve(finish["material"])), encoding="utf-8")
        return job

    def _cache_key(self, mesh: Mesh, finish: dict, res: int, samples: int) -> str:
        digest = hashlib.sha256()
        digest.update(b"atta.render/v1\n")
        digest.update(canonical_hash(mesh, q=1e-6).encode("ascii"))
        digest.update(json.dumps(finish, sort_keys=True).encode("utf-8"))
        digest.update(("%d:%d" % (int(res), int(samples))).encode("ascii"))
        return digest.hexdigest()

    # --------------------------------------------------------------- verbs

    def render(self, mesh: Mesh, finish: dict | None = None,
               views=("iso",), res: int = 512, samples: int = 8,
               workdir=None, *, dedupe: bool = True,
               timeout: float | None = None) -> dict:
        """Render judgement images; cache is per (mesh, finish, view, res, samples)."""
        norm = normalize_finish(finish)
        view_list = [str(v) for v in views]
        if not view_list:
            raise ValueError("views must not be empty")
        unknown = [v for v in view_list if v not in RENDER_VIEWS]
        if unknown:
            raise ValueError("unknown view(s) %s; known: %s"
                             % (", ".join(unknown), ", ".join(RENDER_VIEWS)))
        res = max(16, int(res))
        samples = max(1, int(samples))
        timeout = max(float(timeout or self.timeout), _JOB_TIMEOUT)

        cached: dict[str, dict] = {}
        missing: list[str] = []
        job = None
        if workdir is None and dedupe:
            key = self._cache_key(mesh, norm, res, samples)
            job = self._job_dir(self.atta_dir / "render_cache" / key[:32])
            for view in view_list:
                record = job / "out" / ("%s.json" % view)
                image = job / "out" / ("%s.png" % view)
                if record.is_file() and image.is_file():
                    try:
                        data = json.loads(record.read_text(encoding="utf-8"))
                    except ValueError:
                        data = None
                    if (data and data.get("bytes") == image.stat().st_size):
                        cached[view] = dict(data, cached=True)
                        continue
                missing.append(view)
        else:
            job = self._write_job(mesh, norm, workdir)
            missing = list(dict.fromkeys(view_list))

        response: dict = {"ok": True, "blender": None, "engine": None,
                          "images": [], "degraded": [], "views": view_list,
                          "res": res, "samples": samples, "dir": str(job),
                          "exact": False}
        rendered: dict[str, dict] = {}
        if missing:
            self._write_job(mesh, norm, workdir=job, reuse_mesh=True)
            response = self.request({"op": "render", "dir": str(job),
                                     "views": missing, "res": res,
                                     "samples": samples}, timeout=timeout)
            for image in response.get("images", []):
                view = str(image.get("view"))
                record = dict(image, cached=False)
                if response.get("blender"):
                    record.setdefault("blender", response["blender"])
                rendered[view] = record
                out = job / "out" / ("%s.json" % view)
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(record, sort_keys=True),
                                   encoding="utf-8")
                except OSError:
                    pass

        ordered = []
        for view in view_list:
            if view in rendered:
                ordered.append(rendered[view])
            elif view in cached:
                ordered.append(cached[view])
            else:  # pragma: no cover - rendering failed without raising
                raise BlenderError("missing render for view %r" % view,
                                   code="blender_render_missing")
        response = dict(response)
        response["images"] = ordered
        response["views"] = view_list
        response["cached_views"] = [v for v in view_list if v in cached]
        response["rendered_views"] = [v for v in view_list if v in rendered]
        if ordered:
            response["engine"] = response.get("engine") or ordered[0].get("engine")
            response["blender"] = response.get("blender") or ordered[0].get("blender")
        return response

    # ------------------------------------------------------- named-part scene

    def _write_parts_job(self, parts, settings: dict, workdir=None,
                         reuse: bool = False, reference=None) -> Path:
        job = self._job_dir(workdir)
        mesh_dir = job / "parts"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for i, part in enumerate(parts):
            mesh = part["mesh"]
            if not isinstance(mesh, Mesh):
                raise TypeError("part.mesh must be a Mesh")
            path = mesh_dir / ("%04d.npz" % i)
            if not (reuse and path.exists()):
                np.savez_compressed(
                    path,
                    verts=np.ascontiguousarray(mesh.verts, dtype=np.float64),
                    faces=np.ascontiguousarray(mesh.faces, dtype=np.int64))
            finish = normalize_finish(part.get("finish") or {})
            material = part.get("material")
            if isinstance(material, dict):
                mat_spec = dict(material)
            else:
                mat_spec = materials.resolve(str(material or finish["material"]))
            rows.append({
                "id": str(part.get("id") or "part_%d" % i),
                "mesh": str(path.relative_to(job)),
                "finish": finish,
                "material": mat_spec,
            })
        ref_name = None
        if reference is not None:
            ref_path = job / "reference.npz"
            if not (reuse and ref_path.exists()):
                np.savez_compressed(
                    ref_path,
                    verts=np.ascontiguousarray(reference.verts, dtype=np.float64),
                    faces=np.ascontiguousarray(reference.faces, dtype=np.int64))
            ref_name = str(ref_path.relative_to(job))
        (job / "parts.json").write_text(
            json.dumps({"parts": rows, "settings": settings or {},
                        "reference": ref_name}, sort_keys=True),
            encoding="utf-8")
        return job

    def _parts_cache_key(self, parts, settings: dict, res: int,
                         samples: int, reference=None) -> str:
        digest = hashlib.sha256()
        digest.update(b"atta.render_parts/v2\n")
        for part in parts:
            digest.update(str(part.get("id")).encode("utf-8"))
            digest.update(canonical_hash(part["mesh"], q=1e-6).encode("ascii"))
            finish = normalize_finish(part.get("finish") or {})
            digest.update(json.dumps(finish, sort_keys=True).encode("utf-8"))
            digest.update(json.dumps(part.get("material"), sort_keys=True,
                                     default=str).encode("utf-8"))
        digest.update(json.dumps(settings or {}, sort_keys=True,
                                 default=str).encode("utf-8"))
        digest.update(("%d:%d" % (int(res), int(samples))).encode("ascii"))
        if reference is not None:
            digest.update(canonical_hash(reference, q=1e-6).encode("ascii"))
        return digest.hexdigest()

    def render_parts(self, parts, *, settings=None, views=("iso",),
                     res: int = 512, samples: int = 8, workdir=None,
                     dedupe: bool = True, timeout: float | None = None,
                     reference=None) -> dict:
        """Render a named-part scene with per-part materials.

        ``parts`` is a list of ``{"id", "mesh", "finish", "material"}``.
        ``settings`` carries lighting/background/ground/lens/exposure/look.
        """
        parts = list(parts)
        if not parts:
            raise ValueError("parts must not be empty")
        settings = dict(settings or {})
        view_list = [str(v) for v in views]
        if not view_list:
            raise ValueError("views must not be empty")
        unknown = [v for v in view_list if v not in RENDER_VIEWS]
        if unknown:
            raise ValueError("unknown view(s) %s; known: %s"
                             % (", ".join(unknown), ", ".join(RENDER_VIEWS)))
        res = max(16, int(res))
        samples = max(1, int(samples))
        timeout = max(float(timeout or self.timeout), _JOB_TIMEOUT)
        passes = _normalize_passes(settings.get("passes"))

        def image_name(view, pass_name):
            return "%s.png" % view if pass_name == "beauty" else "%s_%s.png" % (
                view, pass_name)

        cached: dict[tuple[str, str], dict] = {}
        missing_views: set[str] = set()
        job = None
        if workdir is None and dedupe:
            key = self._parts_cache_key(parts, settings, res, samples, reference)
            job = self._job_dir(self.atta_dir / "render_cache" / ("parts_" + key[:32]))
            for view in view_list:
                for pass_name in passes:
                    name = image_name(view, pass_name)
                    record = job / "out" / (name[:-4] + ".json")
                    image = job / "out" / name
                    if record.is_file() and image.is_file():
                        try:
                            data = json.loads(record.read_text(encoding="utf-8"))
                        except ValueError:
                            data = None
                        if (data and data.get("bytes") == image.stat().st_size
                                and data.get("sha256") == _sha256_file(image)):
                            cached[(view, pass_name)] = dict(data, cached=True)
                            continue
                    missing_views.add(view)
        else:
            job = self._write_parts_job(parts, settings, workdir, reference=reference)
            missing_views = set(view_list)
        missing = [v for v in view_list if v in missing_views]

        response: dict = {"ok": True, "blender": None, "engine": None,
                          "images": [], "degraded": [], "views": view_list,
                          "res": res, "samples": samples, "dir": str(job),
                          "exact": False, "passes": passes}
        rendered: dict[tuple[str, str], dict] = {}
        if missing:
            self._write_parts_job(parts, settings, workdir=job, reuse=True,
                                  reference=reference)
            response = self.request(
                {"op": "render_parts", "dir": str(job), "views": missing,
                 "res": res, "samples": samples, "passes": passes,
                 **{k: settings[k] for k in (
                     "lighting", "background", "ground", "lens_mm",
                     "exposure", "look", "ortho", "distance_factor",
                 "deviation_range_mm", "focus", "aspect", "focus_padding")
                    if k in settings}},
                timeout=timeout)
            for image in response.get("images", []):
                view = str(image.get("view"))
                pass_name = str(image.get("pass") or "beauty")
                record = dict(image, cached=False)
                if response.get("blender"):
                    record.setdefault("blender", response["blender"])
                rendered[(view, pass_name)] = record
                out = job / "out" / (image_name(view, pass_name)[:-4] + ".json")
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(record, sort_keys=True),
                                   encoding="utf-8")
                except OSError:
                    pass

        ordered = []
        for view in view_list:
            for pass_name in passes:
                if (view, pass_name) in rendered:
                    ordered.append(rendered[(view, pass_name)])
                elif (view, pass_name) in cached:
                    ordered.append(cached[(view, pass_name)])
                else:  # pragma: no cover
                    raise BlenderError("missing render for %s/%s" % (view, pass_name),
                                       code="blender_render_missing")
        response = dict(response)
        response["images"] = ordered
        response["views"] = view_list
        response["passes"] = passes
        response["cached_frames"] = [list(k) for k in cached]
        response["rendered_frames"] = [list(k) for k in rendered]
        if ordered:
            response["engine"] = response.get("engine") or ordered[0].get("engine")
            response["blender"] = response.get("blender") or ordered[0].get("blender")
        return response

    def evaluate(self, mesh: Mesh, finish: dict | None = None,
                 timeout: float | None = None) -> tuple[Mesh, dict]:
        """Apply modifiers in Blender and return ``(mesh, receipt)``.

        The returned mesh is triangulated (the :class:`Mesh` contract) and in
        metres, so verify sees the beveled/shaded geometry rather than the
        numpy draft.
        """
        norm = normalize_finish(finish)
        job = self._write_job(mesh, norm)
        response = self.request({"op": "eval", "dir": str(job)},
                                timeout=max(float(timeout or self.timeout),
                                            _JOB_TIMEOUT))
        data = np.load(response["path"])
        out = Mesh(np.asarray(data["verts"], dtype=np.float64),
                   np.asarray(data["faces"], dtype=np.int64),
                   {"kind": "evaluated", "job": str(job),
                    "finish": norm, "exact": False})
        info = {"job": str(job), "dir": str(job),
                "path": response["path"],
                "n_vertices": int(response.get("n_vertices", len(out.verts))),
                "n_tris": int(response.get("n_tris", len(out.faces))),
                "live_modifiers": response.get("live_modifiers", []),
                "finish_applied": response.get("finish_applied"),
                "degraded": response.get("degraded", []),
                "exact": False}
        return out, info

    def export(self, mesh: Mesh, finish: dict | None = None,
               formats=("glb", "obj"), stem: str = "model", workdir=None,
               *, timeout: float | None = None) -> dict:
        """Export GLB / OBJ / STL / BLEND; returns a per-file receipt."""
        norm = normalize_finish(finish)
        wanted = [str(f).lower() for f in formats]
        unknown = [f for f in wanted if f not in EXPORT_FORMATS]
        if unknown:
            raise ValueError("unsupported format(s) %s; use %s"
                             % (", ".join(unknown), ", ".join(EXPORT_FORMATS)))
        job = self._write_job(mesh, norm, workdir)
        response = self.request({"op": "export", "dir": str(job),
                                 "formats": wanted, "stem": str(stem)},
                                timeout=max(float(timeout or self.timeout),
                                            _JOB_TIMEOUT))
        response = dict(response)
        response["job"] = str(job)
        response["dir"] = response.get("dir") or str(job / "export")
        return response

    # -------------------------------------------------------------- context

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self) -> str:  # pragma: no cover - display only
        state = "running" if self._alive() else "stopped"
        return "BlenderRuntime(%s, %s)" % (self.root, state)


# -------------------------------------------------------------------- singleton

_RUNTIME: BlenderRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def runtime(root=None, *, timeout: float = _WORKER_TIMEOUT) -> BlenderRuntime:
    """Process-wide resident runtime. First call fixes ``root`` and timeout."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = BlenderRuntime(root=root, timeout=timeout)
            atexit.register(_RUNTIME.close)
        return _RUNTIME
