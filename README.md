# Atta

<img src="docs/assets/mark.svg" width="88" align="right" alt="Atta mark">

**English | [中文](README.zh-CN.md)**

Atta is a parametric CAD kernel built for language models to drive. You describe a
part as a rebuildable spec, Atta computes the geometry in deterministic Python,
**measures** it, and only then lets a render or an export exist. An agent never has
to guess a shape from a screenshot, and it can prove the result is a closed solid
before claiming it is done.

Ten tools, 23 recipes, 25 feature ops. Blender is optional.

## Why it is different

| Principle | What it means in practice |
|---|---|
| **Facts before pixels** | `describe` returns bbox, volume, topology, wall thickness, rays and ASCII sections in under 0.3 s. A render is judgement, never evidence. |
| **The spec is the model** | A `model_id` stores *how to rebuild*, not a mesh blob. The same spec reproduces the same geometry hash. |
| **Three-state gate** | `verify` returns `pass` / `degraded` / `fail` with a receipt. `deliver` refuses anything that is not `pass` (or `degraded` with an explicit override). |
| **Errors are patches** | Every failure carries a `code`, a `path` and a concrete `fix` — not an apology. |
| **Blender is a backend** | Geometry, facts, verify and perception are pure numpy. Only renders and GLB/BLEND export need Blender. |

## Install

```bash
git clone https://github.com/laszapens/Atta.git atta && cd atta
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Optional extras:

```bash
pip install -e ".[brep]"   # analytic STEP export (build123d / OpenCASCADE)
pip install -e ".[dev]"    # pytest + jsonschema, for running the test suite
```

Requires **Python ≥ 3.10**. Core dependencies are `numpy`, `scipy`, `trimesh`,
`manifold3d` and `pillow` — all installed by `pip install -e .`.

## Quickstart

```bash
python3 -m atta doctor --deep                 # 1. is the engine healthy?
python3 -m atta catalog --verbose             # 2. what can it build? (23 recipes)
python3 -m atta model --recipe product.mug    # 3. build a draft in well under a second
python3 -m atta describe --wall               # 4. read the measured facts
python3 -m atta perceive --res 24             # 5. see it as text/ASCII, no render
python3 -m atta verify --level standard       # 6. gate it: pass / degraded / fail
```

`model` prints its `model_id`; every later verb defaults to the most recent model,
so you do not have to copy it around. Add `--json` to any command for the full
`{ok, data, error}` envelope instead of the human view.

When the verdict is `pass`, produce files:

```bash
python3 -m atta preview --mode studio --views hero --res 1200   # needs Blender
python3 -m atta deliver --formats glb,obj,stl                   # needs Blender
```

`perceive` looks like this — a sub-second viewfinder you can iterate against
without waiting on a render:

```
product.mug: 116.7x83.1x95.0 mm; aspect 1.00:0.71:0.81; V=93.69 ml;
closed=yes; genus=1; comps=1; convex=0.19; sym=y; holes~1; dih95=3.7

