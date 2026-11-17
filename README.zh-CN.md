# Atta

<img src="docs/assets/mark.svg" width="88" align="right" alt="Atta mark">

**[English](README.md) | 中文**

Atta 是一个给 LLM agent 用的参数化 CAD 内核。零件用一份 JSON spec 描述，Atta 用
Python（numpy）把几何算出来、量一遍、检查一遍，然后才去渲染或导出。agent 拿到的是
包围盒、体积、壁厚、拓扑这些实际数字，不用对着截图猜形状；交付之前也能先确认它是不是
一个闭合实体。

**当前版本：atta-1.2.2beta**（Python 侧 `1.2.2b3`，插件包 `1.2.2-beta.3`）。
12 个工具、23 个 recipe、47 个 feature op。Blender 可选。

这一版新增了有理 NURBS 曲面、精确 CAD 圆角/倒角（STEP 与网格同源）、有界组合场、
定向模块装配、跨操作的质量传播，以及一组生物形态算子。相机、贴图和外观拆到了独立的
`basid` 程序里（见 [BASID.md](docs/BASID.md)）。具体能力和已知限制见[发布说明](docs/RELEASE-1.2.2beta.md)。

## 基本思路

- **先测量，再渲染。** `describe` 大约 0.3 秒返回 bbox、体积、拓扑、壁厚、射线探测和
  ASCII 截面。渲染图拿来看看可以，但不算检查结果。
- **spec 就是模型。** `model_id` 存的是重建步骤，不是网格文件。同一份 spec 重建出来的
  几何哈希不变。
- **三种判定。** `verify` 返回 `pass` / `degraded` / `fail`，附一份回执。`deliver`
  只导出 `pass` 的模型（`degraded` 需要显式允许）。
- **报错带修法。** 每个错误都有 `code`、指向 spec 的 `path`，以及一条 `fix` 提示。
- **Blender 只是后端。** 几何、测量、验收、ASCII 感知都是纯 numpy，只有渲染和
  GLB/BLEND 导出才用到 Blender。

## 安装

