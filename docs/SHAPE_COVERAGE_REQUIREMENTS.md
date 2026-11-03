# Atta v1.1 — 形状覆盖与泛化能力需求文档

> 版本：v1.0 · 2026-11-02
>
> 目标读者：超级爬虫 / 数据工程 agent / 后续实现者
>
> 一句话：**不要抓“模型图片”，要抓“可参数化、可验证、许可证清晰的建模知识”。**
>
> 工作区与目录约定见 [`../corpus/README.md`](../corpus/README.md)。
>
> **历史基线**：本文的“已有 9 个配方 / 20 个 op”是 v1.0 的起点；当前是
> **23 个配方 / 25 个 op**，见 [`../README.md`](../README.md) 与
> [`SHAPE_COVERAGE.md`](SHAPE_COVERAGE.md)。需求条目仍然有效，数字以当前文档为准。

---

## 0. 背景与目标

Atta v1.1 已有：

- 9 个配方（mug / bowl / vase / bottle / gear / panel / dome / bracket / enclosure）；
- 20 个 feature op（primitive / revolve / loft / sweep / coons / gordon / boolean / pattern / thicken / lobes / transform）；
- 三态 verify、事实卡、文本/ASCII 感知层；
- 10 个 agent 工具。

当前短板不是“不会建某个具体件”，而是**家庭覆盖面窄、泛化套路少**：

- 遇到新形状，容易退化成一次性硬编码；
- 缺少“从 3–5 个例子抽象出一个参数化 family”的模板；
- 缺少机械、建筑、装配、晶格、文字、公差等常见家族的系统覆盖；
- 缺少可复现的 held-out 泛化基准。

本需求文档定义一个**语料采集 → 归一化 → 分类 → 提名 → 实现 → 验收**的流水线，让爬虫为 Atta 的广度服务，而不是制造一堆不可维护的案例。

### 0.1 成功标准（v1.2 阶段）

| 指标 | 目标 |
|---|---|
| 归一化语料 | ≥ 300 条 accepted records，许可证明确 |
| 形状家族 | 新增 ≥ 12 个参数化 family（recipe 或 feature template） |
| 覆盖矩阵 | 12 个一级类别中至少 9 个达到“有 family + 有测试” |
| 泛化基准 | 100 个 held-out shapes，`atta_model` 成功率 ≥ 90%，`atta_verify` pass ≥ 75% |
| 复用率 | 每个新 family 至少覆盖 5 个语料例子，而不是 1 个 |
| 许可证 | Tier C / unknown 为 0 进入实现层；全部有 attribution |

> 注意：这是“广度 + 泛化”目标，不是“把 300 个模型塞进仓库”。最终进入 Atta 的应是 **family 模板 + 参数 schema + 测试**，不是 300 段一次性代码。

---

## 1. 采集什么 / 不采集什么

### 1.1 高价值内容（优先）

1. **参数化模型定义**
   - OpenSCAD `.scad`（module/function/参数）
   - CadQuery / build123d Python
   - FreeCAD macro / Python
   - Blender Python（procedural / parametric）
   - SDF / implicit modeling（libfive、sdf、voxel）
2. **可复用的几何/特征算法**
   - fillet/chamfer、shell、offset、loft/sweep、Coons/Gordon、lattice、knurl、text engraving、boolean robustness。
3. **形状家族与参数模式**
   - 同一 family 的多个变体（尺寸/比例/孔位/阵列数不同）。
4. **约束与验收知识**
   - 最小壁厚、间隙/干涉、配合、拔模、genus/水密期望、装配 mates。
5. **官方文档与教程**
   - OpenSCAD/CadQuery/build123d/FreeCAD/Blender/Manifold 手册中的 feature 语义与参数约定。
6. **测试与 golden cases**
   - 带 expected bbox/volume/genus/closed 的例子，优先于只有渲染图的例子。

### 1.2 低价值 / 不采集

- 只有 PNG/JPG 渲染图、没有代码/参数/尺寸的“作品展示”；
- 扫描件、截图、付费模型、盗版资源；
- 无许可证或许可证不明的内容；
- 需要登录/付费/绕过 ToS 才能获取的内容；
- 含个人隐私、密钥、客户数据的内容；
- 纯视觉材质/贴图（Atta v1.1 的感知层是文本/ASCII，不靠贴图）；
- 一次性不可参数化的 mesh（可以作参考，但不进入实现语料）。

### 1.3 采集原则

- **代码/参数优先于图片**；
- **family 优先于单件**：同一形状至少抓 3–5 个变体；
- **许可证优先于数量**；
- **可验证优先于好看**；
- **保留 provenance**：URL、repo、commit、path、license、作者、抓取时间。

