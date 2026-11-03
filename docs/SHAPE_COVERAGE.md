# Shape coverage

23 recipes across 11 of the 12 shape categories. Every recipe in this table builds
a **closed, single-component manifold** at `quality=draft` and passes
`atta_verify --level standard` with its declared genus as an error-level check.

The numbers below are measured, not asserted. Reproduce the whole table with:

```bash
python3 -m pytest tests/test_families.py tests/test_coverage_gate.py -q
```

## The 23 recipes

`params` is the number of typed parameters the recipe exposes (`catalog
--verbose` prints them with bounds, units and defaults). `genus` is the
**measured** Euler characteristic of the delivered mesh, which must equal the
recipe's declared `expected_genus`. Build time is a draft build on a warm
process; the verify column is the verdict at `standard`.

| recipe | params | genus | triangles | draft build | verify |
|---|---|---|---|---|---|
| `primitive.chamfer_box` | 8 | 0 | 72 | 0.03 s | pass |
| `part.bracket` | 7 | 2 | 612 | 0.01 s | pass |
| `part.enclosure` | 8 | 0 | 44 | 0.01 s | pass |
| `mechanical.flange` | 13 | 7 | 2328 | 0.06 s | pass |
| `mechanical.gear.involute` | 10 | 1 | 2546 | 0.26 s | pass |
| `mechanism.gear` | 7 | 1 | 1856 | 0.05 s | pass |
| `tolerance.clearance_ladder` | 7 | 0 | 2272 | 0.15 s | pass |
| `product.mug` | 13 | 1 | 25482 | 0.10 s | pass |
| `product.bowl` | 11 | 0 | 20544 | 0.03 s | pass |
| `product.vase` | 12 | 0 | 30912 | 0.04 s | pass |
| `product.bottle` | 8 | 0 | 2684 | 0.08 s | pass |
| `product.screw_cap` | 13 | 0 | 12030 | 0.20 s | pass |
| `assembly.bolt_nut` | 13 | 1 | 440 | 0.02 s | pass |
| `pattern.honeycomb_panel` | 10 | 35 | 1088 | 0.02 s | pass |
| `lattice.gyroid_slab` | 7 | 39 | 171912 | 0.59 s | pass |
| `surface.panel` | 7 | 0 | 3296 | 0.01 s | pass |
| `surface.dome` | 9 | 0 | 3296 | 0.01 s | pass |
| `surface.coons_duct` | 9 | 1 | 8192 | 0.02 s | pass |
| `surface.fillet_network` | 7 | 0 | 16616 | 4.40 s | pass |
| `architecture.fluted_column` | 12 | 0 | 22708 | 0.61 s | pass |
| `architecture.tower.growth` | 11 | 0 | 196 | 0.05 s | pass |
| `organic.seashell` | 10 | 0 | 7676 | 0.01 s | pass |
| `text.engraved_plate` | 19 | 0 | 594 | 0.02 s | pass |

Total draft-build time for all 23 on one warm process: **6.8 s**.
`surface.fillet_network` is the outlier at ~4.4 s because it resamples an SDF
offset field; every other recipe is under 0.7 s and 20 of them are under 0.25 s.

## Categories

`docs/coverage_matrix.json` defines the 12 categories and what each one must
accept. The gate is executable: `tests/test_coverage_gate.py` reads the matrix
and fails any recipe that violates its own category's acceptance rule (closed,
`min_wall_mm`, genus, single component).

| category | recipes shipped | ids |
|---|---|---|
| primitive | 1 | `primitive.chamfer_box` |
| mechanical | 4 | `mechanical.flange`, `mechanical.gear.involute`, `mechanism.gear`, `part.bracket` |
| product | 6 | `product.mug`, `product.bowl`, `product.vase`, `product.bottle`, `product.screw_cap`, `part.enclosure` |
| surface | 4 | `surface.panel`, `surface.dome`, `surface.coons_duct`, `surface.fillet_network` |
| architecture | 2 | `architecture.fluted_column`, `architecture.tower.growth` |
| lattice | 1 | `lattice.gyroid_slab` |
| pattern | 1 | `pattern.honeycomb_panel` |
| organic | 1 | `organic.seashell` |
| assembly | 1 | `assembly.bolt_nut` |
| tolerance | 1 | `tolerance.clearance_ladder` |
| text | 1 | `text.engraved_plate` |
| material_finish | 0 | — |

