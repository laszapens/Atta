---
name: blender-modeling
description: Blender 3D modeling / 参数化建模 / 3D asset creation with Atta v1.1. Prefer Atta v1.1's ten semantic tools (doctor / catalog / spec / model / variant / describe / verify / preview / deliver / run) for recipes, Spec v3, feature graphs, measured facts, three-state verification, previews and GLB/OBJ/STL delivery. Use the legacy Atta-Z/workbench only as an explicit fallback for custom build.py, micron metrology, gate lint or complex scene diagnostics. Keywords — Blender, 3D model, mesh, surface, parametric, recipe, feature graph, Coons, Gordon, mug, bowl, vase, bottle, gear, dome, bracket, enclosure, GLB, OBJ, STL, 建模, 曲面, 出图, 验收, Atta v1.1.
whenToUse: 用户要建/改/检查/导出 Blender 3D 模型，或说“建一个杯子/碗/花瓶/瓶子/齿轮/曲面面板/穹顶/支架/外壳”“参数化建模”“出几个方案”“验证水密/厚度/间隙”“导出 GLB/OBJ/STL”“Atta v1.1”“mcp__atta__*”时；也用于需要可重建 Spec、可验证回执的建模任务。
when_to_use: 先走 Atta v1.1（优先 MCP 工具 mcp__atta__*，其次 python3 -m atta）；只有 v1.1 缺能力且用户同意时，才用 legacy Atta-Z/workbench。
license: 内部项目专用
metadata:
  verified: '2026-11-01'
  reference_environment: 'Ubuntu + Blender 5.2.2 LTS + Atta v1.1'
---

# Blender 建模（Atta v1.1 优先）

Atta v1.1 在 `/home/laszapen/DSH/Atta-v1.1`。它把建模变成：
**意图 → 可重建 Spec → 确定性几何 → 测量事实 → 三态验收 → 交付**。

模型不靠看图猜。`atta_describe` 给数字；`atta_preview` 只用于判断外观；
`atta_verify` 才是判决；没有 verify 回执，不许说“建好了”。

单位：公开 Spec/参数用 **mm** 和 **deg**；几何内核/Blender 场景用 **米**。

## 0 · 入口

**优先 MCP 工具**（ZCode / DSH / Claude / Cursor 已安装）：

```
atta_doctor  atta_catalog  atta_spec     atta_model   atta_variant
atta_describe atta_verify  atta_preview  atta_deliver atta_run
```

工具不可用时走 CLI（同一个 engine）：

```bash
cd /home/laszapen/DSH/Atta-v1.1
python3 -m atta doctor --deep
python3 -m atta catalog --verbose
python3 -m atta model --recipe product.mug --quality draft
python3 -m atta describe --probe '["bbox","topology","silhouette"]' --wall
python3 -m atta verify --level standard
python3 -m atta preview --views iso,front --res 512
python3 -m atta deliver --formats glb,obj
```

真实项目用 `ATTA_HOME` 隔离状态：

```bash
ATTA_HOME=/path/to/project python3 -m atta model --recipe product.mug
```

不设 `ATTA_HOME` 时，状态落在当前目录的 `_atta/`。

## 1 · 十动词

| 工具 | 回答的问题 |
|---|---|
| `atta_doctor` | 引擎/Blender/配方/feature/材质是否就绪？ |
| `atta_catalog` | 有哪些配方、feature、constraint、材质、质量档？ |
| `atta_spec` | 这个 Spec 合法吗？补了哪些设计默认值？ |
| `atta_model` | 建或 refine 一个模型；返回 model_id + facts |
| `atta_variant` | 在参数空间采样并排序候选（fit 用 base） |
| `atta_describe` | 量 + 感知：bbox/体积/拓扑/壁厚/曲率/探针；`perceive` 给 ASCII 轮廓/截面；`trace` 给构造步骤；`compare_to` 给差异 |
| `atta_verify` | pass / degraded / fail，附 receipt 和 fix |
| `atta_preview` | 多机位判断图 + facts + render receipt |
| `atta_deliver` | verify 通过后导出 GLB/OBJ/STL/BLEND + manifest |
| `atta_run` | 一次调用跑多步流水线，支持 `$prev` / `$ref` |

`refine` = `atta_model` 带 `patches`；`fit` = `atta_variant` 带 `base`；
`probe` = `atta_describe` 带 `probe`。表面只有十个动词。

## 2 · 标准流程

```
atta_doctor
  -> atta_catalog
  -> atta_spec(op="validate")          # 只校验，不建几何
  -> atta_spec(op="propose")           # 参数不全时补设计默认值
  -> atta_model(quality="draft")       # 建 + 记录；返回 model_id
  -> atta_describe(perceive=true)      # text + ASCII 轮廓/截面，不启动 Blender
  -> atta_verify                       # 三态判决 + receipt
  -> （不满足）atta_variant / atta_model(patches=...)
  -> atta_preview                      # 只在需要判断外观时
  -> atta_deliver                      # 非 pass 不写文件
```

快速循环：`catalog -> spec -> model(draft) -> describe(perceive) -> model(patches)`。
验收循环：`verify -> preview -> deliver`。

### 先感知，再渲染

`atta_describe(perceive=true)`（CLI：`python3 -m atta perceive`）是快速取景器，
0.1–0.3 秒，不启动 Blender：

- `perception.text`：一行尺寸/体积/闭合/genus/凸度/对称/孔估计；
- `perception.views`：front/top/right 的 `#`/`.` ASCII 轮廓；
- `perception.sections`：Z 截面 ASCII；
- `perception.traits`：aspect/sphericity/convexity/symmetry/holes_est；
- `perception.signature`：可直接比较的签名。

迭代时用：

```json
{"model_id": "<new>", "compare_to": "<old>", "views": ["front","top"]}
{"model_id": "mdl_...", "trace": true}
```

