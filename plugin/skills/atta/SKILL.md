---
name: atta
description: Atta v1.2 参数化建模与交付：用 recipe 或 Spec v3（JSON）驱动 numpy/Blender 内核，做零件、回转体、曲面、装配、参数求解、结构测量、三态数值验收与 GLB/OBJ/STL/STEP 交付。适用于用户要求建 3D 模型、改尺寸、量壁厚/间隙/体积、批量比较方案、导出 CAD 文件，或需要"建完能证明它对"的场景。
---

# Atta v1.2 — 建模出好东西

你的目标是做出**好看且可验证**的模型，不是跑通工具。本文件是唯一权威。
细节按需读 `references/`（不要一开始全读）。

单位：公开参数/约束/探针一律 **mm + deg**；内核和 Blender 场景是**米**。
转换只在边界发生，你**永远**不要在参数里做米制换算。
坐标：`+Z` 向上，`+X` 向右，`+Y` 向后。回转体轴线默认 `+Z`，底面落在 `z=0`。

## 十条铁律

1. **先 `atta_doctor`**，一次即可。Blender 不可用时不要调 preview/deliver。
2. **先 `atta_catalog`**，不要猜 recipe 参数名或 feature 字段名。它默认返回**紧凑索引**
   （每页 14 条 recipe + 全部 op 名 + 材料名）；要看某个 recipe 的参数就
   `atta_catalog(recipe="product.mug")`，要看某个 op 的 args 就
   `atta_catalog(feature="revolve")`。**不要**随手 `verbose=true`（约 180 KB，会吃掉上下文）。
3. **先 spec，后几何**。构建前用 `atta_spec(op="validate")`。
4. **没有 `atta_verify` 的 `verdict=="pass"`，不许说完成**。`degraded` 不是 pass。
5. **先感知，后渲染**。形状没定之前只用 `describe(perceive=true)`（0.1–0.3 s，不起 Blender）。
6. **交付只走 `atta_deliver`**。不要手拼 GLB/STL，不要绕过 gate。
7. **假设不是测量**。`propose` 补的默认值进 `assumption_ledger`，关键尺寸（孔位、轮廓、壁厚）必须显式给。
8. **壁厚是射线采样**（`exact:false`）。不说"全模型最薄处的证明"。
9. **一次只改一个变量**，然后 `perceive` + `verify` 对比。不要一次改五个参数再猜是哪个起了作用。
10. **失败读 `error.fix` / `issues[].fix`**，那是可直接执行的补丁，不是道歉。

## 决策：recipe / graph / assembly 怎么选

按这个顺序问自己，命中就停：

| 问题 | 选择 | 例子 |
|---|---|---|
| `atta_catalog` 里有现成 recipe 吗？ | **recipe**（首选） | 杯碗花瓶瓶、齿轮法兰、支架外壳、穹顶面板、铭牌瓶盖、蜂窝板、晶格、海螺、凹槽柱、配合量块 |
| 标准件组合、需要零件间定位吗？ | **assembly**（`kind="assembly"`） | 螺栓+螺母、壳体+盖板+螺丝 |
| 能用 ≤8 个 feature 从基本体拼出来吗？ | **graph** | 带孔板、简单支架、转接块 |
| 是场/晶格/周期结构吗？ | **graph + `sdf`** | gyroid、Schwarz P、Mandelbulb |
| 以上都不是 | **graph**：revolve/loft/sweep/coons 打底再布尔 | 自由曲面件 |

**能套 recipe 就不要写 graph**——recipe 自带参数校验、预设、`expected_genus` 和
带 `fix` 的 `constraints`，graph 没有这些保护。preset：`safe` / `nominal` / `hero`，
先 `nominal`；极端比例用 `hero`；要过打印/装配保守值用 `safe`。

## 主循环（必须照这个顺序）