`material_finish` has `target_families: 0` in the matrix — it asks for reference
examples, not a family — so it is not a gap and the gate exempts it.

## The 25 feature ops

`atta/families/*.py` can register ops without touching the core, and five of the
25 come from families rather than `features.py`: `gyroid`, `offset`, `sdf`,
`text_engrave`, `text_emboss`.

`box`, `cylinder`, `sphere`, `cone`, `torus`, `revolve`, `loft`, `sweep`, `coons`,
`gordon`, `extrude`, `thicken`, `lobes`, `union`, `difference`, `intersection`,
`mirror`, `pattern_linear`, `pattern_polar`, `transform`, `offset`, `gyroid`,
`sdf`, `text_engrave`, `text_emboss`.

`python3 -m atta catalog --verbose` prints each op's `required` / `optional`
arguments, arity and doc string — that output is the authoritative list.

## Known gaps

These are real limits of the current tree, not polish items:

1. **Genus is a constant per recipe, not a function of the parameters.** Three
   recipes derive their genus from a hole/cell count and rewrite
   `expected_genus` per build (`mechanical.flange` = 7, `pattern.honeycomb_panel`
   = 35, `lattice.gyroid_slab` = 39; the gyroid value only holds for the 3×2×1
   slab). Change the count parameter and the declared genus no longer describes
   the mesh, so verify's genus check will fail. Treat the genus in the table as
   "true at `preset=nominal`".
2. **Implicit-field recipes are dimensionally fragile.** `lattice.gyroid_slab`
   compares a field value against a metric distance with no unit guard; the
   delivered volume moves between ~26 200 mm³ and ~18 000 mm³ depending on
   inputs while the topology stays legal. A `pass` verdict on an implicit recipe
   does not by itself prove the volume is right.
3. **Bevel is charged against the wall budget.** `verify` applies the finish
   bevel before sampling wall thickness, so a shallow boolean intersection plus
   a bevel can read as a knife edge (observed: a 110 mm solid measuring 0.118 mm
   locally). Since the gate reads the minimum, one bad sample fails the part.
4. **Boundary cases are not in CI.** `tests/test_families.py` builds each recipe
   once at nominal. Boundary parameter sets are not re-run per commit.
5. **`surface.fillet_network` is slow** relative to everything else (~4.4 s draft,
   from the SDF offset resample). Budget for it in batch pipelines.

## Corpus (bench only)

What ships is `corpus/bench/generalization_v1.json`: **100 held-out cases**
expressed as generic Spec v3 feature graphs, so they never call a shipped recipe.
They are scored on the `draft` quality level.

| metric | value |
|---|---|
| cases | 100 |
| categories covered | 12 |
| build success | 100 % |
| faithful build | 80 % |
| verify pass | 94 % |
| op coverage | 94.5 % |

All 100 cases **build**; what separates them is fidelity. `faithful build`
(80 %) is the strict measure: built **and** every reference op expressible
**and** declared topology matched. The 20 that miss it split cleanly, and the
distinction matters:

* **14** lose it on `ops_missing` — the source record needs grammar/L-system ops
  (`rule` 11, `startshape` 10, `cf::symmetry` 8) that Atta does not implement, so
  the generic template produces a legitimate but structurally simpler part.
  These still pass `verify`; they are an expressiveness gap, not a defect.
* **6** lose it on declared topology, and those 6 are exactly the `verify`
  failures (94 % pass): gyroid sheet, Schwarz P, gyroid cube, porous torus,
  porous coral and the Mandelbulb. Every one builds and delivers geometry; what
  fails is the declared-`genus` / `n_components` check. This is the same
  exact-implicit-topology limitation listed under known gaps.

The corpus *counting* data (`corpus/coverage_report.json`,
`corpus/license_report.json`) ships as static snapshots of a larger collection
that is not redistributed. The raw records and the harvest tooling are not part
of this repository; the reports are kept because they document which shape
families exist in the wild and how their licences were tiered.
