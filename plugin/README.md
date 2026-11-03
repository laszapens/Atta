# @atta/dsh-atta

Atta v1.1 的 DSH 无头建模插件。它不复制几何内核，只把 Atta 的十个动词包成
DSH 工具：

- recipe / Spec v3 / feature graph -> numpy geometry -> Blender finish
- 结构事实：bbox、体积、质心、拓扑、壁厚、曲率、ray/silhouette/section/clearance 探针
- 三态 verify（`pass | degraded | fail`）+ receipt + `issues[].fix`
- 参数采样 / targets 约束求解 / Pareto / fit（`atta_variant`）
- 交付 gate：verify 通过后导出 GLB/OBJ/STL/BLEND（图形子集另有 STEP）+ manifest
- 视觉：按零件材质、摄影棚灯光、微距 focus、normal/depth/id/curvature/deviation 通道

## 安装

```bash
# 前置：Atta v1.1 可 import
# $ATTA_ROOT = 本仓库根目录
export ATTA_ROOT=/path/to/Atta-v1.1
cd "$ATTA_ROOT"
python3 -c "import atta"

# 更新 ~/.dsh/profiles/* 的依赖、bundle 与 symlink
python3 -m atta.agent.install dsh           # 预览要改哪些 profile
python3 -m atta.agent.install dsh --create  # 实际写入
# 重启 DSH 后生效
```

## 工具

| 工具 | 说明 |
|---|---|
| `atta_doctor` | 引擎 / Blender / recipe 数 / 当前模型 |
| `atta_catalog` | recipe、feature/constraint schema、工作流 |
| `atta_spec` | Spec v3 validate / propose / compile / schema |
| `atta_model` | recipe / spec / graph 建模；`patches` refine |
| `atta_variant` | 采样、targets/constraints 求解、Pareto；`base` 做 fit；装配 `part.param` |
| `atta_describe` | 结构事实（bbox/体积/拓扑/壁厚/曲率/空间探针） |
| `atta_verify` | 三态判决 + receipt |
| `atta_preview` | studio/catalog/dark/clay/diagnostic 图 + facts + 视觉统计（图片回传） |
| `atta_deliver` | 非 pass 不写文件；GLB/OBJ/STL/BLEND（图形子集 +STEP）+ manifest |
| `atta_run` | 一次调用跑多步 |

## CLI 契约

插件不直接 import Python，而是调用主包提供的 CLI：

```bash
python3 -m atta --json tool <name> --args-file <tmp.json>
```

stdout 是一个 JSON 信封：

```json
{"ok": true, "tool": "atta_doctor", "data": {...}, "error": null, "ms": 12.3}
{"ok": false, "tool": "atta_model", "data": null,
 "error": {"code": "bad_arguments", "message": "...", "path": "params.height",
           "fix": "..."}, "ms": 3.1}
```

插件解析 `data`；失败时保留 `data`（若有部分结果）和 `error`。Python 的
traceback 永远不会出现在工具输出里。

## 超时

| 工具 | 上限 |
|---|---|
| `atta_model` | 600 s |
| `atta_variant` | 1200 s |
| `atta_preview` | 3600 s |
| `atta_deliver` | 600 s |
| `atta_run` | 1800 s |
| 其余 | 120–600 s |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ATTA_ROOT` | 插件上级目录 | Atta 引擎根（同时作为 cwd / `PYTHONPATH`） |
| `ATTA_PYTHON` | `python3` | 运行 CLI 的解释器 |

## 口径

- `pass` 只等于 `verdict == "pass"`；`degraded` 不是 pass。
- 壁厚是射线采样近似（`exact:false`）：standard 是 warn，strict 是 fail。
- `atta_preview` 是判断图，不是验收。
- 没有 `atta_verify` 回执，不宣称完成。
- 交付只导出 verify 通过（或显式 `allow_degraded`）的构建。

## 健康检查

```bash
python3 -m atta.agent.install zcode --print
python3 -m atta.agent call atta_doctor
python3 -m atta.agent mcp          # MCP stdio
python3 -m atta.agent http --port 0
node --check lib/index.js
```
