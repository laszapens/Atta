# Shape corpus

Atta's runtime imports **none** of this directory. The only part that ships and
that the toolkit reads is `corpus/bench/`: held-out generalization cases scored by
`scripts/run_bench.py`. Everything else here documents the shape corpus that
informed the recipe set.

What Atta actually consumes from a corpus survey is a **family template +
parameter schema + tests + verify receipt** — not a folder of one-off models.

## What ships

```
corpus/
  bench/generalization_v1.json   100 held-out cases, generic Spec v3 graphs
  coverage_report.json           static snapshot: which shape families exist, per category
  license_report.json            static snapshot: licence tiers of the surveyed records
  nominations.template.json      template for proposing a new family
  README.md  DELIVERY.md         this file and the provenance summary
```

The raw record corpus (`raw/`, `normalized/`, `rejected/`) and the harvest tooling
are **not redistributed**: they are third-party geometry and metadata under mixed
licences. The two report JSONs are kept because they are self-contained summaries
(counts, tiers, gaps) with no redistributed content.

## The bench set

`corpus/bench/generalization_v1.json` is the only input `scripts/run_bench.py`
needs. Each case is a generic Spec v3 feature graph, so the benchmark never calls
a shipped recipe and cannot be gamed by tuning one.

```bash
python3 scripts/run_bench.py                 # score all 100 cases
python3 scripts/run_bench.py --limit 10      # quick smoke run
python3 scripts/run_bench.py --out /tmp/b.json
```

The file carries its own `protocol`, `summary` and `failures` blocks; read
`summary` for the headline numbers and `failures` for the case-by-case misses.
Interpretation of the numbers (what "faithful build" means, where the 6 misses
cluster) is in [`../docs/SHAPE_COVERAGE.md`](../docs/SHAPE_COVERAGE.md).

## Adding a family

Recipe ids encode their category (`product.mug`, `surface.dome`); the 12
categories and their acceptance rules live in
[`../docs/coverage_matrix.json`](../docs/coverage_matrix.json) and are enforced by
`tests/test_coverage_gate.py`. A family must carry typed parameters, constraints,
and end in an `atta_verify` receipt plus a perception signature
(`atta_describe(perceive=true)`).

See [`../CONTRIBUTING.md`](../CONTRIBUTING.md) for the mechanics.

---

## 采集结果 (2026-11-02)

本次采集（2026-11-02）的复核口径：**不信任 harvester 的许可证声明**，逐个仓库重取证据。
下列数字是那次采集的静态快照；原始记录与复核脚本不在本仓库内分发。

### 数量与许可证构成

| 指标 | 数值 |
|---|---|
| 记录总数 | 573 |
| schema 合法 | 573（invalid 0） |
| `accepted`（tier A/B） | 561 |
| `substantive`（accepted 且有 ≥1 参数） | 520 |
| `reference_only`（accepted 但无参数，仅参考） | 41 |
| family 数（按 substantive 计） | 183 |

| Tier | 记录数 | 占比 | 是否允许 `code` |
|---|---|---|---|
| A（MIT/Apache/BSD/CC0/0BSD/Unlicense） | 379 | 66.1 % | 是 |
| B（LGPL/MPL/CC-BY/CC-BY-SA） | 182 | 31.8 % | **否**（实测 0 条带 code） |
| C（GPL/AGPL/NC/未声明） | 12 | 2.1 % | **否**（实测 0 条带 code） |

> 上表的权威来源是同目录的 `license_report.json`（`by_tier`），不是本文的历史草稿。

来源构成（`license_report.json:by_source_kind`）：GitHub 416 · Printables 139 ·
Wikibooks 11 · 数据集站点 3 · Blender docs 1 · ReadTheDocs 1 · deep-geometry.github.io 1 ·
其他站点 1。

主要来源（按记录数，来自采集快照的逐仓库统计——原始记录未分发，这一行无法在
公开 checkout 上重算，其余数字都可由 `license_report.json` / `coverage_report.json` 复核）：
`BelfrySCAD/BOSL2` 169 · `revarbat/BOSL` 63 · `openscad/MCAD` 39 ·
`curv3d/curv` 18 · `libfive/libfive` 13 · `alexadam/context-free-art` 11 ·
`Irev-Dev/Round-Anything` 11 · `rcolyer/threads-scad` 10 · `deadsy/sdfx` 6 ·
`elalish/manifold` 6 · `jscad/OpenJSCAD.org` 5。

