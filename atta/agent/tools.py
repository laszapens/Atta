# -*- coding: utf-8 -*-
"""The ten agent-facing tools — the single source of truth for every host.

MCP, HTTP, the DSH plugin and the OpenAI/Anthropic manifests all describe the
same surface declared here.  Geometry work is delegated to ``atta.engine``,
imported lazily so this module (and its schemas) can be inspected and tested
without a working engine.

Envelope::

    {"ok": true,  "tool": "...", "data": ..., "error": null, "ms": 1.23}
    {"ok": false, "tool": "...", "data": null, "error": {code, message,
                                                          path, fix}, "ms": ...}

Failures are values.  A traceback never crosses a protocol boundary.
"""

from __future__ import annotations

import base64
import importlib
import threading
import time
from pathlib import Path
from typing import Any

from atta.errors import AttaError

# Modeling is stateful: model -> verify -> preview must observe the same
# workspace.  Serialize calls inside one host process; the MCP server also
# uses a single worker so arrival order is preserved.
_CALL_LOCK = threading.RLock()

__all__ = [
    "TOOLS",
    "TOOL_NAMES",
    "TOOL_BY_NAME",
    "DEFAULT_TIMEOUTS",
    "tool_schemas",
    "call_tool",
    "images_for_result",
]

# --------------------------------------------------------------------------- #
# JSON Schema helpers
# --------------------------------------------------------------------------- #


def _obj(properties: dict, required: list[str] | None = None,
         extra: bool = False) -> dict:
    out: dict = {
        "type": "object",
        "properties": properties,
        "additionalProperties": bool(extra),
    }
    if required:
        out["required"] = list(required)
    return out


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_JSON = {"type": "object", "additionalProperties": True}
_SPEC = {
    "type": "object",
    "additionalProperties": True,
    "description": "Atta Spec v3 JSON（atta.spec/v3）；不确定字段先调 atta_catalog。",
}


# --------------------------------------------------------------------------- #
# The ten tools
# --------------------------------------------------------------------------- #

