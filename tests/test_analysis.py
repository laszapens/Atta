# -*- coding: utf-8 -*-
"""Analysis-layer tests: metrology, probes, facts, and the verify gate."""

from __future__ import annotations

import numpy as np
import pytest

from atta import facts, metrology, probe, verify
from atta.mesh import Mesh, concat, topology


def cube(size: float = 100.0, center=(0.0, 0.0, 0.0)) -> Mesh:
    """Outward-wound closed box; ``size`` and ``center`` are mm."""
    s = size / 1000.0
    cx, cy, cz = (float(x) / 1000.0 for x in center)
    x0, x1 = cx - s / 2, cx + s / 2
    y0, y1 = cy - s / 2, cy + s / 2
    z0, z1 = cz - s / 2, cz + s / 2
    v = np.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ], dtype=np.float64)
    f = np.array([
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ], dtype=np.int64)
    return Mesh(v, f)


def two_boxes(gap_mm: float = 0.0) -> Mesh:
    """Two 100 mm cubes separated by ``gap_mm`` along X."""
    step = 100.0 + float(gap_mm)
    return concat([cube(100.0, (-step / 2, 0, 0)),
                   cube(100.0, (step / 2, 0, 0))])


# ---------------------------------------------------------------- measures

def test_topology_and_measures():
    m = cube(100.0)
    topo = topology(m)
    assert topo["n_tris"] == 12
    assert topo["closed"] is True
    assert topo["n_components"] == 1
    assert topo["genus"] == 0
    assert topo["non_manifold_edges"] == 0
    assert topo["inconsistent_winding_edges"] == 0

    assert metrology.volume_mm3(m) == pytest.approx(1_000_000.0)
    assert metrology.area_mm2(m) == pytest.approx(60_000.0)
    assert metrology.bbox_mm(m)["size"] == pytest.approx([100.0, 100.0, 100.0])
    assert metrology.centroid_mm(m) == pytest.approx([0.0, 0.0, 0.0], abs=1e-9)

    mass = metrology.mass_properties(m, density_kg_m3=1000.0)
    assert mass["mass_kg"] == pytest.approx(1.0)
    assert mass["volume_mm3"] == pytest.approx(1_000_000.0)


def test_two_box_topology():
    m = two_boxes()
    topo = topology(m)
    assert topo["n_components"] == 2
    assert topo["closed"] is True
    assert topo["genus"] == 0
    assert metrology.volume_mm3(m) == pytest.approx(2_000_000.0)
    assert metrology.area_mm2(m) == pytest.approx(120_000.0)


def test_wall_dihedral_curvature():
    m = cube(100.0)
    wall = metrology.wall_thickness(m)
    assert wall["exact"] is False
    assert wall["n_hits"] == wall["n_samples"]
    assert wall["min_mm"] == pytest.approx(100.0, abs=0.01)
    for key in ("p05_mm", "p50_mm", "p95_mm"):
        assert wall[key] == pytest.approx(100.0, abs=0.01)

    dih = metrology.dihedral(m)
    assert dih["max_deg"] == pytest.approx(90.0)
    assert dih["p95_deg"] == pytest.approx(90.0)
    assert dih["count_gt_30"] == 12
    assert dih["count_gt_60"] == 12
    assert dih["exact"] is False

    cur = metrology.curvature(m)
    assert cur["n_edges"] > 0
    assert 0.0 < cur["p50_deg"] <= cur["p95_deg"] <= cur["max_deg"]
    assert cur["exact"] is False

    measured = metrology.measure(m, wall=True, curvature=True)
    for key in ("bbox_mm", "volume_mm3", "area_mm2", "centroid_mm",
                "topology", "wall", "dihedral", "curvature"):
        assert key in measured
    assert measured["units"] == "mm"
    assert measured["exact"] is False


# ---------------------------------------------------------------- probes