```
atta_doctor                                    # 一次，之后不再调
atta_catalog                                   # 紧凑索引：recipe id + 一行摘要 + op 名
  └ 选定 recipe → atta_catalog(recipe="<id>")  # 该 recipe 的完整参数 schema
  └ 写 graph   → atta_catalog(feature="<op>")  # 该 op 的 args
atta_spec(spec=..., op="validate")             # 便宜；字段错了这里就报
  └ 缺尺寸 → op="propose"  # 只补设计默认值，看 assumption_ledger / unresolved
atta_model(recipe=..., params=..., quality="draft")   # 返回 model_id + facts
atta_describe(perceive=true, res=24)           # 读 text + ASCII，不渲染
  └ 形状不对 → atta_model(recipe=..., patches={...}) → 再 perceive
  └ 形状对了 → atta_verify(level="standard")
      └ degraded/fail → 读 issues[].fix → 改参数 → 回 model
      └ pass → atta_preview(mode="studio", views=["hero"])  # 只看 1 张
atta_deliver(formats=["glb","obj"])            # 内部再 verify 一次
```

**快速循环（亚秒）**：`catalog → spec → model(draft) → describe(perceive) → model(patches)`。
形状还在动的时候**不要**调 `preview`——它要起 Blender，几十秒。

### refine 的正确写法

`patches` **必须和 `recipe` 或 `spec` 一起传**。只传 patches 会得到
`bad_arguments: atta_model needs recipe or spec`。

```json
{"recipe": "product.mug", "patches": {"height": 120, "wall": 3.2}}
```

不要重发整个 spec 只改一个数——用 `patches` 做增量，这样 `trace` 和
`applied_patches` 能看出你改了什么。

### ⚠ 参数越界会被静默 clamp

超出 recipe 声明范围的值**不会报错**，会被夹到边界，只在两处留痕：

```json
"applied_patches": [{"op":"clamp","path":"wall","value":6.0}]
"issues": [{"code":"param_out_of_range","severity":"warn","path":"wall",
            "message":"wall clamped to maximum 6.0","actual":12.0,
            "expected":"<= 6.0","fix":"wall = 6.0"}]
```

**每次 `model` 之后检查 `applied_patches` 与 `issues`。** 你以为设了 `wall=12`，
实际建的是 `wall=6.0`，而 `verify` 仍然会 `pass`——它验的是被夹过的几何。
"参数传了但形状没变"几乎总是这个原因，不是工具坏了。

### 感知包怎么读

`describe(perceive=true)` 返回 `perception`：`text`（一行摘要：尺寸/aspect/体积/
closed/genus/凸度/对称/孔估计/dihedral）、`views`（front/top/right ASCII，`#` 实体
`.` 空）、`sections`（Z 截面）、`traits`（`aspect` `sphericity` `convexity`
`symmetry` `holes_est` `sharp_edges_gt30`）、`signature`（可比较）。

`res` 取 16–40：24 够判断轮廓，40 才看得出小特征。`sections=[0.25,0.5,0.75]` 看三处截面。

迭代对比：`describe(model_id=<new>, compare_to=<old>, views=["front","top"])`
→ `diff.views[*].iou` + ASCII `+/-/#`，外加 `volume_delta_mm3` / `bbox_delta_mm` /
`topology_before,after`。**IoU 实测**（mug，front，基线 95）：+1 mm→0.989；
+5 mm→0.964；+15 mm→0.835；+35 mm→0.715。即 **>0.98 几乎没动，0.93–0.98 微调，
<0.90 明显形变**。别只看体积——配合 `topology_before/after` 看 genus/components。

## 做出"好看"的东西（这一节决定作品质量）

### 比例

`atta_catalog` 里每个 recipe 的默认参数就是**一组已验证过的比例**——先照抄默认值，
再按用户要求偏离。实测默认值的比例：

| 品类 | 默认 `高 / 直径` | 说明 |
|---|---|---|
| `product.mug` | 1.13（95 / 84） | 手感好的杯子区间；>1.5 显瘦，<0.9 显矮胖 |
| `product.bowl` | 0.44（65 / 148） | 碗本来就扁；口沿半径 / 高 ≈ 1.14 |
| `product.vase` | 1.59（165 / 104） | 花瓶偏修长 |
| `product.bottle` | 2.62（220 / 84） | 瓶类最高 |
| `part.bracket` | 长宽比 1.67（100 / 60） | 板件 3:2 附近最稳 |

