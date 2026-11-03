# -*- coding: utf-8 -*-
"""Command line: the same ten verbs, for humans and for host plugins.

Examples::

    python3 -m atta doctor
    python3 -m atta --json catalog --verbose
    python3 -m atta model --recipe product.mug --quality draft
    python3 -m atta --json tool atta_verify --args-file /tmp/args.json

The ``tool`` subcommand is the bridge used by the DSH plugin; it always
returns the agent envelope ``{ok, tool, data, error, ms}``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import engine
from .errors import AttaError, fail, ok


def _read_json_or_text(text: str | None):
    """Accept a JSON value or a plain sentence (probe/objective names)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _read_json_arg(text: str | None, path: str | None, what: str):
    if path:
        try:
            return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            raise AttaError("file_not_found", "cannot read %s: %s" % (what, exc),
                            path=path, fix="check the path and JSON syntax")
        except ValueError as exc:
            raise AttaError("bad_json", "%s is not valid JSON: %s" % (what, exc),
                            path=path, fix="fix the JSON syntax")
    if text:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise AttaError("bad_json", "%s is not valid JSON: %s" % (what, exc),
                            fix="pass valid JSON or a --*-file")
    return None


def _print(data, as_json: bool):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    # Human mode is still machine-readable, but keeps the single most useful
    # line first so a terminal reader does not have to parse 100 lines.
    if isinstance(data, dict):
        for key in ("one_line", "status", "verdict", "model_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                print(value)
                break
        facts = data.get("facts")
        if isinstance(facts, dict) and facts.get("one_line"):
            print(facts["one_line"])
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_perception(data: dict) -> None:
    """Human/agent-friendly viewer: text + ASCII, no JSON wall."""
    pack = data.get("perception") or {}
    if pack.get("text"):
        print(pack["text"])
    for name, view in (pack.get("views") or {}).items():
        print("\n[%s] coverage=%.3f  # solid, . empty"
              % (name, float(view.get("coverage") or 0.0)))
        for row in view.get("ascii") or []:
            print(row)
    for sec in pack.get("sections") or []:
        print("\n[section z=%s mm, loops=%s]"
              % (sec.get("at_mm"), sec.get("n_loops")))
        for row in sec.get("ascii") or []:
            print(row)
    trace = data.get("trace") or {}
    if trace.get("text"):
        print("\n[trace] " + str(trace["text"]))
    diff = data.get("diff") or {}
    if diff.get("views"):
        print("\n[diff] volume %+.3f mm3" % float(diff.get("volume_delta_mm3") or 0.0))
        for name, row in diff["views"].items():
            print("  %s IoU=%.3f" % (name, float(row.get("iou") or 0.0)))
        for name, row in diff["views"].items():
            print("\n[diff:%s] # both, + added, - removed" % name)
            for line in row.get("ascii") or []:
                print(line)


def _add_common(p):
    p.add_argument("--home", default=None,
                   help="workspace (sets ATTA_HOME); default: $ATTA_HOME or cwd")


def build_parser():
    p = argparse.ArgumentParser(prog="atta", description="Atta v1.1 — LLM-native modeling")
    p.add_argument("--json", action="store_true", default=None,
                   help="emit the {ok,data,error} envelope")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="verb")

    def cmd(name, help_text):
        sp = sub.add_parser(name, help=help_text)
        _add_common(sp)
        return sp

    sp = cmd("doctor", "environment and capability self-check")
    sp.add_argument("--deep", action="store_true")

    sp = cmd("catalog", "recipes, features, materials, views")
    sp.add_argument("--verbose", action="store_true")

    sp = cmd("spec", "validate / propose / compile a Spec")
    sp.add_argument("--op", default="validate", choices=["validate", "propose", "compile", "schema"])
    sp.add_argument("--spec", default=None, help="Spec JSON string")
    sp.add_argument("--spec-file", default=None)
    sp.add_argument("--assume", action="store_true")

    sp = cmd("model", "build or refine a model")
    sp.add_argument("--recipe", default=None)
    sp.add_argument("--spec", default=None)
    sp.add_argument("--spec-file", default=None)
    sp.add_argument("--params", default=None)
    sp.add_argument("--params-file", default=None)
    sp.add_argument("--patches", default=None)
    sp.add_argument("--preset", default="nominal")
    sp.add_argument("--quality", default="draft")
    sp.add_argument("--name", default=None)
    sp.add_argument("--material", default=None)
    sp.add_argument("--finish", default=None)

    sp = cmd("variant", "sample or solve the parameter space")
    sp.add_argument("--recipe", default=None)
    sp.add_argument("--base", default=None)
    sp.add_argument("--count", type=int, default=6)
    sp.add_argument("--ranges", default=None)
    sp.add_argument("--ranges-file", default=None)
    sp.add_argument("--strategy", default="lhs")
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--objective", default=None)
    sp.add_argument("--constraints", default=None)
    sp.add_argument("--targets", default=None)
    sp.add_argument("--targets-file", default=None)
    sp.add_argument("--max-evaluations", type=int, default=None)
    sp.add_argument("--tolerance", type=float, default=1e-6)
    sp.add_argument("--initial", default=None)
    sp.add_argument("--initial-file", default=None)
    sp.add_argument("--pareto", action="store_true")
    sp.add_argument("--free", default=None)
    sp.add_argument("--fixed", default=None)
    sp.add_argument("--solve", action="store_true")
    sp.add_argument("--quality", default="draft")

    sp = cmd("describe", "facts, spatial probes and text perception")
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--probe", default=None)
    sp.add_argument("--wall", action="store_true")
    sp.add_argument("--curvature", action="store_true")
    sp.add_argument("--perceive", action="store_true",
                    help="return ASCII silhouettes/sections and shape traits")
    sp.add_argument("--views", default=None)
    sp.add_argument("--res", type=int, default=24)
    sp.add_argument("--sections", default=None)
    sp.add_argument("--compare-to", default=None)
    sp.add_argument("--trace", action="store_true")

    sp = cmd("perceive", "text/ASCII shape view; no render")
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--views", default=None)
    sp.add_argument("--res", type=int, default=24)
    sp.add_argument("--sections", default=None)
    sp.add_argument("--wall", action="store_true")
    sp.add_argument("--curvature", action="store_true")
    sp.add_argument("--compare-to", default=None)
    sp.add_argument("--trace", action="store_true")

    sp = cmd("verify", "three-state gate with a receipt")
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--level", default="standard", choices=["basic", "standard", "strict"])
    sp.add_argument("--constraints", default=None)
    sp.add_argument("--reference", default=None)

    sp = cmd("preview", "render judgement images")
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--views", default=None)
    sp.add_argument("--res", type=int, default=None)
    sp.add_argument("--samples", type=int, default=None)
    sp.add_argument("--quality", default=None)
    sp.add_argument("--mode", default="beauty",
                    choices=["beauty", "studio", "catalog", "dark", "clay",
                             "diagnostic"])
    sp.add_argument("--lighting", default=None,
                    choices=["studio3", "studio", "catalog", "dark", "clay", "none"])
    sp.add_argument("--background", default=None,
                    choices=["transparent", "dark", "white", "gray", "gradient"])
    sp.add_argument("--ground", action="store_true", default=None)
    sp.add_argument("--no-ground", action="store_false", dest="ground")
    sp.add_argument("--lens-mm", type=float, default=None)
    sp.add_argument("--exposure", type=float, default=None)
    sp.add_argument("--look", default=None)
    sp.add_argument("--ortho", action="store_true", default=None)
    sp.add_argument("--distance-factor", type=float, default=None)
    sp.add_argument("--materials", default=None,
                    help='JSON: "steel" or {"can":"steel","*":"plastic"}')
    sp.add_argument("--passes", default=None,
                    help="comma list: beauty,normal,depth,id,curvature,deviation")
    sp.add_argument("--reference", default=None, help="reference model id")
    sp.add_argument("--deviation-range-mm", type=float, default=None)
    sp.add_argument("--focus", default=None, help="part id to frame")
    sp.add_argument("--focus-padding", type=float, default=None)
    sp.add_argument("--aspect", default=None,
                    help="auto | square | width/height")
    sp.add_argument("--outdir", default=None)

    sp = cmd("deliver", "verify then export GLB/OBJ/STL/BLEND (+STEP subset) + manifest")
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--formats", default=None,
                    help="comma list: glb,obj,stl,blend,step")
    sp.add_argument("--outdir", default=None)
    sp.add_argument("--allow-degraded", action="store_true")
    sp.add_argument("--name", default=None)

    sp = cmd("run", "run a batch pipeline")
    sp.add_argument("--steps", default=None)
    sp.add_argument("--steps-file", default=None)
    sp.add_argument("--no-stop-on-error", action="store_true")

    sp = cmd("tool", "call one agent tool by name")
    sp.add_argument("name")
    sp.add_argument("--args", default=None)
    sp.add_argument("--args-file", default=None)

    return p