def test_raycast_grid_shape_and_axes():
    m = cube(100.0)
    g = probe.raycast_grid(m, axis="z", res=(8, 6))
    assert len(g["ascii"]) == 6
    assert all(len(row) == 8 for row in g["ascii"])
    assert len(g["depth_mm"]) == 6
    assert all(len(row) == 8 for row in g["depth_mm"])
    assert len(g["hit_mask"]) == 6
    assert all(len(row) == 8 for row in g["hit_mask"])
    assert g["stats"]["rays"] == 48
    assert g["stats"]["hits"] == 48
    assert g["stats"]["coverage"] == pytest.approx(1.0)
    assert g["exact"] is False

    for axis in ("+x", "-x", "+y", "-y", "+z", "-z"):
        got = probe.raycast_grid(m, axis=axis, res=(4, 4))
        assert got["stats"]["hits"] == 16
        assert got["direction"] in (-1, 1)


def test_silhouette_shape_and_coverage():
    m = cube(100.0)
    sil = probe.silhouette(m, views=("front", "top", "left", "iso"), res=16)
    assert set(sil["views"]) == {"front", "top", "left", "iso"}
    for view in sil["views"].values():
        assert len(view["ascii"]) == 16
        assert all(len(row) == 16 for row in view["ascii"])
        assert len(view["bitmap"]) == 16
        assert all(len(row) == 16 for row in view["bitmap"])
        assert 0.0 < view["coverage"] <= 1.0
    assert sil["views"]["front"]["coverage"] == pytest.approx(1.0)
    assert sil["views"]["iso"]["coverage"] < 1.0


def test_section_contour():
    m = cube(100.0)
    sec = probe.section(m, plane="z", offset_mm=0.0, res=32)
    assert sec["n_loops"] == 1
    loop = sec["loops"][0]
    assert loop["closed"] is True
    assert loop["area_mm2"] == pytest.approx(10_000.0, rel=0.02)
    assert loop["perimeter_mm"] == pytest.approx(400.0, rel=0.02)
    assert all(len(p) == 2 for p in loop["points_mm"])
    assert sec["exact"] is False


def test_clearance_interference_nearest_inside():
    a = cube(100.0, (-55, 0, 0))
    b = cube(100.0, (55, 0, 0))
    clr = probe.clearance(a, b)
    assert clr["min_mm"] == pytest.approx(10.0, abs=1e-3)
    assert clr["distance_mm"] == clr["min_mm"]
    assert clr["exact"] is False

    o1 = cube(100.0, (-25, 0, 0))
    o2 = cube(100.0, (25, 0, 0))
    inter = probe.interference(o1, o2)
    assert inter["intersects"] is True
    assert inter["volume_mm3"] == pytest.approx(500_000.0, rel=0.02)

    far = probe.interference(a, b)
    assert far["intersects"] is False
    assert far["volume_mm3"] == pytest.approx(0.0, abs=1e-3)

    n = probe.nearest(cube(100.0), (0.0, 0.0, 200.0))
    assert n["distance_mm"] == pytest.approx(150.0, abs=1e-3)
    assert n["point_mm"] == pytest.approx([0.0, 0.0, 50.0], abs=1e-3)

    m = cube(100.0)
    assert probe.inside(m, (0.0, 0.0, 0.0)) is True
    assert probe.inside(m, (200.0, 0.0, 0.0)) is False


# ---------------------------------------------------------------- facts

def test_facts_card():
    m = cube(100.0)
    f = facts.facts(m, {"name": "cube", "material": "steel"}, wall=True)
    for key in ("closed", "genus", "components", "bbox_mm", "volume_mm3",
                "area_mm2", "wall", "dihedral", "material", "units", "exact",
                "one_line"):
        assert key in f
    assert f["closed"] is True
    assert f["genus"] == 0
    assert f["components"] == 1
    assert f["material"] == "steel"
    assert f["units"] == "mm"
    assert f["exact"] is False
    assert f["wall"]["min_mm"] == pytest.approx(100.0, abs=0.01)
    assert isinstance(f["one_line"], str)
    assert 0 < len(f["one_line"]) < 200
    assert "cube" in f["one_line"]

    plain = facts.facts(m)
    assert plain["wall"] is None
    assert len(plain["one_line"]) < 200


# ---------------------------------------------------------------- verify

