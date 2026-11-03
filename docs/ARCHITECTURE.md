# Architecture

> Blender-first. LLM-native. Small surface, hard receipts.

## 1. The product decision

Atta is the agent-facing modeling kernel of this repository. The design keeps
the ideas that survived contact with real work:

* a small number of semantic verbs;
* a declarative spec that can rebuild the model;
* facts measured from geometry, not inferred from renders;
* a three-state verification gate with a receipt;
* assumptions recorded separately from measurements;
* a persistent Blender worker for finish/render/export.

It deliberately drops:

* a large flat op surface as the agent interface (25 graph ops, 10 verbs);
* GUI-mimicking click/selection scripts;
* global `params.json` shared across projects;
* model state that cannot be rebuilt from a spec;
* verify paths that can be bypassed by delivery.

## 2. Layers

```
┌──────────────────────────────────────────────────────────────┐
│ agent hosts: ZCode MCP · DSH plugin · Claude · Cursor · HTTP │
├──────────────────────────────────────────────────────────────┤
│ agent/tools.py — 10 tool contracts (single source of truth)  │
├──────────────────────────────────────────────────────────────┤
│ engine.py — doctor/catalog/spec/model/variant/describe/...   │
├──────────────────────────────────────────────────────────────┤
│ spec.py — validate / propose / compile / hash                │
│ recipes.py — curated parametric families                     │
│ features.py — general DAG for fine modeling                  │
├──────────────────────────────────────────────────────────────┤
│ mesh.py · primitives · curves · surfaces · boolean           │
│ (pure numpy; metres; no bpy)                                 │
├──────────────────────────────────────────────────────────────┤
│ analysis: metrology · probe · facts · verify · perception    │
├──────────────────────────────────────────────────────────────┤
│ blender: runtime · worker · finish · materials               │
│ (bpy only here; finish, render, evaluate, export)            │
├──────────────────────────────────────────────────────────────┤
│ state.py — models.json, content-addressed artifacts          │
└──────────────────────────────────────────────────────────────┘
```

Rule: **bpy never leaks upward**. Geometry, facts, and verdicts are computable
without opening Blender. Blender is the finishing/rendering/export backend.

## 3. The ten tools

| tool | question it answers |
|---|---|
| `atta_doctor` | is the engine healthy, and what can it do? |
| `atta_catalog` | which recipes/features/materials exist, with schemas? |
| `atta_spec` | is this spec valid; what defaults were assumed? |
| `atta_model` | build or refine a model; return id + facts |
| `atta_variant` | sample, solve targets/constraints, Pareto/fit |
| `atta_describe` | measure + perceive: facts, probes, ASCII views/sections, trace, diff |
| `atta_verify` | pass / degraded / fail, with a receipt |
| `atta_preview` | studio/catalog/dark/clay/diagnostic images + analysis |
| `atta_deliver` | verify, export GLB/OBJ/STL/BLEND (+STEP subset), manifest |
| `atta_run` | run a batch pipeline in one call |

`refine` is `atta_model` with `patches`. `fit` is `atta_variant` with a
`base` model. `probe` is `atta_describe` with `probe`. This keeps the surface
small without losing power.

## 4. Spec v3

```json
{
  "schema": "atta.spec/v3",
  "name": "mug-01",
  "model": {
    "kind": "recipe",
    "id": "product.mug",
    "preset": "nominal",
    "params": {"height": 95}
  },
  "quality": "draft",
  "material": "ceramic",
  "finish": {"bevel_mm": 0.8, "smooth_deg": 35},
  "constraints": {
    "bbox": {"x": [60, 140]},
    "min_wall_mm": 2.5,
    "volume_mm3": [100000, 900000]
  },
  "assumptions": []
}
```

Or general fine modeling:

```json
{
  "schema": "atta.spec/v3",
  "name": "bracket",
  "model": {
    "kind": "graph",
    "output": "cut",
    "features": [
      {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
      {"id": "holes", "op": "cylinder",
       "args": {"radius": 4, "height": 20, "center": [15, 25, 3]}},
      {"id": "cut", "op": "difference", "inputs": ["plate", "holes"]}
    ]
  }
}
```

Feature arguments are in **mm**; the evaluator converts to metres. Values may
be `{"$param": "name"}` (resolved by `spec`) or `{"$expr": "w/2 + 4"}` (safe
arithmetic over params).