def _dispatch(args, as_json: bool) -> int:
    if args.version and not args.verb:
        from . import __version__
        print(__version__)
        return 0
    if not args.verb:
        build_parser().print_help()
        return 2
    if args.home:
        os.environ["ATTA_HOME"] = str(Path(args.home).expanduser().resolve())

    if args.verb == "tool":
        from .agent.tools import call_tool
        tool_args = _read_json_arg(args.args, args.args_file, "tool args") or {}
        result = call_tool(args.name, tool_args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    try:
        if args.verb == "doctor":
            data = engine.doctor(deep=args.deep)
        elif args.verb == "catalog":
            data = engine.catalog(verbose=args.verbose)
        elif args.verb == "spec":
            spec = _read_json_arg(args.spec, args.spec_file, "spec")
            data = engine.spec(spec=spec, op=args.op, assume=args.assume)
        elif args.verb == "model":
            data = engine.model(
                recipe=args.recipe,
                spec=_read_json_arg(args.spec, args.spec_file, "spec"),
                params=_read_json_arg(args.params, args.params_file, "params"),
                patches=_read_json_arg(args.patches, None, "patches"),
                preset=args.preset,
                quality=args.quality,
                name=args.name,
                material=args.material,
                finish=_read_json_arg(args.finish, None, "finish"),
            )
        elif args.verb == "variant":
            data = engine.variant(
                recipe=args.recipe, base=args.base, count=args.count,
                ranges=_read_json_arg(args.ranges, args.ranges_file, "ranges"),
                strategy=args.strategy, seed=args.seed,
                objective=_read_json_or_text(args.objective),
                constraints=_read_json_arg(args.constraints, None, "constraints"),
                quality=args.quality,
                targets=_read_json_arg(args.targets, args.targets_file, "targets"),
                max_evaluations=args.max_evaluations,
                tolerance=args.tolerance,
                initial=_read_json_arg(args.initial, args.initial_file, "initial"),
                pareto=args.pareto,
                free=args.free,
                fixed=args.fixed,
                solve=args.solve,
            )
        elif args.verb == "describe":
            views = ([v.strip() for v in args.views.split(",") if v.strip()]
                     if args.views else None)
            data = engine.describe(
                model_id=args.model_id, probe=_read_json_or_text(args.probe),
                wall=args.wall, curvature=args.curvature,
                perceive=args.perceive, views=views, res=args.res,
                sections=_read_json_or_text(args.sections),
                compare_to=args.compare_to, trace=args.trace,
            )
        elif args.verb == "perceive":
            views = ([v.strip() for v in args.views.split(",") if v.strip()]
                     if args.views else None)
            data = engine.describe(
                model_id=args.model_id, perceive=True,
                wall=args.wall, curvature=args.curvature,
                views=views, res=args.res,
                sections=_read_json_or_text(args.sections),
                compare_to=args.compare_to, trace=args.trace,
            )
        elif args.verb == "verify":
            data = engine.verify(
                model_id=args.model_id, level=args.level,
                constraints=_read_json_arg(args.constraints, None, "constraints"),
                reference=args.reference,
            )
        elif args.verb == "preview":
            views = [v.strip() for v in args.views.split(",") if v.strip()] if args.views else None
            passes = ([p.strip() for p in args.passes.split(",") if p.strip()]
                      if args.passes else None)
            materials = None
            if args.materials:
                try:
                    materials = json.loads(args.materials)
                except ValueError:
                    materials = args.materials
            data = engine.preview(
                model_id=args.model_id, views=views, res=args.res,
                samples=args.samples, quality=args.quality, mode=args.mode,
                outdir=args.outdir, lighting=args.lighting,
                background=args.background, ground=args.ground,
                lens_mm=args.lens_mm, exposure=args.exposure, look=args.look,
                materials=materials, passes=passes, ortho=args.ortho,
                distance_factor=args.distance_factor, reference=args.reference,
                deviation_range_mm=args.deviation_range_mm,
                focus=args.focus,
                focus_padding=args.focus_padding,
                aspect=(args.aspect if args.aspect in (None, "auto", "square")
                        else float(args.aspect)),
            )
        elif args.verb == "deliver":
            formats = [f.strip() for f in args.formats.split(",") if f.strip()] if args.formats else None
            data = engine.deliver(
                model_id=args.model_id, formats=formats, outdir=args.outdir,
                allow_degraded=args.allow_degraded, name=args.name,
            )
        elif args.verb == "run":
            steps = _read_json_arg(args.steps, args.steps_file, "steps")
            data = engine.run(steps=steps, stop_on_error=not args.no_stop_on_error)
        else:  # pragma: no cover - argparse prevents this
            raise AttaError("bad_arguments", "unknown verb %r" % args.verb,
                            fix="run atta --help")
    except AttaError as exc:
        result = fail(exc)
        _print(result, True)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI must not dump a raw traceback
        result = fail({"code": type(exc).__name__, "message": str(exc),
                       "fix": "run atta --json doctor; this is an engine bug"})
        _print(result, True)
        return 1

    viewer = (not as_json and isinstance(data, dict)
              and data.get("perception")
              and (args.verb == "perceive"
                   or bool(getattr(args, "perceive", False))))
    if viewer:
        _print_perception(data)
    else:
        _print(ok(data) if as_json else data, as_json)
    if isinstance(data, dict) and data.get("status") in ("blocked", "failed"):
        return 1
    if isinstance(data, dict) and data.get("verdict") == "fail":
        return 1
    return 0


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw
    raw = [a for a in raw if a != "--json"]
    parser = build_parser()
    args = parser.parse_args(raw)
    return _dispatch(args, as_json)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
