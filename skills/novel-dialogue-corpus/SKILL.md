---
name: novel-dialogue-corpus
description: 小说对话语料库构建技能。当用户要求"对某本小说建立对话语料库 / 提取对话场景 / 做对话模式聚类 / 把新书语料并入已有 patterns 语料库"时使用。对任意指定的小说文本（单文件或章节目录）执行：对话单元与场景抽取 → 质量抽检 → 结构签名聚类（说话人数×轮数×切换率）→ 隔离子代理产模式卡 → 与已有语料库整合（样本级合并或场景级重聚类）。全程仅使用技能目录内脚本，纯标准库，无外部依赖。
agent_created: true
---

# 小说对话语料库构建技能

把任意小说文本加工为可复用的"对话模式语料库"：结构化对话场景 + 按结构签名聚类的模式簇 + 每簇模式卡，并能与既有语料库整合。方法论出处：结构签名聚类（说话人数 P × 轮数 T × 切换率 S，不靠饱和 tag）+ 隔离子代理产卡。

## 目录结构（技能自包含，不引用技能外任何资源）

```
scripts/
  dialog_extract.py     抽取核心库（弯引号配对/说话人识别/六维单元指标），勿单独跑
  dialog_pipeline.py    步骤1：小说文本 → dialogue_units.json + dialogue_scenes.json
  corpus_qa.py          步骤1.5：说话人 Top-N + 代词桶抽样质检
  pattern_cluster.py    步骤2：场景 → 结构签名聚类 → _cluster_XX.json + _cluster_index.json
  merge_corpus.py       步骤4：新语料并入已有语料库（先自动备份）
references/
  method.md             方法论细节与关键参数口径
  pattern_card.md       模式卡写作模板与示例
  data_format.md        四产物（units/scenes/两个md）的数据格式与字段说明
```

## 工作流（按序执行）

### 步骤 0：确认输入与输出
- 输入 = 用户指定的小说文本：单个 `.txt`/`.md` 文件（自动按「第X章」行切分；无标记则整体一章）或章节目录（每个 `.md`/`.txt` 文件一章）。
- 与用户确认两个路径：`工作目录`（存放本次中间产物与语料库）和 `已有语料库目录`（若用户要求整合；该目录须含 `_cluster_XX.json` 与 `_cluster_index.json`）。**不要假设任何默认路径。**
- Python：仅标准库，任意 3.10+ 均可。

### 步骤 1：抽取
```bash
python scripts/dialog_pipeline.py --input <小说.txt|章节目录> --out-dir <工作目录>/data --title <书名>
```
产出 `dialogue_units.json`（单元级）、`dialogue_scenes.json`（场景级）、`对话场景清单.md`、`对话特点归纳.md`。字段级格式见 `references/data_format.md`。

### 步骤 1.5：质量抽检
```bash
python scripts/corpus_qa.py --units <工作目录>/data/dialogue_units.json
```
判读：Top 说话人表用于发现识别噪声；代词桶可疑比例 >30% 时，先检查该小说引号格式（本抽取器只配对中文弯引号 " "，「」直角引号对话不被提取——若目标小说用直角引号，须先扩展 `dialog_extract.py` 的引号配对并重跑）。

### 步骤 2：结构签名聚类
```bash
python scripts/pattern_cluster.py --scenes <工作目录>/data/dialogue_scenes.json:<书名> --out-dir <工作目录>/patterns
```
产出 12 个左右 `_cluster_XX.json`（每簇 ≤16 个多样本抽样，含 source 标记）+ `_cluster_index.json`。合格线：`turns≥4 且 不同说话人≥2`。

### 步骤 3：隔离子代理产模式卡
- 对每个簇：读取 `references/pattern_card.md` 模板 → 为该簇派一个**隔离子代理**，只把该簇 `_cluster_XX.json` 传给它（主上下文不读原文，防污染）→ 子代理产出 `NN_<簇名>.md` 模式卡，写入 `<工作目录>/patterns/`。
- 卡中"真实对白示例"须引用簇文件 excerpt 中的原句。
- 生成 `patterns/README.md`：模式卡索引表（签名 / 规模 / essence 一句话）、关键发现（如群像占比）、构建方式记录。

### 步骤 4：与已有语料库整合（用户要求时）
先**必须**向用户确认已有语料库目录，再选模式：
- **样本级（默认，保留旧簇结构）**：
  ```bash
  python scripts/merge_corpus.py --corpus-dir <已有语料库> --scenes <新书scenes.json> --source <书名>
  ```
  新书合格场景按签名归入对应簇（每签名 ≤8 抽样，簇样本上限 32）；新签名自动建新簇。脚本先把旧簇文件备份到 `backup_<时间戳>/`，并写 `merge_report.json`。
- **场景级（用户能提供旧书 `dialogue_scenes.json` 时，更彻底）**：
  ```bash
  python scripts/merge_corpus.py --corpus-dir <已有语料库> --scenes <新书scenes.json> --source <书名> --old-scenes <旧书scenes.json>
  ```
  新旧场景合并整体重聚类，签名可能重排：**完成后逐簇核对新旧签名映射，更新受影响的模式卡**（卡与签名一一对应的约定见下游引擎 `SIG_TO_CARD` 类映射，若有须同步刷新）。
- 整合后：在 `patterns/README.md` 追加"来源分布"说明（各签名 `source_counts`）。

### 步骤 5：交付报告
向用户汇总：总对话句数 / 场景数 / 合格率、簇数与各簇规模（含来源分布）、质检结论、产出文件清单、（若整合）备份目录与 merge_report 要点。

## 硬约束
- 所有脚本仅用标准库；不在脚本内外引用任何固定磁盘路径，一切路径由运行时参数给定。
- 不得直接修改用户已有语料库的模式卡内容；整合只动 `_cluster_*.json` / `_cluster_index.json` / `merge_report.json`，卡片更新须人工核对签名映射后进行。
- 对抗词典分（`lexicon_adv`/`tension_mean`）是【未验证词典代理】，报告中必须保留"非张力度量"注记，不得当张力基线。
- 簇样本偏小（≤6 场景）的簇在 README 中标注"与相邻簇重叠，作补充参考"。
