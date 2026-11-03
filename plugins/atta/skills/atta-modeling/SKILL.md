---
name: atta-modeling
description: Use Atta v1.1 through its ten MCP tools for parametric 3D modeling, constraint solving, multi-part assemblies, measured inspection, verification and GLB/OBJ/STL/STEP delivery. Trigger when the user asks to create, edit, solve, assemble, inspect, validate or export a 3D model, or mentions Atta, Blender meshes, CAD parts, mates, DOF or STEP.
---

# Atta v1.1 — modeling with receipts

Atta is an LLM-native parametric kernel. You do not drive Blender's UI and you
do not write a `build.py`. You call ten MCP tools exposed by this plugin:

| tool | use it for |
|---|---|
| `atta_doctor` | engine, Blender, recipe/op inventory; run once at the start |
| `atta_catalog` | recipes, feature ops, solve/assembly capabilities, schema |
| `atta_spec` | validate / propose / compile / schema for Spec v3 |
| `atta_model` | recipe, Spec v3 graph, or assembly (`kind=assembly`) |
| `atta_variant` | parameter sampling, constrained solving, assembly fit |
| `atta_describe` | measured facts + text/ASCII perception, probes, trace, diff |
| `atta_verify` | the only gate: `pass | degraded | fail` + receipt |
| `atta_preview` | judgement images; not acceptance |
| `atta_deliver` | export only after verify; GLB/OBJ/STL/BLEND and STEP |
| `atta_run` | a short multi-step pipeline |

## The rule that matters

Never say a model is done without an `atta_verify` receipt whose
`verdict == "pass"`. `degraded` is not `pass`. Facts come from measurement, not
from looking at a render. If a call returns `{ok:false}`, read `code`, `path`
and `fix`, then correct that exact thing.

Units: every public spec/param/constraint is **mm and degrees**; the kernel and
Blender scene are metres. Do not convert by hand — the tools do it at the
boundary.

## Fast loop

```
catalog -> spec(propose) -> model(draft) -> describe(perceive=true)
        -> model(patches=...) -> verify -> preview -> deliver
```

`describe` is the sub-second viewfinder: ASCII silhouettes, sections, shape
traits, construction trace and silhouette diff. Use it instead of rendering
while the shape is still moving.

## Solving parameters

When the user gives a dimension or constraint instead of a value, solve:

```json
atta_variant({
  "recipe": "product.mug",
  "targets": {"bbox_z": 100, "min_wall_mm": {"min": 2.5}},
  "constraints": {"volume_mm3": {"max": 250000}},
  "max_evaluations": 120
})
```

Targets are measured facts (`bbox_x/y/z`, `volume_mm3`, `min_wall_mm`, `genus`,
`closed`, `n_components`, `dihedral_max_deg`, `curvature_p95_deg`). The result
has `feasible`, `solution` (with `model_id`), `closest`, `diagnostics`,
`sensitivity`, `recommendations` (including side effects) and `pareto`. If it is
infeasible, report the closest achievable point and what relaxing a target would
cost — never invent a passing model.

Assembly-level solve uses `variant(base=<assembly model_id>, targets={...})`
with variables addressed as `part.param`.

## Assemblies

`spec.model.kind = "assembly"` takes `parts[]`, `ports[]` and `mates[]`.
Thirteen mate types include `coincident`, `concentric`, `plane`, `distance`,
`angle`, `tangent`, `gear`, `insert`, `parallel`, `perpendicular`, `lock` and
`point_on_plane`. `model` returns `dof` from the Jacobian rank; `verify` checks
per-part solids, mate residuals, DOF, broadphase collisions, clearance and
envelope. `deliver` writes a named-part GLB plus per-part OBJ/STL.

## Implicit fields

`atta_model` with a graph containing `{"op":"sdf"}` builds gyroid, Schwarz P,
Neovius, Mandelbulb, sphere, box or torus fields via marching tetrahedra:
`size`, `field`, `period`, `thickness`, `offset`, `clip`, `resolution`. This is
the composable replacement for treating a lattice as a single opaque recipe.

## Visual

`atta_preview` is not just a beauty render. Use `mode="studio"` (or `catalog`,
`dark`, `clay`) for per-part materials, studio lights, ground and an 85 mm lens;
add `focus="<part_id>"` for a padded macro view and `aspect="auto"` for tall
parts. Use `mode="diagnostic"` with `passes=["normal","depth","id","curvature",
"deviation"]` (and `reference=<model_id>` for deviation) for engineering maps.
Beauty images return `analysis` (Adams zones, clipping, contrast, entropy,
CIELAB/OKLab, colourfulness, edge density, key/fill). The analysis warns; it is
not a verdict.

## Delivery

`atta_deliver(formats=["glb","obj","step"])`. STEP is an analytic B-rep produced
by build123d/OCCT for the graph subset (box/cylinder/sphere/cone/torus/revolve/
extrude/boolean/transform/mirror/pattern). Unsupported ops fail with
`brep_unsupported` and a fix; there is no fake tessellated STEP. The mesh verify
receipt still gates delivery.

## Style

Match the user's language. Report `model_id`, `verdict`, hashes and artifact
paths. Show the smallest useful artifact first (facts, ASCII, one image), then
the detail. Do not narrate the whole tool log.