### 许可证纠错（API / 声明 vs 实物证据）

GitHub API 的 `license` 字段**不可作为证据**：6 个仓库返回 `NOASSERTION`/`Other`/`null`，
另有 2 个数据集仓库的 code 许可证与数据许可不可混同。

| 仓库 | 声明 | 实物证据 | 结论 | 影响记录 |
|---|---|---|---|---|
| `hannobraun/fornjot` | 0BSD | `LICENSE.md` = Zero-Clause BSD 全文 | 确认（API 报 NOASSERTION） | 3 |
| `kennetek/gridfinity-rebuilt-openscad` | MIT | `LICENSE` = "Copyright (c) 2023 Kenneth Hodson" | 确认（API 报 NOASSERTION） | 0 |
| `libfive/libfive` | MPL-2.0 | `README.md` §License：kernel/stdlib MPL-2.0，Studio 二进制 GPL-2.0+ | 确认（分层，API 为 null） | 13 |
| `mkeeter/antimony` | MIT | `README.md` §License 内含 MIT 全文（无 LICENSE 文件） | 确认（API 为 null） | 0 |
| `openscad/MCAD` | LGPL-2.1 | `README.markdown`：*"All the files are licensed under the LGPL 2.1"* | 确认（无 LICENSE 文件，故 API 取不到） | 39 |
| `Haskell-Things/ImplicitCAD` | AGPL-3.0 | 仓库 LICENSE | 确认 | 1 |
| `ChrisWu1997/DeepCAD` | NOASSERTION (C) | `LICENSE` = MIT，但仅限**代码** | 保持 C（数据许可未声明） | 1 |
| `hducg/MFCAD` | NOASSERTION (C) | `LICENSE` = MIT，同上 | 保持 C | 1 |

未解决 3 条（`Fusion360GalleryDataset`、`Text2CAD`、`CAD-Recode`）：无许可证文件；
三者本就 Tier C（NC 或未声明），保守分层正确。

**规则（已写入 `corpus.detect_spdx_from_text`）**：API 报 `NOASSERTION`/`Other`/`null`
时必须打开 `LICENSE` / `LICENSE.md` / `LICENSE.txt` / `COPYING` 或 README 的 License 段落。

### 未完成的核验（诚实声明）

**`printables.com` 记录的许可证无法独立核验**（`license_report.json:unverified_claims.count`
记 139 条，其中 Tier A 26 / Tier B 113）：该站对自动抓取返回 HTTP 403，许可证字段由前端渲染。
这些记录**不含任何源代码、也不含模型文件**，仅为形状家族参考与署名元数据，
最坏情况是署名字符串有误，而非再分发合规问题。
对外分发前需人工复核这些页面。详见 `license_report.json:unverified_claims`（含 `reason` / `exposure` / `remediation`）。

### 覆盖（按 substantive 统计，不向上取整）

**7 类达标 / 5 类不足。**

以 `coverage_report.json` 为准（下表与该文件 `categories[].substantive` 逐条一致）：

达标：`primitive` 70/30 · `mechanical` 92/60 · `product` 41/40 · `pattern` 46/40 ·
`assembly` 85/30 · `surface` 95/30 · `material_finish` 19/10。

不足：**`architecture` 20/40（缺 20）** · **`organic` 13/30（缺 17）** ·
**`text` 9/20（缺 11）** · **`lattice` 13/20（缺 7）** · **`tolerance` 17/20（缺 3）**。

> 汇总数字会掩盖问题：520 条 substantive 对 300 条目标看似充裕，
> 但 5 个类别单独看仍不足。`product`(41/40) 与 `tolerance`(17/20) 最贴线，最脆弱。

缺口根因（非投入不足，而是许可证与生态限制）：
- `architecture`：开源 code-CAD 几乎不建模建筑；可用的干净来源只有 POV 室内场景与 CFDG 文法。
- `organic`：L-system 语料为 GPL 或受版权保护，上限即为 CFDG + Curv/sdfx 隐式场。
- `text`：缺 stroke vs outline 两派字体家族（sdfx `Text2D` vs JSCAD `vectorText`）。
- `lattice`：已有 gyroid/Schwarz/Neovius/kagome，缺 octet、voronoi-foam、梯度密度。
- `tolerance`：最接近达标线，`harvest-fit` 仍在补充。