[front] coverage=0.481  # solid, . empty
....##########..........
....###########.........
...############.........
...###################..
..####################..
..#################.##..
..###############..###..
..###############..###..
```

## The ten tools

Every verb is also an agent tool with the same name (`atta_` prefix).

| Tool | One line |
|---|---|
| `atta_doctor` | Environment and capability self-check; run it first. |
| `atta_catalog` | Recipes, feature ops, materials, view and format lists, with parameter schemas. |
| `atta_spec` | `validate` / `propose` / `compile` / `schema` for a Spec v3 document — no geometry. |
| `atta_model` | Build from a recipe, a spec or a feature graph; `patches=` refines an existing spec. |
| `atta_variant` | Sample the parameter space, or solve real targets against measured metrics. |
| `atta_describe` | Facts, spatial probes, ASCII perception, construction trace, silhouette diff. |
| `atta_verify` | The three-state gate with a receipt you can store. |
| `atta_preview` | Studio/diagnostic renders plus image analysis. Needs Blender. |
| `atta_deliver` | Verify, then export GLB/OBJ/STL/BLEND (+ analytic STEP for a graph subset) with a manifest. |
| `atta_run` | Run a multi-step pipeline in one call, threading `model_id` between steps. |

The CLI adds two conveniences that are not tools: `perceive` (a shortcut for
`describe --perceive`) and `tool <name> --args-file`, the bridge the host plugins
use.

## Spec v3

A spec is JSON. There are three model kinds; `recipe` is the short path.

```json
{
  "schema": "atta.spec/v3",
  "name": "mug-01",
  "model": {"kind": "recipe", "id": "product.mug", "preset": "nominal",
            "params": {"height": 95, "wall": 3.2, "handle_enabled": true}},
  "quality": "draft",
  "material": "ceramic",
  "finish": {"bevel_mm": 0.6, "smooth_deg": 35},
  "constraints": {"bbox": {"z": [80, 120]}, "min_wall_mm": 2.4}
}
```

```bash
python3 -m atta model --spec-file examples/mug/spec.json
```

A `graph` spec is a DAG of feature ops, all arguments in **mm**:

```json
{
  "schema": "atta.spec/v3",
  "name": "bracket-01",
  "model": {
    "kind": "graph",
    "output": "cut",
    "features": [
      {"id": "plate",  "op": "box", "args": {"size": [80, 50, 6]}},
      {"id": "hole",   "op": "cylinder", "args": {"radius": 4, "height": 20, "center": [14, 25, 3]}},
      {"id": "cut",    "op": "difference", "inputs": ["plate", "hole"]}
    ]
  },
  "constraints": {"bbox": {"x": [75, 85], "y": [45, 55]}, "volume_mm3": [15000, 26000]}
}
```

```bash
python3 -m atta model --spec-file examples/bracket/spec.json
python3 -m atta deliver --formats step          # analytic B-rep, no Blender needed
```

The 25 ops are `box`, `cylinder`, `sphere`, `cone`, `torus`, `revolve`, `loft`,
`sweep`, `coons`, `gordon`, `extrude`, `thicken`, `lobes`, `union`, `difference`,
`intersection`, `mirror`, `pattern_linear`, `pattern_polar`, `transform`, plus
`offset`, `gyroid`, `sdf`, `text_engrave` and `text_emboss`. Arguments may use
`{"$param": "w"}` or `{"$expr": "w/2 + 4"}`. `atta_catalog --verbose` documents
every op's required and optional keys.

The third kind, `assembly`, adds `parts[]`, ports and 13 mate types, solved in
SE(3) with DOF from the Jacobian rank — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §8.

## Connect an agent

### MCP (any host)

The server is stdio JSON-RPC; print the config for your host and paste it in.

```bash
python3 -m atta.agent.install claude     # Claude Desktop JSON
python3 -m atta.agent.install cursor     # Cursor JSON
python3 -m atta.agent.install zcode      # merges into ~/.zcode/cli/config.json
python3 -m atta.agent.mcp                # or run the server directly
```

Every emitted entry uses absolute paths: `python -m atta.agent.mcp` with `cwd`,
`ATTA_ROOT` and `PYTHONPATH` set to this checkout. Restart the host afterwards to
load the `atta_*` tools.

### DSH

```bash
python3 -m atta.agent.install dsh            # dry run: which profiles would change
python3 -m atta.agent.install dsh --create   # write dependency + bundle + symlink
```

The plugin lives in [`plugin/`](plugin) and its skill in
[`plugin/skills/atta/SKILL.md`](plugin/skills/atta/SKILL.md). It shells out to
`python3 -m atta`, so it never re-implements geometry. Set `ATTA_PYTHON` if the
host's default interpreter cannot `import atta`, and `ATTA_ROOT` if the checkout
moves.

### ZCode

Two options. The standalone MCP entry above is enough for the tools; the native
plugin adds a routing skill and the local marketplace.

```bash
python3 -m atta.agent.install zcode-plugin
# writes plugins/atta/.mcp.json (per-machine, git-ignored) from .mcp.json.example
```

Then in ZCode: **Plugin Marketplace → Add → Add Plugin Marketplace** → paste this
repo's `plugins/` directory → **Personal → dev-atta-local → Atta → Install**.
Plugin files: [`plugins/atta/`](plugins/atta) and [`plugins/marketplace.json`](plugins/marketplace.json).
Enable either the standalone `mcp.servers.atta` entry or the plugin, not both, or
you will see duplicate tool listings.

### HTTP bridge

```bash
python3 -m atta.agent.http --port 8765

