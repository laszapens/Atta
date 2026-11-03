# DELIVERY — 语料出处与再分发清单 (attribution manifest)

> 生成于 2026-11-02 21:28，从当时采集到的记录现算，不是抄来的数字。
> 本文件是那次采集的**静态快照**：原始记录、原始快照与采集脚本均不在本仓库内分发。

## 0. 复用口径（先读这一段）

这批语料是**研究材料**，不是 Atta 的代码来源。进入 Atta 实现层的只有
**family 模板 + 参数 schema + 约束 + 测试**（`atta/families/*.py`、`tests/`），
它们由本项目重写，参数名与设计规则对齐语料语义，**没有复制语料代码**。

| 问题 | 答案 |
|---|---|
| 语料会被执行吗？ | 不会。抓取阶段只解析（AST/正则），从不 import/exec。 |
| Tier B 有 `code` 吗？ | 没有。`code` 字段在 Tier A 之外一律清空。 |
| Tier C 进 accepted 吗？ | 不进。只留元数据 + 拒绝原因。 |
| 语料代码进 Atta 了吗？ | 没有。见上方复用口径。 |

下表是这个口径的实测结果：只有 Tier A 携带 `code`，B/C 的 `code` 一律清空。
原始记录不在本仓库内，因此这条断言只能在内部采集环境里复跑，无法在公开 checkout 上重验。

## 1. 数字（实测）

下表是采集快照的计数。原始记录、拒绝记录与采集脚本都不随仓库分发。

| 指标 | 数量 |
|---|---|
| schema 有效记录 | 573 |
| accepted（Tier A+B，licence_ok） | **561** |
| ├ Tier A | 379 |
| ├ Tier B | 182 |
| └ Tier C（accepted 之外） | 12 |
| reference_only（accepted 但无参数） | 41 |
| accepted 中带 `code` 的记录 | 见 `license_report.json:policy_checks`（Tier B/C 带 code 的清单必须为空，实测为空） |
| 带 code 且非 Tier A 的记录 | **0** |
| 拒绝记录（采集时的 rejected 集合，未分发） | 159 |
| 不同出处（source） | 24 |

按 kind：build123d 3、cadquery 42、docs 21、mesh 139、openscad 299、other 51、sdf 6

按 category：architecture 27、assembly 85、lattice 15、material_finish 19、mechanical 99、organic 20、pattern 46、primitive 70、product 51、surface 95、text 12、tolerance 22

> 上表与同目录 `license_report.json`（`total_records` / `accepted` / `substantive` /
> `reference_only` / `by_tier`）一致；`coverage_report.json` 的 `categories[].accepted`
> 给出按类别的同一口径拆分。
>
> 口径提醒：采集脚本 `corpus.py stats` 的 `reference_only` = accepted 但**无参数**；
> `scripts/validate_corpus.py` 的 `reference-only` = **未 accepted**（即 Tier C）。两者不是同一个数。

## 2. 逐出处署名（accepted）

每一行的 licence 取自记录 `source.license_spdx`（抓取时从仓库 LICENSE / 平台页面实取，
并逐条复核 `source` 是否真的在盘上）。
**逐条**的 `source.url / path / commit / retrieved_at` 在对应 JSONL 行里，
另有 `corpus/license_report.json`（QA 组）做逐域名复核。

### 2.1 Tier A — permissive（MIT / BSD / Apache / MPL / CC0 / 0BSD / public-domain）

Tier A 是唯一保留 `code` 字段的一档；Atta 从中读的是**结构与参数**。

| source | licence (SPDX) | records | categories | canonical URL |
|---|---|---|---|---|
| `https://github.com/BelfrySCAD/BOSL2` | BSD-2-Clause | 169 | assembly, mechanical, pattern, primitive, product, surface | https://github.com/BelfrySCAD/BOSL2 |
| `https://github.com/revarbat/BOSL` | BSD-2-Clause | 63 | assembly, mechanical, primitive, surface | https://github.com/revarbat/BOSL |
| `https://github.com/gumyr/cq_warehouse` | Apache-2.0 | 39 | assembly, mechanical, tolerance | https://github.com/gumyr/cq_warehouse |
| `www.printables.com` | CC0-1.0 | 26 | architecture, mechanical, organic, primitive, product, text, tolerance | https://www.printables.com |
| `https://github.com/curv3d/curv` | Apache-2.0 | 18 | lattice, material_finish, organic, surface, text | https://github.com/curv3d/curv |
| `https://github.com/alexadam/context-free-art` | MIT | 11 | architecture, organic | https://github.com/alexadam/context-free-art |
| `https://github.com/Irev-Dev/Round-Anything` | MIT | 11 | surface | https://github.com/Irev-Dev/Round-Anything |
| `https://github.com/rcolyer/threads-scad` | CC0-1.0 | 10 | assembly | https://github.com/rcolyer/threads-scad |
| `https://github.com/deadsy/sdfx` | MIT | 6 | assembly, lattice, material_finish, surface, text | https://github.com/deadsy/sdfx |
| `https://github.com/elalish/manifold` | Apache-2.0 | 5 | lattice, material_finish, surface | https://github.com/elalish/manifold |
| `https://github.com/jscad/OpenJSCAD.org` | MIT | 5 | material_finish, surface, text | https://github.com/jscad/OpenJSCAD.org |
| `https://github.com/adrianschlatter/threadlib` | BSD-3-Clause | 3 | assembly | https://github.com/adrianschlatter/threadlib |
| `https://github.com/CadQuery/cadquery` | Apache-2.0 | 3 | pattern, primitive, surface | https://github.com/CadQuery/cadquery |
| `https://github.com/gumyr/build123d` | Apache-2.0 | 3 | surface | https://github.com/gumyr/build123d |
| `https://github.com/hannobraun/fornjot` | 0BSD | 3 | material_finish, surface | https://github.com/hannobraun/fornjot |
| `https://github.com/susam/pov25` | MIT | 3 | architecture | https://github.com/susam/pov25 |
| `github.com/elalish/manifold` | Apache-2.0 | 1 | material_finish | https://github.com/elalish/manifold |