TOOLS: list[dict] = [
    {
        "name": "atta_doctor",
        "description": (
            "[Atta v1.1] 引擎自检：引擎版本、Blender 可用性、recipe/spec/feature 数量、"
            "当前构建。开始任何建模前先跑它；失败先修环境，不要直接建模。"
        ),
        "inputSchema": _obj({
            "deep": {**_BOOL, "description": "true 时返回更完整的探测（可能较慢）"},
        }),
    },
    {
        "name": "atta_catalog",
        "description": (
            "[Atta v1.2] 能力目录。默认只返回**紧凑索引**（recipe id + 一行说明 + 类别），"
            "体积小。要看某个 recipe 的完整参数 schema 就传 recipe=\"<id>\"（可传数组）；"
            "verbose=true 返回全量（很大，仅在没有具体目标时偶然需要）。"
            "不确定 recipe 或参数字段时先调它，不要猜。"
        ),
        "inputSchema": _obj({
            "recipe": {
                "type": ["string", "array"],
                "items": _STR,
                "description": "recipe id 或 id 数组；给定时返回这些 recipe 的完整参数 schema",
            },
            "verbose": {**_BOOL,
                        "description": "true 时返回全量目录（含每个 recipe 的完整参数与 "
                                       "feature doc，很大）"},
            "offset": {**_INT,
                       "description": "索引翻页起点（用上一页的 next_offset）；默认 0"},
            "feature": {**_STR,
                        "description": "单个 graph op 的完整 args schema（如 feature=\"revolve\"）"},
        }),
    },
    {
        "name": "atta_spec",
        "description": (
            "[Atta v1.1] Spec v3 入口：validate 校验/归一化（不建几何），propose 补可解释的"
            "设计默认值并返回 assumption_ledger，compile 生成可复查脚本，schema 返回能力表。"
        ),
        "inputSchema": _obj({
            "op": {
                "type": "string",
                "enum": ["validate", "propose", "compile", "schema"],
                "default": "validate",
                "description": "validate | propose | compile | schema",
            },
            "spec": _SPEC,
            "assume": {**_BOOL,
                       "description": "validate 时先补设计默认值（等价于 propose + validate）"},
        }),
    },
    {
        "name": "atta_model",
        "description": (
            "[Atta v1.1] 建模：recipe + params、Spec v3 或 feature graph 构建；patches 是 "
            "refine。返回 model_id + facts。建完先 atta_describe 读事实，再 atta_verify 出判决。"
        ),
        "inputSchema": _obj({
            "recipe": {**_STR, "description": "recipe id，例如 product.mug；先查 atta_catalog"},
            "spec": _SPEC,
            "params": {**_JSON, "description": "recipe 参数覆盖（mm / deg）"},
            "patches": {**_JSON, "description": "refine 补丁：对已有 spec/params 的局部覆盖"},
            "preset": {**_STR, "description": "recipe preset（默认 nominal）"},
            "quality": {"type": "string", "enum": ["draft", "review", "deliver"],
                        "default": "draft", "description": "draft/review/deliver"},
            "name": {**_STR, "description": "模型名（省略时由 recipe/spec 推导）"},
            "material": {**_STR, "description": "材质名，例如 ceramic / aluminum"},
            "finish": {**_JSON,
                       "description": 'finish：{"bevel_mm":0.8,"smooth_deg":35}'},
        }),
    },
    {
        "name": "atta_variant",
        "description": (
            "[Atta v1.1] 参数采样/求解：无 targets 时按 ranges 采样排序；给 targets 或 "
            "solve=true 时用真实草稿网格做约束求解（可行解 + 灵敏度 + 最近可达点），"
            "支持 Pareto。base 是参考模型 id。"
        ),
        "inputSchema": _obj({
            "recipe": {**_STR, "description": "recipe id；与 base 二选一"},
            "base": {**_STR, "description": "参考模型 id（fit：围绕它采样）"},
            "count": {**_INT, "default": 6, "description": "初始采样数（默认 6）"},
            "ranges": {**_JSON,
                       "description": '参数区间：{"height":{"min":80,"max":120}} 或 {name:[min,max]}；求解时省略则用参数全界'},
            "strategy": {**_STR, "default": "lhs",
                         "description": "lhs|auto|compass|nelder-mead|differential-evolution|local|pareto"},
            "seed": {**_INT, "default": 42, "description": "随机种子"},
            "objective": {**_JSON,
                          "description": '目标：{"volume_mm3":"min"} 或 [{metric,direction,weight}]'},
            "targets": {**_JSON,
                        "description": '目标指标：{"bbox_z":100,"volume_mm3":[250000,320000],"min_wall_mm":{"min":2.5}}'},
            "constraints": {**_JSON,
                            "description": "与 atta_verify 同口径的约束（求解时按语义转换）"},
            "max_evaluations": {**_INT,
                                "description": "求解评估预算（默认 max(48,12*count)）"},
            "tolerance": {"type": "number", "default": 0.000001,
                          "description": "归一化可行性容差（默认 1e-6）"},
            "initial": {**_JSON, "description": "求解起点参数"},
            "pareto": {**_BOOL, "description": "true 时返回 Pareto 前沿"},
            "free": {"type": "array", "items": _STR,
                     "description": "只求解这些参数（省略=全部数值参数）"},
            "fixed": {"type": "array", "items": _STR,
                      "description": "固定这些参数"},
            "solve": {**_BOOL, "description": "true 强制进入求解模式"},
            "quality": {"type": "string", "enum": ["draft", "review", "deliver"],
                        "default": "draft", "description": "候选评估质量"},
        }),
    },
    {
        "name": "atta_describe",
        "description": (
            "[Atta v1.1] 结构事实与纯文本感知：bbox/体积/质心/拓扑/壁厚/曲率/探针；"
            "perceive=true 返回低功耗 ASCII 轮廓/截面 + 形状特征；trace=true 返回构造步骤；"
            "compare_to=另一个 model_id 返回差异。先读这些，不要靠渲染图猜尺寸。"
        ),
        "inputSchema": _obj({
            "model_id": {**_STR, "description": "模型 id；省略 = 当前模型"},
            "probe": {**_JSON,
                      "description": '探针名/数组/对象：bbox、topology、wall、raycast、silhouette、section、clearance、nearest 等'},
            "wall": {**_BOOL, "description": "true 时跑壁厚射线采样（approx）"},
            "curvature": {**_BOOL, "description": "true 时统计曲率"},
            "perceive": {**_BOOL, "description": "true 时返回纯文本感知包（ASCII 轮廓/截面 + traits）"},
            "views": {"type": "array", "items": _STR,
                      "description": "perceive 视图：front/top/right/left/back/iso（默认 front,top,right）"},
            "res": {**_INT, "description": "ASCII 分辨率（默认 24，范围 8..64）"},
            "sections": {**_JSON,
                         "description": "Z 截面比例：0.5 或 [0.25,0.5,0.75]（默认 0.5）"},
            "compare_to": {**_STR, "description": "对照模型 id；返回轮廓 IoU 与 ASCII +/- 差异"},
            "trace": {**_BOOL, "description": "true 时返回 recipe/graph 的构造步骤与逐步体积"},
        }),
    },
    {
        "name": "atta_verify",
        "description": (
            "[Atta v1.1] 严格验收：拓扑 + bbox/体积窗口 + 间隙/干涉 + 壁厚/孔，三态 "
            "pass|degraded|fail，附 receipt。pass 只等于 verdict==pass；degraded 不算 pass。"
        ),
        "inputSchema": _obj({
            "model_id": {**_STR, "description": "模型 id；省略 = 当前模型"},
            "level": {"type": "string", "enum": ["basic", "standard", "strict"],
                      "default": "standard", "description": "验收等级"},
            "constraints": {**_JSON,
                            "description": "bbox/volume_mm3/min_wall_mm/clearance/interference/holes"},
            "reference": {**_STR, "description": "参考模型 id（偏差/间隙对照）"},
        }),
    },
    {
        "name": "atta_preview",
        "description": (
            "[Atta v1.1] 判断图与视觉分析：多机位 PNG + 事实卡 + render receipt。"
            "mode=beauty 保留旧判断图；studio/catalog/dark/clay 是带摄影棚灯光、"
            "地面和按零件材质的展示渲染；diagnostic 输出 normal/depth/id/curvature/"
            "deviation 通道。每张 beauty 图附带曝光/Zone/CIELAB/对比度/色度/边缘/"
            "key-fill 统计。图与分析都不是验收判决；几何仍看 atta_verify。"
        ),
        "inputSchema": _obj({
            "model_id": {**_STR, "description": "模型 id；省略 = 当前模型"},
            "views": {"type": "array", "items": _STR,
                      "description": "iso/front/top/left/right/back/hero/hero_l/hero_r"},
            "res": {**_INT, "description": "最长边像素（默认由模式决定）"},
            "samples": {**_INT, "description": "采样数（默认由模式决定）"},
            "quality": {"type": "string", "enum": ["draft", "review", "deliver"],
                        "description": "分辨率档（默认继承模型）"},
            "mode": {"type": "string",
                     "enum": ["beauty", "studio", "catalog", "dark", "clay",
                              "diagnostic"],
                     "description": "beauty/studio/catalog/dark/clay/diagnostic"},
            "lighting": {"type": "string",
                         "enum": ["studio3", "studio", "catalog", "dark", "clay", "none"],
                         "description": "灯光预设（默认由 mode 决定）"},
            "background": {"type": "string",
                           "enum": ["transparent", "dark", "white", "gray", "gradient"],
                           "description": "背景（默认由 mode 决定）"},
            "ground": {**_BOOL, "description": "是否加地面/接触阴影"},
            "lens_mm": {"type": "number", "description": "相机焦距 mm（85 更接近产品摄影）"},
            "exposure": {"type": "number", "description": "曝光补偿（stops）"},
            "look": {**_STR, "description": "AgX look，例如 'AgX - Medium High Contrast'"},
            "ortho": {**_BOOL, "description": "true 时用正交相机"},
            "distance_factor": {"type": "number", "description": "相机距离倍数（<1 更近）"},
            "materials": {
                "type": ["string", "object"], "additionalProperties": True,
                "description": "全局材质名，或按零件覆盖：{part_id: material, '*': material}"},
            "passes": {"type": "array", "items": _STR,
                       "description": "beauty/normal/depth/id/curvature/deviation"},
            "reference": {**_STR, "description": "参考模型 id（deviation 通道用）"},
            "deviation_range_mm": {"type": "number",
                                   "description": "偏差色标上限 mm（默认 p95×2）"},
            "focus": {**_STR, "description": "只框住某个零件 id（微距/局部视图）"},
            "aspect": {"type": ["string", "number"],
                       "description": "画幅宽高比；auto 按投影包围盒，square 正方形"},
            "focus_padding": {"type": "number",
                              "description": "focus 取景外扩比例（默认 0.3）"},
            "outdir": {**_STR, "description": "输出目录（默认 _atta/out）"},
        }),
    },
    {
        "name": "atta_deliver",
        "description": (
            "[Atta v1.1] 交付：先 verify；非 pass 即 block（degraded 需 allow_degraded=true）。"
            "通过后导出 GLB/OBJ/STL + manifest（sha256 + verify receipt）；"
            "图形子集可用 formats=[\"step\"] 交付解析 B-rep。"
        ),
        "inputSchema": _obj({
            "model_id": {**_STR, "description": "模型 id；省略 = 当前模型"},
            "formats": {"type": "array", "items": _STR,
                        "description": "glb/obj/stl/blend/step，默认 glb/obj/stl"},
            "outdir": {**_STR, "description": "输出目录（默认 _atta/out）"},
            "allow_degraded": {**_BOOL,
                               "description": "true 才允许 degraded 交付；pass 不受影响"},
            "name": {**_STR, "description": "导出文件名 stem"},
        }),
    },
    {
        "name": "atta_run",
        "description": (
            "[Atta v1.1] 一次调用跑多步流水线（model→describe→verify→preview→deliver）。"
            "步骤可命名、结果按 name 找回；禁止嵌套 atta_run。默认遇错即停。"
        ),
        "inputSchema": _obj({
            "steps": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": '[{"tool":"atta_model","args":{...},"name":"build"}, ...]；'
                               "也接受引擎的 {\"op\", \"args\", \"as\"} 形状",
                "minItems": 1,
            },
            "stop_on_error": {**_BOOL, "default": True, "description": "遇错即停（默认 true）"},
        }, required=["steps"]),
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in TOOLS)
TOOL_BY_NAME: dict[str, dict] = {t["name"]: t for t in TOOLS}