Graph ops are 25: 20 core primitive/CSG/transform/surface ops plus `offset`,
`gyroid`, a general `sdf` field op (`gyroid`, `schwarz_p`, `neovius`,
`mandelbulb`, `sphere`, `box`, `torus`), and `text_engrave`/`text_emboss`. `sdf` is sampled on a regular grid and extracted by marching
tetrahedra; edge crossings are keyed by the exact integer node pair, so the
result is closed without a distance-tolerance weld. See §7 for the solver view
and §9 for the B-rep backend.

## 5. Verify semantics

```
error-level check fails  -> fail
only warn-level fails    -> degraded
no failed check          -> pass
```

`deliver` is blocked unless `pass`, or `degraded` with an explicit
`allow_degraded=true` and a reason. Assumptions never change the verdict;
they are attached to the receipt as provenance.

A verify receipt contains: `spec_hash`, `mesh_hash`, `level`, `verdict`,
`score`, `checks[]`, `issues[]`, `metrics`, `facts`, `assumptions`,
`exact: false` and `method`.

## 6. Perception layer (no render)

Rendering is the slow loop. `atta_describe(perceive=true)` is the fast
viewfinder, implemented in `atta/perception.py` with pure numpy:

```
facts + traits
  size/aspect/volume/genus/convexity/symmetry/holes_est
ASCII views
  front/top/right occupancy at 8..64 px
ASCII sections
  Z fractions (default 0.5), even-odd fill
trace
  recipe or feature-graph construction steps with per-step volume
diff(compare_to)
  silhouette IoU + ASCII + / - / #
```

Cost is typically 0.1–0.3 s and never starts Blender. It is a **viewfinder**,
not a verdict: `verify` still decides. The intended loop is:

```
model(draft) -> describe(perceive=true) -> model(patches) -> describe(perceive=true)
             -> verify -> preview -> deliver
```

## 7. Parameter solving

`atta/solve.py` is a black-box constrained solver over **measured** mesh
metrics. `atta_variant` wires it to a draft build (`_recipe_metrics`) without
committing candidates, so the tool report contains a real solution and a real
receipt.

* Variables are normalized to `[0,1]`; integers are rounded.
* Targets: `metric -> value | [min,max] | {target,tol} | {min,max}`.
* Verify constraints are translated with their real semantics:
  `min_wall_mm` is `>=`, `dihedral_max_deg`/`curvature_p95_deg` are `<=`,
  `genus`/`closed`/`n_components` are equality, `volume_mm3`/bbox are windows.
* Strategies: `lhs`, `compass` (Hooke-Jeeves-style pattern search),
  `nelder-mead`, `differential-evolution`, `local` (Levenberg-Marquardt on
  residuals), `pareto`.
* Screening: two probes per variable keep only variables that move a violated
  target, which stops a ten-parameter recipe from wandering into invalid
  parameter combinations.
* Diagnostics: per-target normalized violation, active constraints, closest
  achievable point, finite-difference sensitivity, recommendations with
  side effects, and a Pareto front for multi-objective runs.
* `degraded`/`fail` are never faked: infeasible problems return
  `status="infeasible"` and do not commit a model.

Assembly parameters use the same engine through
`atta_variant(base=<assembly model_id>)`; variables are addressed as
`part.param`, and the metric set includes `dof`, `collisions`,
`interference_mm3`, `min_clearance_mm`, `mate_conflicts` and `worst_residual_mm`.

## 8. Assembly layer

`atta/assembly.py` is mesh-native. A part is any recipe or graph; a port is a
local frame; a mate is a residual equation in SE(3).

* Parts: inline recipe/graph/spec or a `model_id` reference; initial
  `transform` (`translate_mm`, `rotate_deg` or a 4x4 mm matrix), `fixed`,
  material, declared ports.
* Ports: declared `{origin_mm, axis, normal, radius_mm}` or auto-derived bbox
  frames (`center`, `axis_x/y/z`, `face_x±/y±/z±`).
* Mates: `coincident`, `concentric`/`axis`, `plane`, `distance`, `angle`,
  `tangent`, `gear`, `insert`, `parallel`, `perpendicular`, `lock`,
  `point_on_plane`; `align`, `offset_mm`, `value_mm`, `angle_deg`, `weight`,
  `allow_interference`.
* Solve: free parts have six pose variables; fixed parts are excluded. The
  first part is anchored if none is fixed. `scipy.optimize.least_squares`
  (TRF, soft-L1, weak prior) minimizes mate residuals, with rotational rows
  scaled by a characteristic length.
* DOF: finite-difference Jacobian of mate residuals, SVD rank,
  `dof = 6*n_free - rank`; redundant rows, conflicts and per-part mobility are
  reported.
