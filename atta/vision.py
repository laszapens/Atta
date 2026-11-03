# -*- coding: utf-8 -*-
"""Mathematical perception of a rendered image — no Blender, no verdicts.

A preview image is a *judgement* artifact.  This module measures it so the
judgement has numbers behind it: exposure placement (Ansel Adams' Zone System),
linear and perceptual lightness, clipping, contrast, entropy, colourfulness,
CIELAB chroma/hue, a crude illuminant estimate, edge density and a key-to-fill
ratio.  It never returns ``pass``/``fail``: an image is not a measurement of the
part, and lighting/material/tone-mapping are part of the uncertainty budget
(ISO 14253-1 / JCGM 106 decision-rule reasoning).

Everything is sRGB display-referred on input and linear-light internally.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

__all__ = ["analyze_image", "analyze_array", "srgb_to_linear",
           "linear_to_srgb", "srgb_to_lab", "srgb_to_oklab",
           "colorfulness", "zone_histogram", "compare_images"]


def _backdrop_mask(rgb, threshold: float = 0.12, max_colors: int = 8):
    """Mask pixels that differ from the border colour vocabulary.

    A single median fails on gradients and ground planes; quantizing the border
    into a small palette keeps sky/floor/backdrop without a semantic segmenter.
    """
    border = np.concatenate([rgb[0, :, :], rgb[-1, :, :],
                             rgb[:, 0, :], rgb[:, -1, :]], axis=0)
    quant = np.round(border / 0.05) * 0.05
    colors, counts = np.unique(quant, axis=0, return_counts=True)
    keep = colors[np.argsort(-counts)[:max_colors]]
    dist = np.full(rgb.shape[:2], np.inf)
    for color in keep:
        dist = np.minimum(dist, np.linalg.norm(rgb - color[None, None, :], axis=2))
    return dist > float(threshold)


def _as_rgb(image, alpha_threshold: float = 0.5):
    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError("image must be HxWx3 or HxWx4")
    rgb = arr[:, :, :3].astype(np.float64)
    if rgb.max(initial=0.0) > 1.5:
        rgb /= 255.0
    rgb = np.clip(rgb, 0.0, 1.0)
    mask = None
    if arr.shape[2] >= 4:
        alpha = arr[:, :, 3].astype(np.float64)
        if alpha.max(initial=0.0) > 1.5:
            alpha /= 255.0
        mask = alpha > float(alpha_threshold)
    if mask is None or mask.mean() > 0.98:
        # Opaque studio/catalogue renders: estimate the backdrop from the
        # border and keep pixels that differ from it.  This is gray-world
        # segmentation, not a semantic mask; it is only used for statistics.
        border_mask = _backdrop_mask(rgb)
        if border_mask.mean() > 0.01:
            mask = border_mask
        elif mask is None:
            mask = np.ones(rgb.shape[:2], dtype=bool)
    return rgb, mask


def srgb_to_linear(rgb):
    rgb = np.asarray(rgb, dtype=np.float64)
    return np.where(rgb <= 0.04045, rgb / 12.92,
                    ((rgb + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(lin):
    lin = np.asarray(lin, dtype=np.float64)
    return np.where(lin <= 0.0031308, lin * 12.92,
                    1.055 * np.power(np.maximum(lin, 0.0), 1.0 / 2.4) - 0.055)


def _xyz(rgb_linear):
    r, g, b = rgb_linear[..., 0], rgb_linear[..., 1], rgb_linear[..., 2]
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    return np.stack([x, y, z], axis=-1)


def srgb_to_lab(rgb):
    """sRGB (0..1 display values) → CIELAB D65."""
    xyz = _xyz(srgb_to_linear(np.clip(rgb, 0.0, 1.0)))
    white = np.array([0.95047, 1.00000, 1.08883])
    t = xyz / white
    delta = 6.0 / 29.0
    f = np.where(t > delta ** 3, np.cbrt(t),
                 t / (3.0 * delta ** 2) + 4.0 / 29.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def srgb_to_oklab(rgb):
    """sRGB (0..1) → OKLab (Ottosson 2020)."""
    lin = srgb_to_linear(np.clip(rgb, 0.0, 1.0))
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b2 = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return np.stack([L, a, b2], axis=-1)


def zone_histogram(luminance, bins: int = 11):
    """Adams zones 0..X: 18% linear middle gray is zone V; one zone = 1 stop."""
    y = np.asarray(luminance, dtype=np.float64)
    safe = np.maximum(y, 1e-8)
    z = 5.0 + np.log2(safe / 0.18)
    z = np.clip(z, 0.0, 10.0)
    hist, _ = np.histogram(z, bins=bins, range=(0.0, 10.0))
    total = max(1, hist.sum())
    return {
        "histogram": [round(float(v) / total, 6) for v in hist],
        "mean_zone": round(float(z.mean()), 4) if z.size else 0.0,
        "p05_zone": round(float(np.percentile(z, 5)), 4) if z.size else 0.0,
        "p95_zone": round(float(np.percentile(z, 95)), 4) if z.size else 0.0,
        "clipped_low": round(float(np.mean(z <= 0.05)), 6) if z.size else 0.0,
        "clipped_high": round(float(np.mean(z >= 9.95)), 6) if z.size else 0.0,
    }


def colorfulness(rgb_linear):
    """Hasler–Süsstrunk (2003) on opponent channels; neutral gray ≈ 0."""
    r = rgb_linear[..., 0]
    g = rgb_linear[..., 1]
    b = rgb_linear[..., 2]
    rg = r - g
    yb = 0.5 * (r + g) - b
    std = math.sqrt(float(np.var(rg)) + float(np.var(yb)))
    mean = math.sqrt(float(np.mean(rg)) ** 2 + float(np.mean(yb)) ** 2)
    return 0.3 * std + 0.7 * mean if std or mean else 0.0


def _sobel(lum):
    # 3x3 Sobel, reflect-padded; returns gradient magnitude in display units.
    p = np.pad(lum, 1, mode="reflect")
    gx = (p[:-2, 2:] + 2.0 * p[1:-1, 2:] + p[2:, 2:]
          - p[:-2, :-2] - 2.0 * p[1:-1, :-2] - p[2:, :-2])
    gy = (p[2:, :-2] + 2.0 * p[2:, 1:-1] + p[2:, 2:]
          - p[:-2, :-2] - 2.0 * p[:-2, 1:-1] - p[:-2, 2:])
    return np.sqrt(gx * gx + gy * gy) / 8.0


def _round(value, digits=6):
    if value is None:
        return None
    if isinstance(value, (np.floating, float)):
        if not math.isfinite(float(value)):
            return None
        return round(float(value), digits)
    return value


def analyze_array(image, *, reference=None, edge_threshold: float = 0.06) -> dict:
    """Measure exposure, colour and lighting of one image (sRGB array)."""
    rgb, mask = _as_rgb(image)
    if not mask.any():
        return {"ok": False, "code": "empty", "warnings": ["empty frame"]}
    lin = srgb_to_linear(rgb)
    lum = 0.2126729 * lin[..., 0] + 0.7151522 * lin[..., 1] + 0.0721750 * lin[..., 2]
    subject = np.clip(lum[mask], 0.0, 1.0)
    srgb_subject = np.clip(rgb[mask], 0.0, 1.0)
    lab = srgb_to_lab(rgb[mask])
    oklab = srgb_to_oklab(rgb[mask])
    lab_chroma = np.sqrt(lab[:, 1] ** 2 + lab[:, 2] ** 2)
    lab_hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1]))

    p = {q: _round(np.percentile(subject, q)) for q in (1, 5, 25, 50, 75, 90, 95, 99)}
    p["0"] = _round(subject.min())
    p["100"] = _round(subject.max())
    p95, p05 = float(np.percentile(subject, 95)), float(np.percentile(subject, 5))
    michelson = (p95 - p05) / (p95 + p05) if (p95 + p05) > 0 else 0.0
    rms = float(np.std(subject))
    mean = float(np.mean(subject))

    # sRGB code clipping is about display, so measure it before linearization.
    srgb_lum = (0.2126 * srgb_subject[:, 0] + 0.7152 * srgb_subject[:, 1]
                + 0.0722 * srgb_subject[:, 2])
    clip_low = float(np.mean(srgb_lum <= 1.0 / 255.0))
    clip_high = float(np.mean(srgb_lum >= 254.0 / 255.0))

    hist, _ = np.histogram(subject, bins=64, range=(0.0, 1.0))
    probs = hist.astype(np.float64) / max(1, hist.sum())
    probs = probs[probs > 0]
    entropy = float(-(probs * np.log2(probs)).sum())

    zones = zone_histogram(subject)
    zones["clipped_low"] = _round(clip_low)
    zones["clipped_high"] = _round(clip_high)
    edges = _sobel(rgb.mean(axis=2))
    edge_density = float(np.mean(edges[mask] > float(edge_threshold)))
    color = colorfulness(srgb_to_linear(rgb)[mask])

    # Dominant image-plane shading gradient: a crude key-light direction.
    gy, gx = np.gradient(rgb.mean(axis=2))
    w = np.sqrt(gx * gx + gy * gy)
    valid = mask & np.isfinite(w)
    if valid.any() and float(w[valid].sum()) > 1e-9:
        vx = float(np.sum(gx[valid]))
        vy = float(np.sum(gy[valid]))
        norm = math.hypot(vx, vy)
        direction = math.degrees(math.atan2(-vy, vx)) % 360.0
        coherence = norm / max(float(w[valid].sum()), 1e-12)
    else:
        direction, coherence = 0.0, 0.0

    # Crude illuminant estimate: gray-world gains on the linear subject.
    lin_subject = srgb_to_linear(srgb_subject)
    means = np.maximum(lin_subject.mean(axis=0), 1e-9)
    gray = float(np.mean(means))
    gray_world = [round(float(gray / means[i]), 6) for i in range(3)]

    # Key/fill is a ratio of stops, not an absolute light measurement.
    key = float(np.percentile(subject, 90))
    fill = float(np.percentile(subject, 25))
    key_fill = (key + 1e-9) / (fill + 1e-9)
    key_fill_stops = math.log2(key_fill) if key_fill > 0 else 0.0

    bg_mean = None
    if (~mask).any():
        bg_lum = lum[~mask]
        bg_mean = _round(float(np.mean(bg_lum)))
    bg_contrast = None
    if bg_mean is not None:
        bg_contrast = _round(abs(mean - bg_mean))

    warnings = []
    if clip_high > 0.02:
        warnings.append("highlight clipping > 2% (lower exposure or key)")
    if clip_low > 0.02:
        warnings.append("shadow clipping > 2% (raise fill or exposure)")
    if mean < 0.02:
        warnings.append("subject is very dark (median linear < 0.02)")
    if mean > 0.90:
        warnings.append("subject is very bright (median linear > 0.90)")
    if rms < 0.03:
        warnings.append("low RMS contrast (< 0.03)")
    if color > 0.12:
        warnings.append("high colourfulness; check material/white balance")
    if edge_density < 0.005:
        warnings.append("almost no edges; check focus/framing")

    result = {
        "ok": True,
        "method": "atta.vision/v1",
        "space": "sRGB display → linear → CIELAB/OKLab D65",
        "subject_pixels": int(mask.sum()),
        "subject_fraction": _round(float(mask.mean())),
        "luminance": {
            "mean": _round(mean),
            "rms_contrast": _round(rms),
            "michelson": _round(michelson),
            "percentiles": p,
            "dynamic_range_stops": _round(
                math.log2(max(p95, srgb_to_linear(1.0 / 255.0))
                          / max(p05, srgb_to_linear(1.0 / 255.0)))),
            "clipping_low": _round(clip_low),
            "clipping_high": _round(clip_high),
            "entropy_bits": _round(entropy),
        },
        "zone_system": zones,
        "color": {
            "colorfulness": _round(color),
            "lab_mean": {
                "L": _round(float(lab[:, 0].mean())),
                "a": _round(float(lab[:, 1].mean())),
                "b": _round(float(lab[:, 2].mean())),
                "chroma": _round(float(lab_chroma.mean())),
                "hue_deg": _round(float(lab_hue.mean())),
            },
            "oklab_mean": {
                "L": _round(float(oklab[:, 0].mean())),
                "a": _round(float(oklab[:, 1].mean())),
                "b": _round(float(oklab[:, 2].mean())),
            },
            "gray_world_gains": gray_world,
        },
        "light": {
            "key_fill_ratio": _round(key_fill),
            "key_fill_stops": _round(key_fill_stops),
            "gradient_direction_deg": _round(direction, 2),
            "gradient_coherence": _round(coherence),
            "background_mean": bg_mean,
            "subject_background_delta": bg_contrast,
            "edge_density": _round(edge_density),
        },
        "warnings": warnings,
        "note": ("image statistics flag and rank; they are not a geometry "
                 "verdict. Lighting, material and tone mapping are not "
                 "traceable measurements."),
    }
    if reference is not None:
        ref_rgb, ref_mask = _as_rgb(reference)
        if ref_rgb.shape == rgb.shape:
            result["comparison"] = _compare_arrays(rgb, ref_rgb, mask & ref_mask)
        else:
            result["comparison"] = {"ok": False,
                                    "code": "size_mismatch",
                                    "shape": list(rgb.shape),
                                    "reference_shape": list(ref_rgb.shape)}
    return result


def _ssim(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    mu_a, mu_b = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = float(np.mean((a - mu_a) * (b - mu_b)))
    c1, c2 = (0.01 ** 2), (0.03 ** 2)
    num = (2 * mu_a * mu_b + c1) * (2 * cov + c2)
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2)
    return float(num / den) if den else 0.0


def _compare_arrays(a, b, mask):
    if not mask.any():
        return {"ok": False, "code": "empty_mask"}
    la = srgb_to_lab(a[mask])
    lb = srgb_to_lab(b[mask])
    dE = np.sqrt(np.sum((la - lb) ** 2, axis=1))
    return {
        "ok": True,
        "method": "global SSIM + mean CIE76 ΔE (approx; use ΔE00 for tolerance)",
        "ssim": _round(_ssim(a.mean(axis=2)[mask], b.mean(axis=2)[mask])),
        "delta_e76_mean": _round(float(dE.mean())),
        "delta_e76_p95": _round(float(np.percentile(dE, 95))),
        "delta_e76_max": _round(float(dE.max())),
    }


def analyze_image(path, *, reference=None, edge_threshold: float = 0.06) -> dict:
    """Load a PNG/JPG and analyze it.  Returns a JSON-able dict."""
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - Pillow is a normal dep here
        raise RuntimeError("Pillow is required for image analysis: %s" % exc) from exc
    with Image.open(Path(path)) as im:
        arr = np.asarray(im.convert("RGBA"), dtype=np.float64) / 255.0
    ref = None
    if reference is not None:
        with Image.open(Path(reference)) as im:
            ref = np.asarray(im.convert("RGBA"), dtype=np.float64) / 255.0
    out = analyze_array(arr, reference=ref, edge_threshold=edge_threshold)
    out["path"] = str(path)
    return out


def compare_images(path_a, path_b):
    """Convenience: compare two image files (same size)."""
    from PIL import Image
    with Image.open(Path(path_a)) as im:
        a = np.asarray(im.convert("RGBA"), dtype=np.float64) / 255.0
    with Image.open(Path(path_b)) as im:
        b = np.asarray(im.convert("RGBA"), dtype=np.float64) / 255.0
    if a.shape != b.shape:
        return {"ok": False, "code": "size_mismatch",
                "a": list(a.shape), "b": list(b.shape)}
    mask = (a[..., 3] > 0.5) & (b[..., 3] > 0.5)
    return _compare_arrays(a, b, mask)
