# Internal interfaces

This file is the contract between parallel workstreams. Do not change a
signature without updating this file and every caller.

## Mesh

`atta.mesh.Mesh(verts: (n,3) float64 metres, faces: (m,3) int64, meta: dict)`

Helpers: `bbox`, `volume`, `area`, `centroid`, `topology`, `canonical_hash`,
`weld`, `concat`, `transform`, `oriented`.

Topology keys: `n_vertices, n_tris, n_edges, boundary_edges,
non_manifold_edges, inconsistent_winding_edges, euler, n_components, genus,
closed, degenerate_faces, empty`.

## Geometry (metres in, metres out)

`primitives.py`

```python
box(size, center=(0,0,0), align="center") -> Mesh
cylinder(radius, height, center=(0,0,0), axis="z", segments=64, caps=True) -> Mesh
sphere(radius, center=(0,0,0), segments=64, rings=32) -> Mesh
cone(r_bottom, r_top, height, center=(0,0,0), axis="z", segments=64) -> Mesh
torus(major_radius, minor_radius, center=(0,0,0), axis="z",
      seg_major=96, seg_minor=32) -> Mesh
```

`curves.py`

```python
polyline(points, closed=False) -> np.ndarray
catmull_rom(points, samples_per_seg=16, closed=False) -> np.ndarray
resample_arclength(points, n, closed=False) -> np.ndarray
parallel_frames(path, up=(0,0,1)) -> (tangents, normals, binormals)
```

`surfaces.py`

```python
revolve(profile_rz, segments=96, axis="z", caps=True) -> Mesh
loft(rings, closed_ring=True, caps=True) -> Mesh
sweep(section_xy, path, closed=False, twist_deg=0.0, scale_end=1.0) -> Mesh
coons(edges, u_samples=32, v_samples=32, flip=False) -> Mesh       # edges=(bottom,right,top,left)
gordon(u_curves, v_curves, u_samples=32, v_samples=32, flip=False) -> Mesh
thicken(surface, thickness, offset=0.0) -> Mesh
lobes(mesh, count=12, amplitude=0.03, phase_deg=0.0, twist_deg=0.0) -> Mesh
extrude(loop_xy, height, direction=(0,0,1), caps=True, holes_xy=None) -> Mesh
```

`boolean.py`

```python
boolean(a, b, op="union") -> Mesh
union(a, b) -> Mesh
difference(a, b) -> Mesh
intersection(a, b) -> Mesh
mirror(mesh, plane="x") -> Mesh
pattern_linear(mesh, count, spacing, axis="x") -> Mesh
pattern_polar(mesh, count, axis="z", angle_deg=360.0, center=(0,0,0)) -> Mesh
```

`features.py`

```python
FEATURE_OPS: dict[str, dict]   # op -> {required, optional, doc}
evaluate(features, params=None, units="mm", output=None) -> Mesh
```

Feature row: `{"id": str, "op": str, "args": {...}, "inputs": [str, ...]}`.
`args` may contain `{"$param": "name"}` or `{"$expr": "w/2 + 4"}`.
Errors: `unknown_feature`, `duplicate_id`, `unknown_input`, `cycle`,
`bad_arg`, each with `path` and `fix`.

## Analysis

```python
metrology.measure(mesh, *, wall=False, dihedral=True, curvature=False,
                  density_kg_m3=None) -> dict
metrology.wall_thickness(mesh, max_samples=600, seed=0) -> dict
metrology.dihedral(mesh, thresholds=(30.0, 60.0)) -> dict
metrology.curvature(mesh) -> dict

probe.raycast_grid(mesh, axis="z", res=(32,32), bounds=None,
                   direction=1) -> dict
probe.silhouette(mesh, views=("front","top","left","iso"), res=32) -> dict
probe.section(mesh, plane="z", offset_mm=0.0, res=64) -> dict
probe.clearance(a, b, max_samples=2000) -> dict
probe.interference(a, b) -> dict
probe.nearest(mesh, point_mm) -> dict
probe.inside(mesh, point_mm) -> bool

facts.facts(mesh, record=None, *, wall=False, curvature=False) -> dict

verify.verify(mesh, *, constraints=None, expected_genus=None,
              level="standard", render_receipt=None, parts=None,
              assumptions=None) -> dict
```

