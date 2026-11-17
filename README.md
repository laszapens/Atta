# Atta

<img src="docs/assets/mark.svg" width="88" align="right" alt="Atta mark">

**English | [中文](README.zh-CN.md)**

Atta is a parametric CAD kernel meant to be driven by an LLM agent. You write a
part as a JSON spec, Atta builds the geometry in plain Python (numpy), measures
it, and checks it before you render or export anything. The agent gets real
numbers back (bounding box, volume, wall thickness, topology) instead of having
to judge a screenshot, and it can confirm the part is a closed solid before it
says it's done.

**Current release: atta-1.2.2beta** (`1.2.2b3` on the Python side,
`1.2.2-beta.3` for the plugin packages). 12 tools, 23 recipes, 47 feature ops.
Blender is optional.

New in this beta: rational NURBS patches, exact CAD fillets/chamfers with
matching STEP output, bounded fields, oriented module assembly, and a set of
bio-inspired morphology ops. Cameras, textures and appearance moved out into a
separate program, `basid` (see [BASID.md](docs/BASID.md)). Full details and
known limits are in the [release notes](docs/RELEASE-1.2.2beta.md).

## How it works

- **Measure first, render later.** `describe` returns bbox, volume, topology,
  wall thickness, ray probes and ASCII sections in about 0.3 s. Renders are
  useful for eyeballing, but they don't count as a check.
- **The spec is the model.** A `model_id` stores how to rebuild the part, not a
  mesh. Rebuilding the same spec gives the same geometry hash.
- **Three verdicts.** `verify` returns `pass`, `degraded` or `fail`, plus a
  receipt. `deliver` only exports `pass` (or `degraded` if you explicitly allow it).
- **Errors say how to fix them.** Each error has a `code`, a `path` into the
  spec, and a `fix` hint.
- **Blender is just a backend.** Geometry, measurement, verification and ASCII
  perception are all numpy. You only need Blender for renders and GLB/BLEND export.

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

Needs **Python ≥ 3.10**. The core dependencies (`numpy`, `scipy`, `trimesh`,
`manifold3d`, `pillow`) are pulled in by `pip install -e .`.

## Quickstart

```bash
python3 -m atta doctor --deep                 # 1. check the environment
python3 -m atta catalog --verbose             # 2. list what it can build (23 recipes)
python3 -m atta model --recipe product.mug    # 3. build a draft (well under a second)
python3 -m atta describe --wall               # 4. read the measurements
python3 -m atta perceive --res 24             # 5. look at it as ASCII, no render
python3 -m atta verify --level standard       # 6. pass / degraded / fail
```

`model` prints a `model_id`, and later commands default to the most recent
model, so you don't need to pass it around. Add `--json` to any command to get
the raw `{ok, data, error}` envelope instead of the human-readable output.

Once it passes, export files:

```bash
python3 -m atta preview --mode studio --views hero --res 1200   # needs Blender
python3 -m atta deliver --formats glb,obj,stl                   # needs Blender
```

This is what `perceive` prints. It's fast enough to iterate on without waiting
for a render:

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

## Tools

Each CLI verb is also exposed as an agent tool with an `atta_` prefix.

| Tool | What it does |
|---|---|
| `atta_doctor` | Environment and capability check. Run this first. |
| `atta_catalog` | Lists recipes, feature ops, materials, views and formats, with parameter schemas. |
| `atta_spec` | `validate` / `propose` / `compile` / `schema` for Spec v3 documents. Doesn't build geometry. |
| `atta_model` | Builds from a recipe, a spec or a feature graph. `patches=` edits an existing spec. |
| `atta_variant` | Samples the parameter space, or solves for target values of measured metrics. |
| `atta_describe` | Measurements, spatial probes, ASCII perception, construction trace, silhouette diff. |
| `atta_verify` | Returns the verdict and a receipt you can keep. |
| `atta_preview` | Studio and diagnostic renders plus image stats. Needs Blender. |
| `atta_deliver` | Verifies, then exports GLB/OBJ/STL/BLEND (and analytic STEP for part of the graph ops) with a manifest. |
| `atta_run` | Runs a multi-step pipeline in one call, passing `model_id` between steps. |
| `atta_conjure` | Generates a parametric build from an archetype, and records where each value came from. |
| `atta_design` | Design-rule and manufacturing checks, kept separate from geometric verification. |

## Beyond primitives

