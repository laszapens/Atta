# -*- coding: utf-8 -*-
"""Mathematical image perception: exposure, zone system, colour, structure."""

from __future__ import annotations

import numpy as np
import pytest

from atta import vision


def _gray(value: float, size: int = 32) -> np.ndarray:
    encoded = vision.linear_to_srgb(np.array([value, value, value]))
    return np.ones((size, size, 3)) * encoded


def test_middle_gray_sits_on_zone_v():
    out = vision.analyze_array(_gray(0.18))
    assert out["ok"] is True
    assert out["zone_system"]["mean_zone"] == pytest.approx(5.0, abs=0.2)
    assert out["color"]["colorfulness"] == pytest.approx(0.0, abs=1e-4)
    assert out["luminance"]["clipping_low"] == 0.0
    assert out["luminance"]["clipping_high"] == 0.0


def test_black_white_frame_reports_clipping_and_contrast():
    img = np.zeros((32, 32, 3))
    img[:16] = 1.0
    out = vision.analyze_array(img)
    assert out["luminance"]["clipping_high"] == pytest.approx(0.5, abs=0.01)
    assert out["luminance"]["michelson"] == pytest.approx(1.0, abs=0.02)
    assert out["luminance"]["entropy_bits"] > 0.5


def test_red_is_chromatic_and_not_misread_as_clipped():
    img = np.zeros((32, 32, 3))
    img[..., 0] = 1.0
    out = vision.analyze_array(img)
    lab = out["color"]["lab_mean"]
    assert lab["a"] > 60 and lab["chroma"] > 60
    assert out["color"]["colorfulness"] > 0.5
    # A saturated red is not a luminance shadow/highlight clip.
    assert out["luminance"]["clipping_low"] == 0.0
    assert out["luminance"]["clipping_high"] == 0.0


def test_transparent_frame_is_empty():
    out = vision.analyze_array(np.zeros((16, 16, 4)))
    assert out["ok"] is False
    assert out["code"] == "empty"


def test_reference_comparison_reports_ssim_and_delta_e():
    a = np.zeros((16, 16, 4))
    a[..., 3] = 1.0
    b = a.copy()
    b[..., 0] = 0.5
    out = vision.analyze_array(a, reference=b)
    cmp = out["comparison"]
    assert cmp["ok"] is True
    assert 0.0 <= cmp["ssim"] <= 1.0
    assert cmp["delta_e76_mean"] > 1.0
