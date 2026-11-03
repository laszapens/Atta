# -*- coding: utf-8 -*-
"""Geometry kernel tests: primitives, curves, surfaces, booleans and features."""

import math

import numpy as np
import pytest

from atta import curves as C
from atta import primitives as P
from atta import surfaces as S
from atta.boolean import (boolean, difference, intersection, mirror,
                          pattern_linear, pattern_polar, union)
from atta.errors import AttaError
from atta.features import FEATURE_OPS, evaluate
from atta.mesh import area, topology, volume


def _closed(mesh):
    return topology(mesh)["closed"]


def _genus(mesh):
    return topology(mesh)["genus"]


# ---------------------------------------------------------------- primitives

def test_box_is_closed_and_exact():
    m = P.box([0.1, 0.2, 0.3])
    assert _closed(m) and _genus(m) == 0
    assert volume(m) == pytest.approx(0.006, rel=1e-9)
    assert m.bbox()["size"] == pytest.approx([0.1, 0.2, 0.3])


def test_box_align_min():
    m = P.box([0.02, 0.02, 0.02], center=(0.1, 0.2, 0.3), align="min")
    assert m.bbox()["min"] == pytest.approx([0.1, 0.2, 0.3])


def test_cylinder():
    m = P.cylinder(0.03, 0.1, segments=96)
    assert _closed(m) and _genus(m) == 0
    assert volume(m) == pytest.approx(math.pi * 0.03 ** 2 * 0.1, rel=0.02)


def test_sphere():
    m = P.sphere(0.05, segments=64, rings=32)
    assert _closed(m) and _genus(m) == 0
    assert volume(m) == pytest.approx(4.0 / 3.0 * math.pi * 0.05 ** 3, rel=0.02)


def test_cone_and_apex():
    m = P.cone(0.04, 0.0, 0.1, segments=96)
    assert _closed(m) and _genus(m) == 0
    assert volume(m) == pytest.approx(math.pi * 0.1 * 0.04 ** 2 / 3.0, rel=0.02)
    frustum = P.cone(0.04, 0.02, 0.1, segments=96)
    assert _closed(frustum)
    assert volume(frustum) > volume(m)


def test_torus_is_genus_one():
    m = P.torus(0.05, 0.015, seg_major=128, seg_minor=48)
    assert _closed(m) and _genus(m) == 1
    assert volume(m) == pytest.approx(2.0 * math.pi ** 2 * 0.05 * 0.015 ** 2, rel=0.02)


def test_primitive_errors_are_actionable():
    for call in (lambda: P.sphere(-0.01), lambda: P.torus(0.01, 0.02),
                 lambda: P.cone(0.0, 0.0, 0.1), lambda: P.box([0.1, 0.0, 0.1])):
        with pytest.raises(AttaError) as ei:
            call()
        assert ei.value.code and ei.value.fix


# ---------------------------------------------------------------- curves

def test_polyline_close():
    p = C.polyline([[0, 0, 0], [0.1, 0, 0], [0.1, 0.1, 0]], closed=True)
    assert len(p) == 4
    assert np.allclose(p[0], p[-1])


def test_catmull_rom_closed_count_and_smoothness():
    pts = np.array([[0.0, 0, 0], [0.1, 0, 0], [0.1, 0.1, 0], [0.0, 0.1, 0]])
    p = C.catmull_rom(pts, samples_per_seg=8, closed=True)
    assert len(p) == len(pts) * 8
    steps = np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1)
    assert np.all(steps < 0.05)


def test_resample_arclength():
    p = C.resample_arclength([[0, 0, 0], [0.1, 0, 0], [0.1, 0.1, 0]], 11)
    assert len(p) == 11
    assert np.allclose(p[0], [0, 0, 0])
    assert np.allclose(p[-1], [0.1, 0.1, 0])
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    assert np.allclose(seg, seg[0], rtol=1e-6)


def test_parallel_frames_are_orthonormal():
    path = np.column_stack([np.linspace(0, 0.1, 9), np.zeros(9), np.zeros(9)])
    t, n, b = C.parallel_frames(path)
    assert np.allclose(np.linalg.norm(t, axis=1), 1.0)
    assert np.allclose(np.linalg.norm(n, axis=1), 1.0)
    assert np.allclose(np.sum(t * n, axis=1), 0.0, atol=1e-9)
    assert np.allclose(b, np.cross(t, n), atol=1e-9)


# ---------------------------------------------------------------- surfaces

def test_revolve_closed_tube_and_caps():
    prof = np.array([[0.04, 0.0], [0.04, 0.1], [0.03, 0.1], [0.03, 0.0], [0.04, 0.0]])
    tube = S.revolve(prof, segments=64)
    assert _closed(tube) and _genus(tube) == 1
    assert volume(tube) == pytest.approx(math.pi * (0.04 ** 2 - 0.03 ** 2) * 0.1, rel=0.02)

    capped = S.revolve([[0.02, 0.0], [0.02, 0.05]], segments=64, caps=True)
    assert _closed(capped) and _genus(capped) == 0
    assert volume(capped) == pytest.approx(math.pi * 0.02 ** 2 * 0.05, rel=0.02)


