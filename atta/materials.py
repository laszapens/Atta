# -*- coding: utf-8 -*-
"""Declarative material presets — pure data, no bpy.

The host owns the vocabulary; the Blender worker owns the node graph.  A
finish references a preset by name, :func:`resolve` expands it into a flat
parameter dict, and the worker writes exactly those values into a Principled
BSDF.  One source of truth, no preset table duplicated inside Blender.

Colors are linear RGB in 0..1.  ``emission_strength`` is a multiplier, not
watts; the worker maps it onto Principled's Emission Strength.
"""

from __future__ import annotations

import copy

__all__ = [
    "BASE", "PRESETS", "DEFAULT", "get", "names", "catalog", "resolve",
    "validate", "is_preset",
]

DEFAULT = "ceramic"

#: Every preset is this dict with per-material overrides applied.
BASE = {
    "base_color": (0.62, 0.62, 0.62),
    "metallic": 0.0,
    "roughness": 0.42,
    "ior": 1.50,
    "alpha": 1.0,
    "transmission": 0.0,
    "coat_weight": 0.0,
    "coat_roughness": 0.08,
    "sheen_weight": 0.0,
    "sheen_roughness": 0.30,
    "anisotropic": 0.0,
    "subsurface_weight": 0.0,
    "emission_color": (0.0, 0.0, 0.0),
    "emission_strength": 0.0,
}

#: ``label``/``category`` are display metadata; everything else is a BSDF key.
PRESETS: dict[str, dict] = {
    "ceramic": {
        "label": "月白陶瓷",
        "category": "ceramic",
        "base_color": (0.66, 0.62, 0.53),
        "roughness": 0.40,
        "ior": 1.50,
        "sheen_weight": 0.20,
        "sheen_roughness": 0.30,
        "coat_weight": 0.10,
    },
    "quartz": {
        "label": "白石英",
        "category": "stone",
        "base_color": (0.55, 0.57, 0.59),
        "roughness": 0.32,
        "ior": 1.54,
        "coat_weight": 0.45,
        "coat_roughness": 0.05,
    },
    "glass_frosted": {
        "label": "磨砂玻璃",
        "category": "glass",
        "base_color": (0.010, 0.020, 0.030),
        "roughness": 0.18,
        "ior": 1.45,
        "transmission": 0.92,
        "coat_weight": 0.15,
    },
    "glass_clear": {
        "label": "清玻璃",
        "category": "glass",
        "base_color": (0.006, 0.012, 0.018),
        "roughness": 0.03,
        "ior": 1.49,
        "transmission": 0.98,
    },
    "brushed_steel": {
        "label": "拉丝钢",
        "category": "metal",
        "base_color": (0.36, 0.38, 0.40),
        "metallic": 1.0,
        "roughness": 0.24,
        "anisotropic": 0.45,
        "coat_weight": 0.10,
    },
    "powder_blue": {
        "label": "粉末蓝",
        "category": "painted",
        "base_color": (0.075, 0.145, 0.260),
        "roughness": 0.55,
        "coat_weight": 0.08,
    },
    "gold": {
        "label": "熔金",
        "category": "metal",
        "base_color": (0.830, 0.620, 0.240),
        "metallic": 1.0,
        "roughness": 0.16,
        "anisotropic": 0.30,
        "coat_weight": 0.18,
    },
    "dark": {
        "label": "暗陶瓷",
        "category": "ceramic",
        "base_color": (0.012, 0.014, 0.018),
        "metallic": 0.55,
        "roughness": 0.28,
        "coat_weight": 0.15,
    },
    "clay": {
        "label": "陶土",
        "category": "ceramic",
        "base_color": (0.42, 0.21, 0.14),
        "roughness": 0.82,
        "ior": 1.47,
        "subsurface_weight": 0.05,
    },
    "resin": {
        "label": "琥珀树脂",
        "category": "polymer",
        "base_color": (0.72, 0.40, 0.10),
        "roughness": 0.14,
        "ior": 1.52,
        "transmission": 0.60,
        "coat_weight": 0.30,
        "coat_roughness": 0.04,
    },
    "emissive": {
        "label": "发光体",
        "category": "emissive",
        "base_color": (0.02, 0.02, 0.02),
        "roughness": 0.35,
        "emission_color": (1.00, 0.72, 0.35),
        "emission_strength": 6.0,
    },
    "matte_white": {
        "label": "哑光白",
        "category": "painted",
        "base_color": (0.82, 0.82, 0.80),
        "roughness": 0.72,
    },
}