#: Long-running tools get a host-side default; callers may override with
#: ``call_tool(..., timeout=...)``.
DEFAULT_TIMEOUTS: dict[str, float] = {
    "atta_model": 600.0,
    "atta_variant": 1200.0,
    "atta_preview": 3600.0,
    "atta_deliver": 600.0,
    "atta_run": 1800.0,
}

_KNOWN_LIST = ", ".join(TOOL_NAMES)


# --------------------------------------------------------------------------- #
# Argument / error plumbing
# --------------------------------------------------------------------------- #


def _schema_keys(name: str) -> set[str]:
    tool = TOOL_BY_NAME.get(name) or {}
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    return set(props)


def _clean_args(name: str, arguments: Any) -> dict:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise AttaError(
            "bad_arguments",
            "arguments must be a JSON object, got %s" % type(arguments).__name__,
            path="arguments",
            fix='pass {"key": value}',
        )
    keys = _schema_keys(name)
    # Drop unknown keys and explicit nulls so agent hallucinations cannot turn
    # into TypeError inside the engine.
    return {k: v for k, v in arguments.items() if k in keys and v is not None}


#: A failure without a patch is a dead end for an agent.  When an error carries
#: no ``fix`` of its own we substitute the best generic patch for its family.
_FALLBACK_FIX = {
    "bad_arguments": "call atta_catalog (or atta_spec op='schema') and copy the "
                     "field names from the schema instead of guessing",
    "engine_unavailable": "run atta_doctor; if atta.engine is missing, "
                          "pip install -e . (or -e '.[all]') in the Atta checkout",
    "timeout": "raise the host timeout, or split the work (model draft -> "
               "preview one view) so no single call is long",
    "file_not_found": "call atta_model / atta_preview first so the artifact "
                      "exists, then retry with an absolute path",
    "internal_error": "run atta_doctor --deep, then retry the same call with "
                      "the minimal args needed to reproduce",
}