- **先定一个主尺寸，其他从它派生。** 马克杯：`height=95`，`outer_radius=42`。
- 板件长宽比控制在 3:2 或 4:3；超过 5:1 会显得像条子。
- 对称优先。`perception.traits.symmetry` 里 `y: true` 是大部分产品的基线。
- 偏离默认比例时**一次只动一个比**，然后 `perceive` 看 `traits.aspect`。

### 壁厚（决定能不能造）

| 工艺 | 最小壁厚 | 建议 |
|---|---|---|
| FDM 打印 | 1.2 mm（=3 圈 0.4 喷嘴） | **2.0–3.0 mm** |
| SLA 树脂 | 0.8 mm | 1.5–2.5 mm |
| 注塑 | 1.5 mm | 2.0–3.0 mm，且尽量均匀 |
| 陶瓷（杯碗） | 3.0 mm | **3.5–4.5 mm**，口沿可薄到 2.5 |
| CNC 铝 | 1.0 mm | 2.0 mm 起，深腔另算 |

薄壁 + 大平面 = 翘曲。壁厚突变处一定加过渡，不要从 4 mm 直接跳到 1.5 mm。

### 圆角 / 倒角（决定"像不像 CG"）

- **每一道外露硬边都要有处理**。裸 90° 边是渲染看起来假的第一原因。
- 视觉件：圆角 `R = min(相邻两壁厚) × 0.3–0.5`，别超过短边的 1/4。
- 加工件：倒角 `C0.5–C1.0` 比圆角更符合工艺直觉，也更好加工。
- `finish={"bevel_mm":0.6,"smooth_deg":35}` — `bevel_mm` 是**毫米**，不是米。
  `smooth_deg` 35 是硬表面通用值；低于 30 会显得面片化，高于 45 会糊掉特征边。
- 装饰性曲线（花瓣、扭转）用 `revolve` + `lobes`，不要用大量布尔堆。

### 避免退化几何

- 两个面**恰好相切**的布尔会产出退化三角形 → `verify` 会报 `non_manifold`
  或 `degenerate_faces`。让 cutter **穿透**目标 0.5–1 mm。
- 孔不要正好落在边线上；孔心距边 ≥ 1.5 × 孔径。
- 圆柱 `segments` 64 起（视觉件 96），低于 32 会看到明显多边形。
- 极坐标阵列 `count > 1` 时要保证相邻 cutter 不重叠。

### 材质

`atta_catalog` 默认给的 `materials` 是 `name -> label`（47 种，`verbose` 才有 PBR）。
选材：杯碗 `ceramic`/`porcelain`；机加工 `aluminium`/`brass`；结构 `steel`/`stainless`；
打印 `abs`/`nylon`/`resin`；透明 `glass_clear`；木 `wood`。
**不要编材质名**——未知材质报 `bad_arguments`，`message` 会列出全部合法名。

材质影响 preview 观感，**不影响 verify 判决**。不要为了让图好看改材质来掩盖几何问题。

## 参数求解：不要手调

用户给的是**指标**（"高 100 mm"、"壁厚至少 2.5"）而不是参数值时，用
`atta_variant` 反解，不要自己试：

```json
{"recipe": "product.mug",
 "targets": {"bbox_z": 100, "min_wall_mm": {"min": 2.5}},
 "constraints": {"volume_mm3": {"max": 250000}},
 "max_evaluations": 120}
```

可解目标（measured facts）：`bbox_x/y/z`、`volume_mm3`、`min_wall_mm`、
`genus`、`closed`、`n_components`、`dihedral_max_deg`、`curvature_p95_deg`。

返回 `feasible` / `solution`（含 `model_id`）/ `closest` / `sensitivity` /
`recommendations.side_effects` / `pareto`。
**不可行时报告最近可达点和放松代价，绝不编一个通过的模型。**

## 验收语义

```
error 级检查失败  → fail
只有 warn 级失败   → degraded
没有失败检查      → pass
```

receipt 关键字段：`verdict`、`pass`、`score`、`level`、`checks[]`、`issues[]`、
`metrics`、`facts`、`assumptions`、`exact:false`、`method`、`next`。
`issues[].fix` 可直接变成下一轮 patches。