# Declarative aliases kept for spec/recipe vocabulary.  They are full presets,
# not magic: every name an agent can put in a spec must resolve here.
PRESETS.update({
    "porcelain": {
        "label": "白瓷", "category": "ceramic",
        "base_color": (0.86, 0.85, 0.81), "roughness": 0.30,
        "ior": 1.52, "coat_weight": 0.22, "coat_roughness": 0.04,
    },
    "glass": {
        "label": "透明玻璃", "category": "glass",
        "base_color": (0.02, 0.03, 0.04), "roughness": 0.06,
        "ior": 1.46, "transmission": 0.98, "coat_weight": 0.10,
    },
    "steel": {
        "label": "钢", "category": "metal",
        "base_color": (0.38, 0.39, 0.41), "metallic": 1.0,
        "roughness": 0.30,
    },
    "stainless": {
        "label": "不锈钢", "category": "metal",
        "base_color": (0.46, 0.47, 0.49), "metallic": 1.0,
        "roughness": 0.20,
    },
    "aluminum": {
        "label": "铝", "category": "metal",
        "base_color": (0.62, 0.63, 0.65), "metallic": 1.0,
        "roughness": 0.34,
    },
    "aluminium": {
        "label": "铝", "category": "metal",
        "base_color": (0.62, 0.63, 0.65), "metallic": 1.0,
        "roughness": 0.34,
    },
    "brass": {
        "label": "黄铜", "category": "metal",
        "base_color": (0.58, 0.42, 0.16), "metallic": 1.0,
        "roughness": 0.24,
    },
    "copper": {
        "label": "紫铜", "category": "metal",
        "base_color": (0.55, 0.25, 0.16), "metallic": 1.0,
        "roughness": 0.28,
    },
    "plastic": {
        "label": "塑料", "category": "polymer",
        "base_color": (0.52, 0.54, 0.56), "roughness": 0.48,
        "coat_weight": 0.08,
    },
    "abs": {
        "label": "ABS 白", "category": "polymer",
        "base_color": (0.80, 0.80, 0.78), "roughness": 0.62,
    },
    "abs_white": {
        "label": "ABS 白", "category": "polymer",
        "base_color": (0.80, 0.80, 0.78), "roughness": 0.62,
    },
    "nylon": {
        "label": "尼龙", "category": "polymer",
        "base_color": (0.60, 0.58, 0.50), "roughness": 0.58,
    },
    "wood": {
        "label": "木材", "category": "organic",
        "base_color": (0.32, 0.20, 0.10), "roughness": 0.64,
    },
    "powder_steel": {
        "label": "粉末钢", "category": "metal",
        "base_color": (0.16, 0.17, 0.19), "metallic": 0.78,
        "roughness": 0.52, "coat_weight": 0.08,
    },
    "engine_outer": {
        "label": "引擎外壳", "category": "metal",
        "base_color": (0.11, 0.12, 0.14), "metallic": 0.62,
        "roughness": 0.44, "coat_weight": 0.10,
    },
})