def _error_from(exc: BaseException) -> dict:
    """Normalise any exception into {code, message, path, fix}."""
    if isinstance(exc, AttaError):
        payload = exc.to_dict()
        code = str(payload.get("code") or "atta_error")
        out = {
            "code": code,
            "message": str(payload.get("message") or exc),
            "path": payload.get("path"),
            # An AttaError may carry fix=None.  An agent cannot act on that, so
            # fall back to the family patch rather than shipping a null.
            "fix": payload.get("fix") or _FALLBACK_FIX.get(
                code, "run atta_doctor, then retry with the minimal args"),
        }
        if "details" in payload:
            out["details"] = payload["details"]
        return out

    if isinstance(exc, ModuleNotFoundError) and (
            exc.name in {"atta.engine", "atta"} or "atta.engine" in str(exc)):
        return {
            "code": "engine_unavailable",
            "message": "atta.engine is not available: %s" % exc,
            "path": "atta.engine",
            "fix": "实现 atta/engine.py（见 docs/INTERFACES.md 的 Engine 签名），"
                   "或 pip install -e . 后确认 import atta.engine 可用",
        }
    if isinstance(exc, TimeoutError):
        return {
            "code": "timeout",
            "message": str(exc) or "operation timed out",
            "path": None,
            "fix": "提高 timeout，或把任务拆小；Blender 渲染/导出可能确实需要更久",
        }
    if isinstance(exc, FileNotFoundError):
        return {
            "code": "file_not_found",
            "message": str(exc),
            "path": getattr(exc, "filename", None),
            "fix": "检查路径是否存在，或先运行 atta_model / atta_preview 生成产物",
        }
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return {
            "code": "bad_arguments",
            "message": "%s: %s" % (type(exc).__name__, exc),
            "path": None,
            "fix": "对照 atta_catalog 的 schema 修正参数类型/取值",
        }
    return {
        "code": "internal_error",
        "message": "%s: %s" % (type(exc).__name__, exc),
        "path": None,
        "fix": "这是引擎内部错误；先用 atta_doctor 自检，再带 args 复现",
    }


def _engine():
    """Import atta.engine lazily so the contract is testable standalone."""
    try:
        return importlib.import_module("atta.engine")
    except Exception as exc:  # noqa: BLE001 - converted to a value below
        raise AttaError(
            "engine_unavailable",
            "atta.engine is not available: %s" % exc,
            path="atta.engine",
            fix="实现 atta/engine.py（见 docs/INTERFACES.md 的 Engine 签名），"
                "或 pip install -e . 后确认 import atta.engine 可用",
        ) from exc


# --------------------------------------------------------------------------- #
# Handlers (one per tool)
# --------------------------------------------------------------------------- #


def _doctor(engine, args: dict) -> Any:
    return engine.doctor(deep=bool(args.get("deep", False)))


#: One short line per recipe in the default (index) catalogue.
_CATALOG_SUMMARY_KEYS = ("id", "name", "tags", "n_params", "expected_genus",
                         "material")
#: Longest one-line summary kept in the index (full text via recipe=<id>).
_CATALOG_SUMMARY_CHARS = 72
#: Recipe ids shown per index page.  The index is the payload an agent reads
#: before it has picked a target, so it stays small and says how to page.
_CATALOG_PAGE = 14


def _one_line(text: Any, limit: int = _CATALOG_SUMMARY_CHARS) -> str | None:
    """First sentence/clause of a description, cut on a word-ish boundary."""
    if not isinstance(text, str):
        return None
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for sep in ("。", "；", "，", ". ", "; ", ", "):
        pos = cut.rfind(sep)
        if pos >= limit // 2:
            return cut[:pos + len(sep)].rstrip()
    return cut.rstrip() + "…"