def test_verify_pass_degraded_fail():
    m = cube(100.0)
    passed = verify.verify(m)
    assert passed["verdict"] == "pass"
    assert passed["pass"] is True
    assert passed["score"] == pytest.approx(1.0)
    assert passed["issues"] == []
    for key in ("verdict", "score", "level", "checks", "issues", "metrics",
                "facts", "assumptions", "exact", "method", "next"):
        assert key in passed
    assert passed["method"] == "atta.verify/v3"
    assert passed["exact"] is False
    assert all("fix" in c and "ok" in c and "severity" in c
               for c in passed["checks"])

    degraded = verify.verify(two_boxes())
    assert degraded["verdict"] == "degraded"
    assert any(c["id"] == "components" and c["severity"] == "warn"
               and c["fix"] for c in degraded["issues"])

    failed = verify.verify(m, constraints={"volume_mm3": [1.0, 2.0]})
    assert failed["verdict"] == "fail"
    assert any(c["id"] == "volume_mm3" for c in failed["issues"])
    assert all(c["fix"] for c in failed["issues"])


def test_verify_constraints_have_fix():
    r = verify.verify(cube(100.0),
                      constraints={"bbox": {"x": [10.0, 20.0]},
                                   "min_wall_mm": 150.0})
    assert r["verdict"] == "fail"
    ids = {c["id"] for c in r["issues"]}
    assert "bbox.x" in ids
    assert "min_wall_mm" in ids
    for c in r["issues"]:
        assert c["fix"]


def test_verify_pair_constraints():
    a = cube(100.0, (-55, 0, 0))
    b = cube(100.0, (55, 0, 0))
    parts = {"a": a, "b": b}
    bad = verify.verify(a, constraints={"clearance": [
        {"a": "a", "b": "b", "min": 20.0}]}, parts=parts)
    assert bad["verdict"] == "fail"
    assert any(c["id"] == "clearance.0" for c in bad["issues"])

    good = verify.verify(a, constraints={"clearance": [
        {"a": "a", "b": "b", "min": 5.0}]}, parts=parts)
    assert good["verdict"] == "pass"

    o1 = cube(100.0, (-25, 0, 0))
    o2 = cube(100.0, (25, 0, 0))
    hit = verify.verify(o1, constraints={"interference": [
        {"a": "a", "b": "b", "max": 0.0}]}, parts={"a": o1, "b": o2})
    assert hit["verdict"] == "fail"
    assert any(c["id"] == "interference.0" for c in hit["issues"])

    missing = verify.verify(a, constraints={"clearance": [
        {"a": "nope", "b": "b", "min": 1.0}]}, parts={"b": b})
    assert missing["verdict"] == "fail"
    assert "parts=" in missing["issues"][0]["fix"]


def test_verify_assumptions_pass_through():
    assumed = [{"name": "height", "value": 95.0, "status": "assumed"}]
    r = verify.verify(cube(100.0), assumptions=assumed)
    assert r["verdict"] == "pass"
    assert r["assumptions"] == assumed


def test_verify_render_receipt():
    m = cube(100.0)
    cons = {"render": {"min_coverage": 0.5, "require_ndc": True,
                       "res": [256, 256], "samples": 8}}
    bad = {"images": [{"view": "iso", "width": 512, "height": 512,
                       "samples": 8, "coverage": 0.0, "worst_ndc": 0.5}]}
    r = verify.verify(m, constraints=cons, render_receipt=bad)
    assert r["verdict"] == "fail"
    ids = {c["id"] for c in r["issues"]}
    assert "render.empty_frame" in ids
    assert "render.coverage" in ids

    good = {"images": [{"view": "iso", "width": 512, "height": 512,
                        "samples": 8, "coverage": 0.8, "worst_ndc": 0.5}]}
    assert verify.verify(m, constraints=cons, render_receipt=good)["verdict"] == "pass"

    missing = verify.verify(m, constraints=cons)
    assert missing["verdict"] == "degraded"
    assert any(c["id"] == "render.gate" and c["severity"] == "warn"
               for c in missing["issues"])


def test_verify_levels_and_expected_genus():
    m = cube(100.0)
    assert verify.verify(m, level="basic",
                         constraints={"bbox": {"x": [1, 2]}})["verdict"] == "pass"
    assert verify.verify(m, level="strict",
                         constraints={"min_wall_mm": 150.0})["verdict"] == "fail"
    assert verify.verify(m, expected_genus=0)["verdict"] == "pass"
    assert verify.verify(m, expected_genus=1)["verdict"] == "fail"