# Ocular / soft-tissue presets used by the anatomical assemblies.  Wet
# surfaces carry a coat layer; translucent media use transmission.
PRESETS.update({
    "bio_sclera": {
        "label": "巩膜", "category": "organic",
        "base_color": (0.355, 0.295, 0.235), "roughness": 0.54,
        "ior": 1.38, "coat_weight": 0.30, "coat_roughness": 0.10,
        "subsurface_weight": 0.18,
    },
    "bio_cornea": {
        "label": "角膜", "category": "organic",
        "base_color": (1.0, 1.0, 1.0), "roughness": 0.012,
        "ior": 1.376, "alpha": 0.030, "coat_weight": 1.0, "coat_roughness": 0.01,
    },
    "bio_iris": {
        "label": "虹膜金绿", "category": "organic",
        "base_color": (0.185, 0.088, 0.009), "roughness": 0.50,
        "ior": 1.40, "coat_weight": 0.55, "coat_roughness": 0.05,
        "sheen_weight": 0.25, "sheen_roughness": 0.35,
        "subsurface_weight": 0.10,
    },
    "bio_iris_deep": {
        "label": "虹膜深褐", "category": "organic",
        "base_color": (0.030, 0.012, 0.0025), "roughness": 0.72,
        "ior": 1.40, "coat_weight": 0.04, "sheen_weight": 0.0,
    },
    "bio_pupil": {
        "label": "瞳孔", "category": "organic",
        "base_color": (0.0035, 0.003, 0.003), "roughness": 0.58,
        "ior": 1.50, "coat_weight": 0.04,
    },
    "bio_vitreous": {
        "label": "玻璃体", "category": "organic",
        "base_color": (0.010, 0.012, 0.014), "roughness": 0.10,
        "ior": 1.336, "transmission": 0.55, "coat_weight": 0.05,
    },
    "bio_lens": {
        "label": "晶状体", "category": "organic",
        "base_color": (0.045, 0.055, 0.045), "roughness": 0.01,
        "ior": 1.42, "transmission": 0.97, "coat_weight": 0.10,
    },
    "bio_blood": {
        "label": "动脉血", "category": "organic",
        "base_color": (0.135, 0.0055, 0.0035), "roughness": 0.55,
        "ior": 1.36, "coat_weight": 0.25, "coat_roughness": 0.10,
        "subsurface_weight": 0.12,
    },
    "bio_blood_dark": {
        "label": "静脉血", "category": "organic",
        "base_color": (0.052, 0.005, 0.011), "roughness": 0.55,
        "ior": 1.36, "coat_weight": 0.20,
    },
    "bio_muscle": {
        "label": "眼外肌", "category": "organic",
        "base_color": (0.145, 0.028, 0.022), "roughness": 0.52,
        "ior": 1.40, "coat_weight": 0.35, "sheen_weight": 0.30,
        "subsurface_weight": 0.32,
    },
    "bio_tendon": {
        "label": "肌腱", "category": "organic",
        "base_color": (0.62, 0.56, 0.45), "roughness": 0.46,
        "ior": 1.41, "coat_weight": 0.30, "subsurface_weight": 0.12,
    },
    "bio_bone": {
        "label": "眶骨", "category": "organic",
        "base_color": (0.115, 0.095, 0.068), "roughness": 0.74,
        "ior": 1.52, "coat_weight": 0.12, "subsurface_weight": 0.06,
    },
    "bio_cartilage": {
        "label": "软骨", "category": "organic",
        "base_color": (0.55, 0.52, 0.42), "roughness": 0.40,
        "ior": 1.44, "coat_weight": 0.35,
    },
    "bio_fat": {
        "label": "眶脂", "category": "organic",
        "base_color": (0.38, 0.255, 0.055), "roughness": 0.58,
        "ior": 1.44, "coat_weight": 0.20, "subsurface_weight": 0.40,
    },
    "bio_nerve": {
        "label": "神经", "category": "organic",
        "base_color": (0.66, 0.60, 0.47), "roughness": 0.40,
        "ior": 1.42, "coat_weight": 0.25, "subsurface_weight": 0.18,
    },
    "bio_choroid": {
        "label": "脉络膜", "category": "organic",
        "base_color": (0.17, 0.020, 0.016), "roughness": 0.50,
        "ior": 1.40, "coat_weight": 0.30, "subsurface_weight": 0.20,
    },
    "bio_skin": {
        "label": "鳄皮", "category": "organic",
        "base_color": (0.032, 0.036, 0.024), "roughness": 0.72,
        "ior": 1.45, "coat_weight": 0.22, "coat_roughness": 0.18,
        "sheen_weight": 0.35, "sheen_roughness": 0.40,
        "subsurface_weight": 0.08,
    },
    "bio_skin_dark": {
        "label": "鳄皮暗纹", "category": "organic",
        "base_color": (0.042, 0.046, 0.034), "roughness": 0.62,
        "ior": 1.45, "coat_weight": 0.15, "sheen_weight": 0.25,
    },
    "bio_membrane": {
        "label": "瞬膜", "category": "organic",
        "base_color": (0.45, 0.40, 0.33), "roughness": 0.14,
        "ior": 1.38, "transmission": 0.62, "coat_weight": 0.20,
        "subsurface_weight": 0.15,
    },
    "bio_keratin": {
        "label": "角质鳞", "category": "organic",
        "base_color": (0.135, 0.125, 0.095), "roughness": 0.50,
        "ior": 1.48, "coat_weight": 0.30, "sheen_weight": 0.20,
    },
})


