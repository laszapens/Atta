# 形状覆盖 (shape coverage) — v1.2

> 生成于 2026-11-02 21:09。本文由 `work/harvest/delivery/make_coverage_doc.py` 从实测数据生成：
> 每条 family 的 `verify` 回执来自真实 draft 构建（`work/harvest/delivery/inventory.json`），
> 语料数字来自**当前** `corpus/normalized/*.jsonl`。这是评审先读的一页。

## 0. 一句话

v1.2 把 Atta 的 shape 面从 **9 个 recipe** 扩到 **23 个**（+14），一级类别覆盖从 **3 类**到 **11 类**（共 12 类），新增 **5 个 feature op**（`gyroid`、`offset`、`sdf`、`text_emboss`、`text_engrave`，让 `text` 类别从 0 到有），并把「什么算覆盖」变成**可执行的门**：
`tests/test_coverage_gate.py` 直接读 `docs/coverage_matrix.json` 的 acceptance，任何 family 达不到自己类别的规则就红。

## 1. 这次加了什么

| 维度 | 之前 | 现在 | 证据 |
|---|---|---|---|
| recipe / family | 9 | **23** | `atta/families/*.py` + `atta/recipes.py` |
| feature op | 20 | **25**（+5：`gyroid`、`offset`、`sdf`、`text_emboss`、`text_engrave`） | family 自己用 `features.register_op` 注册（`text.py` / `offset_ops.py`），不改 `features.py` |
| 一级类别有 family | 3 类（mechanical、product、surface） | **11 / 12 类** | 下表 §3 |
| 语料 accepted / substantive | — | **561 / 520** | `corpus/normalized/*.jsonl`、`corpus/coverage_report.json` |
| 覆盖门 | 无 | `tests/test_families.py` + `tests/test_coverage_gate.py` | `tests/` |

架构侧（不是形状，但决定了上面这些能不能长期维护）：
`atta/registry.py` 把注册表从 `recipes.py` 拆出；`atta/kit.py` 收公共 builder 助手；
`atta/families/<category>.py` 一类别一文件、**自动发现**、宽容加载（`ATTA_FAMILIES_STRICT=1` 时严格）；
`features.register_op` 让 family 自己加 op 而不改 `features.py`。

## 2. 14 个新 family（含验收证据）

全部 `quality=draft`、`verify --level standard` 实测。`genus` 列是**实测**拓扑亏格，与 recipe 上声明的 `expected_genus` 逐条相等——verify 把这一项当 **error 级**，写错直接 fail，所以这列没法糊弄。

| # | recipe id | 类别 | 参数 | 约束 | genus（实测） | 三角面 | 构建 (draft) | verify |
|---|---|---|---|---|---|---|---|---|
| 1 | `primitive.chamfer_box` | primitive | 8 | 4 | 0 | 72 | 0.030 s | **pass** |
| 2 | `mechanical.flange` | mechanical | 13 | 11 | 7 | 2328 | 0.056 s | **pass** |
| 3 | `mechanical.gear.involute` | mechanical | 10 | 6 | 1 | 2546 | 0.240 s | **pass** |
| 4 | `product.screw_cap` | product | 13 | 12 | 0 | 12030 | 0.195 s | **pass** |
| 5 | `architecture.fluted_column` | architecture | 12 | 9 | 0 | 22708 | 0.522 s | **pass** |
| 6 | `architecture.tower.growth` | architecture | 11 | 8 | 0 | 196 | 0.041 s | **pass** |
| 7 | `organic.seashell` | organic | 10 | 5 | 0 | 7676 | 0.010 s | **pass** |
| 8 | `pattern.honeycomb_panel` | pattern | 10 | 5 | 35 | 1088 | 0.021 s | **pass** |
| 9 | `assembly.bolt_nut` | assembly | 13 | 7 | 1 | 440 | 0.017 s | **pass** |
| 10 | `text.engraved_plate` | text | 19 | 5 | 0 | 594 | 0.018 s | **pass** |
| 11 | `lattice.gyroid_slab` | lattice | 7 | 5 | 39 | 171912 | 0.493 s | **pass** |
| 12 | `surface.coons_duct` | surface | 9 | 5 | 1 | 8192 | 0.022 s | **pass** |
| 13 | `surface.fillet_network` | surface | 7 | 5 | 0 | 12016 | 6.072 s | **pass** |
| 14 | `tolerance.clearance_ladder` | tolerance | 7 | 6 | 0 | 2272 | 0.117 s | **pass** |