* Collision: AABB sweep-and-prune broadphase, exact manifold boolean
  intersection for candidates, sampled clearance within a threshold.
* Receipt: per-part closed/volume, mate residual, DOF gate, collisions,
  clearance/interference constraints, envelope; `pass|degraded|fail`.
* Delivery: one named-part GLB scene plus per-part OBJ/STL and a manifest with
  placements, mates, DOF, collisions and the verify receipt.

## 9. B-rep / STEP backend

`atta/brep.py` is an optional second backend for the graph subset that maps
cleanly onto OpenCASCADE via `build123d`. It exists because a mesh is not a
B-rep: a STEP file full of triangles preserves the extension but loses the
analytic cylinder, the axis, the radius, and the ability to fillet or tolerance
the part downstream.

* Supported ops: `box`, `cylinder`, `sphere`, `cone`, `torus`, `revolve`,
  `extrude`, `union`, `difference`, `intersection`, `transform`, `mirror`,
  `pattern_linear`, `pattern_polar`.
* Unsupported ops raise `brep_unsupported` with a `fix`; there is no silent
  tessellated fallback.
* `deliver(formats=["glb","obj","step"])` writes the mesh exports through
  Blender and the STEP through build123d. The manifest records `backend`
  (`mesh` or `brep+mesh`), the B-rep volume and the ops used.
* The verdict is still the mesh `verify` receipt. STEP is a delivery format, not
  a second acceptance path; a future B-rep-native verify can be added behind the
  same spec without changing the tools.

## 10. Visual pipeline

`preview` has two rendering paths and one analysis layer:

* **Beauty / studio** — EEVEE with per-part materials, a named-part scene, an
  area-light preset (`studio3`, `studio`, `catalog`, `dark`, `clay`), optional
  ground and contact shadow, 50–85 mm lens, orthographic option, exposure and
  AgX look. `focus=<part_id>` frames one part for a macro/detail view.
* **Diagnostic** — Workbench with vertex colours, flat light, transparent
  background: `normal` (RGB = n·0.5+0.5), `depth` (near white, far black),
  `id` (one colour per part), `curvature` (log mean-curvature magnitude), and
  `deviation` (nearest reference-surface distance, blue=0 → red=range).
  Deviation uses a numpy voxel hash, not scipy/rtree, because Blender's Python
  does not ship scipy.
* **Analysis** — `atta/vision.py` linearizes sRGB, converts to CIELAB and
  OKLab, and reports luminance percentiles, Adams zones 0–X, clipping, RMS and
  Michelson contrast, Shannon entropy, Hasler–Süsstrunk colourfulness, chroma
  and hue, a gray-world illuminant estimate, Sobel edge density and a
  key-to-fill ratio in stops. It returns warnings, never `pass`/`fail`.

The lineage is explicit: Adams' Zone System for exposure placement, CIE 1931 /
CIELAB for colour, Weber–Fechner and Stevens for ratio perception, Land–McCann
Retinex and Buchsbaum gray-world for illuminant normalization, Hasler–Süsstrunk
for colourfulness, Gershun's light field and Ramamoorthi–Hanrahan spherical
harmonics for irradiance thinking, Cook–Torrance/Schlick for PBR material
readability, Koenderink–van Doorn shape index/curvedness for surface shape,
Marr's primal sketch and Canny/Sobel for edges, and ISO 14253-1/JCGM 106 for
the rule that a near-limit measurement is indeterminate rather than a verdict.
A rendered image is not traceable metrology; the analysis flags, ranks and
localizes.

## 11. Units and coordinate conventions

* Public spec/params: **mm**, **degrees**.
* Geometry kernel: **metres**, **degrees at the boundary**.
* Blender scene: metres.
* Feature graph args: mm.
* `+Z` is up unless a recipe says otherwise. `+X` is right, `+Y` is back.

## 12. Loop design

Fast loop (agent, sub-second):

```
catalog -> spec(propose) -> model(draft) -> describe -> refine
variant(targets) -> feasible? -> model_id : closest + side_effects
```

Slow loop (proof):

```
verify -> preview -> deliver
```

Assembly loop:

```
spec(kind=assembly) -> model -> describe(dof) -> verify(mates/collisions) -> deliver
```

Draft geometry is pure numpy. Final geometry is evaluated through Blender
modifiers before verify, so the verdict sees bevels and smoothing.

## 13. Non-goals

* B-rep/NURBS as the primary kernel (STEP exists only as a delivery subset).
* Photoreal output as an acceptance goal (studio previews are judgement, not metrology).
* GUI/click simulation.