`verify` returns:

```python
{
  "verdict": "pass" | "degraded" | "fail",
  "score": 0.0..1.0,
  "level": "basic" | "standard" | "strict",
  "checks": [{"id","ok","severity","value","expected","message","fix"}],
  "issues": [...],                  # only failed checks
  "metrics": {...},                 # mm units
  "facts": {...},
  "assumptions": [...],
  "exact": False,
  "method": "atta.verify/v3",
  "next": [...]
}
```

Constraint keys accepted: `bbox`, `volume_mm3`, `min_wall_mm`, `genus`,
`closed`, `n_components`, `dihedral_max_deg`, `curvature_p95_deg`,
`clearance`, `interference`, `interfaces`, `envelope`, `holes`, `render`.

## Perception (pure numpy, no Blender)

```python
perception.perceive(mesh, record=None, *, views=("front","top","right"),
                    res=24, sections=(0.5,), wall=False,
                    curvature=False) -> dict
perception.diff(before, after, *, views=("front","top","right"),
                res=24) -> dict
perception.trace(record, mesh=None) -> dict
```

`perceive` returns `text`, `traits`, `views` (ASCII + bitmap + coverage),
`sections` (ASCII + loop areas), `signature`. `diff` returns per-view IoU and
ASCII `+/-/#` rows. `trace` returns recipe or feature-graph steps with
per-step bbox/volume. All lengths in mm; all results `exact: false`.

## Blender

```python
find_blender() -> str | None

class BlenderRuntime:
    available() -> bool
    start()
    close()
    ping() -> dict
    request(req: dict, timeout: float | None = None) -> dict
    render(mesh, finish, views=("iso",), res=512, samples=8,
           workdir=None, *, dedupe=True, timeout=None) -> dict
    render_parts(parts, *, settings=None, views=("iso",), res=512,
                 samples=8, workdir=None, dedupe=True, timeout=None,
                 reference=None) -> dict
    evaluate(mesh, finish) -> (Mesh, dict)
    export(mesh, finish, formats=("glb","obj"), stem="model",
           workdir=None) -> dict

runtime(root=None) -> BlenderRuntime
```

`finish` is a dict:

```python
{
  "smooth": True,
  "smooth_angle_deg": 35.0,
  "bevel": {"width": 0.0008, "segments": 2, "angle_deg": 40.0},  # metres
  "material": "ceramic"
}
```

## Spec

```python
schema() -> dict
validate_spec(spec) -> {"ok": bool, "spec": dict, "errors": [...]}
propose_spec(spec) -> {"ok": bool, "spec": dict, "assumption_ledger": [...],
                       "assumption_summary": {...}, "unresolved": [...],
                       "errors": [...]}
compile_spec(spec) -> {"ok": bool, "compiled": dict, "errors": [...]}
spec_hash(spec) -> str
```

Recipe registry API (owned by `recipes.py`):

```python
get(recipe_id) -> Recipe
all_recipes() -> list[Recipe]

Recipe:
  id, name, description, params: dict[str, ParamSpec], presets,
  constraints, expected_genus, material, tags
  resolve(patch=None, preset="nominal") -> Resolved
  build_shape(values, quality="draft") -> (Mesh, finish_dict)

ParamSpec:
  type ("float"|"int"|"bool"|"enum"|"str"), default, min, max, unit,
  description, group, advanced, enum
```

## Engine (agent surface calls only this)

