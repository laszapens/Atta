# Contributing to Atta

Atta is a parametric CAD kernel whose primary user is a language model. That
constrains the design: the tool surface stays small, every failure must be
actionable, and a model is never "finished" without a receipt that proves it.

## The one rule

Never claim a model is done without an `atta_verify` receipt whose
`verdict == "pass"`. `degraded` is not `pass`. Assumptions in a spec are design
defaults, never measurements.

## Development setup

```bash
git clone <repo> atta && cd atta
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python3 -m pytest
```

Requires **Python ≥ 3.10**. Blender is optional: install it and put it on `PATH`
(or set `ATTA_BLENDER`) only if you want the render/export tests to run; the
suite skips them otherwise. `pip install -e ".[brep]"` adds `build123d` for STEP
delivery.

Run the tests against a scratch workspace so you do not pollute your project
state:

```bash
ATTA_HOME=/tmp/atta-test python3 -m pytest -q
```

The full suite takes about three minutes, mostly in the two solver tests.

## Where to change what

| You want to | Change |
|---|---|
| add a shape family (recipe) | a new file in `atta/families/<category>.py` |
| add a feature-graph op | `atta/features.py`, or `register_op` from a family |
| solver behaviour | `atta/solve.py` |
| assembly mates / DOF / collisions | `atta/assembly.py` |
| B-rep / STEP | `atta/brep.py` |
| image analysis | `atta/vision.py` |
| Blender worker protocol | `atta/blender/worker.py` |
| the public tool surface | `atta/agent/tools.py` — keep it at ten tools |

## Adding a recipe

A family is one file. `atta/families/__init__.py` discovers every module in that
package with `pkgutil` and imports it, so **creating the file is the whole
registration step** — there is no list to edit and no import to add.

```python
# atta/families/product.py  (or any new module in that package)
from ..primitives import box
from ..registry import recipe

@recipe(
    "product.widget", "小部件",                       # id, display name
    "One line on the geometry and the algorithm.",     # description
    params={
        "size": {"type": "float", "default": 20.0, "min": 5.0, "max": 80.0,
                 "unit": "mm", "group": "body", "description": "边长"},
    },
    presets={"safe": {"size": 15.0}, "nominal": {"size": 20.0},
             "hero": {"size": 30.0}},
    tags=["product", "primitive"],
    expected_genus=0, material="abs_white",
    constraints=[
        {"id": "writable", "expr": "size >= 5", "fix": "size = 10"},
        {"id": "sane", "expr": "size <= 80", "fix": "size = 40"},
    ],
)
def build_widget(v, quality="draft"):
    """Build and return ``(mesh, finish_dict)``. ``v`` is in metres."""
    return box((v["size"],) * 3), {}
```

The builder contract:

* `v` holds **kernel units — metres** — already converted from the declared
  `unit`. Angles stay in degrees; counts and ratios pass through unchanged.
* Return `(mesh, finish)`. `finish` may be `{}`; the engine fills defaults.
* Build from `atta.primitives`, `atta.surfaces`, `atta.boolean`, `atta.curves`
  and the helpers in `atta.kit` rather than hand-rolling vertex arrays.
* Return a **closed, single-component manifold**. That is the point of the
  library; `verify` will fail the part otherwise.

What the gates require of a recipe, and where they live:

| Requirement | Enforced by |
|---|---|
| presets include `safe`, `nominal`, `hero` | `tests/test_families.py::test_family_metadata_is_complete` |
| at least 2 constraints | same test |
| non-empty name, description and tags | same test |
| every numeric param has `min` and `max`, with `min <= default <= max` | same test |
| all three presets resolve without an error-severity issue | `test_every_family_resolves_all_presets` |
| draft build is closed, single-component, non-degenerate | `test_every_family_builds_a_closed_solid` |
| the recipe meets its category's acceptance rule | `tests/test_coverage_gate.py` |

### The gotcha: a new id prefix needs a `LEGACY_CATEGORY` entry

`tests/test_coverage_gate.py` decides which acceptance rule a recipe must satisfy
by looking at the prefix of its id:

```python
PREFIX_CATEGORY = {"mechanical": "mechanical", "product": "product", ...}
```

An id whose prefix is not a key of that map falls through to category
`"other"`, which has no row in `docs/coverage_matrix.json`. The gate then applies
an almost empty rule set — no `closed`, no `min_wall_mm` — so the family looks
covered while imposing nothing. `part.` and `mechanism.` are exactly this case,
which is why the built-ins `part.bracket`, `part.enclosure` and `mechanism.gear`
each have an explicit entry.

