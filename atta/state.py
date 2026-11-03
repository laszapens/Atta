# -*- coding: utf-8 -*-
"""Workspace state: what was built, how to rebuild it, where the evidence is.

A record stores **how to rebuild**, never a large mesh.  This is what makes
``model_id`` portable: the spec, quality, finish and expected topology are
enough to reconstruct the geometry on demand.

Layout::

    $ATTA_HOME/_atta/models.json
    $ATTA_HOME/_atta/out/<model_id>/
    $ATTA_HOME/_atta/cache/<key>.npz      # optional geometry cache
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from . import mesh as M

SCHEMA = "atta.models/v3"
_STATE_LOCK = threading.RLock()


def workspace(root=None) -> Path:
    p = Path(root or os.environ.get("ATTA_HOME") or os.getcwd()).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def atta_dir(root=None) -> Path:
    p = workspace(root) / "_atta"
    (p / "out").mkdir(parents=True, exist_ok=True)
    (p / "cache").mkdir(parents=True, exist_ok=True)
    return p


def models_path(root=None) -> Path:
    return atta_dir(root) / "models.json"


def load(root=None) -> dict:
    p = models_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("models"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "models": {}}


def _atomic_replace(tmp: Path, target: Path, *, attempts: int = 5) -> None:
    """Move ``tmp`` onto ``target`` atomically, tolerating Windows sharing.

    ``os.replace`` is atomic on both POSIX and Windows, so it is the right
    primitive -- unlike ``os.rename``, which fails on Windows when the target
    exists.  What differs is that Windows raises ``PermissionError`` (errno
    EACCES) while another process merely has the file open for reading, which
    a concurrent reader (a second Atta process, an editor, antivirus) can do
    at any moment.  A short retry turns that transient sharing violation into
    a delay instead of a failed build.
    """
    last = None
    for attempt in range(max(1, int(attempts))):
        try:
            os.replace(str(tmp), str(target))
            return
        except PermissionError as exc:  # pragma: no cover - Windows behaviour
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.05 * (attempt + 1))
        except OSError as exc:  # pragma: no cover - exotic filesystems
            if getattr(exc, "errno", None) not in (errno.EACCES, errno.EBUSY,
                                                   errno.EPERM):
                raise
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last if last is not None else OSError("atomic replace failed")


def save(data: dict, root=None) -> None:
    p = models_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    # pid + thread id: two threads of one process must not share a temp name
    tmp = p.with_name("%s.tmp-%d-%d" % (p.name, os.getpid(),
                                        threading.get_ident()))
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        _atomic_replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def put(record: dict, root=None) -> dict:
    with _STATE_LOCK:
        data = load(root)
        data["models"][str(record["model_id"])] = record
        save(data, root)
    return record


def get(model_id: str, root=None) -> dict | None:
    return load(root)["models"].get(str(model_id))


def list_models(root=None, limit: int = 20) -> list[dict]:
    rows = sorted(load(root)["models"].values(),
                  key=lambda r: str(r.get("created") or ""), reverse=True)
    return rows[:max(1, int(limit))]


def last(root=None) -> dict | None:
    rows = list_models(root, 1)
    return rows[0] if rows else None


def new_id(mesh_hash: str, root=None) -> str:
    base = "mdl_%s" % str(mesh_hash)[:10]
    existing = load(root)["models"]
    if base not in existing:
        return base
    i = 2
    while "%s_%d" % (base, i) in existing:
        i += 1
    return "%s_%d" % (base, i)


def out_dir(model_id: str, root=None, sub: str | None = None) -> Path:
    p = atta_dir(root) / "out" / str(model_id)
    if sub:
        p = p / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_key(*parts) -> str:
    import hashlib
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def save_mesh(key: str, mesh: M.Mesh, root=None) -> Path:
    p = atta_dir(root) / "cache" / ("%s.npz" % str(key)[:64])
    tmp = p.with_name("%s.tmp-%d-%d.npz" % (p.stem, os.getpid(),
                                            threading.get_ident()))
    try:
        np.savez_compressed(tmp, verts=mesh.verts, faces=mesh.faces)
        _atomic_replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return p


def load_mesh(key: str, root=None) -> M.Mesh | None:
    p = atta_dir(root) / "cache" / ("%s.npz" % str(key)[:64])
    if not p.is_file():
        return None
    try:
        data = np.load(p)
        return M.Mesh(data["verts"], data["faces"], {"cached": True})
    except (OSError, ValueError, KeyError):
        return None


def clear_cache(root=None) -> dict:
    p = atta_dir(root) / "cache"
    n = 0
    if p.exists():
        for f in p.iterdir():
            try:
                if f.is_file():
                    f.unlink()
                else:
                    shutil.rmtree(f)
                n += 1
            except OSError:
                pass
    return {"removed": n, "dir": str(p)}


def touch(record: dict, **updates) -> dict:
    """Merge updates into a record already in the registry."""
    with _STATE_LOCK:
        data = load()
        current = data["models"].get(str(record.get("model_id")), {})
        current.update({k: v for k, v in updates.items() if v is not None})
        current["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["models"][str(current["model_id"])] = current
        save(data)
    return current


__all__ = [
    "SCHEMA", "workspace", "atta_dir", "models_path", "load", "save", "put",
    "get", "list_models", "last", "new_id", "out_dir", "cache_key",
    "save_mesh", "load_mesh", "clear_cache", "touch",
]
