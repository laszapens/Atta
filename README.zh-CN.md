# Atta

<img src="docs/assets/mark.svg" width="88" align="right" alt="Atta mark">

**[English](README.md) | 中文**

Atta 是一个给语言模型驱动的参数化 CAD 内核。你把零件描述成一份**可重建的 spec**，
Atta 用确定性的 Python 算出几何、**量出真实数字**，然后才允许渲染或导出存在。
agent 不需要从截图里猜形状，也能在说"做完了"之前证明它是一个闭合实体。

十个工具，23 个 recipe，25 个 feature op。Blender 是可选的。

## 它不一样在哪

| 原则 | 落到实处的含义 |
|---|---|
| **先测量，再渲染** | `describe` 在 0.3 秒内返回 bbox、体积、拓扑、壁厚、射线和 ASCII 截面。渲染是主观判断，永远不是证据。 |
| **spec 就是模型** | `model_id` 存的是*怎么重建*，不是一坨网格。同一份 spec 永远复现同一个几何哈希。 |
| **三态闸门** | `verify` 返回 `pass` / `degraded` / `fail` 加一份回执。`deliver` 拒绝一切非 `pass`（`degraded` 需显式覆盖）。 |
| **错误即补丁** | 每个失败都带 `code`、`path` 和一条具体的 `fix`，不是一句道歉。 |
| **Blender 只是后端** | 几何、事实、验收、感知全是纯 numpy。只有渲染和 GLB/BLEND 导出需要 Blender。 |

## 安装

```bash
git clone https://github.com/laszapens/atta.git atta && cd atta
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

可选扩展：

```bash
pip install -e ".[brep]"   # 解析式 STEP 导出（build123d / OpenCASCADE）
pip install -e ".[dev]"    # pytest + jsonschema，跑测试用
```

需要 **Python ≥ 3.10**。核心依赖是 `numpy`、`scipy`、`trimesh`、`manifold3d` 和
`pillow`，`pip install -e .` 会一并装好。

## 快速上手

```bash
python3 -m atta doctor --deep                 # 1. 引擎健康吗？
python3 -m atta catalog --verbose             # 2. 能建什么？（23 个 recipe）
python3 -m atta model --recipe product.mug    # 3. 建个草稿，远低于一秒
python3 -m atta describe --wall               # 4. 读测量出来的事实
python3 -m atta perceive --res 24             # 5. 用文本/ASCII 看它，不渲染
python3 -m atta verify --level standard       # 6. 过闸门：pass / degraded / fail
```

`model` 会打印它的 `model_id`；之后每个动词默认作用于最近一次模型，所以不用手动传递。
给任意命令加 `--json` 就能拿到完整的 `{ok, data, error}` 信封，而不是给人看的视图。

判定为 `pass` 之后，产出文件：

```bash
python3 -m atta preview --mode studio --views hero --res 1200   # 需要 Blender
python3 -m atta deliver --formats glb,obj,stl                   # 需要 Blender
```

`perceive` 长这样——一个亚秒级的取景器，可以对着它迭代，不必等渲染：

```
product.mug: 116.7x83.1x95.0 mm; aspect 1.00:0.71:0.81; V=93.69 ml;
closed=yes; genus=1; comps=1; convex=0.19; sym=y; holes~1; dih95=3.7