`diff.views[*].iou` + ASCII `+/-/#` 看改了什么；`trace.steps` 看每一步
feature 的体积/bbox。只有需要判断外观/材质时才 `atta_preview`。

## 3 · 配方

| 用户意图 | 先试配方 |
|---|---|
| 马克杯 / 带把手杯子 | `product.mug` |
| 碗 / 花瓣碗 | `product.bowl` |
| 花瓶 / 扭转瓶 | `product.vase` |
| 瓶子 / 中空瓶 | `product.bottle` |
| 齿轮 / 直齿 | `mechanism.gear` |
| 四边曲面面板 | `surface.panel` |
| 穹顶 / 曲线网络薄壳 | `surface.dome` |
| 带孔安装板 | `part.bracket` |
| 开口电子外壳 | `part.enclosure` |

配方参数、预设、约束用 `atta_catalog(verbose=true)` 查，不要猜字段名。

## 4 · Spec v3

### recipe 路线（首选）

```json
{
  "schema": "atta.spec/v3",
  "name": "mug-01",
  "model": {
    "kind": "recipe",
    "id": "product.mug",
    "preset": "nominal",
    "params": {"height": 95, "wall": 3.2}
  },
  "quality": "draft",
  "material": "ceramic",
  "finish": {"bevel_mm": 0.6, "smooth_deg": 35},
  "constraints": {"bbox": {"z": [80, 120]}, "min_wall_mm": 2.4}
}
```

### feature graph 路线（配方都不像时）

```json
{
  "schema": "atta.spec/v3",
  "name": "bracket-01",
  "model": {
    "kind": "graph",
    "output": "cut",
    "units": "mm",
    "segments": 64,
    "features": [
      {"id": "plate", "op": "box", "args": {"size": [80, 50, 6], "center": [40, 25, 3]}},
      {"id": "hole", "op": "cylinder", "args": {"radius": 4, "height": 20, "center": [40, 25, 3]}},
      {"id": "cut", "op": "difference", "inputs": ["plate", "hole"]}
    ]
  },
  "constraints": {"bbox": {"x": [75, 85]}}
}
```

可用 feature：`box/cylinder/sphere/cone/torus/revolve/loft/sweep/coons/gordon/
extrude/thicken/lobes/union/difference/intersection/mirror/pattern_linear/
pattern_polar/transform`。参数是 mm，支持 `{"$param":"w"}` 和
`{"$expr":"w/2+4"}`。

不要为常见形状新写 `build.py`；先看 `atta_catalog`，再用 graph 或新配方。

## 5 · 事实与验收铁律

1. **先 `atta_doctor`**。引擎/Blender 没就绪，后面的回执不可信。
2. **先 `atta_catalog` / `atta_spec(op="schema")`**。不要猜参数名和字段。
3. **没有 `atta_verify` 回执，不许说完成**。`degraded` 不等于 `pass`。
4. **先感知，再渲染**。`atta_describe(perceive=true)` 先给 text + ASCII；
   `atta_preview` 只在需要判断外观时看，且不是判决。
5. **交付只走 `atta_deliver`**。fail 会 block；degraded 必须显式
   `allow_degraded=true`，不要手拼 GLB/STL。
6. **假设不是测量**。`propose` 补的默认值进 `assumption_ledger`；关键孔位、
   截面、轮廓缺失会进 `unresolved`，必须显式给出。
7. **壁厚/间隙是采样近似**（`exact:false`），不要说成“全模型最薄处证明”。
8. **失败先看 `issues[].fix`**，通常可以直接变成 `patches` 或参数修改。

回执里至少报告：`model_id`、`verdict`、`score`、`mesh_hash`、`spec_hash`、
产物路径。`verify` 通过才 `deliver`。

## 6 · 常见操作

```bash
# 只校验/补默认值
python3 -m atta --json spec --op validate --spec-file examples/mug/spec.json
python3 -m atta --json spec --op propose  --spec-file examples/mug/spec.json

# 参数采样
python3 -m atta variant --recipe product.mug --count 6 \
  --ranges '{"height":[80,110]}' --objective '{"volume_mm3":"max"}'

# 空间探针
python3 -m atta describe --model-id <id> --probe '["bbox","silhouette","section"]'
python3 -m atta describe --model-id <id> --probe '{"op":"raycast","axis":"z","res":[24,24]}'

# 批量流水线
python3 -m atta --json run --steps-file examples/pipeline.json
```

`atta_run` 的后续步骤可以用 `{"$prev":"model_id"}` 或 `{"$ref":"model_id"}`。

## 7 · Legacy fallback（只在 v1.1 缺能力时）

以下能力 v1.1 暂时没有，且用户明确同意时，才用
`/home/laszapen/DSH/归一（blender）` 的 Atta-Z/workbench：

- 自定义 `build.py` / 复杂 Blender 场景脚本；
- 微米级计量、双向偏差、gate lint；
- 需要 workbench 的场景查询、诊断图、常驻场景编辑。

调用时必须明确说明“这一步走的是 legacy Atta-Z/workbench”，不要把它的产物
和新 Atta v1.1 的 `model_id` / verify 回执混在同一份报告里。

旧入口：

```bash
cd "/home/laszapen/DSH/归一（blender）"
python3 -m attaz doctor
python3 -m workbench status
```

## 8 · 结束条件

一轮工作结束必须同时有：

1. `atta_verify` 的判决（`pass / degraded / fail`）；
2. `atta_describe` 或 verify 回执里的数字（bbox、体积、闭合、genus、壁厚等）；
3. 如果交付了：`atta_deliver` 的 manifest 路径和文件列表。

没有回执的“看起来差不多”不算完成。不要用截图或 `model_id` 代替判决。
