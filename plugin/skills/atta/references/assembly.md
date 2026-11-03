# 装配（multi-part assembly）

单件用 `model.kind="recipe"|"graph"`；**需要零件间相对定位**才用 assembly。
不要把一个多零件产品做成单个合并网格再叫它装配。

## 结构

```json
{"schema": "atta.spec/v3", "name": "clamp",
 "model": {"kind": "assembly",
   "parts": [
     {"id": "base", "model": {"kind": "graph", "output": "out",
       "features": [{"id":"out","op":"box","args":{"size":[80,50,10]}}]}},
     {"id": "cap", "model": {"kind": "graph", "output": "out",
       "features": [{"id":"out","op":"box","args":{"size":[80,50,6]}}]}}
   ],
   "ports": [
     {"part": "base", "id": "up", "origin_mm": [0,0,10],
      "axis": [0,0,1], "radius_mm": 25},
     {"part": "cap", "id": "down", "origin_mm": [0,0,0],
      "axis": [0,0,1], "radius_mm": 25}
   ],
   "mates": [
     {"id": "m1", "type": "coincident", "a": "base.up", "b": "cap.down"}
   ]}}
```

## ports

声明一个装配参考坐标系：`{part, id, origin_mm, axis, radius_mm, normal?}`。
也可以不声明，用自动端口：`center` / `axis_x|y|z` / `face_*`。

mate 的 `a` / `b` 写成 `"<part>.<port>"`。

## 13 种 mate

`coincident` `concentric` `axis` `plane` `distance` `angle` `tangent`
`gear` `insert` `parallel` `perpendicular` `lock` `point_on_plane`

常用：

```jsonc
{"type":"coincident","a":"base.up","b":"cap.down"}
{"type":"concentric","a":"shaft.axis","b":"hole.bore"}
{"type":"distance","a":"base.up","b":"cap.down","value_mm":2.0}
{"type":"angle","a":"arm.axis","b":"base.x","value_deg":30}
```

## DOF

`model` 返回 `dof`（来自 mate Jacobian 的秩，不是猜的）与
`fully_constrained`。**`dof > 0` 说明还有自由度没约束住**——
`verify` 会把它当门（gate）。

不是"能拼上就算对"：欠约束的装配会在下游漂移。

## 装配 verify

比单件多查：

- 每个 part 各自是闭合实体（per-part solids）
- 每条 mate 的残差 `residual_mm`
- DOF 门
- broadphase 碰撞（`collisions`）
- clearance / envelope

返回的是**装配 receipt**，里面按 part、按 mate 分列。单件 receipt 不能替代它。

## 装配 describe

`describe` 在装配上额外给 `assembly.part_count` / `parts` / `mates`（含
`residual_mm`）/ `dof` / `conflicts` / `collision` / `clearance` / `placements`。

## 装配 deliver

写**按零件命名的 GLB** + 每个零件的 OBJ/STL + assembly receipt。
不是把整装配合成一个网格。

## 常见问题

| 现象 | 原因 | 修法 |
|---|---|---|
| `dof > 0` | mate 不足或端口选错 | 加 mate，或换 `lock` 兜底 |
| mate `residual_mm` 大 | 端口几何上无法同时满足 | 检查端口轴是否平行；用 `distance` 代替硬 `coincident` |
| `collisions` 非零 | 零件实际穿模 | 加 clearance，或改端口偏置 |
| `conflicts` 非空 | mate 之间互相矛盾 | 去掉过约束的那条 |