def test_loft_is_closed():
    th = np.linspace(0.0, 2.0 * math.pi, 48, endpoint=False)
    r0 = np.column_stack([0.03 * np.cos(th), 0.03 * np.sin(th), np.zeros_like(th)])
    r1 = r0.copy()
    r1[:, 2] = 0.1
    m = S.loft([r0, r1], caps=True)
    assert _closed(m) and _genus(m) == 0
    assert volume(m) == pytest.approx(math.pi * 0.03 ** 2 * 0.1, rel=0.02)


def _circle_section(radius, n=20):
    a = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return np.column_stack([radius * np.cos(a), radius * np.sin(a)])


def test_sweep_closed_loop_and_open_path():
    a = np.linspace(0.0, 2.0 * math.pi, 48, endpoint=False)
    path = np.column_stack([0.05 * np.cos(a), 0.05 * np.sin(a), np.zeros_like(a)])
    ring = S.sweep(_circle_section(0.01), path, closed=True)
    assert _closed(ring) and _genus(ring) == 1
    assert volume(ring) == pytest.approx(2.0 * math.pi ** 2 * 0.05 * 0.01 ** 2, rel=0.05)

    line = np.column_stack([np.zeros(8), np.zeros(8), np.linspace(0.0, 0.1, 8)])
    tube = S.sweep(_circle_section(0.01), line)
    assert _closed(tube) and _genus(tube) == 0
    assert volume(tube) == pytest.approx(math.pi * 0.01 ** 2 * 0.1, rel=0.03)


def test_extrude_is_closed():
    square = np.array([[-0.05, -0.03], [0.05, -0.03], [0.05, 0.03], [-0.05, 0.03]])
    m = S.extrude(square, 0.02)
    assert _closed(m) and _genus(m) == 0
    assert volume(m) == pytest.approx(0.1 * 0.06 * 0.02, rel=1e-9)


def _flat_coons_edges(half=0.1, n=16):
    x = np.linspace(-half, half, n)
    y = np.linspace(-half, half, n)
    z = np.zeros(n)
    bottom = np.column_stack([x, np.full(n, -half), z])
    right = np.column_stack([np.full(n, half), y, z])
    top = np.column_stack([x[::-1], np.full(n, half), z])
    left = np.column_stack([np.full(n, -half), y[::-1], z])
    return [bottom, right, top, left]


def test_coons_is_open_and_thicken_closes_it():
    surface = S.coons(_flat_coons_edges(), 16, 16)
    assert topology(surface)["closed"] is False
    assert area(surface) == pytest.approx(0.04, rel=1e-6)
    solid = S.thicken(surface, 0.002)
    assert _closed(solid) and _genus(solid) == 0
    assert volume(solid) == pytest.approx(0.04 * 0.002, rel=0.01)


def test_gordon_is_open_and_thicken_closes_it():
    n = 12
    xs = np.linspace(-0.1, 0.1, n)
    ys = np.linspace(-0.1, 0.1, n)
    u_curves = [np.column_stack([xs, np.full(n, y), np.zeros(n)])
                for y in (-0.1, 0.0, 0.1)]
    v_curves = [np.column_stack([np.full(n, x), ys, np.zeros(n)])
                for x in (-0.1, 0.0, 0.1)]
    surface = S.gordon(u_curves, v_curves, 16, 16)
    assert topology(surface)["closed"] is False
    assert area(surface) == pytest.approx(0.04, rel=1e-6)
    solid = S.thicken(surface, 0.002)
    assert _closed(solid) and _genus(solid) == 0
    assert volume(solid) > 0.0


def test_lobes_keep_the_solid_closed():
    base = P.cylinder(0.04, 0.1, segments=96)
    m = S.lobes(base, count=8, amplitude=0.05, twist_deg=30.0)
    assert _closed(m) and _genus(m) == 0
    assert m.hash() != base.hash()
    assert m.bbox()["size"][0] != pytest.approx(base.bbox()["size"][0], rel=1e-6)


# ---------------------------------------------------------------- booleans

def _two_boxes():
    a = P.box([0.1, 0.1, 0.1])
    b = P.box([0.1, 0.1, 0.1], center=(0.05, 0.0, 0.0))
    return a, b


def test_boolean_volumes():
    a, b = _two_boxes()
    u, d, i = union(a, b), difference(a, b), intersection(a, b)
    for m in (u, d, i):
        assert _closed(m) and _genus(m) == 0
    assert volume(u) == pytest.approx(0.0015, rel=1e-5)
    assert volume(d) == pytest.approx(0.0005, rel=1e-5)
    assert volume(i) == pytest.approx(0.0005, rel=1e-5)


