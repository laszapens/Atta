---
name: atta
description: Atta v1.1 无头参数化建模与交付：用 recipe 或 Spec v3（JSON）驱动 numpy/Blender 内核，做零件、板件、回转体、装配间隙、参数采样、结构测量、三态数值验收与 GLB/OBJ/STL 交付。适用于用户要求建 3D 模型、改尺寸、量壁厚/间隙/体积、批量比较方案、导出 CAD 文件，或需要“建完能证明它对”的场景。
---

# Atta v1.1：十动词建模工作台

Atta 的模型来自 **可重建的 spec**，不是一次性的 GUI 操作。你写 JSON（或
选 recipe + 参数），确定性代码建几何；数字来自几何测量，不来自渲染猜测。

单位固定 **mm** 和 **deg**。几何内核用米，Blender 场景用米，公开参数用毫米；
转换只发生在边界。`+Z` 向上，`+X` 向右，`+Y` 向后。

## 铁律

1. **先 `atta_doctor`**。引擎/Blender 没就绪，后面的回执都不可信。
2. **先 `atta_catalog` / `atta_spec(op="schema")`**。不要猜 recipe 参数名或
   feature/constraint 字段。
3. **先 spec，后几何**。不确定就先 `atta_spec(op="validate")`，再
   `atta_model`。
4. **没有 `atta_verify` 回执，不许说完成**。`degraded` 不等于 `pass`。
5. **先感知，再渲染**。`atta_describe(perceive=true)` 给 text + ASCII
   轮廓/截面 + traits，0.1–0.3 秒且不启动 Blender；`atta_preview` 只在需要
   判断外观/材质时看 1–2 张（需要细节再加 diagnostic 通道），且不是判决。
6. **交付只走 `atta_deliver`**。非 pass 会 block；`degraded` 必须显式
   `allow_degraded=true`，不要手拼 GLB/STL。
7. **假设不是测量**。`propose`/`assume` 只补可解释的设计默认值，进入
   `assumption_ledger`；关键孔位、截面、轮廓仍必须显式给出。
8. **壁厚是采样近似**（`exact:false`）。不要把它说成“全模型最薄处的证明”。

## 十个工具

| 工具 | 回答的问题 |
|---|---|
| `atta_doctor` | 引擎健康吗？Blender 可用吗？能做什么？ |
| `atta_catalog` | 有哪些 recipe / feature / constraint，schema 是什么？ |
| `atta_spec` | 这个 spec 合法吗？补了哪些默认值？ |
| `atta_model` | 建或 refine 一个模型；返回 model_id + facts |
| `atta_variant` | 采样、按 targets/constraints 求解、Pareto；fit 用 base；装配用 part.param |
| `atta_describe` | 量 + 感知：拓扑/bbox/体积/壁厚/曲率/探针；`perceive` 给 ASCII 轮廓/截面；`trace` 给构造步骤；`compare_to` 给差异 |
| `atta_verify` | pass / degraded / fail，附 receipt |
| `atta_preview` | beauty/studio/catalog/dark/clay/diagnostic 图 + facts + 视觉统计 |
| `atta_deliver` | verify 通过后导出 GLB/OBJ/STL/BLEND（图形子集另有 STEP）+ manifest |
| `atta_run` | 一次调用跑多步流水线 |

`refine` = `atta_model` 带 `patches`；`fit` = `atta_variant` 带 `base`；
`probe` = `atta_describe` 带 `probe`。表面只有十个动词。

## 标准流程

```
atta_doctor
  -> atta_catalog
  -> atta_spec op=validate        # 只校验，不建几何
  -> （参数不全）atta_spec op=propose
  -> atta_model                   # 建 + 记录；返回 model_id
  -> atta_describe(perceive=true) # 先读 text + ASCII，不启动 Blender
  -> atta_verify                  # 三态判决 + receipt
  -> （不满足）atta_variant       # 采样/反解，不要自己猜
  -> atta_preview                 # 只在需要判断外观时
  -> atta_deliver                 # 非 pass 不写文件
```

快速循环（亚秒级）：`catalog -> spec -> model(draft) -> describe(perceive) -> model(patches)`。

验收循环：`verify -> preview -> deliver`。

### 先感知，再渲染

`atta_describe` 的低功耗感知包：

```json
{"model_id": "mdl_...", "perceive": true, "res": 24,
 "views": ["front", "top", "right"], "sections": [0.5]}
```

