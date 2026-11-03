# @atta/dsh-atta

Atta v1.2 的 DSH 无头建模插件。它不复制几何内核，只把 Atta 的十个动词包成
DSH 工具（`atta_*`）。几何真值全部来自 Atta 引擎：

- recipe / Spec v3 / feature graph -> numpy geometry -> Blender finish
- 结构事实：bbox、体积、质心、拓扑、壁厚、曲率、ray/silhouette/section/clearance 探针
- 三态 verify（`pass | degraded | fail`）+ receipt + `issues[].fix`
- 参数采样 / targets 约束求解 / Pareto / fit（`atta_variant`）
- 交付 gate：verify 通过后导出 GLB/OBJ/STL/BLEND（图形子集另有 STEP）+ manifest
- 视觉：按零件材质、摄影棚灯光、微距 focus、normal/depth/id/curvature/deviation 通道

## 设计上的三条硬规则（为什么代码长这样）

1. **载入期绝不抛异常。** 模块在宿主 import 时抛错 = 十个工具整体消失，而宿主只回
   一句 `failed to import`，真因被吞掉。所以所有「可能解析不到」的东西（宿主的
   `defineTool`、schemastery、Python 解释器、仓库根）都走"返回结果并记录试过什么"
   的函数。解析失败就**降级**：只注册一个 `atta_plugin_status` 工具，说明原因与修法，
   插件本身仍然加载。
2. **所有工具返回值只过一个消毒出口。** 宿主用比 `JSON.stringify` 严格得多的
   walker 校验返回值：一个 `undefined`、`NaN`、`Infinity`、`-0`、`Date`、`Map`、
   循环引用或类实例，就会让整条回执被判 `tool returned invalid output` —— 活干完了，
   回执被拒收。所以出口统一 sanitize，并把每一处改动写进回执（不静默）。
3. **工具名与全局状态都带命名空间。** 只用 `atta_*`，不写 `globalThis`。这样同一个
   profile 里可以同时装 `dsh-blender-plugin`（它的 `blender_*` 工具）而互不干扰。

## 安装

插件不假设自己装在仓库里：`link:`、registry 安装、Windows/macOS 都能用。

```bash
# 1) 引擎可 import（三选一）
python3 -c "import atta"                    # 已 pip install atta
# 或：在 Atta 仓库里  python3 -m pip install -e .
# 或：export ATTA_ROOT=/path/to/atta-checkout

# 2) 把插件装进 profile（二选一）
python3 -m atta.agent.install dsh --create   # 写 profile 的 dependency + bundle + symlink
# 或：dsh plugin add /path/to/Atta/plugin

# 3) 重启 DSH，然后先调 atta_doctor
```

## 排障：工具不见了

先调 `atta_plugin_status`。它一定在（除非插件连载入都没成功），会直接说明：

| 回执里的现象 | 含义 | 修法 |
|---|---|---|
| `DEGRADED ... cannot resolve @deepseek-ai/dsh-tools` | 插件找不到宿主 API | 从 DSH 自己的安装启动 DSH；升级 DSH 后重启 |
| `no Python interpreter can 'import atta'` | 找不到能 import atta 的解释器 | `pip install -e <repo>`，或设 `ATTA_PYTHON` |
| `ALREADY LOADED` | 同一个包被装了两遍（bundle + 注入器） | 只保留一条安装路径，删掉注入器那条 |
| 工具能调，但结果是 `python_missing` | 解释器 pin 错了 | 修 `ATTA_PYTHON`，或不设它让插件自动探测 |

解释器探测顺序：`config.python` → `ATTA_PYTHON` → 仓库内 `.venv`/`venv`/`env` →
`python3` → `python`（Windows 另加 `py -3`）。**显式 pin 的（前两项）如果失败不会
回退** —— 静默换一个环境跑几何比报错更糟。

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
| `atta_plugin_status` | **仅降级时出现**：诊断占位工具 |

## 配置

```yaml
# profile 里该 bundle 的 config
- id: atta
  name: '@atta/dsh-atta'
  config:
    python: /opt/venv/bin/python    # 可选：pin 解释器（失败不回退）
    workspace: /path/to/project     # 可选：模型产物落哪（默认 cwd）
    timeoutScale: 2                 # 可选：把工具超时整体放大
```

## CLI 契约

插件不 import Python，而是调用主包提供的 CLI：

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

超时是常量（`defineTool` 只收常量），每个都取引擎自身预算 + 余量，所以先触发的是
引擎的可读错误，而不是宿主的截止。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ATTA_PYTHON` | 自动探测 | 运行 CLI 的解释器；也可用 `config.python` |
| `ATTA_ROOT` | 向上查找 | Atta 引擎根（pip 安装时不需要） |
| `ATTA_HOME` | `cwd` | 模型/产物工作区 |

## 口径

- `pass` 只等于 `verdict == "pass"`；`degraded` 不是 pass。
- 壁厚是射线采样近似（`exact:false`）：standard 是 warn，strict 是 fail。
- `atta_preview` 是判断图，不是验收。
- 没有 `atta_verify` 回执，不宣称完成。
- 交付只导出 verify 通过（或显式 `allow_degraded`）的构建。

## 开发者：离线自检

不需要 DSH 宿主即可验证插件契约：

```bash
cd plugin && node test/smoke.mjs      # 或 npm test
```

覆盖：载入期不抛异常、降级模式、lossless 出口、工具名不冲突、路径/解释器探测
（含 Windows `py -3`）、打包清单、client-half 陷阱、与 `blender_*` 共存、
以及**真实宿主** `dsh-tools` / `dsh-util-values` 的校验器。

其他健康检查：

```bash
python3 -m atta.agent.install zcode --print
python3 -m atta.agent call atta_doctor
python3 -m atta.agent mcp          # MCP stdio
python3 -m atta.agent http --port 0
node --check lib/index.js
```