def is_preset(name: object) -> bool:
    return isinstance(name, str) and name in PRESETS


def names() -> list[str]:
    """All preset names, sorted."""
    return sorted(PRESETS)


def get(name: str | None = None) -> dict:
    """Expand one preset into a full parameter dict.

    Unknown names fall back to :data:`DEFAULT`; use :func:`validate` when the
    caller needs to reject typos instead of silently substituting.
    """
    key = name if is_preset(name) else DEFAULT
    out = copy.deepcopy(BASE)
    out.update({k: v for k, v in PRESETS[key].items()
                if k not in ("label", "category")})
    out["name"] = key
    out["label"] = PRESETS[key].get("label", key)
    out["category"] = PRESETS[key].get("category", "generic")
    return out


def catalog() -> list[dict]:
    """Compact list for ``atta_catalog``: name, label, category, parameters."""
    return [get(n) for n in names()]


def validate(material: object) -> tuple[bool, list[dict]]:
    """Check a preset name (or an explicit spec dict) without expanding it."""
    if material is None:
        return True, []
    if isinstance(material, dict):
        spec = material
        bad = [k for k in spec
               if k not in BASE and k not in ("name", "label", "category")]
        errors = [{"code": "unknown_material_param", "path": "material.%s" % k,
                   "message": "unknown material parameter %r" % k,
                   "fix": "use one of: %s" % ", ".join(sorted(BASE))}
                  for k in bad]
        return not errors, errors
    if is_preset(material):
        return True, []
    return False, [{
        "code": "unknown_material",
        "path": "finish.material",
        "message": "unknown material %r" % (material,),
        "fix": "use one of: %s" % ", ".join(names()),
    }]


def resolve(material: object = None) -> dict:
    """Expand a preset name or an inline spec dict into a full parameter dict.

    Inline dicts are merged over :data:`BASE`, so a caller can start from a
    preset with ``{"name": "gold", "roughness": 0.4}``.  Unknown names are
    rejected here; :func:`get` is the lenient variant.
    """
    if material is None:
        return get(DEFAULT)
    if isinstance(material, dict):
        base_name = material.get("name", DEFAULT)
        out = get(base_name if is_preset(base_name) else DEFAULT)
        for key, value in material.items():
            if key in ("label", "category"):
                continue
            if key in BASE or key == "name":
                out[key] = value
        if not is_preset(base_name):
            out["name"] = "custom"
            out["label"] = "custom"
        return out
    ok, errors = validate(material)
    if not ok:
        raise ValueError(errors[0]["message"] + " — " + errors[0]["fix"])
    return get(str(material))