> `architecture.tower.growth` 是这批里**唯一一个「技巧本身就是主张」**的 family：
> 它把递归生长律（四叉/门格式产生式，参数 `levels`/`branching`/`scale`/`member`/`taper`）
> 写进 **recipe builder**（普通 Python 返回 mesh），而不是新增 op——递归不需要 Spec DAG 变形。
> 它的语料证据是薄的（提名排名 #12，自评「a declared 3-variant bet」），上表里该照实显示小数字。
> 它同时正面回答 `architecture` 的语料短少（20/40 substantive）：**生长律能在没有参数化建筑语料的地方生成结构**。
> 它也是 `work/harvest/deep/NOTE.md` 那个「中断的想法 / 递归」问题的答复——这一轮是用代码关掉的，不是用承诺。

每条的参数表 / 约束 / nominal+boundary 回执 / 感知 ASCII 在对应报告里：

| 类别 | 报告 |
|---|---|
| mechanical | `work/reports/family-mech.md` |
| tolerance | `work/reports/family-mech.md` |
| pattern | `work/reports/family-pattern.md` |
| assembly | `work/reports/family-pattern.md` |
| primitive | `work/reports/family-pattern.md` |
| product | `work/reports/family-product.md` |
| architecture | `work/reports/family-product.md` |
| surface | `work/reports/family-surface.md` |
| organic | `work/reports/family-surface.md` |
| lattice | `work/reports/family-surface.md` |
| text | `work/reports/family-text.md` |

## 3. 12 个类别：谁有 family、语料够不够

`目标` = `docs/coverage_matrix.json` 的 `target_examples`。`语料` 口径 = **substantive**（accepted **且**有参数），即 §3.1 的 implementation-grade 口径，与 `corpus/coverage_report.json` 完全一致；reference-only 记录（无参数）**不计入目标**。**短少的类别直接写短少，不四舍五入。**

`bench` 列 = 该类别在 100 例 held-out 基准里的 **verify pass**（例数见括注），基准用通用 Spec v3 图、从不调用现成 recipe（`corpus/bench/generalization_v1.json`）。

| 类别 | family 数 | family id | 语料 substantive | accepted（参考） | 目标 | 语料状态 | bench verify |
|---|---|---|---|---|---|---|---|
| primitive | 1 | `primitive.chamfer_box` | 70 | 70 | 30 | ✅ 达标 | 8/8 |
| mechanical | 4 | `mechanical.flange`、`mechanical.gear.involute`、`mechanism.gear`、`part.bracket` | 92 | 99 | 60 | ✅ 达标 | 16/16 |
| product | 6 | `part.enclosure`、`product.bottle`、`product.bowl`、`product.mug`、`product.screw_cap`、`product.vase` | 41 | 51 | 40 | ✅ 达标 | 11/11 |
| architecture | 2 | `architecture.fluted_column`、`architecture.tower.growth` | 20 | 27 | 40 | ⚠️ 短少 20 | 11/11 |
| organic | 1 | `organic.seashell` | 13 | 20 | 30 | ⚠️ 短少 17 | 5/8 |
| pattern | 1 | `pattern.honeycomb_panel` | 46 | 46 | 40 | ✅ 达标 | 11/11 |
| assembly | 1 | `assembly.bolt_nut` | 85 | 85 | 30 | ✅ 达标 | 8/8 |
| text | 1 | `text.engraved_plate` | 9 | 12 | 20 | ⚠️ 短少 11 | 3/3 |
| lattice | 1 | `lattice.gyroid_slab` | 13 | 15 | 20 | ⚠️ 短少 7 | 2/5 |
| surface | 4 | `surface.coons_duct`、`surface.dome`、`surface.fillet_network`、`surface.panel` | 95 | 95 | 30 | ✅ 达标 | 10/10 |
| tolerance | 1 | `tolerance.clearance_ladder` | 17 | 22 | 20 | ⚠️ 短少 3 | 6/6 |
| material_finish | 0 | — | 19 | 19 | 10 | ⛔ 无 family；语料 19/10 | 3/3 |