def _lean_features(features: Any) -> Any:
    """Just the op names.  Arg names for one op come from ``feature=<op>``.

    A 25-op table of arg lists costs ~2 KB for information an agent needs only
    *after* it has chosen an op and is writing a graph, so the index keeps the
    vocabulary and pushes the schema behind a filter.
    """
    if not isinstance(features, dict):
        return features
    return sorted(features)
#: Fields that are already small and are what an agent needs to plan a build.
_CATALOG_KEEP_KEYS = ("version", "recipes", "features", "constraints", "solve",
                      "assembly", "perception", "views", "formats", "units",
                      "qualities", "quality_profiles", "finish", "verbs",
                      "loops", "materials")
#: Materials ship full PBR blocks; the index only needs name/label/category.
_MATERIAL_SUMMARY_KEYS = ("name", "label", "category")


def _catalog_index(full: dict, limit: int = _CATALOG_PAGE) -> dict:
    """Compact catalogue: what an agent needs to *choose*, nothing more.

    The per-recipe parameter schema is the bulk of the payload and only matters
    once a recipe has been chosen, so it moves behind ``recipe=<id>``.  Feature
    prose (``doc``) and material PBR blocks move to ``verbose=true``.  Nothing is
    silent: ``truncated`` / ``total_*`` / ``returned_*`` / ``omitted`` / ``hint``
    state exactly what was left out and how to get it.
    """
    out: dict = {}
    if "version" in full:
        out["version"] = full["version"]

    recipes = [r for r in (full.get("recipes") or []) if isinstance(r, dict)]
    page = recipes[:max(0, int(limit))]
    index = []
    for r in page:
        row = {"id": r.get("id")}
        summary = _one_line(r.get("description"))
        if summary:
            row["summary"] = summary
        # genus and param count are the two numbers that decide feasibility.
        if r.get("expected_genus") is not None:
            row["genus"] = r.get("expected_genus")
        if r.get("n_params") is not None:
            row["n_params"] = r.get("n_params")
        index.append(row)
    out["recipes"] = index

    materials = full.get("materials")
    if isinstance(materials, list):
        # name -> label is all an agent needs to pick one; PBR lives in
        # verbose.  A dict keeps 47 entries under 1 KB.
        out["materials"] = {
            m.get("name"): m.get("label") for m in materials
            if isinstance(m, dict) and m.get("name")
        }

    if "features" in full:
        out["features"] = _lean_features(full["features"])
    if "constraints" in full:
        out["constraints"] = sorted(full["constraints"])
    if "solve" in full:
        solve = full["solve"]
        if isinstance(solve, dict):
            out["solve"] = {"metrics": solve.get("metrics"),
                            "strategies": solve.get("strategies")}
        else:
            out["solve"] = solve

    # Small declarative blocks are folded into one container.  They are cheap
    # individually but every top-level key costs the model a scan.
    caps = {}
    for key in ("assembly", "perception", "views", "formats", "units",
                "qualities", "quality_profiles", "finish", "verbs", "loops"):
        if key in full:
            caps[key] = full[key]
    if caps:
        out["capabilities"] = caps

    out["truncated"] = len(page) < len(recipes) or True
    out["total_recipes"] = len(recipes)
    out["returned_recipes"] = len(index)
    out["omitted"] = ["recipes[].params", "recipes[].constraints",
                      "recipes[].description", "features[].doc",
                      "features[].arity", "materials[].pbr"]
    out["hint"] = ('某个 recipe 的完整参数用 atta_catalog(recipe="<id>")；'
                   '缺的 recipe 用 offset= 翻页；全量用 verbose=true')
    if len(page) < len(recipes):
        out["next_offset"] = len(page)
    return out


def _catalog_selected(full: dict, wanted: list[str]) -> dict:
    """Full parameter schema for the requested recipes only."""
    recipes = {r.get("id"): r for r in (full.get("recipes") or [])
               if isinstance(r, dict)}
    found, missing = [], []
    for rid in wanted:
        key = str(rid)
        if key in recipes:
            found.append(recipes[key])
        else:
            missing.append(key)
    out: dict = {"version": full.get("version"), "recipes": found}
    if missing:
        out["missing"] = missing
        out["missing_hint"] = ("unknown recipe id(s); call atta_catalog with no "
                               "arguments to list valid ids")
    if "features" in full:
        out["features"] = full["features"]
    if "constraints" in full:
        out["constraints"] = full["constraints"]
    if "solve" in full:
        out["solve"] = full["solve"]
    out["hint"] = "只返回被请求的 recipe 的完整参数；其余 recipe 用 index 查询"
    return out


