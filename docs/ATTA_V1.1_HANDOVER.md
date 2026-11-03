# Atta v1.1 — handover

> Historical handover snapshot (2026-11-02). Current status: `README.md`;
> current architecture: `docs/ARCHITECTURE.md`. Numbers below are from the initial v1.1.

> One page to continue the work without rereading the old repositories.

## Identity

**Atta v1.1** is the single agent-facing modeling product. It is Blender-first,
LLM-native, and lives at `$ATTA_ROOT`.

Everything else is reference or practice:

* `$ATTA_LEGACY_BREP` — old B-rep/88-op workbench (spec/verify ideas).
* `$ATTA_LEGACY_BLENDER` — Blender resident workbench + attaz.
* `$ATTA_PAUSED_PROTOTYPE` — paused Blender-first prototype.
* `$ATTA_CAD` — separate 2D DXF product.
* `Atta-D`, `Atta-Cuwork` — practice projects and notes.

v1.1 imports none of them at runtime.

## What works now

```bash
cd $ATTA_ROOT
python3 -m atta doctor --deep
python3 -m atta catalog --verbose
python3 -m atta model --recipe product.mug
python3 -m atta describe --probe '["bbox","topology","silhouette"]' --wall
python3 -m atta perceive --res 24      # text + ASCII views/sections, no render
python3 -m atta verify --level standard
python3 -m atta preview --mode studio --views hero --res 1200
python3 -m atta deliver --formats glb,obj
```

* 23 recipes: product, mechanical, surface, architecture, pattern, assembly,
  tolerance, lattice, organic, text, primitive.
* 25 feature ops: primitives, revolve/loft/sweep, coons/gordon, extrude,
  booleans, transforms, patterns, thicken, lobes, offset, gyroid, sdf,
  text_engrave/text_emboss.
* 10 tools: doctor, catalog, spec, model, variant, describe, verify, preview,
  deliver, run.
* 3-state verify with `spec_hash`, `mesh_hash`, checks, issues and `fix`.
* Assumption ledger; unresolved key geometry is not invented.
* Blender 5.2.2: bevel/smooth evaluate, EEVEE renders, GLB/OBJ/STL/BLEND export.
* MCP stdio, DSH plugin, HTTP bridge, OpenAI/Anthropic manifests, installers.
* Text/ASCII perception (`atta_describe(perceive=true)` / `atta perceive`):
  silhouettes, sections, traits, construction trace, silhouette diff; no
  Blender, typically 0.1–0.3 s.
* 215 tests pass, including a real Blender render, an MCP model->verify test,
  the perception pack, the corpus validator, studio previews, diagnostic
  passes and the image-analysis math.

## Architecture in one screen

```
agent host (ZCode MCP / DSH plugin / Claude / Cursor / HTTP)
  -> atta/agent/tools.py        10 tool contracts, envelopes, ordering lock
  -> atta/engine.py             10 verbs, state, receipts, batch
  -> atta/spec.py               validate / propose / compile / hash
     atta/recipes.py            curated parametric families
     atta/features.py           general feature DAG
  -> atta/mesh.py + primitives/curves/surfaces/boolean   (numpy, metres)
  -> atta/metrology/probe/facts/verify/perception        (mm facts + ASCII view)
  -> atta/blender/runtime+worker                         (bpy only here)
  -> atta/state.py + locks.py                            (_atta workspace)
```

## Key decisions

1. **Facts before pixels.** Renders are judgement, not evidence.
2. **Small surface.** 10 verbs; refine/fit/probe fold into model/variant/describe.
3. **Spec is the unit of truth.** A record stores how to rebuild, not a mesh.
4. **Assumptions ≠ measurements.** Ledger entries are `basis=design_default`.
5. **Blender is a backend.** Geometry is computable without opening Blender.
6. **One build at a time per workspace.** `project.lock` + ordered tool calls.

## Installation status

* ZCode MCP config `~/.zcode/cli/config.json` now points to:
  `python3 -m atta.agent.mcp`, `cwd=$ATTA_ROOT`.
  **Restart ZCode** to replace the old in-process MCP server; this session still
  shows the old `cad.status` server.
* DSH profiles `~/.dsh/profiles/{web,headless}` point their
  `@atta/dsh-atta` dependency and symlink at `$ATTA_ROOT/plugin`.
  **Restart DSH** to load the new plugin.
* The old Atta plugin/server is not deleted; it is simply no longer linked.

## Closed since this snapshot

1. **Assembly** — parts, ports, 13 mate types, SE(3) solve, DOF, collisions,
   assembly receipt.
2. **Diagnostic preview** — per-part materials, studio/catalog/dark/clay,
   `focus=<part>` macro, normal/depth/id/curvature/deviation, image analysis.
3. **Constraint solve** — `targets`/`constraints`, sensitivity, closest
   achievable, Pareto, assembly-level `part.param`.
4. **B-rep backend** — analytic STEP for the graph subset via build123d/OCCT.

Remaining: kernel fillet/chamfer, ribs/bosses, shell openings, grammar/L-system,
exact reference genus on six TPMS/fractal cases, depth of field/HLR line art,
geometry cache and parallel variants.

See `docs/ROADMAP.md` for the radar mapping and
`docs/SHAPE_COVERAGE_REQUIREMENTS.md` for the crawler-facing breadth plan.

## Boundaries

* Do not fabricate measurements or verify receipts.
* Do not expose the 88 low-level ops as 88 tools.
* Do not add GUI/click simulation.
* Do not make `deliver` bypass verify.
* Do not merge the 2D DXF product into this kernel.

## Fast health check

```bash
cd $ATTA_ROOT
python3 -m pytest -q
python3 -m atta --json doctor --deep
python3 -m atta --json run --steps-file examples/pipeline.json
```