[front] coverage=0.481  # 实体, . 空
....##########..........
....###########.........
...############.........
...###################..
..####################..
..#################.##..
..###############..###..
..###############..###..
```

## 十个工具

每个动词同时也是一个同名 agent 工具（`atta_` 前缀）。

| 工具 | 一句话 |
|---|---|
| `atta_doctor` | 环境与能力自检；先跑它。 |
| `atta_catalog` | recipe、feature op、材质、视角和格式清单，含参数 schema。 |
| `atta_spec` | Spec v3 的 `validate` / `propose` / `compile` / `schema`——不建几何。 |
| `atta_model` | 用 recipe、spec 或 feature graph 建模；`patches=` 精修已有 spec。 |
| `atta_variant` | 采样参数空间，或对着测量指标反解真实目标。 |
| `atta_describe` | 事实、空间探针、ASCII 感知、构造轨迹、轮廓差异。 |
| `atta_verify` | 三态判决，附 receipt（`spec_hash`、`mesh_hash`、逐条 check、`fix`）。 |
| `atta_preview` | 判断图/展示图 + 图像统计。是判断，不是判决。 |
| `atta_deliver` | 先 verify，再导出 GLB/OBJ/STL/BLEND（graph spec 另有 STEP）。 |
| `atta_run` | 一次调用跑多步流水线，用 `$prev` 串接 id。 |

## Spec v3

一份最小的 recipe spec：

```json
{
  "schema": "atta.spec/v3",
  "name": "mug-01",
  "model": {"kind": "recipe", "id": "product.mug", "preset": "nominal",
            "params": {"height": 95, "wall": 3.2}},
  "quality": "draft",
  "material": "ceramic",
  "finish": {"bevel_mm": 0.8, "smooth_deg": 35},
  "constraints": {"bbox": {"z": [80, 120]}, "min_wall_mm": 2.5}
}
```

一份最小的自由形状 graph spec：

```json
{
  "schema": "atta.spec/v3",
  "name": "bracket",
  "model": {
    "kind": "graph",
    "output": "cut",
    "features": [
      {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
      {"id": "hole", "op": "cylinder",
       "args": {"radius": 4, "height": 20, "center": [15, 25, 3]}},
      {"id": "cut", "op": "difference", "inputs": ["plate", "hole"]}
    ]
  },
  "constraints": {"bbox": {"x": [75, 85]}}
}
```

feature 的 `args` 用 mm，可以写 `{"$param": "w"}` 或 `{"$expr": "w/2+4"}`
（受限算术，不是 `eval`）。完整字段见 `python3 -m atta catalog --verbose`。

## 接入 agent

### 任何 MCP 宿主

```bash
python3 -m atta.agent.install claude     # Claude Desktop 配置
python3 -m atta.agent.install cursor     # Cursor 配置
python3 -m atta.agent.install zcode      # 合并进 ~/.zcode/cli/config.json
python3 -m atta.agent.mcp                # 或直接起服务
```

生成的每条配置都用绝对路径：`python -m atta.agent.mcp`，并把 `cwd`、`ATTA_ROOT`、
`PYTHONPATH` 指向这份 checkout。改完要重启宿主才会加载 `atta_*` 工具。

### DSH

```bash
python3 -m atta.agent.install dsh            # 试运行：会改哪些 profile
python3 -m atta.agent.install dsh --create   # 写入依赖 + bundle + 软链
```

插件在 [`plugin/`](plugin)，它的 skill 在
[`plugin/skills/atta/SKILL.md`](plugin/skills/atta/SKILL.md)。插件只是把活转给
`python3 -m atta`，从不重新实现几何。如果宿主的默认解释器 `import atta` 失败，
设 `ATTA_PYTHON`；checkout 挪了位置就设 `ATTA_ROOT`。

### ZCode

两种方式。上面的独立 MCP 条目已经够用；原生插件额外提供路由 skill 和本地市场。

```bash
python3 -m atta.agent.install zcode-plugin
# 从 .mcp.json.example 生成 plugins/atta/.mcp.json（本机专用，已 gitignore）
```

然后在 ZCode 里：**插件市场 → 添加 → 添加插件市场** → 粘贴本仓库的 `plugins/`
目录 → **个人 → dev-atta-local → Atta → 安装**。
插件文件见 [`plugins/atta/`](plugins/atta) 和 [`plugins/marketplace.json`](plugins/marketplace.json)。
独立 `mcp.servers.atta` 条目和插件二选一，同时开会看到重复的工具列表。

### HTTP bridge

```bash
python3 -m atta.agent.http --port 8765

curl -s localhost:8765/health
curl -s localhost:8765/tools
curl -s "localhost:8765/manifest?format=openai"
curl -s -X POST localhost:8765/call -H 'content-type: application/json' \
     -d '{"tool":"atta_model","args":{"recipe":"product.mug"}}'
```

仅回环、无鉴权：这是本机 agent 桥，不是网络服务。不要把端口暴露出去。

## 环境要求与无 Blender 时的能力

| 能力 | 无 Blender | 有 Blender |
|---|---|---|
| `doctor`、`catalog`、`spec` | 可用 | 可用 |
| `model`、`variant`、`describe`、`perceive`、`verify`、`run` | 可用 | 可用 |
| `deliver --formats step`（graph spec + `build123d`） | 可用 | 可用 |
| `deliver --formats glb,obj,stl,blend` | 不可用——每个文件报 `blender_unavailable`，`status: "partial"` | 可用 |
| `preview`（任何模式） | 不可用——`blender_unavailable` | 可用 |

网格导出是降级而不是崩溃：`deliver` 仍会验收并写 manifest，文件列表里每个未导出的
格式都带一条 `blender_unavailable` 错误和 `fix`。`preview` 没有可降级的东西，直接返回
错误——没有 Blender 时改用 `describe --perceive`。

Atta 在 `PATH` 上找 Blender；用 `ATTA_BLENDER=/path/to/blender` 固定某个具体版本
（它是权威的——解析不到就报告"没有 Blender"，而不是悄悄用另一个）。`doctor` 会显示找到的版本。

工作区状态（`models.json`、预览、导出）放在 `$ATTA_HOME/_atta/`，默认是当前目录。
把 `ATTA_HOME` 指向项目目录可以让多次运行互不干扰，并记得把 `_atta/` 加进该项目的 `.gitignore`。

## 故障排查

| 现象 | 原因与修法 |
|---|---|
| `blender_unavailable` | Blender 没装或不在 `PATH`。几何、事实、验收、`perceive` 仍然可用；只有需要渲染或网格导出时才要装 Blender 或设 `ATTA_BLENDER`。`deliver --formats step` 是不需要 Blender 的解析式路径。 |
| `no_models` | 工作区还没有模型，或 `ATTA_HOME` 指到别处了。先跑 `model`，或把之前结果里的 `model_id` 传进来。 |
| `unknown_recipe` | id 拼错了。错误里的 `fix` 会列出全部 23 个合法 id；`catalog` 能看到它们的参数。 |
| 建模返回 `status: "repaired"` | 有参数越界被夹紧了。看 `applied_patches` / `issues`——你拿到的模型不是你要求的那个。 |
| `deliver` 返回 `status: "blocked"` | `verify` 没过。修 `verify.issues` 里的第一条（`fix` 已填好）再重建；`degraded` 需要显式 `allow_degraded=true`。 |
| 宿主插件 `import atta` 失败 | 宿主用的解释器不是你的 venv。把 `ATTA_PYTHON` 指向 venv 的 python，或用宿主那个解释器 `pip install -e .`。 |

更多细节：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 讲分层怎么搭的，
[`docs/LLM_NATIVE.md`](docs/LLM_NATIVE.md) 讲面向 agent 的设计规则，
[`docs/INTERFACES.md`](docs/INTERFACES.md) 是内部 API 契约，
[`docs/QUALITY.md`](docs/QUALITY.md) 说明"好"是按什么检查的，
[`docs/SHAPE_COVERAGE.md`](docs/SHAPE_COVERAGE.md) 列出每个 recipe 覆盖什么、哪里还有缺口。

## 仓库结构

```
atta/             Python 包（numpy 内核 + Blender 后端 + agent 层）
  families/       每个形状类别一个模块，自动发现
  agent/          工具契约、MCP、HTTP、manifest、宿主安装器
  blender/        唯一 import bpy 的地方
tests/            单元、契约、端到端测试
examples/         水杯和支架的 spec，外加一条批处理流水线
docs/             架构、接口、LLM 设计说明、路线图、覆盖度、质量标准
  assets/         标识（纯图标）
plugin/           DSH 插件
plugins/          ZCode 插件 + 本地市场
corpus/bench/     留出的泛化测试用例
scripts/          维护工具（基准跑批、corpus 校验）
```

## 参与

最有用的贡献是带可复现 spec 的 bug 报告。怎么加 recipe、加 feature op、跑测试、
以及仓库实际使用的代码风格，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

Atta 使用 MIT 许可——见 [LICENSE](LICENSE)。