def _catalog(engine, args: dict) -> Any:
    verbose = bool(args.get("verbose", False))
    wanted = args.get("recipe")
    wanted_feature = args.get("feature")

    def _full() -> Any:
        return engine.catalog(verbose=True)

    if wanted_feature:
        full = _full()
        features = full.get("features") if isinstance(full, dict) else None
        if not isinstance(features, dict):
            # Unknown engine shape: never reshape something we do not recognise.
            return full
        op = str(wanted_feature).strip()
        if op not in features:
            raise AttaError(
                "bad_arguments", "unknown feature op: %r" % op,
                path="feature",
                fix="use one of: %s" % ", ".join(sorted(features)),
            )
        return {"feature": op, "schema": features[op],
                "hint": "其余 op 用 atta_catalog 默认索引（features 列出全部 op 名）"}
    if wanted:
        ids = [wanted] if isinstance(wanted, str) else list(wanted)
        ids = [str(x) for x in ids if x]
        # A filtered lookup needs the full recipe records even when the caller
        # did not ask for verbose.
        full = _full()
        if not isinstance(full, dict) or "recipes" not in full:
            return full
        return _catalog_selected(full, ids)
    if verbose:
        full = engine.catalog(verbose=True)
        if isinstance(full, dict):
            # Explicitly requested, so nothing is removed — but an agent that
            # reached for it by reflex gets an honest size warning plus the
            # cheap path out, appended rather than dropped.
            import json as _json
            kb = len(_json.dumps(full, ensure_ascii=False)) // 1024
            full = dict(full)
            full["warning"] = (
                "verbose=true returned the full catalogue (~%d KB). Prefer "
                "atta_catalog(recipe=\"<id>\") or feature=\"<op>\" next time." % kb)
        return full
    full = engine.catalog(verbose=False)
    if not isinstance(full, dict) or "recipes" not in full:
        # A host/test double with a different shape: pass it straight through so
        # the envelope contract stays the only thing we guarantee.
        return full
    offset = args.get("offset")
    if offset:
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            raise AttaError("bad_arguments", "offset must be an integer",
                            path="offset", fix="pass offset=30 (next_offset)")
        recipes = [r for r in (full.get("recipes") or []) if isinstance(r, dict)]
        page = recipes[start:start + _CATALOG_PAGE]
        out = _catalog_index({**full, "recipes": page})
        out["offset"] = start
        out["total_recipes"] = len(recipes)
        nxt = start + len(page)
        if nxt < len(recipes):
            out["next_offset"] = nxt
        else:
            out.pop("next_offset", None)
        return out
    return _catalog_index(full)


def _spec(engine, args: dict) -> Any:
    op = str(args.get("op") or "validate").strip().lower()
    allowed = {"validate", "propose", "compile", "schema"}
    if op not in allowed:
        raise AttaError(
            "bad_arguments",
            "unknown spec op: %r" % op,
            path="op",
            fix="use one of: %s" % ", ".join(sorted(allowed)),
        )
    return engine.spec(
        spec=args.get("spec"),
        op=op,
        assume=bool(args.get("assume", False)),
    )


def _model(engine, args: dict) -> Any:
    return engine.model(
        recipe=args.get("recipe"),
        spec=args.get("spec"),
        params=args.get("params"),
        patches=args.get("patches"),
        preset=args.get("preset") or "nominal",
        quality=args.get("quality") or "draft",
        name=args.get("name"),
        material=args.get("material"),
        finish=args.get("finish"),
    )


def _variant(engine, args: dict) -> Any:
    count = int(args.get("count", 6))
    if count < 1:
        raise AttaError("bad_arguments", "count must be >= 1",
                        path="count", fix="pass count >= 1")
    seed = int(args.get("seed", 42))
    return engine.variant(
        recipe=args.get("recipe"),
        base=args.get("base"),
        count=count,
        ranges=args.get("ranges"),
        strategy=args.get("strategy") or "lhs",
        seed=seed,
        objective=args.get("objective"),
        constraints=args.get("constraints"),
        quality=args.get("quality") or "draft",
        targets=args.get("targets"),
        max_evaluations=args.get("max_evaluations"),
        tolerance=(float(args["tolerance"]) if args.get("tolerance") is not None
                   else 1e-6),
        initial=args.get("initial"),
        pareto=bool(args.get("pareto", False)),
        free=args.get("free"),
        fixed=args.get("fixed"),
        solve=bool(args.get("solve", False)),
    )


def _describe(engine, args: dict) -> Any:
    views = args.get("views")
    if isinstance(views, str):
        views = [v.strip() for v in views.split(",") if v.strip()]
    return engine.describe(
        model_id=args.get("model_id"),
        probe=args.get("probe"),
        wall=bool(args.get("wall", False)),
        curvature=bool(args.get("curvature", False)),
        perceive=bool(args.get("perceive", False)),
        views=views,
        res=int(args.get("res", 24)),
        sections=args.get("sections"),
        compare_to=args.get("compare_to"),
        trace=bool(args.get("trace", False)),
    )


def _verify(engine, args: dict) -> Any:
    level = str(args.get("level") or "standard").strip().lower()
    if level not in {"basic", "standard", "strict"}:
        raise AttaError("bad_arguments", "unknown verify level: %r" % level,
                        path="level", fix="use basic | standard | strict")
    return engine.verify(
        model_id=args.get("model_id"),
        level=level,
        constraints=args.get("constraints"),
        reference=args.get("reference"),
    )