---

## 2. 数据来源与查询策略

### 2.1 代码托管

- GitHub / GitLab / Codeberg：
  - `topic:openscad`、`topic:cadquery`、`topic:build123d`、`topic:freecad`、`topic:parametric-modeling`、`topic:procedural-generation`
  - `language:OpenSCAD`、`"CadQuery"`、`"build123d"`、`"FreeCAD macro"`、`"Blender addon" parametric`
  - 关键词：`bracket`、`enclosure`、`gear`、`knurl`、`lattice`、`honeycomb`、`text engraving`、`assembly mates`、`tolerance`、`fillet`、`loft`、`sweep`、`coons`、`gordon`。
- 官方示例仓库：
  - CadQuery examples、build123d examples、OpenSCAD libraries（BOSL2、MCAD、NopSCADlib）、FreeCAD macros、Blender addons、Manifold/libfive examples。

### 2.2 文档与知识库

- OpenSCAD User Manual（feature 语义、参数、CSG）；
- CadQuery / build123d 文档（Workplane、Sketch、fillet/chamfer、loft/sweep）；
- FreeCAD Wiki（Part/PartDesign、约束、装配）；
- Blender Manual（modifier、bevel、boolean、geometry nodes）；
- Manifold / libfive / SDF 文档（稳健布尔、隐式曲面）；
- 机械设计/公差/配合的公开教材（只取概念与参数，不复制受版权保护的图表）。

### 2.3 模型社区（严格许可证过滤）

- Printables / Thingiverse / GrabCAD / Wikifactory / Instructables：
  - 只取明确允许再分发/改编的许可证；
  - 默认只做“形状家族分类参考”，不直接复制模型文件；
  - 记录 license、author、URL、抓取时间。
- 优先 CC0 / CC-BY / MIT / Apache-2.0；CC-BY-SA 只做参考，不直接并入 MIT 代码。

### 2.4 学术/开放数据集（先审许可证）

- Thingi10K、ABC、Fusion 360 Gallery、DeepCAD、ShapeNet 等：
  - 很多是 research-only / 非商用 / 需同意条款；
  - 先产出 license report，再决定是否可用于“统计/分类”，不默认进入代码语料。

### 2.5 爬虫行为约束

- 遵守 `robots.txt`、站点 ToS、API rate limit；
- 默认 1 req/s/domain，可配置退避；
- 不绕过登录/付费墙/验证码；
- 不抓取个人数据；
- 下载内容进隔离目录，**抓取阶段不执行任何代码**；
- 所有 URL/许可证/时间写入 provenance。

---

## 3. 统一语料模型（JSONL）

每条记录一行 JSON，必须通过
[`shape_corpus.schema.json`](shape_corpus.schema.json)。

最小示例：

```json
{
  "id": "gh_bosl2_gears_001",
  "source": {
    "url": "https://github.com/.../gears.scad",
    "repo": "https://github.com/...",
    "commit": "abc1234",
    "path": "gears.scad",
    "license": "BSD-2-Clause",
    "license_spdx": "BSD-2-Clause",
    "license_url": "https://.../LICENSE",
    "author": "...",
    "retrieved_at": "2026-11-02T12:00:00Z"
  },
  "kind": "openscad",
  "title": "Involute spur gear module",
  "description": "Parametric involute gear with pressure angle and module.",
  "language": "openscad",
  "shape": {
    "family": "gear.spur.involute",
    "category": "mechanical",
    "tags": ["gear", "involute", "module", "bore"],
    "units": "mm",
    "symmetry": ["radial"],
    "expected": {"closed": true, "genus": 1, "components": 1}
  },
  "parameters": [
    {"name": "module", "type": "float", "unit": "mm", "default": 2.0, "min": 0.5, "max": 6.0},
    {"name": "teeth", "type": "int", "default": 20, "min": 6, "max": 120},
    {"name": "pressure_angle", "type": "float", "unit": "deg", "default": 20.0}
  ],
  "operations": ["extrude", "revolve", "difference", "pattern_polar"],
  "graph": null,
  "code": "module gear(...) { ... }",
  "constraints": [
    {"id": "bore_fits", "expr": "bore_d < pitch_radius * 0.6"}
  ],
  "tests": [
    {"name": "nominal", "params": {"module": 2, "teeth": 20}, "expect": {"closed": true, "genus": 1}}
  ],
  "assets": [],
  "quality": {
    "license_tier": "A",
    "license_ok": true,
    "has_params": true,
    "runnable": true,
    "has_tests": true,
    "has_preview": false,
    "shape_confidence": 0.9
  },
  "coverage": {
    "category": "mechanical",
    "family_gap": 0.4,
    "reuse_score": 0.8,
    "priority": "P1"
  },
  "nomination": {
    "family": "gear.spur.involute",
    "reason": "involute gear is missing; 5 variants available",
    "suggested_recipe": "mechanism.gear.involute",
    "suggested_features": ["involute_profile", "pattern_polar"]
  },
  "notes": "License verified; code is a module, not a one-off script."
}
```