### 2.2 Tier B — share-alike / documentation（CC-BY / CC-BY-SA / LGPL / GPL-doc）

Tier B 不保留 `code`，只保留参数、结构、出处；与 MIT 实现代码隔离。

| source | licence (SPDX) | records | categories | canonical URL |
|---|---|---|---|---|
| `www.printables.com` | CC-BY-4.0, CC-BY-SA-4.0 | 113 | architecture, assembly, lattice, mechanical, organic, pattern, primitive, product, text, tolerance | https://www.printables.com |
| `https://github.com/openscad/MCAD` | LGPL-2.1 | 39 | assembly, mechanical, primitive, surface | https://github.com/openscad/MCAD |
| `en.wikibooks.org` | CC-BY-SA-4.0 | 11 | lattice, material_finish, surface, text | https://en.wikibooks.org |
| `github.com/libfive/libfive` | MPL-2.0 | 7 | lattice, material_finish, organic, surface | https://github.com/libfive/libfive |
| `https://github.com/libfive/libfive` | MPL-2.0 | 6 | architecture, material_finish, organic, surface | https://github.com/libfive/libfive |
| `https://github.com/JustinSDK/dotSCAD` | LGPL-3.0 | 4 | primitive, surface | https://github.com/JustinSDK/dotSCAD |
| `cadquery.readthedocs.io` | CC-BY-SA-4.0 | 1 | material_finish | https://cadquery.readthedocs.io |
| `docs.blender.org` | CC-BY-SA-4.0 | 1 | material_finish | https://docs.blender.org |

### 2.3 Tier C — 不进入 accepted

Tier C（GPL/AGPL/unknown/无证）按 BRIEF 硬规则 1 **禁止**写入 `code`、
禁止计入 accepted。它们只以元数据 + 拒绝原因存在。

| source | licence | 记录数 | 说明 |
|---|---|---|---|
| `https://github.com/MtnViewJohn/context-free` | GPL-2.0 | 1 | 仅元数据，无 code，不计入 accepted |
| `ten-thousand-models.appspot.com` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `deep-geometry.github.io` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `github.com/AutodeskAILab/Fusion360GalleryDataset` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `github.com/ChrisWu1997/DeepCAD` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `shapenet.org` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `objaverse.allenai.org` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `github.com/SadilKhan/Text2CAD` | CC-BY-NC-SA-4.0 | 1 | 仅元数据，无 code，不计入 accepted |
| `github.com/filaPro/cad-recode` | CC-BY-NC-4.0 | 1 | 仅元数据，无 code，不计入 accepted |
| `github.com/hducg/MFCAD` | NOASSERTION | 1 | 仅元数据，无 code，不计入 accepted |
| `structuresynth.sourceforge.net` | GPL-3.0 | 1 | 仅元数据，无 code，不计入 accepted |
| `https://github.com/Haskell-Things/ImplicitCAD` | AGPL-3.0 | 1 | 仅元数据，无 code，不计入 accepted |

## 3. 平台级结果（模型社区）

下表是平台级取数结论（原始证据随采集快照保管，不在本仓库内分发）。

| 平台 | 结果 | 依据 |
|---|---|---|
| Printables (api.printables.com GraphQL) | **可达**：790 个 licence-clean 模型被枚举，517 个取到完整元数据，363 条记录按配额收敛为 118 条 | 采集快照 §1 |
| Printables（网页） | **被 Cloudflare 拦**（含 sp.py TLS 指纹） | `PLATFORMS.md` §1 |
| Thingiverse | 页面元数据可读，但唯一参数源 Customizer 被 robots.txt `Disallow`；**贡献 0 条 mesh 记录** | 采集快照 §4 |
| Thangs / GrabCAD | 被 Cloudflare / CloudFront 拦 | `PLATFORMS.md` §1 |
| Instructables | JS-only，正文取不到 | `PLATFORMS.md` §1 |
| Wikifactory | 返回空 body | `PLATFORMS.md` §1 |

> Printables 记录全部是 **kind=mesh、code 为空** 的 reference-only / 元数据记录：
> 没有下载任何模型文件或 CAD 源，参数来自公开参数表/文件名的证据。

## 4. 数据集（kind=dataset，全部 Tier C，只做许可结论）

这些记录是**许可元数据**，
不是可用语料：没有一份数据集许可允许我们把它的几何并入 Atta。

## 5. 复现

公开 checkout 上没有原始记录可供复算，因此本文件**不可就地重跑**；它是采集时的
结论快照。仓库内可复跑的只有语料 schema 校验（需要你自己提供 JSONL）：

```bash
python3 scripts/validate_corpus.py path/to/records/*.jsonl --coverage
```

机器可读的同批结论在 `corpus/license_report.json` 与 `corpus/coverage_report.json`。
原始快照（逐字节 + LICENSE 副本 + `_provenance.json`）与采集脚本由维护者单独保管，
不随仓库分发。