**短少的类别（substantive 语料未达标）**：architecture 20/40（缺 20）、organic 13/30（缺 17）、text 9/20（缺 11）、lattice 13/20（缺 7）、tolerance 17/20（缺 3）。

**没有 family 的类别**：material_finish。

`material_finish` 在矩阵里 `target_families: 0`（只要求语料样例，不要求 family），所以它**不算缺口**；`tests/test_coverage_gate.py` 也按这个规则豁免它。

## 4. 语料：总数与许可分层（实测）

| 指标 | 数量 |
|---|---|
| schema 有效记录 | 573 |
| accepted（Tier A+B） | **561** |
| ├ substantive（accepted 且有参数，覆盖口径） | **520** |
| └ reference-only（accepted 但无参数，不计入目标） | 41 |
| Tier A | 379 |
| Tier B | 182 |
| Tier C（不计入 accepted） | 12 |
| 家族聚类 / 可提名 | 见 `python3 work/tools/analyze.py`（输出粘在 `work/reports/final.md`） |

出处清单（逐 source 的 licence + URL）在 `corpus/DELIVERY.md`；逐条 provenance 在 JSONL 行内。Tier 规则：**只有 Tier A 保留 `code`**，`python3 work/tools/audit_provenance.py` 会打印 `code by tier` 并对此断言。

## 5. 「覆盖」的判据（§14 落地）

需求文档 §14 说：一个 family 要同时有 *参数 schema + 约束 + 测试 + verify 回执 + 感知签名 + 许可证* 才算进入覆盖。这里逐项对应到文件：

| 要件 | 落在哪 | 谁在守 |
|---|---|---|
| 参数 schema | `@recipe(params=...)`，`atta catalog --verbose` 可导出 | `tests/test_families.py::test_family_metadata_is_complete`（类型/单位/min/max/default 齐全） |
| 约束 | `@recipe(constraints=[...])` | 同上 + `test_every_family_resolves_all_presets` |
| 测试 | `tests/test_families.py`（闭合/单实体/无非流形）、`tests/test_coverage_gate.py`（类别 acceptance） | 全仓 `pytest tests/` |
| verify 回执 | `work/harvest/delivery/inventory.json` + 各 `work/reports/family-*.md` | `verify --level standard`，verdict 必须 `pass` |
| 感知签名 | `python3 -m atta perceive`（text+ASCII+section+sha256） | `tests/test_perception.py` |
| 许可证 | 语料侧 `corpus/DELIVERY.md`；实现侧为本仓库自有代码，参数命名对齐语料语义 | `audit_provenance.py` |

## 6. 现在还不行的（诚实清单）

1. **`tests/corpus/` 不存在**：需求 §10 建议「每个 family 的 nominal + boundary」做成独立测试。实际是 `tests/test_families.py` 只跑 **nominal**（每 family 一次构建），**boundary 用例以记录形式存在于 `work/reports/family-*.md`，不是每次 CI 重跑**。这是真缺口，不是措辞问题。
2. **family 内部实现质量不均**：`mechanical.flange` 与 `pattern.honeycomb_panel` 的亏格（7 / 35）是**孔数派生**的，参数一动亏格就变；它们的 `expected_genus` 只对 nominal 成立，换孔数后 verify 的 genus 检查会 fail（与 `lattice.gyroid_slab` 的单元数限制同类问题，那边已用 warn 约束公开说明）。
3. **`material_finish` 没有 family**（矩阵允许），语料 19 条只作参考。
4. 语料侧仍短少的类别见 §3。**短少就是短少**，没有用 reference-only 记录去凑目标数。

更细的取舍与失败分类在 `work/reports/final.md`（含「我们做不到什么」）。

## 7. 复现

```bash
cd $ATTA_ROOT
python3 -m pytest tests/ -q                      # 全仓门
python3 work/harvest/delivery/inventory.py       # 全部 recipe 的实测回执
python3 work/harvest/delivery/make_coverage_doc.py   # 重新生成本文
python3 work/harvest/delivery/make_delivery.py       # 重新生成 corpus/DELIVERY.md
python3 scripts/validate_corpus.py corpus/normalized/*.jsonl --coverage
python3 work/tools/analyze.py
```