### 3.1 字段要求

| 字段 | 要求 |
|---|---|
| `id` | 稳定、可读、全局唯一；不含个人信息 |
| `source` | 必填；url/repo/path/license/retrieved_at |
| `kind` | openscad/cadquery/build123d/freecad/blender/sdf/mesh/dataset/docs/other |
| `shape.family` | 点分层级，如 `bracket.l_mount`、`gear.spur.involute` |
| `shape.category` | 见第 5 节覆盖矩阵 |
| `parameters` | 名称/类型/单位/默认值/范围；没有参数化的记录降权 |
| `operations` | 用 Atta 的 op 词表归一化；未知 op 放 `raw_ops` |
| `code` | 只保留可读片段；必须有许可证 |
| `constraints` | 可执行的约束/设计规则 |
| `tests` | 至少 nominal；有边界 case 更好 |
| `quality` | license_tier / runnable / has_params / has_tests |
| `coverage` | 类别、family gap、复用分、优先级 |
| `nomination` | 建议的 recipe/feature，不是直接实现 |

### 3.2 归一化词表

`operations` 只允许 Atta 现有/计划中的词：

```
box cylinder sphere cone torus
revolve loft sweep extrude
coons gordon thicken
union difference intersection
mirror pattern_linear pattern_polar
transform
lobes
fillet chamfer shell
text_engrave text_emboss
lattice perforate
```

未知 op 不要硬塞，写进 `raw_ops` 并在 `nomination.suggested_features` 里提议新 op。

---

## 4. 许可与合规

### 4.1 许可证分层

| Tier | 许可证 | 用法 |
|---|---|---|
| A | MIT、Apache-2.0、BSD-2/3、CC0、Unlicense | 可借鉴代码/参数，进入实现语料；保留 attribution |
| B | LGPL、MPL-2.0、CC-BY、CC-BY-SA | 只做算法/概念参考；不直接复制进 MIT 代码；单独隔离 |
| C | GPL、AGPL、proprietary、unknown、no-license | 只做元数据/分类参考；禁止进入实现语料 |

### 4.2 规则

- 每条记录必须有 `license_spdx`；无法识别时 `license_tier=unknown`，进 `rejected.jsonl`；
- 不允许把 Tier C 的代码片段写入 `code` 字段；
- 不允许删除版权头/作者信息；
- 产出 `license_report.json`：按 tier 统计、按 domain 统计、Tier C 清单；
- 对外交付的 manifest 必须带 `attribution` 列表；
- 如果站点 ToS 禁止抓取/再分发，直接跳过。

---

## 5. 覆盖矩阵（12 个一级类别）

机器可读模板见 [`coverage_matrix.json`](coverage_matrix.json)。人读版：

| 类别 | 典型 family | v1.2 目标 | 优先级 |
|---|---|---|---|
| `primitive` | box/cylinder/cone/torus 组合、CSG 练习 | 1 family / 30 examples | P2 |
| `mechanical` | bracket、flange、shaft、bearing、gear、pulley、hinge、clamp、mount | 2 families / 60 examples | P0 |
| `product` | bottle、cup、vase、bowl、packaging、handle、cap | 1 family / 40 examples | P0 |
| `architecture` | wall、window、door、stair、roof、facade、column、railing | 1 family / 40 examples | P1 |
| `organic` | shell、petal、leaf、bone、porous、blob、surface network | 1 family / 30 examples | P1 |
| `pattern` | array、honeycomb、knurl、perforation、lattice、rib | 1 family / 40 examples | P0 |
| `assembly` | fastener、mate、clearance、stack、hinge assembly | 1 family / 30 examples | P0 |
| `text` | engrave、emboss、label、logo、font outline | 1 family / 20 examples | P1 |
| `lattice` | gyroid、octet、voronoi、porous shell | 1 family / 20 examples | P1 |
| `surface` | loft、sweep、coons、gordon、thicken、fillet network | 1 family / 30 examples | P0 |
| `tolerance` | clearance、press fit、slip fit、ISO 286、draft | 1 family / 20 examples | P1 |
| `material_finish` | bevel、smooth、material presets、surface finish | 0 family / 10 examples | P2 |

