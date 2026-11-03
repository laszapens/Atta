# Atta 质量线：什么叫"优秀"

这份文档是**可自检**的。每次 `atta_deliver` 之前，逐条过一遍。
"看起来不错"不算通过——每条都必须有**可复现的数字或回执**作证。

贯穿全部的一条：**优秀 = 好看 且 能证明它对**。只有一项不叫优秀。

---

## 0. 三条否决项

违反任何一条，无论其余多好都不算完成：

- [ ] **没有 `atta_verify` 的 `verdict=="pass"`**，就说"完成"——否决。
- [ ] 把 `degraded` 说成 `pass`——否决。
- [ ] 用渲染图/截图当作"水密/达标"的证据——否决。

---

## 1. 几何有效性（硬门槛）

| 检查 | 通过标准 | 证据来源 |
|---|---|---|
| 闭合流形 | `closed=true`，`boundary_edges=0` | verify `checks[]` |
| 无非流形边 | `non_manifold_edges=0` | verify `checks[]` |
| 绕向一致 | `inconsistent_winding_edges=0` | verify `checks[]` |
| 无退化面 | `degenerate_faces=0` | describe `facts.topology` |
| 零件数 | 单件 `n_components=1` | describe `facts` |
| 拓扑符合预期 | `genus` 等于 recipe 的 `expected_genus`，或你的设计意图 | describe/verify |
| 体积非零 | `volume_mm3 > 0` | describe `facts` |

**genus 最容易被忽略**：带一个通孔的板是 genus 1，不是 0。
`atta_catalog` 里每个 recipe 都声明了 `expected_genus`——对不上就是几何错了。

自检话术：如果我不能用一句话说清这个模型"有几个洞、几个零件"，说明我还没读懂它。

---

## 2. 尺寸正确性

- [ ] 关键尺寸有**显式约束**，不是"我看大约对"

```json
atta_verify(constraints={
  "bbox": {"x": [78, 82], "z": [59, 61]},
  "volume_mm3": [40000, 46000],
  "min_wall_mm": 2.5
})
```

- [ ] 用户给的指标（"高 100 mm"）在 `facts.bbox_mm.size` 里确实命中，
      或由 `atta_variant(targets=...)` 解出来的 `solution` 满足
- [ ] 单位是 mm——`facts.bbox_mm.size` 里的 `[116.7, 83.1, 95.0]` 就是
      116.7 mm 宽。如果看到 `0.1167`，说明有一层米/毫米混淆

---

## 3. 可制造性

### 壁厚

| 工艺 | 最小 | 我们要求 |
|---|---|---|
| FDM | 1.2 mm | ≥ 2.0 mm，均匀 |
| SLA | 0.8 mm | ≥ 1.5 mm |
| 注塑 | 1.5 mm | 2.0–3.0 mm，且厚薄比 < 2:1 |
| 陶瓷 | 3.0 mm | ≥ 3.5 mm |
| CNC | 1.0 mm | ≥ 2.0 mm |

- [ ] `verify` 里 `min_wall_mm` 检查通过
- [ ] 声称"可制造"时用的是 `level="strict"`（standard 下壁厚只是 warn）
- [ ] 不把 `min_wall_mm` 说成"全模型最薄处的证明"——它是射线采样近似（`exact:false`）

### 打印/加工的常识项

- [ ] 没有悬垂超过 45° 的大平面（除非明确要用支撑）
- [ ] 深腔的深宽比 < 3:1（FDM）/ < 5:1（CNC）
- [ ] 孔心距边 ≥ 1.5 × 孔径（否则边缘崩）
- [ ] 相邻特征间距 ≥ 最小壁厚

---

## 4. 比例与美学

这一节最主观，所以要落成可数的规则。

### 主比例

- [ ] **有一个明确的主尺寸**，其余从它派生
- [ ] 高径比在合理区间：
  - 杯：1.0–1.5（1.13 附近手感最好）
  - 碗：0.5–0.9
  - 瓶/花瓶：1.5–3.0
  - 板件：长宽比 ≤ 5:1，优选 3:2 或 4:3