def _preview(engine, args: dict) -> Any:
    views = args.get("views")
    if isinstance(views, str):
        views = [v.strip() for v in views.split(",") if v.strip()]
    if views is not None and not isinstance(views, list):
        raise AttaError("bad_arguments", "views must be an array of strings",
                        path="views", fix='pass ["iso","front","top"]')
    return engine.preview(
        model_id=args.get("model_id"),
        views=views,
        res=int(args["res"]) if args.get("res") is not None else None,
        samples=int(args["samples"]) if args.get("samples") is not None else None,
        quality=args.get("quality"),
        mode=args.get("mode") or "beauty",
        outdir=args.get("outdir"),
        lighting=args.get("lighting"),
        background=args.get("background"),
        ground=args.get("ground"),
        lens_mm=(float(args["lens_mm"]) if args.get("lens_mm") is not None else None),
        exposure=(float(args["exposure"]) if args.get("exposure") is not None else None),
        look=args.get("look"),
        materials=args.get("materials"),
        passes=args.get("passes"),
        ortho=args.get("ortho"),
        distance_factor=(float(args["distance_factor"])
                         if args.get("distance_factor") is not None else None),
        reference=args.get("reference"),
        deviation_range_mm=(float(args["deviation_range_mm"])
                            if args.get("deviation_range_mm") is not None else None),
        focus=args.get("focus"),
        aspect=args.get("aspect"),
        focus_padding=(float(args["focus_padding"])
                       if args.get("focus_padding") is not None else None),
    )


def _deliver(engine, args: dict) -> Any:
    formats = args.get("formats")
    if isinstance(formats, str):
        formats = [f.strip() for f in formats.split(",") if f.strip()]
    if formats is not None and not isinstance(formats, list):
        raise AttaError("bad_arguments", "formats must be an array of strings",
                        path="formats", fix='pass ["glb","obj","stl"]')
    return engine.deliver(
        model_id=args.get("model_id"),
        formats=formats,
        outdir=args.get("outdir"),
        allow_degraded=bool(args.get("allow_degraded", False)),
        name=args.get("name"),
    )


def _run(engine, args: dict) -> Any:
    steps = args.get("steps")
    if steps is None:
        raise AttaError("bad_arguments", "atta_run requires steps",
                        path="steps",
                        fix='pass [{"tool":"atta_model","args":{...}}]')
    if not isinstance(steps, list) or not steps:
        raise AttaError("bad_arguments", "steps must be a non-empty array",
                        path="steps", fix="pass at least one step")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise AttaError("bad_arguments",
                            "steps[%d] must be an object" % index,
                            path="steps[%d]" % index,
                            fix='each step is {"tool": "...", "args": {...}}')
    return engine.run(
        steps=steps,
        stop_on_error=args.get("stop_on_error", True) is not False,
    )


_HANDLERS = {
    "atta_doctor": _doctor,
    "atta_catalog": _catalog,
    "atta_spec": _spec,
    "atta_model": _model,
    "atta_variant": _variant,
    "atta_describe": _describe,
    "atta_verify": _verify,
    "atta_preview": _preview,
    "atta_deliver": _deliver,
    "atta_run": _run,
}


# --------------------------------------------------------------------------- #
# Public dispatcher
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Reply ergonomics: cap runaway arrays, keep the readable text first
# --------------------------------------------------------------------------- #

#: Longest list echoed in full.  Beyond this an agent gets a head/tail plus an
#: explicit "truncated" marker instead of thousands of profile points or rays.
MAX_ARRAY = 64
#: Hard cap on a single serialised string field (ASCII blocks stay well under).
MAX_STRING = 20000


def _truncate_array(values: list) -> Any:
    """Keep a prefix/suffix and say so; never silently shrink a payload."""
    n = len(values)
    if n <= MAX_ARRAY:
        return values
    head = MAX_ARRAY // 2
    tail = MAX_ARRAY - head
    return {
        "truncated": True,
        "n_total": n,
        "n_shown": MAX_ARRAY,
        "note": "array truncated; %d of %d items elided — use a probe/describe "
                "call with a narrower window instead of reading this in full"
                % (n - MAX_ARRAY, n),
        "head": values[:head],
        "tail": values[-tail:],
    }