```bash
git clone https://github.com/laszapens/Atta.git atta && cd atta
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

可选依赖：

```bash
pip install -e ".[brep]"   # 解析式 STEP 导出（build123d / OpenCASCADE）
pip install -e ".[dev]"    # pytest + jsonschema，跑测试用
```

需要 **Python ≥ 3.10**。核心依赖（`numpy`、`scipy`、`trimesh`、`manifold3d`、
`pillow`）会随 `pip install -e .` 一起装上。

## 快速上手

```bash
python3 -m atta doctor --deep                 # 1. 检查环境
python3 -m atta catalog --verbose             # 2. 看看能建什么（23 个 recipe）
python3 -m atta model --recipe product.mug    # 3. 建一个草稿，不到一秒
python3 -m atta describe --wall               # 4. 看测量结果
python3 -m atta perceive --res 24             # 5. 用 ASCII 看形状，不用渲染
python3 -m atta verify --level standard       # 6. 验收：pass / degraded / fail
```

`model` 会打印 `model_id`，后面的命令默认用最近一次的模型，不用手动传。任何命令加
`--json` 都会输出原始的 `{ok, data, error}` 结构，而不是给人看的格式。

验收通过后导出文件：

```bash
python3 -m atta preview --mode studio --views hero --res 1200   # 需要 Blender
python3 -m atta deliver --formats glb,obj,stl                   # 需要 Blender
```

`perceive` 的输出大概是这样，速度够快，改参数时可以直接对着它看，不用等渲染：

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

## 工具

每个命令同时也是一个 agent 工具，名字加 `atta_` 前缀。

| 工具 | 用途 |
|---|---|
| `atta_doctor` | 环境与能力自检，建议第一个跑。 |
| `atta_catalog` | 列出 recipe、feature op、材质、视角和导出格式，含参数 schema。 |
| `atta_spec` | 对 Spec v3 做 `validate` / `propose` / `compile` / `schema`，不生成几何。 |
| `atta_model` | 用 recipe、spec 或 feature graph 建模；`patches=` 修改已有 spec。 |
| `atta_variant` | 在参数空间里采样，或者按测量指标反求参数。 |
| `atta_describe` | 测量数据、空间探测、ASCII 感知、构造记录、轮廓对比。 |
| `atta_verify` | 给出判定和回执（`spec_hash`、`mesh_hash`、逐项检查、`fix`）。 |
| `atta_preview` | 展示图/诊断图 + 图像统计。用来看，不用来验收。 |
| `atta_deliver` | 先 verify，再导出 GLB/OBJ/STL/BLEND（graph spec 还能导出 STEP）。 |
| `atta_run` | 一次调用跑多步流程，用 `$prev` 串联上一步的 id。 |
| `atta_conjure` | 根据原型表生成一套参数化构建方案（适合虚构题材），并记录参数来源。 |
| `atta_design` | 检查设计规则和工艺约束。模型可能 verify 通过，但这里不通过。 |

## 不只是基本体

- **精确 CAD**：按几何条件选边，OCCT 共享拓扑细分，网格和 STEP 来自同一次构造。
  见 [CAD_FEATURES.md](docs/CAD_FEATURES.md)。
- **NURBS 与有界场**：采样有理曲面和可组合的场，会报告误差和预算。见
  [NURBS.md](docs/NURBS.md)、[FIELD_TREE.md](docs/FIELD_TREE.md)。
- **模块装配**：定向端口、确定性 attach、mates/DOF、碰撞检查，以及精确/网格混合的 STEP。
  见 [MODULE_ASSEMBLY.md](docs/MODULE_ASSEMBLY.md)。
- **生物形态**：衣壳、硅藻、神经元、穿孔阀、晶格壳、环堆叠等 graph op。只做形状，不做生物模拟。
- **外观**：贴图、相机和带贴图的导出由独立的 `basid` 包负责，通过可选的
  [atta.look](docs/BASID.md) 接入。Atta 本身只管几何。

## Spec v3

一个最简单的 recipe spec：

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

一个最简单的 graph spec（自由造型）：

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

目前共 47 个 op：基本体、`revolve`/`loft`/`loft2`/`sweep`/`sweep2`、Coons/Gordon 曲面、
`nurbs_surface`、`extrude`、`thicken`、布尔运算、阵列、`transform`/`mirror`、
`fillet`/`chamfer`、`offset`/`repair`/`subdivide`、隐式的 `gyroid`/`sdf`/`sdf_blend`/`field_tree`、
文字，以及生物形态算子（`capsid`、`branch_tree`、`pyramidal_neuron`、`diatom_frustule`、
`perforated_valve`、`perforated_cylinder`、`lattice_shell`、`pore_field_shell`、`ring_stack`）。
feature 的 `args` 单位是 mm，可以写 `{"$param": "w"}` 或 `{"$expr": "w/2+4"}`
（只支持有限的算术表达式，不是 `eval`）。完整字段用 `python3 -m atta catalog --verbose` 查看。

## 接入 agent

### MCP 宿主

```bash
python3 -m atta.agent.install claude     # Claude Desktop 配置
python3 -m atta.agent.install cursor     # Cursor 配置
python3 -m atta.agent.install zcode      # 合并进 ~/.zcode/cli/config.json
python3 -m atta.agent.mcp                # 或者直接启动服务
```

生成的配置都用绝对路径：`python -m atta.agent.mcp`，`cwd`、`ATTA_ROOT`、`PYTHONPATH`
指向当前这份代码。改完重启宿主，`atta_*` 工具才会出现。

### DSH

```bash
python3 -m atta.agent.install dsh            # 试运行，列出会改动哪些 profile
python3 -m atta.agent.install dsh --create   # 写入依赖 + bundle + 软链接
```

插件在 [`plugin/`](plugin)，skill 在 [`plugin/skills/atta/SKILL.md`](plugin/skills/atta/SKILL.md)。
插件只是调用 `python3 -m atta`，不自己实现几何。如果宿主默认的 Python 无法 `import atta`，
设置 `ATTA_PYTHON`；代码目录挪了位置就设置 `ATTA_ROOT`。

从 Git 安装时填**仓库根目录**的地址：

```bash
dsh plugin --profile web add https://github.com/laszapens/Atta.git
# Web 端：插件 → 添加插件 → Git 仓库，填同一个地址
```

根目录的 [`package.json`](package.json) 只是个安装入口，实际的包在
[`plugin/`](plugin/package.json)。DSH 要求装进来的包声明 `dsh.bundle`，没有这个入口的话，
仓库会被当成空的占位包（`@laszapens/atta@0.0.0`），安装时报"这个包没有声明组合包"。
直接装子目录也可以（需要 pnpm 9+），不过一般用不上：

```bash
dsh plugin --profile web add 'https://github.com/laszapens/Atta.git#path:/plugin'
```

### ZCode

两种方式选一种：用上面的 MCP 配置就能用全部工具；装原生插件的话还会多一个路由 skill 和本地插件市场。

```bash
python3 -m atta.agent.install zcode-plugin
# 根据 .mcp.json.example 生成 plugins/atta/.mcp.json（本机专用，已加入 gitignore）
```

然后在 ZCode 里：**插件市场 → 添加 → 添加插件市场**，粘贴本仓库的 `plugins/` 目录，
再选 **个人 → dev-atta-local → Atta → 安装**。
插件文件见 [`plugins/atta/`](plugins/atta) 和 [`plugins/marketplace.json`](plugins/marketplace.json)。
`mcp.servers.atta` 配置和插件只开一个，两个都开的话工具会重复出现。

### HTTP 接口

```bash
python3 -m atta.agent.http --port 8765

