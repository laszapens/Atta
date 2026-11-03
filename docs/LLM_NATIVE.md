# What "LLM-native" means here

Atta is not a GUI automated by a model. It is a modeling system whose native
interface is the thing a language model is actually good at: **declarative
intent, stable ids, measured facts, and actionable errors**.

## Ten rules

1. **Do not make the model look at pixels to decide geometry.**
   Every model returns a fact card first. `describe(perceive=true)` adds ASCII
   silhouettes, sections and shape traits in 0.1–0.3 s without Blender.
   Renders are for judgement, not proof.

2. **One semantic verb beats ten clicks.**
   `revolve(profile, segments)` is a sentence. Clicking a sketch, selecting an
   axis, and dragging is a movie the model has to reconstruct from screenshots.

3. **Every operation is idempotent from a spec.**
   A `model_id` stores how to rebuild, not a mesh blob. Re-running the same spec
   gives the same geometry hash.

4. **Stable ids, not indices.**
   Features have ids; parts have names; models have `mdl_*`. The model can refer
   to `hole_left` next turn without counting objects in a scene.

5. **Facts are low-token and structured.**
   `bbox_mm`, `volume_mm3`, `closed`, `genus`, `wall.min_mm`, `silhouette`
   bitmaps. One line is under 200 characters; the structure is there when needed.

6. **Errors are patches, not apologies.**
   Every check failure carries a check `id` and `fix`. An agent can act on
   `wall = min(base_radius, top_radius) * 0.22` without a human.

7. **Assumptions are explicit.**
   Inferring a default is allowed; pretending it was measured is not. The
   ledger separates `basis: design_default` from measured facts.

8. **Fast loop and slow loop are different products.**
   Draft geometry is numpy, sub-second. Final geometry goes through Blender
   modifiers, render, and verify. Do not pay the slow loop for a shape idea.

9. **The interface is honest about uncertainty.**
   `exact: false` on sampled wall/clearance/curvature. `degraded` is a real
   verdict, not a soft pass. `deliver` refuses it unless a human says otherwise.

10. **One source of truth per surface.**
    Ten tools in `agent/tools.py`; one spec in `spec.py`; one verify in
    `verify.py`; one Blender worker. Host adapters are shells, not forks.

## Anti-patterns explicitly rejected

* Simulating mouse clicks and viewport navigation.
* Letting the model write raw `.blend` or `.dxf` bytes.
* Rendering a full image just to discover the basic shape; use the ASCII
  viewfinder first.
* Passing a rendered image as the only evidence of watertightness.
* A global `params.json` shared by unrelated projects.
* 88 flat ops exposed as 88 tools.
* A "creative mode" that fabricates measurements.
* Silent clamping without an `applied_patches` record.

## The intended agent loop

```
catalog                         what can I build?
spec(propose)                   what did I leave unspecified?
model(draft)                    give me a shape and a fact card
describe(perceive=true)         text + ASCII outlines/sections, no render
describe(trace=true)            how was it built, step by step?
describe(compare_to=<id>)       what changed? silhouette IoU + +/-
model(patches=...)              change one thing, not the world
verify                          is it actually a solid, within constraints?
preview                         let me look, knowing this is judgement
deliver                         export + manifest + receipt
```

The model is allowed to stop after any line. The artifact at each line is
useful on its own.

## The solve loop (parameter targets)

When the user gives a dimension or a constraint instead of a value, do not
guess-and-check by hand:

```
catalog                         which parameters exist and what do they mean?
variant(targets={...})          solve against measured metrics
   -> feasible=true              solution + model_id + verify receipt
   -> feasible=false             closest + violation + recommendations.side_effects
variant(base=<model_id>)        refine around an existing build
variant(pareto=true)            trade-off front when two targets conflict
```

Targets are measured facts, not parameter names: `bbox_x/y/z`, `volume_mm3`,
`min_wall_mm`, `genus`, `closed`, `n_components`, `dihedral_max_deg`,
`curvature_p95_deg`. Verify constraints keep their semantics (`min_wall_mm` is
a lower bound, `dihedral_max_deg` an upper bound). An infeasible solve never
returns a `model_id`; report the closest achievable point and the side effects
of relaxing a target instead of pretending the spec was met.

## The assembly loop

For multi-part work, do not build one merged mesh and call it an assembly:

```
spec(model.kind="assembly")     parts[] + ports[] + mates[]
model(spec=...)                 SE(3) pose solve; returns model_id, dof
describe                        part_count, dof, fully_constrained, collisions
verify                          per-part solids, mate residuals, DOF gate,
                                broadphase collisions, clearance, envelope
preview                         assembled scene, judgement only
deliver                         named-part GLB + per-part OBJ/STL + manifest
```

Mates are the constraint language: `coincident`, `concentric`/`axis`, `plane`,
`distance`, `angle`, `tangent`, `gear`, `insert`, `parallel`, `perpendicular`,
`lock`, `point_on_plane`. `dof` comes from the rank of the mate Jacobian, not
from a guess; `fully_constrained` is a gate, and a residual conflict is reported
per mate. Assembly parameters can be solved too:
`variant(base=<assembly>, targets={"dof": 0, "collisions": 0})` addresses part
parameters as `part.param`.

## Seeing, not guessing

Use `preview` as a judgement aid, not a verdict:

```
preview(mode="studio", views=["hero"], focus="header")   # presentation / macro
preview(mode="catalog")                                  # neutral catalogue (gray backdrop, soft floor)
preview(mode="diagnostic", passes=["normal","depth","curvature"])  # shape maps
preview(mode="diagnostic", passes=["deviation"], reference=<id>)   # tolerance map
```

Studio modes render the named-part scene with each part's own material; the
returned `analysis` measures exposure (Adams zones), clipping, contrast,
entropy, CIELAB/OKLab colour, colourfulness, edge density and key-to-fill.
It emits warnings, never `pass`/`fail`. A rendered image is not traceable
metrology; geometry still goes through `verify`.

## Implicit fields and STEP

Use the graph op `sdf` when the shape is a field, not a face:
`{"op":"sdf","args":{"size":[40,30,20],"field":"gyroid","period":30,
"thickness":3.6}}`. Fields: `gyroid`, `schwarz_p`, `neovius`, `mandelbulb`,
`sphere`, `box`, `torus`; `clip` is `box` or `sphere`. Extraction is marching
tetrahedra with exact edge keys, so the result is closed without welding.

`deliver(formats=["glb","obj","step"])` writes an analytic STEP for the graph
subset that maps to build123d/OCCT. Unsupported ops fail with
`brep_unsupported` and a fix; they never produce a tessellated `.step`. The
verdict is still the mesh `verify` receipt — STEP is a delivery format, not a
second acceptance path.