- [ ] `perception.traits.aspect` 的三元组读出来符合设计意图（不是意外）

### 对称

- [ ] `perception.traits.symmetry` 里，产品品应有的对称轴是 `true`
- [ ] 非对称是**刻意的**（比如把手、单侧接口），不是建模误差

### 曲率连续性

- [ ] 没有不该出现的锐利折线（`traits.sharp_edges_gt30` 数量与设计特征数一致）
- [ ] 回转体的 profile 点列在转折处有过渡，不是硬折

自检话术：把这个模型放进同类产品目录，它会显得廉价吗？如果会，通常是
**比例失衡**或**边缘没处理**。

---

## 5. 圆角/倒角（"像不像 CG"的关键）

- [ ] **所有外露硬边都有处理**：至少 `bevel_mm=0.5`，视觉件 0.8–1.5
- [ ] 圆角半径 ≤ 相邻最短边的 1/4（否则吃掉特征）
- [ ] 圆角半径 ≥ 最小壁厚的 0.3（否则加工不出来）
- [ ] `finish.smooth_deg` 取 30–40（硬表面 35 是通用值）
- [ ] 渲染里看不到"纸片感"的零厚度边

裸 90° 边是渲染看起来假的第一原因。这一条最便宜、收益最大。

---

## 6. 外观判断（preview，只在需要时）

- [ ] 只在形状已经通过 verify **之后**才 preview
- [ ] 最多看 1–2 张；不要一次渲染 9 个机位
- [ ] 展示用 `mode="studio"` 或 `"catalog"`；判断形状用 `describe`，不用 preview
- [ ] 明确区分：preview 是**判断辅助**，不进 verify 判决

`atta_preview` 的 `analysis` 块（曝光/CIELAB/对比度/边缘密度）是**告警**，
不是质量分。看到告警不代表模型不合格，只代表图不好看。

---

## 7. 交付

- [ ] `atta_deliver` 返回 `status: "ok"`（不是 `"blocked"`）
- [ ] `manifest_path` 存在且可读
- [ ] manifest 里有 `sha256` 与 verify receipt
- [ ] `files[]` 里每个路径都真实存在
- [ ] 需要 STEP 的场合才加 `formats=["step"]`；失败时接受 `brep_unsupported`，
      **不要**用三角化网格伪装 STEP

---

## 8. 回答质量（给用户的回复）

- [ ] 报了 `model_id`、`verdict`、关键尺寸（mm）、产物路径
- [ ] 有数字，不是"已经建好了"
- [ ] 如果 `degraded` 交付了，**明说**哪个检查没过、为什么可接受
- [ ] 不叙述整个工具调用日志
- [ ] 先给最小有用证据（一行 facts 或一张图），再给细节

坏回复 vs 好回复：

```
✗ 我已经成功创建了您的杯子，看起来很棒！
✓ product.mug 已建：Ø83.1 × 95.0 mm，壁厚 3.5 mm，体积 93.7 ml，
  verify pass（standard，0 issue）。GLB: _atta/out/.../product.mug.glb
```

---

## 9. 完整自检清单（复制到工作区用）

```
[ ] doctor 通过，Blender 状态已知
[ ] 用 recipe 而不是手写 graph（除非确实需要）
[ ] spec validate 通过
[ ] model 返回 model_id
[ ] describe: closed / genus / n_components 符合预期
[ ] describe: aspect 符合设计比例
[ ] verify verdict == "pass"（不是 degraded）
[ ] 关键尺寸有 constraint 覆盖，不是"看起来对"
[ ] 壁厚 ≥ 工艺最小值，且声称可制造时用了 strict
[ ] 所有外露硬边有 bevel/圆角
[ ] preview 最多 1-2 张（如果看了）
[ ] deliver status == "ok"
[ ] 回复含 model_id + verdict + 尺寸 + 路径
```

13 条全过，才算"优秀"。任何一条打不了勾，就先修它，不要交付。
