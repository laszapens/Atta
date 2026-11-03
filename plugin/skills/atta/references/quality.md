# 质量线：交付前自检

完整版见仓库 `docs/QUALITY.md`。这里是压缩到可执行的 13 条。

## 三条否决项

1. 没有 `verdict=="pass"` 就说完成 → 否决
2. 把 `degraded` 说成 `pass` → 否决
3. 用渲染图当水密/达标证据 → 否决

## 13 条自检

```
[ ] doctor 通过，Blender 状态已知
[ ] 用 recipe 而不是手写 graph（除非确实需要）
[ ] spec validate 通过
[ ] model 返回 model_id
[ ] describe: closed / genus / n_components 符合预期
[ ] describe: aspect 符合设计比例
[ ] verify verdict == "pass"（不是 degraded）
[ ] 关键尺寸有 constraint 覆盖，不是"看起来对"
[ ] 壁厚 ≥ 工艺最小值；声称可制造时用 strict
[ ] 所有外露硬边有 bevel / 圆角
[ ] preview 最多 1-2 张（如果看了）
[ ] deliver status == "ok"
[ ] 回复含 model_id + verdict + 尺寸 + 路径
```

## 几条容易忘的

- **genus**：一个通孔板是 genus 1。对不上 recipe 的 `expected_genus` 就是几何错。
- **壁厚不是证明**：`min_wall_mm` 是射线采样（`exact:false`），别说成"全模型最薄处"。
- **裸边**：所有外露 90° 边都要 `bevel_mm`。这是渲染显假的第一原因，也最便宜。
- **单位**：`facts.bbox_mm.size` 是 mm。看到 `0.1167` 就有一层米/毫米混淆。
- **degraded 交付**：必须明说哪条检查没过、为什么可接受。

## 坏回复 vs 好回复

```
✗ 我已经成功创建了您的杯子，看起来很棒！
✓ product.mug 已建：Ø83.1 × 95.0 mm，壁厚 3.5 mm，体积 93.7 ml，
  verify pass（standard，0 issue）。GLB: _atta/out/.../product.mug.glb
```
