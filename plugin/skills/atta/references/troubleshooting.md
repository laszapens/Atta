# 排错

失败**是值不是异常**：每个 call 都返回 `{ok, tool, data, error, ms}`。
`ok:false` 时读 `error.code` / `error.path` / `error.fix`——`fix` 是可执行补丁。

## 错误码全表

| code | 含义 | 可执行修法 |
|---|---|---|
| `unknown_tool` | 工具名拼错 | 用十动词之一 |
| `bad_arguments` | 参数名/类型/枚举错 | 查 `atta_catalog` schema。**注意：`_clean_args` 会静默丢弃 schema 外的键**，所以"传了没反应"通常是键名错 |
| `engine_unavailable` | `atta.engine` 无法导入 | `atta_doctor`；`pip install -e ".[all]"` |
| `no_models` | 工作区没有模型记录 | 先 `atta_model`，或传有效 `model_id` |
| `bad_record` | 记录里没有可重建的 spec | 重新 `atta_model`（记录存 spec，不存网格） |
| `rebuild_failed` | spec 已无法重建 | 看 `details` 里的 compile errors，修 spec |
| `recipe_blocked` | 参数违反 recipe 内部约束 | `details` 里每条都带 `fix`，通常是关系式（如 `wall = min(base_radius,top_radius)*0.22`） |
| `blender_unavailable` | 没装 Blender / 不在 PATH | draft 几何不受影响；设 `ATTA_BLENDER` 或装 Blender 4.2+ |
| `deliver_failed` | 导出阶段失败 | 看 `_atta/blender/blender.log` |
| `brep_unsupported` | 该 op 无解析 B-rep 映射 | 去掉 `formats=["step"]`，或用受支持的 op 重写（box/cylinder/sphere/cone/torus/revolve/extrude/布尔/transform/mirror/pattern） |
| `lock_timeout` | 同 workspace 有别的构建占锁 | 等待，或换 `ATTA_HOME` |
| `timeout` | 超时 | 提高 timeout，或拆分（单视图 preview + deliver） |
| `file_not_found` | 产物路径不存在 | 先 model/preview 生成，再重试 |
| `internal_error` | 引擎 bug | `atta_doctor --deep`，用最小 args 复现 |

## 三态判决不是二态

```json
{"verdict": "pass",     "pass": true}   // 唯一可宣称完成的状态
{"verdict": "degraded", "pass": false}  // 只有 warn 级失败；不是 pass
{"verdict": "fail",     "pass": false}  // 有 error 级失败
```

常见 `degraded` 来源：壁厚低于 `min_wall_mm`（standard 下是 warn）、
`dihedral_max_deg` 略超、`curvature_p95_deg` 略超。

处理顺序：
1. 读 `issues[].severity`——只有 `warn` 才是真 degraded。
2. 读 `issues[].fix`，通常直接是参数修改（"加厚到 X"）。
3. 改完重新 `model` + `verify`。
4. 确实无法满足且业务可接受，才 `deliver(allow_degraded=true)`，并在回复里写明原因。

## 迭代式排查（省 token）

```
1. atta_doctor                    # 环境问题一次查清
2. atta_catalog                   # 参数名/op 名不对，这里查
3. atta_spec(op="validate")       # spec 结构问题，不建几何
4. atta_model(quality="draft")    # 建
5. atta_describe(perceive=true)   # 形状对不对，不渲染
6. atta_verify(level="standard")  # 拓扑/尺寸对不对
7. atta_preview(1 view)           # 只在需要判断外观时
8. atta_deliver                   # 最后
```

**不要跳步。** 尤其在 shape 还没定时调 preview——它起 Blender，几十秒，
而且渲染对形状问题没有诊断价值。

## describe 的 compare_to 怎么用

```json
{"model_id": "<new>", "compare_to": "<old>", "views": ["front","top"]}
```

实测标定（`product.mug`，front 视图，基线 `height=95 mm`）：

| 改动 | front IoU | volume_delta |
|---|---|---|
| +1 mm | 0.989 | +802 mm³ |
| +5 mm | 0.964 | +4 010 mm³ |
| +15 mm | 0.835 | +12 047 mm³ |
| +35 mm | 0.715 | +28 168 mm³ |

- `iou > 0.98` → 几乎没动
- `0.93–0.98` → 微调
- `< 0.90` → 明显形变

配合 `diff.topology_before` / `topology_after` 看 genus/components 有无变化。
ASCII `+/-/#`：`#` 两者都有，`+` 新增，`-` 移除。

`trace=true` 返回 `trace.steps[]`，每步有 `id` / `op` / `bbox_mm` / `volume_mm3` /
`closed` / `genus` / `components`。用它定位"是哪一步把形状带偏了"——
比反复 compare_to 更快。

## 已知文案问题

`verify` 某些 `fix` 里写 `use atta_solve`——**没有这个工具**。
对应能力是 `atta_variant`（带 `targets` 参数）。
遇到这类 fix，把它翻译成 `atta_variant(targets={...})` 再执行。