- **Exact CAD**: geometric edge selection, shared OCCT tessellation, and STEP
  built from the same `fillet`/`chamfer` construction. See [CAD_FEATURES.md](docs/CAD_FEATURES.md).
- **NURBS and fields**: sampled rational patches and bounded composable fields,
  with error and budget reported. See [NURBS.md](docs/NURBS.md) and
  [FIELD_TREE.md](docs/FIELD_TREE.md).
- **Modules and assemblies**: oriented ports, deterministic attach, mates/DOF,
  collision checks, and mixed exact/mesh STEP. See [MODULE_ASSEMBLY.md](docs/MODULE_ASSEMBLY.md).
- **Bio morphology**: capsid, diatom, neuron, valve, lattice-shell and ring-stack
  graph ops. These are shapes only, there's no biological simulation.
- **Appearance**: textures, cameras and textured export live in the separate
  `basid` package, connected through the optional [atta.look](docs/BASID.md)
  bridge. Atta itself only does geometry.

The CLI also has two commands that aren't tools: `perceive` (short for
`describe --perceive`) and `tool <name> --args-file`, which the host plugins call.

## Spec v3

Specs are JSON. There are three model kinds; `recipe` is the simplest.

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

A `graph` spec is a DAG of feature ops. All arguments are in **mm**:

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

The 47 ops cover primitives, `revolve`/`loft`/`loft2`/`sweep`/`sweep2`, Coons and
Gordon patches, `nurbs_surface`, `extrude`, `thicken`, booleans, patterns,
`transform`/`mirror`, `fillet`/`chamfer`, `offset`/`repair`/`subdivide`, implicit
`gyroid`/`sdf`/`sdf_blend`/`field_tree`, text, and the bio morphology set
(`capsid`, `branch_tree`, `pyramidal_neuron`, `diatom_frustule`, `perforated_valve`,
`perforated_cylinder`, `lattice_shell`, `pore_field_shell`, `ring_stack`).
Arguments can reference parameters with `{"$param": "w"}` or `{"$expr": "w/2 + 4"}`.
`atta_catalog --verbose` lists the required and optional keys for every op.

The third kind, `assembly`, adds `parts[]`, ports and 13 mate types. It's solved
in SE(3), with DOF taken from the Jacobian rank; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §8.

## Connecting an agent

### MCP (any host)

The server speaks stdio JSON-RPC. Print the config for your host and paste it in:

```bash
python3 -m atta.agent.install claude     # Claude Desktop JSON
python3 -m atta.agent.install cursor     # Cursor JSON
python3 -m atta.agent.install zcode      # merges into ~/.zcode/cli/config.json
python3 -m atta.agent.mcp                # or run the server directly
```

The generated entries use absolute paths: `python -m atta.agent.mcp`, with `cwd`,
`ATTA_ROOT` and `PYTHONPATH` pointing at this checkout. Restart the host
afterwards so it picks up the `atta_*` tools.

### DSH

```bash
python3 -m atta.agent.install dsh            # dry run: shows which profiles would change
python3 -m atta.agent.install dsh --create   # writes dependency + bundle + symlink
```

The plugin is in [`plugin/`](plugin) and its skill is
[`plugin/skills/atta/SKILL.md`](plugin/skills/atta/SKILL.md). It just calls
`python3 -m atta` and doesn't reimplement any geometry. If the host's default
interpreter can't `import atta`, set `ATTA_PYTHON`; if you move the checkout,
set `ATTA_ROOT`.

To install from Git, use the **repository root** URL:

```bash
dsh plugin --profile web add https://github.com/laszapens/Atta.git
# Web UI: Plugins -> Add plugin -> Git repository, same URL
```

The root [`package.json`](package.json) is a small shim that points DSH at the
real package in [`plugin/`](plugin/package.json). DSH needs the installed package
to declare `dsh.bundle`; without the shim the repo installs as an empty
placeholder and DSH rejects it with `declares no dsh.bundle`. Installing the
subdirectory directly also works on pnpm 9+, but you shouldn't need it:

```bash
dsh plugin --profile web add 'https://github.com/laszapens/Atta.git#path:/plugin'
```

### ZCode

You can either use the standalone MCP entry above (enough to get the tools), or
install the native plugin, which also adds a routing skill and a local marketplace.

```bash
python3 -m atta.agent.install zcode-plugin
# writes plugins/atta/.mcp.json (per-machine, git-ignored) from .mcp.json.example
```