curl -s localhost:8765/health
curl -s localhost:8765/tools
curl -s "localhost:8765/manifest?format=openai"
curl -s -X POST localhost:8765/call -H 'content-type: application/json' \
     -d '{"tool":"atta_model","args":{"recipe":"product.mug"}}'
```

只监听本机回环地址，没有鉴权，是给本地 agent 用的，不要把端口暴露到外网。

## 没有 Blender 时能做什么

| 能力 | 无 Blender | 有 Blender |
|---|---|---|
| `doctor`、`catalog`、`spec` | 可用 | 可用 |
| `model`、`variant`、`describe`、`perceive`、`verify`、`run` | 可用 | 可用 |
| `deliver --formats step`（graph spec + `build123d`） | 可用 | 可用 |
| `deliver --formats glb,obj,stl,blend` | 不可用（每个文件报 `blender_unavailable`，`status: "partial"`） | 可用 |
| `preview`（任何模式） | 不可用（`blender_unavailable`） | 可用 |

没有 Blender 时，`deliver` 照样会验收并写 manifest，导不出来的格式会标上
`blender_unavailable` 和对应的 `fix`。`preview` 没有替代方案，会直接报错，这时可以改用
`describe --perceive`。

Atta 默认在 `PATH` 里找 Blender，也可以用 `ATTA_BLENDER=/path/to/blender` 指定版本。
指定的路径找不到时会直接报告没有 Blender，不会偷偷换用别的。`doctor` 会显示实际找到的版本。

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| `blender_unavailable` | 没装 Blender 或不在 `PATH` 里。几何、测量、验收、`perceive` 都不受影响，只有渲染和网格导出才需要装 Blender 或设置 `ATTA_BLENDER`。`deliver --formats step` 不需要 Blender。 |
| `no_models` | 工作区里还没有模型，或者 `ATTA_HOME` 指到了别的目录。先跑一次 `model`，或者传入之前得到的 `model_id`。 |
| `unknown_recipe` | id 拼错了。报错的 `fix` 里列出了全部 23 个可用 id，参数可以用 `catalog` 查。 |
| 建模返回 `status: "repaired"` | 有参数超出范围被截断了。看一下 `applied_patches` / `issues`，拿到的模型和你要求的不完全一样。 |
| `deliver` 返回 `status: "blocked"` | `verify` 没通过。按 `verify.issues` 第一条的 `fix` 改完再重建；`degraded` 需要加 `allow_degraded=true`。 |
| 宿主插件 `import atta` 失败 | 宿主用的 Python 不是你的 venv。把 `ATTA_PYTHON` 指向 venv 里的 python，或者用宿主的解释器执行 `pip install -e .`。 |
| DSH：`插件安装失败 / 这个包没有声明组合包` | Git 地址对应的提交在仓库根目录没有 `package.json`，装进来的只是个空占位包。换到较新的提交，或者改填 `...Atta.git#path:/plugin`。 |

更多文档：[`ARCHITECTURE.md`](docs/ARCHITECTURE.md)（整体分层）、
[`LLM_NATIVE.md`](docs/LLM_NATIVE.md)（面向 agent 的设计约定）、
[`INTERFACES.md`](docs/INTERFACES.md)（内部 API 约定）、
[`QUALITY.md`](docs/QUALITY.md)（质量检查的标准）、
[`SHAPE_COVERAGE.md`](docs/SHAPE_COVERAGE.md)（各 recipe 的覆盖范围和还缺什么）。

## 目录结构

```
atta/             Python 包（numpy 内核 + Blender 后端 + agent 层）
  families/       每类形状一个模块，自动加载
  agent/          工具定义、MCP、HTTP、manifest、各宿主的安装脚本
  blender/        唯一 import bpy 的地方
tests/            单元测试、契约测试、端到端测试
examples/         水杯和支架的 spec，以及一个批处理示例
docs/             架构、接口、LLM 设计说明、路线图、覆盖范围、质量标准
  assets/         logo
plugin/           DSH 插件（lib、skills、bundle patch）
plugins/          ZCode 插件 + 本地插件市场
package.json      DSH 插件的安装入口（见上面 DSH 一节）
corpus/bench/     用于泛化测试的留出用例
scripts/          维护脚本（基准测试、corpus 校验）
```

## 参与贡献

最有帮助的是附带可复现 spec 的 bug 报告。怎么加 recipe、加 feature op、跑测试和代码风格，
见 [CONTRIBUTING.md](CONTRIBUTING.md)。

MIT 许可，见 [LICENSE](LICENSE)。