> `family` 是参数化模板数，`examples` 是 accepted 语料数。v1.2 合计 **12 family / 370 examples**，最低验收 300 examples；v2 再扩展到 30+ family / 1000+ examples。
> 每条 accepted 必须能通过 schema + license + 至少一个可执行/可结构化测试。

### 5.1 每个 family 的最低交付

进入 Atta 实现层的一个 family 必须同时有：

1. `spec` 或 recipe 模板；
2. 参数 schema（类型/单位/范围/默认值）；
3. 至少 2 条约束或设计规则；
4. nominal + 至少 1 个边界参数测试；
5. `atta_verify` pass（或明确记录 degraded 原因）；
6. 文本/ASCII 感知签名（`atta_describe(perceive=true)`）；
7. 许可证/来源记录。

---

## 6. 要提炼的“泛化技巧”

爬虫不要只收集“形状”，要标注它用了哪种泛化套路。至少覆盖：

1. **参数分解**：base + feature + pattern；哪些尺寸是自由参数，哪些是派生量。
2. **对称与重复**：镜像、旋转阵列、线性阵列、周期结构。
3. **特征顺序**：CSG 顺序、shell 前后、fillet 放在最后等。
4. **曲面网络**：loft/sweep/Coons/Gordon/thicken 的组合方式。
5. **约束传播**：壁厚、间隙、配合、拔模、最小特征尺寸。
6. **模块库**：可复用 module/function，而不是一次性脚本。
7. **装配 mates**：同轴、贴合、偏移、紧固件、公差链。
8. **文字/标识**：字体轮廓、engrave/emboss、深度与拔模。
9. **晶格/多孔**：gyroid/octet/voronoi、壳内 lattice、开孔率。
10. **稳健布尔**：共面、薄壁、退化边、自交的处理方式。
11. **形状语法/L-system**：分支、递归、分形、生长规则。
12. **SDF/隐式**：blend、smooth union、offset、shell。

每条记录在 `coverage` 或 `nomination` 里标注它体现了哪些技巧；同一 family 至少 2 种技巧才算“可泛化”。

---

## 7. 爬虫流水线

```
seed queries
  -> fetch (respect ToS/robots/rate limit)
  -> license detect (SPDX + manual review)
  -> parse (language AST / module / params / tests)
  -> normalize to JSONL
  -> classify (family/category/operations)
  -> dedupe (text + normalized shape signature)
  -> quality score (license, params, runnable, tests)
  -> nominate families/features
  -> coverage report + license report
  -> human review queue (Tier B/C, ambiguous)
  -> accepted corpus
```

### 7.1 抽取器要求

- OpenSCAD：提取 module/function、参数、`difference/union/intersection`、`linear_extrude/rotate_extrude`、`hull/minkowski`。
- CadQuery/build123d：提取 `Workplane`、`Sketch`、`extrude/revolve/loft/sweep`、`fillet/chamfer/shell`、参数与循环。
- FreeCAD macro：提取 Part/PartDesign 调用、约束、参数。
- Blender Python：提取 primitive/modifier/boolean、参数、geometry nodes 引用。
- SDF：提取 primitive、union/smooth union、offset、shell、lattice 参数。

### 7.2 去重

- 文本：MinHash/SimHash + AST 结构哈希；
- 形状：归一化 bbox/aspect/volume/拓扑签名 + 低分辨率 occupancy hash；
- 同一 family 的变体不要当重复删除；按 `shape.family` 聚类保留 3–5 个代表。

### 7.3 质量评分

```
quality_score =
  0.35 * license_tier_score
+ 0.25 * has_params
+ 0.15 * runnable
+ 0.15 * has_tests
+ 0.10 * shape_confidence
```

低于阈值的进 `rejected.jsonl`，并写明原因。

---

## 8. 提名与实现规则

### 8.1 什么时候提名一个新 family

- 至少 3 个独立来源/变体指向同一 family；
- 该 family 在覆盖矩阵中是 P0/P1 缺口；
- 参数化程度高（≥ 3 个可调参数）；
- 许可证 Tier A 或 B（B 只做概念参考）；
- 能用 Atta 现有 feature 表达，或只缺 1–2 个新 op。

### 8.2 什么时候提名一个新 feature op

- 至少 3 个 family 需要同一几何能力；
- 现有 op 组合会导致不可维护的硬编码；
- 能给出明确的参数 schema、边界条件和 verify 期望；
- 不引入外部大依赖。