### 已知限制

1. **无 rejected 记录**：573 条全部 schema 合法、无重复 id、无低于 0.35 的 accepted 记录，
   故未生成 `rejected/low_quality.jsonl`（空文件会释放错误信号）。
   3 条质量分 0.06 的记录是 Tier C 元数据（公式对 Tier C 必然给 0 分），已按设计排除在
   accepted 之外，保留其参考价值。
2. **variant surplus**：26 个 family 变体数 >5，共 204 条记录在 `notes` 标记
   `"variant surplus"`（保留计数，仅作去重信号）。
3. `reference_only`（41 条，主要是 Printables `kind=mesh`）不计入任何覆盖目标。
4. 这些计数是快照，不再随采集推进更新。

机器可读结果见同目录的 `license_report.json` 与 `coverage_report.json`。

### 命名仲裁：`thread.*` 归入 `assembly`

**问题**：14 条螺纹/紧固件记录（threadlib 3、threads.scad 10、sdfx 1）以 `thread.helical.*` /
`thread.tapered.*` 命名，却存放在 `surface.jsonl`；同期 harvest-core 新增了 `assembly.thread.*` 6 个 family。
同一概念两套命名、两个类别。

**裁定（task-9）**：螺纹是**装配/紧固件**语义，不是曲面语义。`thread.` 根命名空间作废，
按记录自身的 module 语义逐个归类（不是按 id 猜）：

| 原 family | 记录 | 新 family | 理由 |
|---|---|---|---|
| `thread.helical.metric` | 3 | `assembly.thread.metric` | threadlib bolt/nut/tap，ISO 米制 |
| `thread.helical.unified` | 4 | `assembly.thread.metric` | MetricBolt / MetricCountersunkBolt / MetricWoodScrew / ScrewHole（"threaded hole, metric"） |
| `thread.helical.unified` | 2 | `assembly.thread.rod_end` | RodExtender / RodStart（下端内螺纹、上端外螺纹） |
| `thread.helical.unified` | 3 | `assembly.fastener.clearance_hole` | ClearanceHole / Countersunk / Recessed —— 是**孔**，不是螺纹 |
| `thread.helical.unified` | 1 | `assembly.fastener.drive_recess` | PhillipsTip —— 是**驱动槽** |
| `thread.tapered.screw3d` | 1 | `assembly.thread.tapered` | sdfx Screw3D 锥形螺纹 |

结果：`surface` 110→96、`assembly` 71→85，**当时总数 552 不变**，无记录丢失，`thread.*` 零残留。
`surface` 该类记录此前是 `assembly` 计数的重复口径来源，现已消除。

### 已知限制（内核行为，family 作者必读）

这三条来自 family 实现实测，不是推测，属"知识记录"而非缺陷：

1. **`atta_verify` 先施加 finish bevel，再采样壁厚** —— 因此 `finish.bevel_width` **计入壁厚预算**。
   浅角布尔交线叠加 bevel 可退化成刀口：实测原始网格真实壁厚 109.99 mm，但本地测量出现 **0.118 mm**。
   由于验收门读 `min`，**一个坏的采样面就能让整个零件 fail**。
   → family 作者不可把 bevel 当"免费"的美化步骤；给 bevel 留预算，或对浅角交线加保护角。
2. **`pattern.honeycomb_panel` 在 builder 内改写 registry 的 `expected_genus`**（per-model 发布），
   因为 `Recipe.expected_genus` 是静态 int。多孔 family 的 genus 随孔数/行数变化，
   **需要 genus 函数而不是常量** → 属已知架构限制，当前按 per-model 发布处理。
3. **`lattice.gyroid_slab` 的 `expected_genus=39` 只在 3×2×1 cell 板件下成立**（已带 warn 约束）。
   更危险的是**单位混用**：隐式场值与米制距离比较时无提示，体积会在 **26188 ↔ 17963 mm³** 之间变化，
   而拓扑仍然合法 —— 即"verify 通过但体积错了 31%"。
   → 隐式场 family 必须单独审计量纲，不能只信 verify。