```python
doctor(deep=False) -> dict
catalog(verbose=False) -> dict
spec(spec=None, op="validate", assume=False) -> dict
model(recipe=None, spec=None, params=None, patches=None, preset="nominal",
      quality="draft", name=None, material=None, finish=None) -> dict
variant(recipe=None, base=None, count=6, ranges=None, strategy="lhs",
        seed=42, objective=None, constraints=None, quality="draft",
        targets=None, max_evaluations=None, tolerance=1e-6, initial=None,
        pareto=False, free=None, fixed=None, solve=False) -> dict
describe(model_id=None, probe=None, wall=False, curvature=False,
         perceive=False, views=None, res=24, sections=None,
         compare_to=None, trace=False) -> dict
verify(model_id=None, level="standard", constraints=None,
       reference=None) -> dict
preview(model_id=None, views=None, res=None, samples=None, quality=None,
        mode="beauty", outdir=None, lighting=None, background=None,
        ground=None, lens_mm=None, exposure=None, look=None, materials=None,
        passes=None, ortho=None, distance_factor=None, reference=None,
        deviation_range_mm=None, focus=None, aspect=None,
        focus_padding=None) -> dict
deliver(model_id=None, formats=None, outdir=None, allow_degraded=False,
        name=None) -> dict
run(steps, stop_on_error=True) -> dict
```

Engine results are plain dicts. Failures raise `atta.errors.AttaError` with a
`fix`; the agent layer converts them to `{ok:false, error:{...}}`.

### Parameter solving (`variant` with `targets`)

```python
variant(recipe="product.mug", ranges={"height": [60, 140]},
        targets={"bbox_z": 100, "min_wall_mm": {"min": 2.5}},
        constraints={"volume_mm3": {"max": 250000}},
        max_evaluations=120, tolerance=1e-6)
```

- `targets` map measured metrics to `value | [min,max] | {target,tol} |
  {min,max}`. Names are the flattened facts: `bbox_x/y/z`, `volume_mm3`,
  `min_wall_mm`, `genus`, `closed`, `n_components`, `area_mm2`,
  `dihedral_max_deg`, `curvature_p95_deg`.
- `constraints` accepts the same verify constraint keys and keeps verify
  semantics (`min_wall_mm` = lower bound, `dihedral_max_deg` = upper bound).
- The result always carries `status`, `feasible`, `solution`, `best`,
  `closest`, `pareto`, `diagnostics`, `sensitivity`, `recommendations`
  (including `side_effects`), `iterations`. When feasible it also commits the
  best candidate and returns `model_id`; an infeasible solve never fakes one.
- Assembly-level solving uses `variant(base=<assembly model_id>,
  targets={...})`; variables are `part.param`, and metrics include `dof`,
  `collisions`, `interference_mm3`, `min_clearance_mm`, `mate_conflicts`.

### Assembly models

`spec.model.kind = "assembly"` (or `model(recipe=<assembly recipe>)`) returns a
model with `kind: "assembly"`. `describe` adds `part_count`, `dof`,
`fully_constrained`, `collisions`; `verify` returns an assembly receipt with
per-part, per-mate, DOF, collision, clearance and envelope checks; `deliver`
writes a named-part GLB plus per-part OBJ/STL and a manifest. A single-mesh
verify/deliver receipt is never substituted for an assembly receipt.

`deliver(formats=[..., "step"])` also writes an analytic STEP for graphs using
box/cylinder/sphere/cone/torus/revolve/extrude/boolean/transform/mirror/pattern.
Unsupported ops fail with `brep_unsupported` and a `fix`; they never fall back
to a tessellated `.step`. The manifest carries `backend` (`mesh` or
`brep+mesh`), `brep.volume_mm3` and `brep.ops`.

Graph features may use the `sdf` op for implicit fields
(`gyroid`/`schwarz_p`/`neovius`/`mandelbulb`/`sphere`/`box`/`torus`) with
`size`, `period`, `thickness`, `offset`, `clip` and `resolution` in mm.

`preview(mode=...)` supports `beauty`, `studio`, `catalog`, `dark`, `clay` and
`diagnostic`. Studio modes render the named-part scene with each part's own
material, a lighting preset, optional ground, lens/exposure/look and `focus`
(a part id) for macro framing and `aspect="auto"` for projected-bbox framing.
Diagnostic passes are `normal`, `depth`, `id`,
`curvature` and `deviation` (with `reference` and `deviation_range_mm`). Beauty
images carry an `analysis` block (Zone System, CIELAB/OKLab, clipping, contrast,
entropy, colourfulness, edge density, key/fill). Analysis flags quality; it is
not a verify verdict.

## State

`state.py` owns `_atta/models.json` and `_atta/out/`. Records store how to
rebuild, never a large mesh. Content-addressed mesh cache is optional.