`level` 三档：`basic`（只查拓扑/空）→ `standard`（默认，加体积/bbox/壁厚 warn）
→ `strict`（壁厚升为 error）。**声称"可制造"时用 `strict`。**

约束在 `verify(constraints={...})` 里给：`bbox` `{x:[min,max]}`、`volume_mm3`、
`min_wall_mm`、`clearance` `[{a,b,min}]`、`interference`、`envelope`、`holes`、
`genus`、`closed`、`n_components`、`dihedral_max_deg`、`curvature_p95_deg`。

## 交付

`atta_deliver` **先在内部跑 standard verify**：`fail` 直接 block，`degraded` 需
显式 `allow_degraded=true`。产物：GLB / OBJ / STL / BLEND + `<name>_manifest.json`
（含 sha256 和 verify receipt）。图形子集支持 `formats=["step"]` 出解析 B-rep；
不支持的 op 报 `brep_unsupported` 并给 fix，**不会**伪造一个三角化 STEP。

GLB/OBJ 是三角化派生物。几何真值由 spec + measured facts 保证，不是网格本身。

## 常见错误 → 修法

| 现象 | 原因 | 修法 |
|---|---|---|
| `catalog` 看不到某 recipe | 默认一页 14 条 | `offset=<next_offset>` 翻页，或 `catalog(recipe="<id>")` |
| context 爆掉 | 调了 `catalog(verbose=true)` | 用 `recipe=` / `feature=` 精确取（verbose ≈180 KB） |
| `atta_model needs recipe or spec` | 只传了 `patches` | 补上 `recipe=` 或 `spec=` |
| `bad_arguments`（其它） | 字段名/类型/枚举错 | 查 `atta_catalog`；`_clean_args` **静默丢弃** schema 外的键，故"传了没生效"多是名字错 |
| 参数传了但几何没变 | 越界被 clamp / 未知名被忽略 | 读 `applied_patches` 与 `issues[]`；`unknown_param.fix` 列出全部合法参数名 |
| `degraded` | warn 级没过（壁厚等） | 加厚/改参；或 `level="strict"` 复验确认；确可接受才 `allow_degraded=true` |
| `fail` | error 级没过 | 读 `issues[].fix`，改参数后重新 verify |
| `recipe_blocked` | 违反 recipe 内部约束 | 读 `error.details[].fix`，多为 `wall <= min(base_radius,top_radius)*0.22` 之类关系式 |
| `unknown_recipe` / `no_models` | id 错 / 工作区空 | `fix` 里直接列出合法 recipe id；或先 `atta_model` |
| `engine_unavailable` / `blender_unavailable` | 引擎/Blender 缺 | `atta_doctor`；`pip install -e ".[all]"`；draft 几何不依赖 Blender |
| `timeout` / `lock_timeout` | 活太久 / 同 workspace 占锁 | 拆步或提高 timeout；等待，或换 `ATTA_HOME` |

完整错误码与逐步排查见 `references/troubleshooting.md`。
注意：`verify` 个别 fix 文案提到 `atta_solve`——**没有这个工具**，对应能力是
`atta_variant`（带 `targets`）。

## 长任务与红线

`atta_model` / `atta_variant` / `atta_preview` / `atta_deliver` / `atta_run` 是长操作，
宿主上限 600 / 1200 / 3600 / 600 / 1800 秒。"作业已提交" ≠ "建模完成"。

绝不做：凭"体积看起来对"跳过 verify · 把 `degraded` 说成 `pass` ·
把壁厚采样说成全模型证明 · 手拼 GLB/STL 绕开 `atta_deliver` ·
在 spec 里写 Python/`eval` · 用 `atta_run` 嵌套 `atta_run` ·
形状未定时调 `preview` · 一次改多个参数再猜原因 · 把渲染图当验收证据。

## 参考（按需读，不要全读）

- `references/spec-v3.md` — Spec v3 全字段、25 个 graph op、`$param`/`$expr`
- `references/solving.md` — `atta_variant` 求解、Pareto、装配级求解
- `references/assembly.md` — parts/ports/mates、DOF、装配 verify 与交付
- `references/troubleshooting.md` — 错误码全表与逐步排查
- `references/quality.md` — "优秀"的 13 条自检（交付前过一遍）
