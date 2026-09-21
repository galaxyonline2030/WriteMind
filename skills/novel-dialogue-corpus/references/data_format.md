# 语料数据格式说明（dialog_pipeline.py 四产物）

`dialog_pipeline.py` 一次运行产出四个文件。层级关系：**弯引号对 → 对话单元（units）→ 就近聚合 → 对话场景（scenes）→ 两个 md 报告**；`pattern_cluster.py` 聚类只消费 scenes。

```
章节全文文本 (.txt/.md)
  │ 弯引号配对（每对 "…" = 1 单元）
  ▼
dialogue_units.json      数组 · 定位 + 说话人 + 六维指标
  │ 相邻间隔 ≤500 字聚 1 场景
  ▼
dialogue_scenes.json     数组 · participants/turns/tags/excerpt + 场景指标
  ├──► 对话场景清单.md    人读版目录（一景一节）
  └──► 对话特点归纳.md    全书统计报告（七节）
                           │
                           ▼
        pattern_cluster.py 仅消费 scenes → 12 簇语料库
```

## 1. dialogue_units.json（单元级）

JSON 数组，按全文出现顺序排列，**每对中文弯引号 "…" = 一个元素**（一"句"话轮）。字段分四组：

| 组 | 字段 | 说明 |
|---|---|---|
| 定位 | `char_start` / `char_end` / `line` | 引号在全文的字符偏移、所在行号 |
| | `chapter` / `chapter_label` / `title` / `file` | 章号、章标签（"第1章"）、章题、来源文件名 |
| 说话人 | `speaker` / `mode` | 识别出的说话人；`mode` = 识别方式（顺装/倒装/倒装无动词/代词/续接/未知） |
| 内容 | `type` / `content` / `length` | 话轮类型（陈述/疑问/感叹，按 ？！判定）、原文、字数 |
| 六维指标 | `ellipsis` / `rhetorical` / `lexicon_adv` / `adversarial` / `symbolic` / `avoidance` | 省略号留白 / 反问 / 对抗词典分（0–5，**未验证代理**）/ 是否≥1 / 称谓符号化（"山羊头"类描述称谓）/ 回避（前句疑问 + 本句疑问或极短或省略号） |

真实样例：

```json
{ "char_start": 412, "line": 27, "speaker": "山羊头", "mode": "倒装",
  "type": "陈述", "content": "早安，九位。", "length": 6,
  "ellipsis": false, "rhetorical": false, "lexicon_adv": 0.0,
  "adversarial": false, "symbolic": true, "avoidance": false, "chapter": 1 }
```

## 2. dialogue_scenes.json（场景级）

JSON 数组，按章节顺序。**聚合规则：相邻两个单元之间夹的叙述 > 500 字（`SCENE_GAP`）则切新场景**，否则并为一景。字段分四组：

| 组 | 字段 | 说明 |
|---|---|---|
| 标识 | `scene_id` | `CH0001-S01` = 第1章第1景 |
| 定位 | `chapter` / `chapter_label` / `title` / `file` / `start_line`–`end_line` / `char_start`–`char_end` | 场景在原文的行、字符区间（用于回原文核对） |
| 结构 | `turns` / `participants` / `tags` / `excerpt` | 轮数；去重排序的说话人；规则标签（质询/对抗、推理推演、留白/潜台词、威胁/冲突、群像多角色…，按关键词+人数打）；前 10 话轮预览（每条截 42 字） |
| 指标 | `switch_rate` / `ellipsis_rate` / `tension_mean` / `symbolic_rate` / `len_entropy` / `avg_len` | 说话人切换率（换人次数÷轮数-1）、省略号率、对抗词典分均值（**非张力**）、符号化率、句长分布熵（10 桶，越低节奏越均匀）、均句长 |

真实样例（节选）：

```json
{ "scene_id": "CH0001-S01", "chapter": 1, "chapter_label": "第1章",
  "file": "0001_第1章.md", "start_line": 27, "end_line": 87,
  "turns": 13,
  "participants": ["她", "山羊头", "清冷女人", "白大褂中年男人", "纹着花臂男人"],
  "tags": ["质询/对抗", "推理推演", "留白/潜台词", "威胁/冲突", "群像多角色"],
  "excerpt": ["山羊头：早安，九位。", "纹着花臂男人：你……是谁？"],
  "switch_rate": 0.692, "ellipsis_rate": 0.231,
  "tension_mean": 0.5, "symbolic_rate": 0.923,
  "len_entropy": 2.565, "avg_len": 31.0 }
```

`pattern_cluster.py` 聚类只使用：`turns`、`participants`、`switch_rate`、`ellipsis_rate`、`tags`、`excerpt` —— 对 units 无依赖，可独立重跑。

## 3. 对话场景清单.md（人读版目录）

scenes 的 Markdown 渲染，一景一节：

```markdown
## CH0001-S01 ｜ 第1章《空屋》
- **位置**：第 27–87 行
- **参与角色**：她、山羊头、清冷女人、白大褂中年男人…
- **对话轮数**：13 ｜ **特征**：质询/对抗、推理推演、留白/潜台词…
- **摘要**：
  - 山羊头：早安，九位。
  - 纹着花臂男人：你……是谁？
```

用途：人工翻阅定位场景、核对聚类抽样是否抽对；不做机器输入。

## 4. 对话特点归纳.md（全书统计报告）

固定七节：

1. **话轮类型分布** — 陈述/疑问/感叹占比
2. **说话人识别模式** — 顺装/倒装/代词/续接各多少（反映识别质量）
3. **高频说话人 Top30** — 标注"本名/符号化"
4. **六维指标基线表** — 省略号率、反问密度、对抗词占比、短句率、均句长、burstiness、符号化率、TSC、回避率（即这本书的"原作锚定区间"）
5. **场景特征标签分布**
6. **章节对话密度 Top30**
7. **编号条目的写作特点归纳** — 对话即推演/白描驱动/留白…（数值代入）

用途：给后续 AI 生成对话当"基线对照"——生成对话的各项指标应逼近原作区间，用 Δ 度量"AI 味"；也是换书时的关键发现来源（如"96% 是群像"）。

## 分工一句话

**两个 json 是机器语料**（units 供质检/细粒度分析，scenes 供聚类与 few-shot 抽样），**两个 md 是给人看的报告**（清单管"在哪"，归纳管"长什么样"）。