Then in ZCode: **Plugin Marketplace → Add → Add Plugin Marketplace**, paste this
repo's `plugins/` directory, then **Personal → dev-atta-local → Atta → Install**.
Plugin files: [`plugins/atta/`](plugins/atta) and [`plugins/marketplace.json`](plugins/marketplace.json).
Use either the standalone `mcp.servers.atta` entry or the plugin, not both,
otherwise every tool shows up twice.

### HTTP bridge

```bash
python3 -m atta.agent.http --port 8765

curl -s localhost:8765/health
curl -s localhost:8765/tools
curl -s "localhost:8765/manifest?format=openai"
curl -s -X POST localhost:8765/call -H 'content-type: application/json' \
     -d '{"tool":"atta_model","args":{"recipe":"product.mug"}}'
```

It listens on loopback only and has no authentication. It's meant for local
agents, so don't expose the port.

## What works without Blender

| Capability | Without Blender | With Blender |
|---|---|---|
| `doctor`, `catalog`, `spec` | yes | yes |
| `model`, `variant`, `describe`, `perceive`, `verify`, `run` | yes | yes |
| `deliver --formats step` (graph specs + `build123d`) | yes | yes |
| `deliver --formats glb,obj,stl,blend` | no (each file reports `blender_unavailable`, `status: "partial"`) | yes |
| `preview` (any mode) | no (`blender_unavailable`) | yes |

Without Blender, `deliver` still verifies and writes the manifest; formats it
couldn't export are marked `blender_unavailable` with a `fix`. `preview` has no
fallback and just returns the error, so use `describe --perceive` instead.

Atta looks for Blender on `PATH`. Set `ATTA_BLENDER=/path/to/blender` to pin a
specific build. If that path doesn't resolve, Atta reports Blender as missing
rather than falling back to another one. `doctor` shows what it found.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `blender_unavailable` | Blender isn't installed or isn't on `PATH`. Geometry, measurements, verify and `perceive` still work. You only need Blender (or `ATTA_BLENDER`) for renders and mesh export; `deliver --formats step` doesn't need it. |
| `no_models` | No model in the workspace yet, or `ATTA_HOME` points somewhere else. Run `model` first, or pass a `model_id` from an earlier result. |
| `unknown_recipe` | Typo in the id. The error's `fix` lists all 23 valid ids; `catalog` shows their parameters. |
| Build returns `status: "repaired"` | A parameter was out of range and got clamped. Check `applied_patches` / `issues`: the model you got isn't exactly what you asked for. |
| `deliver` returns `status: "blocked"` | `verify` didn't pass. Fix the first item in `verify.issues` (it has a `fix`) and rebuild. `degraded` needs `allow_degraded=true`. |
| Host plugin can't `import atta` | The host uses a different Python than your venv. Point `ATTA_PYTHON` at the venv's python, or run `pip install -e .` with the host's interpreter. |
| DSH: `插件安装失败 / 这个包没有声明组合包` (`declares no dsh.bundle`) | The Git URL resolved to a commit without the root `package.json`, so DSH installed an empty placeholder. Update to a newer commit, or install `...Atta.git#path:/plugin`. |

More docs: [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) (how the layers fit together),
[`LLM_NATIVE.md`](docs/LLM_NATIVE.md) (design rules for the agent-facing side),
[`INTERFACES.md`](docs/INTERFACES.md) (internal API contract),
[`QUALITY.md`](docs/QUALITY.md) (what the quality checks look at), and
[`SHAPE_COVERAGE.md`](docs/SHAPE_COVERAGE.md) (what each recipe covers and
what's still missing).

## Repository layout

```
atta/            the Python package (numpy kernel + Blender backend + agent layer)
  families/      one auto-discovered module per shape category
  agent/         tool contracts, MCP, HTTP, manifests, host installers
  blender/       the only place that imports bpy
tests/           unit, contract and end-to-end tests
examples/        mug and bracket specs plus a batch pipeline
docs/            architecture, interfaces, LLM design notes, roadmap, coverage, quality
  assets/        logo
plugin/          DSH plugin (the package: lib, skills, bundle patch)
plugins/         ZCode plugin + local marketplace
package.json     root install shim for the DSH plugin (see DSH above)
corpus/bench/    held-out generalization cases
scripts/         maintainer tools (benchmark runner, corpus validator)
```

## Contributing

The most useful thing you can send is a bug report with a spec that reproduces
it. [CONTRIBUTING.md](CONTRIBUTING.md) covers adding recipes and feature ops,
running the tests, and code style.

MIT licensed, see [LICENSE](LICENSE).
