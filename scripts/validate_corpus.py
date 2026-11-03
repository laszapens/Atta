#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Atta shape-corpus JSONL against the published schema.

Usage::

    python3 scripts/validate_corpus.py corpus/normalized/*.jsonl
    python3 scripts/validate_corpus.py corpus/normalized/ --coverage

Exit codes: 0 = all records valid, 1 = schema errors, 2 = usage/schema error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

try:
    import jsonschema
except ImportError:  # pragma: no cover - tooling dependency
    jsonschema = None

ROOT = Path(__file__).resolve().parents[1]


def _paths(values):
    out = []
    for value in values:
        path = Path(value).expanduser()
        if path.is_dir():
            out.extend(sorted(path.glob("*.jsonl")))
        else:
            out.append(path)
    return out


def _records(paths):
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield path, lineno, json.loads(line)
                    except ValueError as exc:
                        yield path, lineno, {"__parse_error__": str(exc)}
        except OSError as exc:
            yield path, 0, {"__parse_error__": str(exc)}


def _accepted(rec: dict) -> bool:
    q = rec.get("quality") or {}
    return bool(q.get("license_ok")) and q.get("license_tier") in ("A", "B")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="JSONL files or directories")
    parser.add_argument("--schema", default=str(ROOT / "docs" / "shape_corpus.schema.json"))
    parser.add_argument("--matrix", default=str(ROOT / "docs" / "coverage_matrix.json"))
    parser.add_argument("--coverage", action="store_true",
                        help="print accepted counts vs coverage_matrix targets")
    args = parser.parse_args(argv)

    if jsonschema is None:
        print("jsonschema is required: pip install jsonschema", file=sys.stderr)
        return 2
    try:
        schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
        matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("cannot load schema/matrix: %s" % exc, file=sys.stderr)
        return 2

    validator = jsonschema.Draft202012Validator(schema)
    total = invalid = accepted = reference = 0
    by_cat = Counter()
    by_tier = Counter()
    by_priority = Counter()
    errors = []

    for path, lineno, rec in _records(_paths(args.paths)):
        total += 1
        if "__parse_error__" in rec:
            invalid += 1
            errors.append((path, lineno, ["invalid JSON: %s" % rec["__parse_error__"]]))
            continue
        problems = ["%s: %s" % ("/".join(map(str, err.path)), err.message)
                    for err in validator.iter_errors(rec)]
        if problems:
            invalid += 1
            errors.append((path, lineno, problems))
            continue
        category = (rec.get("shape") or {}).get("category", "unknown")
        tier = (rec.get("quality") or {}).get("license_tier", "unknown")
        priority = (rec.get("coverage") or {}).get("priority", "?")
        by_cat[category] += 1
        by_tier[tier] += 1
        by_priority[priority] += 1
        if _accepted(rec):
            accepted += 1
        else:
            reference += 1

    print("records: %d  valid: %d  invalid: %d  accepted: %d  reference-only: %d"
          % (total, total - invalid, invalid, accepted, reference))
    print("license tiers:", dict(sorted(by_tier.items())))
    print("priorities:", dict(sorted(by_priority.items())))
    print("categories:", dict(sorted(by_cat.items())))

    if args.coverage:
        print("\ncoverage vs targets (accepted):")
        targets = {c["id"]: c for c in matrix.get("categories") or []}
        for cid, target in targets.items():
            got = by_cat.get(cid, 0)
            print("  %-18s %4d / %-4d  %s"
                  % (cid, got, int(target.get("target_examples", 0)),
                     target.get("priority", "")))

    if errors:
        print("\ninvalid records:", file=sys.stderr)
        for path, lineno, problems in errors[:50]:
            print("  %s:%s" % (path, lineno), file=sys.stderr)
            for problem in problems[:8]:
                print("    - " + problem, file=sys.stderr)
        if len(errors) > 50:
            print("  ... %d more" % (len(errors) - 50), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