curl -s localhost:8765/health
curl -s localhost:8765/tools
curl -s "localhost:8765/manifest?format=openai"
curl -s -X POST localhost:8765/call -H 'content-type: application/json' \
     -d '{"tool":"atta_model","args":{"recipe":"product.mug"}}'
```

Loopback only, no authentication: it is a local agent bridge, not a network
service. Do not expose the port.

## Requirements and what works without Blender

| Capability | Without Blender | With Blender |
|---|---|---|
| `doctor`, `catalog`, `spec` | yes | yes |
| `model`, `variant`, `describe`, `perceive`, `verify`, `run` | yes | yes |
| `deliver --formats step` (graph specs + `build123d`) | yes | yes |
| `deliver --formats glb,obj,stl,blend` | no — each file is reported `blender_unavailable`, `status: "partial"` | yes |
| `preview` (any mode) | no — `blender_unavailable` | yes |

Mesh export degrades rather than crashing: `deliver` still verifies and writes the
manifest, and the file list marks each unexported format with a `blender_unavailable`
error and a `fix`. `preview` has nothing to fall back to, so it returns the error
directly — use `describe --perceive` instead when Blender is missing.

Atta finds Blender on `PATH`; set `ATTA_BLENDER=/path/to/blender` to pin a
specific build (it is authoritative — if it does not resolve, Atta reports no
Blender rather than silently using another one). `doctor` shows what it found.

Workspace state — `models.json`, previews, exports — lives under
`$ATTA_HOME/_atta/`, defaulting to the current directory. Point `ATTA_HOME` at a
project directory to keep runs separate, and add `_atta/` to that project's
`.gitignore`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `blender_unavailable` | Blender is not installed or not on `PATH`. Geometry, facts, verify and `perceive` still work; install Blender or set `ATTA_BLENDER` only when you need renders or mesh export. `deliver --formats step` is an analytic path that does not need Blender. |
| `no_models` | The workspace has no model yet, or `ATTA_HOME` points somewhere else. Run `model` first, or pass the `model_id` from an earlier result. |
| `unknown_recipe` | The id is misspelled. The error's `fix` lists all 23 valid ids; `catalog` shows their parameters. |
| Build says `status: "repaired"` | A parameter was outside its bounds and was clamped. Read `applied_patches` / `issues` — the model you got is not the one you asked for. |
| `deliver` returns `status: "blocked"` | `verify` did not pass. Fix the first entry in `verify.issues` (`fix` is populated) and rebuild; `degraded` needs an explicit `allow_degraded=true`. |
| Host plugin cannot `import atta` | The host is using a different interpreter than your venv. Set `ATTA_PYTHON` to the venv's python, or `pip install -e .` with the interpreter the host uses. |

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how the layers fit,
[`docs/LLM_NATIVE.md`](docs/LLM_NATIVE.md) for the agent-facing design rules,
[`docs/INTERFACES.md`](docs/INTERFACES.md) for the internal API contract,
[`docs/QUALITY.md`](docs/QUALITY.md) for what "good" is checked against, and
[`docs/SHAPE_COVERAGE.md`](docs/SHAPE_COVERAGE.md) for what each recipe covers and
where the gaps are.

## Repository layout

```
atta/            the Python package (numpy kernel + Blender backend + agent layer)
  families/      one auto-discovered module per shape category
  agent/         tool contracts, MCP, HTTP, manifests, host installers
  blender/       the only place that imports bpy
tests/           unit, contract and end-to-end tests
examples/        mug and bracket specs plus a batch pipeline
docs/            architecture, interfaces, LLM design notes, roadmap, coverage, quality
  assets/        identity mark (icon only)
plugin/          DSH plugin
plugins/         ZCode plugin + local marketplace
corpus/bench/    held-out generalization cases
scripts/         maintainer tools (benchmark runner, corpus validator)
```

## Contributing

Bug reports with a reproducing spec are the most useful contribution. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to add a recipe, add a feature op, run
the tests, and the code style the repository actually uses.

Atta is MIT licensed — see [LICENSE](LICENSE).