def test_boolean_errors():
    a, b = _two_boxes()
    with pytest.raises(AttaError) as ei:
        boolean(a, b, "xor")
    assert ei.value.code == "bad_arg" and ei.value.path and ei.value.fix

    open_patch = S.coons(_flat_coons_edges(), 8, 8)
    with pytest.raises(AttaError) as ei:
        union(a, open_patch)
    assert ei.value.code == "boolean_input_open" and ei.value.fix


def test_mirror_and_patterns():
    m = P.box([0.02, 0.02, 0.02], center=(0.05, 0.0, 0.0))
    mirrored = mirror(m, "x")
    assert _closed(mirrored)
    assert volume(mirrored) == pytest.approx(volume(m), rel=1e-9)
    assert mirrored.bbox()["min"][0] == pytest.approx(-0.06, rel=1e-9)

    lin = pattern_linear(m, 3, 0.03)
    t = topology(lin)
    assert t["closed"] and t["n_components"] == 3
    assert volume(lin) == pytest.approx(3.0 * volume(m), rel=1e-9)

    pol = pattern_polar(m, 4)
    t = topology(pol)
    assert t["closed"] and t["n_components"] == 4
    assert volume(pol) == pytest.approx(4.0 * volume(m), rel=1e-9)


# ---------------------------------------------------------------- features

def _feature_graph():
    return [
        {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
        {"id": "hole", "op": "cylinder",
         "args": {"radius": {"$expr": "hole_d / 2"}, "height": 20,
                  "center": [0, 0, 3]}},
        {"id": "cut", "op": "difference", "inputs": ["plate", "hole"]},
    ]


def test_features_use_params_and_expressions():
    m = evaluate(_feature_graph(), params={"hole_d": 8})
    assert _closed(m) and _genus(m) == 1
    expected_mm3 = 80 * 50 * 6 - math.pi * (8 / 2.0) ** 2 * 6
    assert volume(m) * 1e9 == pytest.approx(expected_mm3, rel=0.01)

    graph = [{"id": "b", "op": "box",
              "args": {"size": [{"$param": "sx"}, {"$param": "sy"}, {"$param": "sz"}],
                       "center": [{"$expr": "sx / 2"}, 0, 0]}}]
    m = evaluate(graph, params={"sx": 10, "sy": 5, "sz": 2})
    assert m.bbox()["size"] == pytest.approx([0.01, 0.005, 0.002])
    assert m.bbox()["min"][0] == pytest.approx(0.0, abs=1e-12)


def test_features_units_argument():
    graph = [{"id": "b", "op": "box", "args": {"size": [1, 1, 1]}}]
    m = evaluate(graph, units="m")
    assert m.bbox()["size"] == pytest.approx([1.0, 1.0, 1.0])


@pytest.mark.parametrize("graph,code,output", [
    ([{"id": "a", "op": "box", "args": {"size": [1, 1, 1]}},
      {"id": "a", "op": "box", "args": {"size": [1, 1, 1]}}], "duplicate_id", None),
    ([{"id": "a", "op": "box", "args": {"size": [1, 1, 1]}},
      {"id": "b", "op": "union", "inputs": ["a", "missing"]}], "unknown_input", None),
    ([{"id": "a", "op": "union", "inputs": ["b", "c"]},
      {"id": "b", "op": "union", "inputs": ["a", "c"]},
      {"id": "c", "op": "box", "args": {"size": [1, 1, 1]}}], "cycle", "a"),
    ([{"id": "a", "op": "not_an_op", "args": {}}], "unknown_feature", None),
    ([{"id": "a", "op": "box", "args": {"sizes": [1, 1, 1]}}], "bad_arg", None),
    ([{"id": "a", "op": "box", "args": {}}], "bad_arg", None),
])
def test_features_error_paths(graph, code, output):
    with pytest.raises(AttaError) as ei:
        evaluate(graph, output=output)
    assert ei.value.code == code
    assert ei.value.path and ei.value.fix


def test_feature_ops_schema():
    assert len(FEATURE_OPS) >= 15
    for op, spec in FEATURE_OPS.items():
        assert "required" in spec and "optional" in spec and spec["doc"], op


def test_extrude_with_inner_loop_makes_a_through_hole():
    m = evaluate([{
        "id": "ring", "op": "extrude",
        "args": {
            "loop_xy": [[-10, -10], [10, -10], [10, 10], [-10, 10]],
            "holes_xy": [[[-4, -4], [4, -4], [4, 4], [-4, 4]]],
            "height": 5,
        },
    }])
    topo = m.topology()
    assert topo["closed"] is True
    assert topo["genus"] == 1
    assert m.volume() * 1e9 == pytest.approx(1680.0, rel=1e-3)


def test_gyroid_is_a_composable_feature_op():
    assert "gyroid" in FEATURE_OPS
    m = evaluate([{
        "id": "g", "op": "gyroid",
        "args": {"size": [30, 20, 12], "cell_size": 10,
                 "thickness": 1.2, "resolution": 10},
    }])
    topo = m.topology()
    assert topo["closed"] is True
    assert topo["n_components"] == 1
    assert topo["genus"] and topo["genus"] > 0
    assert m.volume() * 1e9 > 100.0