def _compact(value: Any, depth: int = 0, key: str | None = None) -> Any:
    """Recursively bound array/string size without renaming any field."""
    if depth > 12:
        return value
    if isinstance(value, dict):
        return {k: _compact(v, depth + 1, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # ``images`` drives host attachment lookup (MCP content blocks, the DSH
        # plugin's collectImages); ``files`` is a tiny artifact list.  Both must
        # stay plain lists.  ``steps``/``checks``/``issues`` stay lists too, but
        # their *contents* are still compacted: a run step can embed a whole spec
        # (rings, profile point lists) and would otherwise bypass truncation.
        if key in ("images", "files"):
            return list(value)
        if key in ("steps", "checks", "issues"):
            return [_compact(v, depth + 1, None) for v in value]
        # A list of scalars is data; a list of objects is a record table.
        if len(value) > MAX_ARRAY:
            return _truncate_array(list(value))
        return [_compact(v, depth + 1, key) for v in value]
    if isinstance(value, str) and len(value) > MAX_STRING:
        return value[:MAX_STRING] + ("\n...[truncated %d chars]"
                                     % (len(value) - MAX_STRING))
    return value


#: Keys whose payload an agent reads first; hoisting them keeps the human-facing
#: text (one-line fact card, verdict, fix) ahead of large structured blobs.
_FIRST_KEYS = ("status", "verdict", "ok", "one_line", "text", "model_id", "next")


def _text_first(data: Any) -> Any:
    """Reorder a result dict so the short readable keys lead.  Values unchanged."""
    if not isinstance(data, dict):
        return data
    head = {k: data[k] for k in _FIRST_KEYS if k in data}
    if not head:
        return data
    head.update({k: v for k, v in data.items() if k not in head})
    return head


def _ergonomic(tool: str, data: Any) -> Any:
    """Apply the reply-ergonomics pass.  Additive only: no key is renamed."""
    data = _compact(data)
    data = _text_first(data)
    # describe/perceive already nest the readable line; hoist a copy so a model
    # that reads only the first 200 chars still gets the fact card.
    if isinstance(data, dict) and not data.get("one_line"):
        one = None
        facts = data.get("facts")
        if isinstance(facts, dict):
            one = facts.get("one_line")
        if not one:
            pack = data.get("perception")
            if isinstance(pack, dict):
                one = pack.get("text")
        if isinstance(one, str) and one:
            data["one_line"] = one
    return data


def _envelope(tool: str, ok: bool, data: Any, error: dict | None,
              started: float) -> dict:
    return {
        "ok": bool(ok),
        "tool": tool,
        "data": data,
        "error": error,
        "ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


def _run_with_timeout(fn, timeout: float, name: str):
    if timeout <= 0:
        raise AttaError("bad_arguments", "timeout must be > 0",
                        path="timeout", fix="pass seconds > 0")
    box: dict[str, Any] = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller
            box["error"] = exc

    # Daemon thread: an unkillable engine call must not pin the process after a
    # timeout response has already been returned.
    thread = threading.Thread(target=runner, name="atta-%s" % name, daemon=True)
    thread.start()
    thread.join(float(timeout))
    if thread.is_alive():
        raise AttaError(
            "timeout",
            "%s exceeded %.1fs" % (name, float(timeout)),
            path="timeout",
            fix="提高 timeout 或拆分任务；预渲染/preview 可能需要更久",
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


def call_tool(name: str, args: Any = None, timeout: float | None = None) -> dict:
    """Call one Atta tool and always return the structured envelope."""
    started = time.perf_counter()
    tool_name = str(name)

    if tool_name not in TOOL_BY_NAME:
        return _envelope(tool_name, False, None, {
            "code": "unknown_tool",
            "message": "unknown tool: %r (known: %s)" % (tool_name, _KNOWN_LIST),
            "path": "tool",
            "fix": "use one of: %s" % _KNOWN_LIST,
        }, started)

    with _CALL_LOCK:
        try:
            clean = _clean_args(tool_name, args)
            engine = _engine()
            handler = _HANDLERS[tool_name]
            if timeout is None:
                data = handler(engine, clean)
            else:
                data = _run_with_timeout(
                    lambda: handler(engine, clean), float(timeout), tool_name)
            return _envelope(tool_name, True, _ergonomic(tool_name, data),
                             None, started)
        except Exception as exc:  # noqa: BLE001 - the protocol gets values only
            return _envelope(tool_name, False, None, _error_from(exc), started)


def tool_schemas() -> list[dict]:
    """MCP ``tools/list`` shape: name / description / inputSchema."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["inputSchema"],
        }
        for t in TOOLS
    ]


# --------------------------------------------------------------------------- #
# Image discovery (MCP content blocks)
# --------------------------------------------------------------------------- #


def _iter_image_records(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "images" and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
            else:
                yield from _iter_image_records(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_image_records(item)


def _existing_png(raw: Any) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    candidates = [path] if path.is_absolute() else [
        Path.cwd() / path,
        Path(__file__).resolve().parents[2] / path,
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.suffix.lower() == ".png":
            return resolved
    return None


def images_for_result(result: Any, max_images: int = 3) -> list[dict]:
    """Recursively find ``images[].path`` PNGs and read them as base64.

    Returns records ``{"path", "mime_type", "data_base64"}``.  Silent about
    missing/unreadable files: an MCP response must still carry the JSON facts.
    """
    limit = int(max_images)
    if limit <= 0:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for record in _iter_image_records(result):
        raw = (record.get("abs_path") or record.get("path")
               or record.get("file"))
        path = _existing_png(raw)
        if path is None or str(path) in seen:
            continue
        try:
            if path.stat().st_size > 20 * 1024 * 1024:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        seen.add(str(path))
        out.append({
            "path": str(path),
            "mime_type": "image/png",
            "data_base64": base64.b64encode(data).decode("ascii"),
        })
        if len(out) >= limit:
            break
    return out