- `perception.text`：一行尺寸/体积/闭合/genus/凸度/对称/孔估计；
- `perception.views`：`#`/`.` 的 ASCII 轮廓（默认 front/top/right）；
- `perception.sections`：Z 截面 ASCII；
- `perception.traits`：aspect/sphericity/convexity/symmetry/holes_est；
- `perception.signature`：可直接比较的签名。

迭代时再加：

```json
{"model_id": "<new>", "compare_to": "<old>", "views": ["front", "top"]}
{"model_id": "mdl_...", "trace": true}
```

`diff.views[*].iou` + ASCII `+/-/#` 看改了什么；`trace.steps` 看每一步
feature 的体积/bbox。只有需要判断外观时才 `atta_preview`。

## Spec v3

### recipe 路线

```json
{
  "schema": "atta.spec/v3",
  "name": "mug-01",
  "model": {
    "kind": "recipe",
    "id": "product.mug",
    "preset": "nominal",
    "params": {"height": 95}
  },
  "quality": "draft",
  "material": "ceramic",
  "finish": {"bevel_mm": 0.8, "smooth_deg": 35},
  "constraints": {
    "bbox": {"x": [60, 140]},
    "min_wall_mm": 2.5,
    "volume_mm3": [100000, 900000]
  }
}
```

### feature graph 路线

```json
{
  "schema": "atta.spec/v3",
  "name": "bracket",
  "model": {
    "kind": "graph",
    "output": "cut",
    "features": [
      {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
      {"id": "holes", "op": "cylinder",
       "args": {"radius": 4, "height": 20, "center": [15, 25, 3]}},
      {"id": "cut", "op": "difference", "inputs": ["plate", "holes"]}
    ]
  }
}
```

feature `args` 用 mm；值可以是 `{"$param": "name"}` 或
`{"$expr": "w/2 + 4"}`（受限算术，不允许 eval/属性/下标）。

## constraints（verify / variant 共用）

- `bbox`: `{x:[min,max], y:..., z:...}`
- `volume_mm3`: `[min,max]` 或目标值
- `min_wall_mm`: 数字；standard 是 warn，strict 是 fail
- `clearance`: `[{a,b,min}]`
- `interference`: `[{a,b,max}]`
- `interfaces`: 设计内压入/交叠白名单
- `envelope`: 世界包络，防穿地/飞出
- `holes`: `[{diameter,count,tolerance}]`
- `genus`, `closed`, `n_components`, `dihedral_max_deg`, `curvature_p95_deg`

## 验证语义

```
error 级检查失败 -> fail
只有 warn 级失败   -> degraded
没有失败检查       -> pass
```

receipt 包含：`spec_hash`、`mesh_hash`、`level`、`verdict`、`score`、
`checks[]`、`issues[]`、`metrics`、`facts`、`assumptions`、`exact:false`、
`method`。`issues[].fix` 可以直接变成下一轮参数或 spec 修改。

## 交付

- `atta_deliver` 先 verify；`fail` 永远 block；`degraded` 需
  `allow_degraded=true`，并在回执/manifest 里留下原因。
- 交付物：GLB / OBJ / STL + `<name>_manifest.json`，带 sha256 和 verify
  receipt。
- GLB/OBJ 是三角化派生物；几何真值由 spec 和 measured facts 保证。

## 常见错误与修法

| 现象 | 原因 | 修法 |
|---|---|---|
| `engine_unavailable` | 引擎未实现/未安装 | 先 `atta_doctor`，修 `atta.engine` 或 `pip install -e .` |
| `bad_arguments` | 字段名/类型/枚举不对 | 查 `atta_catalog` 的 schema |
| `file_not_found` | 产物路径不存在 | 先 model/preview，再 deliver |
| `timeout` | 渲染/导出确实很久 | 提高 timeout，或拆成 preview + deliver 两步 |
| `degraded` | 壁厚或 warn 级检查没过 | 加厚/改参数；确认可接受才 `allow_degraded=true` |
| `fail` | error 级检查失败 | 读 `issues[].fix`，改 spec/params 后重新 verify |

## 不要做

- 不要凭“体积看起来对”跳过 verify。
- 不要用 degraded 当 pass。
- 不要把壁厚采样当全模型证明。
- 不要手工拼 GLB/STL 或绕过 `atta_deliver`。
- 不要在 spec 里写 Python/eval。
- 不要用 `atta_run` 嵌套 `atta_run`。

## 长任务

`atta_model`、`atta_variant`、`atta_preview`、`atta_deliver`、`atta_run` 是长
操作。宿主会按 600/1200/3600/600/1800 秒给上限；“作业已提交”不等于“建模
完成”，仍以 `model_id`、`facts`、`verify` 回执为准。
