# Spec v3 参考

按需读。主 skill 已给最小可用写法；这里给完整字段。

## 顶层结构

```json
{
  "schema": "atta.spec/v3",
  "name": "mug-01",
  "model": { "kind": "recipe" | "graph" | "assembly", ... },
  "quality": "draft" | "review" | "deliver",
  "material": "ceramic",
  "finish": {"bevel_mm": 0.6, "smooth_deg": 35},
  "constraints": {...},
  "assumptions": []
}
```

- `schema` 必须是 `"atta.spec/v3"`。
- `quality`：`draft`（numpy，亚秒）→ `review` → `deliver`（走 Blender）。
- `finish.bevel_mm` 单位**毫米**；`smooth_deg` 是平滑角阈值。
- `constraints` 与 `verify(constraints=...)` 同口径，见主 skill。

## model.kind = "recipe"

```json
{"kind": "recipe", "id": "product.mug", "preset": "nominal",
 "params": {"height": 95, "wall": 3.5}}
```

- `id` 必须是 `atta_catalog` 里存在的 id。
- `preset`：`safe` / `nominal` / `hero`。
- `params` 的键名必须是该 recipe 声明过的；未知键会被 recipe 的 resolve 流程
  记录进 `applied_patches`，不要依赖它生效。
- recipe 自带的 `constraints`（形如 `wall <= min(base_radius,top_radius)*0.28`）
  违反时会 raise `recipe_blocked`，`details` 里带 `fix`。

## model.kind = "graph"

```json
{"kind": "graph",
 "units": "mm",
 "segments": 64,
 "output": "cut",
 "features": [
   {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
   {"id": "h1", "op": "cylinder", "args": {"radius": 4, "height": 20,
                                            "center": [20, 25, 3]}},
   {"id": "cut", "op": "difference", "inputs": ["plate", "h1"]}
 ]}
```

- `output` 指定哪个 feature 作为最终结果；省略时取最后一个。
- `features[].id` 必须唯一；`inputs` 引用上游 id。
- 每个 feature：`{"id", "op", "args", "inputs"}`。
- `args` 里的长度都是 **mm**；`units` 默认 `mm`。

### 全部可用 op（25 个，`atta_catalog` 实测）

| 类别 | op |
|---|---|
| 基本体 | `box` `cylinder` `sphere` `cone` `torus` |
| 曲面 | `revolve` `loft` `sweep` `coons` `gordon` `extrude` `thicken` `offset` |
| 布尔 | `union` `difference` `intersection` |
| 阵列/变换 | `mirror` `transform` `pattern_linear` `pattern_polar` |
| 装饰 | `lobes` `text_emboss` `text_engrave` |
| 隐式场 | `sdf` `gyroid` |

### 常用 args

```jsonc
{"op":"revolve","args":{"profile_rz":[[0,0],[30,0],[42,60],[38,95],[0,95]],
                        "segments":96}}
{"op":"loft","args":{"rings":[[[x,y,z],...], [...]]}}
{"op":"sweep","args":{"section_xy":[[x,y],...],"path":[[x,y,z],...],
                      "twist_deg":0,"scale_end":1.0}}
{"op":"coons","args":{"edges":[bottom,right,top,left]}}
{"op":"gordon","args":{"u_curves":[...],"v_curves":[...]}}
{"op":"extrude","args":{"loop_xy":[[x,y],...],"height":6,"holes_xy":[[[x,y],...]]}}
{"op":"pattern_polar","args":{"count":12,"axis":"z","angle_deg":360}}
{"op":"lobes","args":{"count":12,"amplitude":0.03,"phase_deg":0,"twist_deg":0}}
{"op":"sdf","args":{"size":[40,30,20],"field":"gyroid","period":30,"thickness":3.6}}
```

- `revolve.profile_rz` 是 `[r, z]` 点列（**不是** `[x,z]`）。首末点若 `r=0`
  会自动封轴；若首末 `r>0` 则开口。
- `pattern_linear` / `pattern_polar` 的 `inputs` 是**一个** mesh。
- `sdf` 的 `field`：`gyroid` `schwarz_p` `neovius` `mandelbulb` `sphere` `box` `torus`；
  `clip` 取 `box` 或 `sphere`；`resolution` 决定网格密度（12–32，越高越慢）。

### 参数化引用

`args` 里任何数值可换成：

- `{"$param": "name"}` — 引用 spec 的 `params_m` / 外部参数
- `{"$expr": "w/2 + 4"}` — 受限算术（`+ - * / ** ( )` 与数字/变量），
  **不允许** `eval`、属性访问、下标、函数调用

## model.kind = "assembly"

```json
{"kind": "assembly",
 "parts": [
   {"id": "body", "model": {"kind":"graph","output":"out","features":[...]}},
   {"id": "cap",  "model": {"kind":"recipe","id":"product.screw_cap",
                            "params":{}}}
 ],
 "ports": [{"part":"body","id":"top","origin_mm":[0,0,60],
            "axis":[0,0,1],"radius_mm":20}],
 "mates": [{"id":"m1","type":"coincident","a":"body.top","b":"cap.bottom"}]}
```

详见 `assembly.md`。

## 校验与编译

| op | 作用 | 会不会建几何 |
|---|---|---|
| `validate` | 校验 + 归一化，返回 `errors[]` | 否 |
| `validate` + `assume=true` | 先 propose 再 validate | 否 |
| `propose` | 补设计默认值，返回 `assumption_ledger` / `unresolved` | 否 |
| `compile` | 解析成最终 params/features | 否 |
| `schema` | 返回字段文档 | 否 |

`propose` 只填**可解释的默认值**。`unresolved` 里的东西（`hole.at`、
`revolve.profile`、`loft.sections`、`sweep.path`）必须由你显式给出——
它们是几何的关键定义，不是可以猜的默认值。

## 常见 spec 错误

| 错误 | 原因 |
|---|---|
| `spec.schema must be 'atta.spec/v3'` | schema 字符串写错 |
| `unknown feature op` | op 不在 25 个里 |
| `duplicate_id` | 两个 feature 同 id |
| `unknown_input` | `inputs` 引用了不存在的 id |
| `cycle` | feature 依赖成环 |
| `bad_arg` | args 字段名/类型错，`path` 指向具体字段 |