### 8.3 禁止

- 为每个抓到的模型建一个 recipe；
- 直接复制 Tier C 代码；
- 用渲染图当唯一验收；
- 把一次性 mesh 当 family；
- 没有测试就宣称覆盖。

---

## 9. 泛化基准（held-out）

从 accepted corpus 中留出 **20% 的 family/变体**，不参与实现，只做评测。

评测项：

| 指标 | 定义 | v1.2 目标 |
|---|---|---|
| build_success | `atta_model` 成功返回 model_id | ≥ 90% |
| verify_pass | `atta_verify` verdict == pass | ≥ 75% |
| param_recovery | 关键尺寸与参考的误差 | ≤ 2% 或 ≤ 0.5 mm |
| op_coverage | 参考用到的 op 在 Atta 中可表达 | ≥ 80% |
| family_coverage | 12 类中至少 9 类有 family | 9/12 |
| reuse | 每个新 family 覆盖 ≥ 5 个语料变体 | 100% |

评测输出：`bench/generalization_v1.json` + 每个 case 的 `spec/facts/verify` 回执。

---

## 10. 交付物

爬虫侧：

- `corpus/raw/`：原始快照（按 domain/date 分目录，带 LICENSE）；
- `corpus/normalized/*.jsonl`：通过 schema 的记录；
- `corpus/rejected/*.jsonl`：拒绝原因；
- `corpus/license_report.json`；
- `corpus/coverage_report.json`；
- `corpus/nominations.jsonl`：family/feature 提名；
- `corpus/README.md`：抓取范围、口径、复现命令。

校验与统计（仓库内已提供）：

```bash
python3 scripts/validate_corpus.py corpus/normalized/*.jsonl --coverage
```

输出 valid/invalid、license tier 分布、category 计数，以及相对
`docs/coverage_matrix.json` 的缺口。

Atta 侧：

- 新 recipe / feature（按提名优先级）；
- `tests/corpus/`：每个 family 的 nominal + boundary；
- `bench/generalization_v1.json` + 运行脚本；
- 更新 `atta_catalog` 的 `shape_families` 与覆盖标签；
- 更新 `docs/ROADMAP.md` 的形状覆盖表。

---

## 11. 里程碑

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| M0 | 确认 schema、覆盖矩阵、seed queries | schema 通过校验；10 个 seed 源可抓 |
| M1 | 抓取 + 归一化 100 条 | Tier A/B 占比 ≥ 80%；无 unknown license |
| M2 | 覆盖报告 + 提名 20 个 family | P0 类别每类至少 1 个候选 |
| M3 | 实现 5 个 family + 测试 | verify pass；bench 初版 |
| M4 | 300 条语料 + 12 family | held-out build ≥ 90%，verify ≥ 75% |

---

## 12. 风险与人工审核

- **许可证风险**：Tier B/C 一律人工复核；不明确就拒绝。
- **代码执行风险**：爬取阶段不执行；执行只在隔离容器，禁网、限时、限内存。
- **过拟合风险**：每个 family 至少 5 个变体；held-out 评测。
- **形状分类错误**：低置信度记录进人工队列，不自动提名。
- **数据膨胀**：语料是“提炼素材”，不是 Atta 仓库的一部分；实现层只保留 family 模板与测试。
- **站点封禁**：遵守 rate limit；优先 API/官方导出。

---

## 13. 给爬虫的 seed 查询（可直接用）

```text
GitHub:
  topic:openscad
  topic:cadquery
  topic:build123d
  topic:freecad
  topic:parametric-modeling
  topic:procedural-generation
  language:OpenSCAD
  "CadQuery" "parametric"
  "build123d" "parametric"
  "FreeCAD macro" "parametric"
  "Blender" "procedural" "python"
  "involute gear" openscad
  "knurl" openscad
  "lattice" cadquery
  "honeycomb" openscad
  "text engraving" openscad
  "assembly mates" cadquery
  "tolerance" "fit" cadquery

Docs:
  OpenSCAD User Manual
  CadQuery documentation
  build123d documentation
  FreeCAD Wiki Part/PartDesign
  Blender Manual modifiers/geometry nodes
  Manifold documentation
  libfive / SDF documentation

Datasets (license first):
  Thingi10K
  ABC dataset
  Fusion 360 Gallery
  DeepCAD
  ShapeNet
```

---

## 14. 一句话验收

> 一个 family 只有同时具备 **参数 schema + 约束 + 测试 + verify 回执 + 感知签名 + 许可证**，才算进入 Atta 的“形状覆盖”。
> 否则它只是一条语料，不是能力。
