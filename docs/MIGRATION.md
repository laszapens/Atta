# Migration map — what came from where

v1.1 is a rewrite, not a copy. This table records what was deliberately kept,
rewritten, or dropped.

| Old thing | Fate in v1.1 | Why |
|---|---|---|
| `Atta/atta/cad/spec.py` assumptions ledger | **kept as a concept**, rewritten in `atta/spec.py` | The separation of design defaults from measured facts is the best idea in v1. |
| `Atta` three-state verify + receipt | **kept**, rewritten in `atta/verify.py` | `pass/degraded/fail` plus `fix` is the contract agents need. |
| `Atta` 88 ops | **dropped from the surface** | Replaced by 10 verbs and a 25-op feature graph. |
| `Atta` jobs/process isolation | **deferred** | v1.1 draft geometry is fast; Blender is already a resident worker. Revisit for batch variants. |
| `Atta2` mesh kernel | **rewritten** in `atta/mesh.py` + geometry modules | Kept the numpy-first, metre-based, closed-manifold discipline; removed the hybrid B-rep prototype. |
| `Atta2` recipes | **rewritten** as 23 curated recipes with constraints and presets | Same product families, cleaner parameter schemas and closed outputs. |
| `Atta2` Blender worker | **rewritten** in `atta/blender/` | Persistent worker, JSON-line protocol, sentinel, render cache, evaluate/export. |
| `归一/workbench` spatial probes | **rewritten** in `atta/probe.py` | Ray grids, silhouettes, sections, clearance, interference, inside/nearest without a Blender scene. |
| `归一/attaz` 6 verbs | **absorbed** | Atta v1.1 uses the same fast/slow loop idea, but the verbs are agent tools. |
| `Atta-Cad` DXF pipeline | **not merged** | It is a different product (2D drawings); keep it independent. |
| Global `params.json` | **removed** | Each model record is a self-contained spec; no cross-project pollution. |
| GUI/click simulation | **removed** | The interface is semantic and declarative. |
| `atta.cad` B-rep | **delivered for a graph subset** | `atta/brep.py` maps box/cylinder/sphere/cone/torus/revolve/extrude/boolean/transform/mirror/pattern to build123d/OCCT and writes analytic STEP. Mesh-only ops fail with `brep_unsupported`. |

## Naming

- `Atta` v1.1 is the product.
- `Atta-Cad` (2D DXF) remains a separate product.
- `归一（blender）` remains the reference workbench.
- `Atta2` remains a paused snapshot.
- Other `Atta-*` directories are practice projects, not runtime dependencies.
