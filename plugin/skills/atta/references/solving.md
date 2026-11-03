# 参数求解（atta_variant）

用户给**指标**而不是参数值时用它。不要手调猜参。

## 两种模式

| 模式 | 触发 | 作用 |
|---|---|---|
| 采样 | 无 `targets`、`solve` 不为 true | 在 `ranges` 里采样 `count` 个候选并排序 |
| 求解 | 有 `targets` 或 `solve=true` | 建真实草稿网格，解约束，给可行解 |

```json
// 采样
{"recipe": "product.mug", "count": 6,
 "ranges": {"height": [80, 110]},
 "objective": {"volume_mm3": "max"}}

// 求解
{"recipe": "product.mug",
 "targets": {"bbox_z": 100, "min_wall_mm": {"min": 2.5}},
 "constraints": {"volume_mm3": {"max": 250000}},
 "max_evaluations": 120}
```

## targets 语法

`metric -> value | [min,max] | {target,tol} | {min,max}`

```jsonc
{"bbox_z": 100}                      // 目标值
{"volume_mm3": [200000, 260000]}     // 区间
{"bbox_x": {"target": 80, "tol": 1.5}}
{"min_wall_mm": {"min": 2.5}}        // 下界（不改语义）
```

可用 metric（实测 `atta_catalog.solve.metrics`）：
`bbox_x` `bbox_y` `bbox_z` `volume_mm3` `area_mm2` `min_wall_mm` `genus`
`closed` `n_components` `dihedral_max_deg` `curvature_p95_deg`

`constraints` 用 verify 同口径的键，语义保持不变（`min_wall_mm` 是下界，
`dihedral_max_deg` 是上界）。

## 求解策略

`strategy`：`auto`（默认推荐）`lhs` `compass` `nelder-mead`
`differential-evolution` `local` `pareto`

- `auto` 会按变量数与是否有离散指标选。
- `pareto=true` 出权衡前沿（两个目标冲突时）。
- `initial` 给起点；`free` / `fixed` 圈定自由变量。

## 返回体怎么读

| 字段 | 含义 |
|---|---|
| `feasible` | 是否找到满足全部 targets 的解 |
| `solution` | 可行解，含 `params` 与 `model_id` |
| `best` | 目标函数最优的候选（可能不可行） |
| `closest` | 不可行时最近可达点 |
| `diagnostics.violation` | 归一化违约量 |
| `diagnostics.violations` | 每条 target 的违约明细 |
| `diagnostics.budget_exhausted` | 是否用完评估预算（**true 时结论不可信**） |
| `sensitivity` | 各参数对目标的敏感度 |
| `recommendations` | 含 `side_effects`（放松某目标会付出什么） |
| `pareto` | 前沿点集 |
| `candidates` | 全部评估过的候选 |

**`feasible=false` 时不返回可用的 `model_id`。** 报告 `closest` + 放松代价，
绝不编一个"看起来通过"的模型。

`diagnostics.budget_exhausted=true` 时先加大 `max_evaluations` 再下结论——
预算是瓶颈，不是几何不可能。

## 装配级求解

```json
{"base": "<assembly model_id>",
 "targets": {"dof": 0, "collisions": 0},
 "free": ["cap.height", "body.wall"]}
```

变量用 `part.param` 寻址；额外 metric：`dof`、`collisions`、`interference_mm3`、
`min_clearance_mm`、`mate_conflicts`。

## 成本

求解会建**真实草稿网格**，每次评估都是一次 `model(draft)`。所以：

- 先用小 `count` + 小 `max_evaluations` 探路
- 变量越少越快：用 `free` 只放开真正要变的 2–4 个
- 求解完不需要再 `model` 一次，`solution.model_id` 就是可用模型
