---
name: atta-modeling
description: Use Atta v1.2 through its ten MCP tools for parametric 3D modeling, constraint solving, multi-part assemblies, measured inspection, verification and GLB/OBJ/STL/STEP delivery. Trigger when the user asks to create, edit, solve, assemble, inspect, validate or export a 3D model, or mentions Atta, CAD parts, mates, DOF, STEP, or "make it printable".
---

# Atta v1.2 — modeling with receipts

This is the **ZCode routing stub**. The authoritative procedure lives in one place:

> **`plugin/skills/atta/SKILL.md`** — read that for the decision table, the
> iteration loop, the aesthetic/manufacturing rules and the error table.
> Reference detail: `plugin/skills/atta/references/`.

Do not maintain a second copy of the procedure here. This file only covers what
is specific to the ZCode/MCP host.

## The surface

Ten MCP tools, named `mcp__atta__<verb>`:

`atta_doctor` · `atta_catalog` · `atta_spec` · `atta_model` · `atta_variant`
`atta_describe` · `atta_verify` · `atta_preview` · `atta_deliver` · `atta_run`

Same ten verbs, same arguments, same envelopes as the DSH plugin — both are shells
over the one Python engine.

## The rule that matters

Never call a model done without an `atta_verify` receipt whose `verdict == "pass"`.
`degraded` is not `pass`. Facts come from measurement, not from looking at a render.

If a call returns `{ok:false}`, read `code`, `path` and `fix`, then correct exactly
that thing. `fix` is a patch, not an apology.

## Units

Every public spec / param / constraint / probe is **mm and degrees**. The kernel
and the Blender scene are metres. Never convert by hand — the tools do it at the
boundary. `+Z` is up.

## Fast loop (memorize this shape)

```
catalog -> spec(validate/propose) -> model(draft) -> describe(perceive=true)
   -> model(patches=...) -> verify -> [preview 1 image] -> deliver
```

`describe(perceive=true)` is the sub-second viewfinder (ASCII silhouettes,
sections, shape traits). Use it while the shape is still moving. Do **not** call
`atta_preview` until `verify` passes — it starts Blender and takes tens of seconds,
and a render has no diagnostic value for shape problems.

## Minimum viable correctness

- A recipe exists? Use the recipe, not a hand-written graph.
- `patches` must travel with `recipe=` or `spec=`; `patches` alone is a
  `bad_arguments` error.
- Check `genus` against the recipe's `expected_genus` — a plate with one through
  hole is genus 1, not 0.
- Give `verify` explicit `constraints` for the dimensions the user actually asked
  for. "It looks about right" is not a constraint.

## Quality

Before `atta_deliver`, run the 13-item checklist in
`plugin/skills/atta/references/quality.md` (full version: `docs/QUALITY.md`).
The short version: closed manifold · genus matches · pass verdict · wall ≥ process
minimum · every exposed hard edge beveled · reply carries model_id + verdict +
dimensions + paths.

## Delivery

`atta_deliver(formats=["glb","obj"])`. STEP (`formats=["step"]`) is analytic B-rep
for the graph subset; unsupported ops fail with `brep_unsupported` and a fix —
there is never a fake tessellated STEP. The mesh verify receipt still gates delivery.

## Style

Match the user's language. Report `model_id`, `verdict`, hashes and artifact paths.
Show the smallest useful artifact first (facts, ASCII, one image), then the detail.
Do not narrate the whole tool log.