If your recipe uses a new prefix, name it after a real category (`product.`,
`surface.`, `mechanical.`, …) or add the entry:

```python
LEGACY_CATEGORY = {
    "mechanism.gear": "mechanical",
    "part.bracket": "mechanical",
    "part.enclosure": "product",
    "widget.thing": "product",        # <- your new id here
}
```

Check it by reading the parametrized test id, which names the category the gate
resolved to:

```bash
python3 -m pytest tests/test_coverage_gate.py -q -k widget --collect-only
```

```
# good  — the gate applies the product acceptance rule
[...::test_family_passes_its_category_acceptance[product.widget-product]]

# bad   — the gate applies the empty "other" rule set and silently passes
[...::test_family_passes_its_category_acceptance[widget.thing-other]]
```

## Adding a feature op

Ops live in `atta/features.py` (`FEATURE_OPS` plus the dispatch). A family can
add one without touching the core:

```python
from ..features import register_op

def widget_mill(size, flutes=4, mesh=None):
    """Called as ``widget_mill(**prepared_args)``; return a Mesh."""
    ...

register_op("widget_mill", {"required": ("size",), "optional": ("flutes",),
                            "arity": 0, "lengths": ("size",),
                            "doc": "Mill a widget pocket; size in mm."},
            widget_mill)
```

The function is invoked as `fn(**prepared_kwargs)` and must return a `Mesh`.
Declare `lengths` for every millimetre argument, `vectors` for coordinate
lists and `ints` for counts, so the core converts units and validates types once
instead of you re-deriving `scale` in every op. An op with `arity: 1` receives the
wired input mesh as a `mesh=` keyword. If the generated argument handling is not
enough, attach a `prepare` attribute — `fn.prepare(args, scale, path)` — and
return the keyword dict yourself.

Verify the op is registered and callable through a spec, not just through your
module:

```bash
python3 -m atta --json model --spec '{"schema":"atta.spec/v3","name":"w",
  "model":{"kind":"graph","features":[{"id":"a","op":"widget_mill","args":{"size":10}}]}}'
```

## Tests

- `python3 -m pytest` — everything. About 215 tests, ~3 min.
- `python3 -m pytest tests/test_families.py tests/test_coverage_gate.py -q` —
  the two shape gates; run these first after adding a recipe.
- `python3 -m pytest tests/test_preview_visual.py -q` — needs Blender.
- `python3 scripts/run_bench.py --out /tmp/bench.json` — the held-out
  generalization bench over `corpus/bench/generalization_v1.json`.

Add a test for every behavior change. If a test needs Blender, skip it when
Blender is missing rather than passing silently.

Careful with `scripts/run_bench.py`: its default `--out` is the shipped
`corpus/bench/generalization_v1.json`, so a plain run rewrites a tracked file. Use
`--out /tmp/bench.json` unless you intend to update the benchmark result.

`scripts/validate_corpus.py` validates the corpus survey's JSONL records. Those
records are not distributed with this repository, so it only does something if
you supply record files yourself.

## Style

The repository is deliberately conservative and has no linter configuration —
match the surrounding code rather than reformatting it.

- Every module starts with `# -*- coding: utf-8 -*-` and a docstring that says
  *why* the module exists, not what the functions are called.
- `from __future__ import annotations` at the top; modern annotations throughout
  (`str | None`, `dict[str, int]`).
- **String formatting is `%`-style**, not f-strings, in the engine path. There are
  ~400 `"..." % (...)` sites and ~70 f-strings; keep the ratio. f-strings are fine
  in new test code.
- Lines run to 100+ characters in places; there is no enforced limit, but the
  long lines are prose in docstrings, not code.
- Errors are `AttaError(code, message, path=..., fix=...)`. The `fix` must be
  something the caller can apply without reading a traceback. Never let a bare
  exception escape a tool boundary.
- Public units are **mm** and **degrees**; the kernel and the Blender scene are
  **metres**. Convert at the boundary, and declare `unit` on every parameter.
- Comments explain intent or a trap. `# noqa: BLE001 - CLI must not dump a raw
  traceback` is the house style for a deliberate broad except.

## Pull requests

Keep the change focused. State the receipt or benchmark that proves it. If a
capability is partial, say exactly which ops or shapes are covered and which are
not — the `brep_unsupported` and `degraded` patterns are the model to follow. A
new recipe should arrive with its coverage-gate row already passing.

Atta is MIT licensed. By contributing you agree to license your work under the
same terms.
