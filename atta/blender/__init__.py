# -*- coding: utf-8 -*-
"""Blender backend: the only place in Atta that imports ``bpy``.

Everything above this layer speaks :class:`atta.mesh.Mesh`, plain dicts and
metres.  Geometry stays in numpy; this package adds finish (bevel, smooth,
material), renders judgement images, evaluates modifiers, and exports files.

Public surface::

    from atta.blender import runtime, find_blender, normalize_finish

    rt = runtime(root=".")
    rt.ping()
    rt.render(mesh, {"bevel_mm": 0.8, "material": "ceramic"}, views=("iso",))
    mesh_eval, receipt = rt.evaluate(mesh, finish)
    rt.export(mesh_eval, finish, formats=("glb", "obj", "stl", "blend"))

Importing this package never starts Blender; only a request does.
"""

from .finish import FINISH_DEFAULTS, VIEWS, normalize_finish, material_spec
from .runtime import (
    EXPORT_FORMATS,
    RENDER_VIEWS,
    BlenderError,
    BlenderRuntime,
    available,
    find_blender,
    runtime,
)

__all__ = [
    "BlenderError", "BlenderRuntime", "EXPORT_FORMATS", "FINISH_DEFAULTS",
    "RENDER_VIEWS", "VIEWS", "available", "find_blender", "material_spec",
    "normalize_finish", "runtime",
]
