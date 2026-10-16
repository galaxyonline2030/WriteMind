---
name: prompt-iteration-optimizer
version: "2.3.0"
display_name: 提示词迭代优化
display_name_en: Prompt Iteration Optimizer
description: "把薄弱、口语化或结构不清的提示词改写为结构化、可落地的高质量提示词（默认 LangGPT 风格）。采用 agent-first 三段式文件协议：确定性引擎做前处理与 G1-G10 门禁，改写与迭代由独立 fulfil 子代理经 dispatch 任务卡执行，收敛后强制无上下文盲评轮（G10），主代理只做文件编排。不依赖外部 LLM SDK client，语义质量由模板与子代理兑现，最终以真实用例验收。触发词：优化提示词、改写 prompt、把提示词写得更结构化、提示词效果不好、提示词工程、结构化提示词、LangGPT、rewrite prompt、make my prompt better、improve this system prompt。"
description_zh: "把薄弱、口语化或结构不清的提示词改写为结构化、可落地的高质量提示词（默认 LangGPT 风格）。采用 agent-first 三段式文件协议：确定性引擎做前处理与 G1-G10 门禁，改写与迭代由独立 fulfil 子代理经 dispatch 任务卡执行，收敛后强制无上下文盲评轮（G10），主代理只做文件编排。不依赖外部 LLM SDK client，语义质量由模板与子代理兑现，最终以真实用例验收。触发词：优化提示词、改写 prompt、把提示词写得更结构化、提示词效果不好、提示词工程、结构化提示词、LangGPT、rewrite prompt、make my prompt better、improve this system prompt。"
description_en: "Rewrite weak, colloquial, or loosely-structured prompts into structured, actionable, high-quality prompts (LangGPT style by default). Uses an agent-first three-stage file protocol: a deterministic engine handles pre-processing and the G1-G10 gates; rewriting and iteration are performed by isolated fulfil sub-agents via dispatch task cards; after convergence a context-free blind-judge round (G10) is mandatory, while the main agent only orchestrates files. No external LLM SDK client required; semantic quality is delivered by templates and sub-agents, and finalized by real-use-case acceptance. Trigger words: optimize prompt, rewrite prompt, structure this prompt better, prompt engineering, LangGPT, make my prompt better, improve this system prompt."
agent_created: true
---

# 提示词迭代优化 v2.3（agent-first + 结构性子代理 + 可视化报告）

> **⚠ 用前必读：[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)**——全部已知问题、假阳性反例、
> 修复状态与元教训。规则引擎只当下限；本技能的正确用法是：规则引擎当下限 →
> 门禁防退步 → 子代理盲评当第二双眼睛 → **真实用例做最终验收**。

## 0. 一句话定位

把一段原始 / 口语化提示词，升级为角色清晰、约束可判定、可落地的结构化提示词
（默认 LangGPT 结构，模板移植自原项目 prompt-optimizer）。
v2.1 引擎换位：**确定性引擎只做前处理（诊断+patchPlan）与后门禁（契约+G1–G10）；
改写、迭代与盲评是生成式接缝，必须由独立子代理执行——主代理只做文件编排，
不代写、不改稿、不打分。**
能力边界与原项目对照见 `CAPABILITY_PARITY.md`。

## 1. 何时使用

- 用户说"优化 / 改写 / 改进这段提示词""帮我写个更好的 system prompt""我的 prompt 效果不好"。
- 用户交付了口语化、缺角色、缺步骤、含模糊词（"一些 / 尽量 / 大概 / 若干"）的提示词。
- 需要评估两条提示词优劣、给提示词打分、做红队审稿或变量抽取。

## 2. 硬性约定（llm-client-sim 范式 + v2.1 修正）

1. **agent-first**：改写（`rewrite_informed`）与迭代（`iterate_directed`）无确定性替身——
   脚本内不产内容，只经 fulfil 文件注入 realizer 过接缝 + 契约校验（B1/B3 修复）。
2. **子代理结构性参与（非可选）**：每一轮改写由 `--dispatch` 渲染的自包含任务卡
   交给**独立 fulfil 子代理**执行；收敛后强制 AWAIT_JUDGE 盲评轮，由**第二个无
   上下文 judge 子代理**只看终稿打分。主代理只 spawn 子代理 + 传文件（实测纠正：
   主代理直写 = 上下文污染 + 运动员兼裁判）。
3. **确定性接缝零 LLM**：analyze / evaluate / compare / patchPlan / G7-G9 规则器直接运行。
4. **三段式文件协议**：`state.json` 是唯一事实源；退出码 10 = 交接信号（非错误），
   0 = 可交付，1 = 回修。
5. **issues 回灌**：每轮迭代的"新需求"由上轮剩余 issues 自动转为 patchPlan（修 B4）；
   盲评 blocking_conflicts 亦回灌为一次有界返工（二次冲突 → FAIL）。
6. **两层收敛（v2 增强 #6，2026-10-10）**：结构层（issues 清零 + 无 blocking_conflicts）
   **且** 质量层（盲评分 ≥ `--min-blind-score` 目标线 且 无单维 < 60 危险线）。
   未达标 → judge weaknesses/patchPlan 回灌为**质量返工**（受 `--quality-reworks`
   上限保护，默认 2 次，返工后无改善即 plateau 提前停）；耗尽仍未达标 → **FAIL**
   （与二次冲突对称，不允许带病交付）。阈值默认 85 为**临时值**（校准 n=1），传 0 关闭质量门禁。
   **Plan A 选优（v2.3）**：任何终态（PASS / FAIL / 达迭代上限）均自动交付「盲评分最高且无
   blocking_conflicts」的那一轮，而非最后一轮；`last_final` 原样留档，选优轮写入 `delivered_final`
   并展示于 `report.html`。无合格盲评记录时退回最后一轮。
6. `scripts/` 内绝不 import / 调用任何 LLM SDK。

## 3. 工作流（三段式乒乓协议 + 强制盲评）

1. **收口需求**：确认 mode（system | user）、场景（general | analytical | format |
   basic | planning | professional）、轮数上限（结构循环 `max_rounds`，默认 2）。另有**全局最大迭代次数 `max_iterations`**（默认 5，可 `--max-iterations` 指定），结构循环与所有返工合计计入此上限，达上限即强制终止并交付历史最优轮。用户不答用默认值并在报告中标注。

   **轮数上限怎么定**（`max_rounds`，命令行第 3 个位置参数，默认 2）：

   | 取值 | 适用 |
   |---|---|
   | 1 | 只想改写一次。⚠ 第 1 轮没清完 issues 会直接 FAIL，风险高 |
   | **2（默认）** | 简单 / 中等提示词 |
   | **3** | 复杂提示词（推荐）——给 issues 回灌留出空间 |
   | ≥5 | 不推荐一开始就设大 |

   设大 ≠ 跑满：迭代在 issues 清零时立刻停止，上限只是天花板。设太大的代价是
   每轮多烧一次子代理调用，且 `plateau`（连续 2 轮 delta < 5）会提前判 FAIL。
   真不够用时三条路：冲突返工**自动 +1**（计入全局 `max_iterations`）；手工改 `state.json` 的 `max_rounds`
   （同时把 `round` 改成下一轮、`phase` 改 `AWAIT_FULFILL`——注意是
   **`AWAIT_FULFILL` 两个 L**）；或重新起一个 run。
   当前进度随时用 `--status` 看：`round=X/Y`。
   **全局最大迭代次数 `max_iterations`（默认 5，可 `--max-iterations N` 指定）**：
   是结构循环 + 所有返工的**合计硬天花板**。冲突返工、质量返工都会让 `max_rounds` 自增，
   真正约束"总轮数不爆"的是 `max_iterations`；达上限立即终止并交付历史最优轮（Plan A 选优）。
2. **Phase A（脚本）**：`cd scripts && python run_pipeline.py "@raw.txt" system 2 general`
   → 诊断、patchPlan、路由模板 → 写 `runs/<ts>/state.json` → **exit 10**。
3. **派发 fulfil 子代理（结构性触点①）**：
   `python run_pipeline.py --dispatch runs/<ts>` → 渲染自包含任务卡
   `dispatch/roundN.md` → 用 **Agent 工具 spawn 独立子代理**，把任务卡**原文**交给
   它执行（第 1 轮 informed 改写；第 2 轮起 iterate_directed 融合修改）→ 子代理把
   终稿写入 `runs/<ts>/fulfil/roundN.txt`。主代理不得代写。
4. **Phase B（脚本）**：`python run_pipeline.py --resume runs/<ts>` → fulfil 注入 realizer
   → 契约校验 → G7/G9 → evaluate/compare → 收敛判定：
   - issues 清零 → 进入 **AWAIT_JUDGE** → **exit 10**（走第 5 步盲评）
   - 仍有 issues 且未达上限 → 回灌为第 N+1 轮 patchPlan → **exit 10**（回到第 3 步）
   - delta 停滞（连续 2 轮 < 5）或轮数耗尽仍有 issues → **exit 1**
5. **盲评 judge 子代理（结构性触点②，C3 独立性）**：
   `--dispatch runs/<ts>` → 渲染 `dispatch/judge.md` → 用 Agent 工具 spawn 一个
   **全新的、无本会话上下文**的子代理（只看终稿文本，无原始稿/历史/分数）→
   judge 把评审 JSON 写入 `runs/<ts>/fulfil/judge.json` → `--resume` 裁决：
   - `blocking_conflicts` 为空且盲评达标（≥ 目标线且无 <60 低维）→ G10 PASS →
     全量 G1–G10 门禁 → **exit 0**（可交付）
   - 有冲突且未返工过 → 冲突 + judge patchPlan 回灌，配额 +1，**exit 10**（回到第 3 步）
   - 返工后二次冲突 → **exit 1**（不允许带病交付；仍按 Plan A 选优交付历史最优轮）
   - 无冲突但盲评未达标 → **质量返工**：weaknesses/patchPlan 回灌（返工卡附
     judge 弱点背景），配额 +1，**exit 10**（回到第 3 步）；返工次数耗尽或
     分数无改善（plateau）或达全局 `max_iterations` → **exit 1**（verdict=BELOW_TARGET，仍按 Plan A 选优交付历史最优轮）
6. **重派**：接缝契约或门禁失败 → 脚本附反馈 exit 10，主代理把它并入重新渲染的
   任务卡再派子代理；每轮上限 3 次，耗尽 exit 1。
7. **交付（Plan A 选优）**：交付「盲评分最高且无 blocking_conflicts」的那一轮终稿（非末轮）+ report.json 摘要（剩余 issues、门禁明细、盲评结论、实际交付轮次 `delivery_selected_round`）。
8. **真实用例验收（强制）**：宣告完成前，必须拿 ≥1 个真实输入跑一遍终稿，把输出留档
   到 run 目录并向用户展示。脚本层只能提醒，无法代劳——这是最后的质量关口。
9. **生成可视化报告（v2.2 起）**：`python run_pipeline.py --report runs/<ts>`
   → 产出零依赖 `runs/<ts>/report.html`，含①每轮**改进点与改进评价**（diff +
   patchPlan + 有效/空转/退步判定）②**完整运行跟踪**（每轮脚本命令、子代理任务卡
   原文、子代理产出原文、派发→产出延迟）③门禁 G1–G10 ④逐次盲评 ⑤原文与终稿。
   交付时把它一并 `present_files` 给用户。

## 4. 命令速查

```bash
cd scripts
# 三段式协议（主路径；子代理触点①②均为 --dispatch + Agent 工具 spawn）
python run_pipeline.py "@raw.txt" system 2 general   # Phase A → exit 10
# 可选 flag：--min-blind-score N（质量目标，默认 85 临时值，0=关闭）
#            --quality-reworks N（质量返工上限，默认 2）
#            --max-iterations N（全局最大迭代次数，默认 5；结构循环+返工合计计入）
python run_pipeline.py "@raw.txt" user 3 professional --min-blind-score 85
python run_pipeline.py --dispatch runs/<ts>          # 渲染子代理任务卡 → exit 10
python run_pipeline.py --resume runs/<ts>            # Phase B / 盲评裁决 → 0 / 10 / 1
python run_pipeline.py --status  runs/<ts>
python run_pipeline.py --report  runs/<ts>   # 生成可视化报告 runs/<ts>/report.html
python run_pipeline.py --legacy "@raw.txt" "@final.md"   # 旧式一步门禁（G10 记 SKIP）

# 确定性单命令
python optimize_cli.py analyze   "@prompt.txt"   # 诊断 + patchPlan
python optimize_cli.py patchplan "@prompt.txt"   # issues → 改写指令
python optimize_cli.py lint      "@prompt.txt"   # G7 自洽性
python optimize_cli.py vars      "@prompt.txt"   # {{var}} 抽取
python optimize_cli.py evaluate  "@prompt.txt"   # 0-100 分
python optimize_cli.py compare   "@a.txt" "@b.txt"

# 生成式接缝探测（无 realizer 时 exit 10 并给协议指引）
python optimize_cli.py informed|iterate|redteam ...
```

## 5. 交付门禁（G1–G10，verify_delivery.py）

| 门禁 | 含义 |
|------|------|
| G1/G2/G3 | 基线诊断已跑 / 迭代管线已跑（协议模式从 state.json 取证）/ 最终稿已复检 |
| G4/G5 | 剩余 issues 清零 / 分数确实提升（winner=after 且 delta>0） |
| G6 | 非通用模板产物（不得等于 rewrite_stub 空壳） |
| G7 | 自洽性 lint：互斥约束（"一句话" vs "五个部分"、禁止输出 vs 要求输出、元约束失效） |
| G9 | 变量完整性：原文全部 {{var}} 逐字保留（对齐原项目 iterate 自检） |
| G10 | 盲评已通过：独立无上下文 judge 子代理 verdict=PASS，且（若启用质量目标）盲评分 ≥ 目标线且无单维 <60 低维（协议模式强制；legacy 记 SKIP） |

已知门禁：互斥约束样例（"一句话" vs "五个部分"）须被 G7 拦截（exit 1）；变量丢失须被 G9 拦截。

## 6. 模板路由（templates/，移植自原项目模板家族）

| mode | scenario | 模板 | 对齐源 |
|------|----------|------|--------|
| system | general（默认） | optimize_general.md | general-optimize.ts |
| system | analytical | optimize_analytical.md | analytical-optimize.ts |
| system | format | optimize_output_format.md | output-format-optimize.ts |
| user | basic / planning / professional | user_optimize_*.md | user-prompt-* 三档 |
| 第 2 轮起 | — | iterate_directed.md | iterate.ts v3.0.0 |
| 按需 | 深度评审 | evaluation_judge.md | evaluation/content.ts（develop；每维 0-100 + overall） |
| 按需 | 评估后改写 | evaluation_rewrite.md | evaluation-rewrite/generic.ts |
| 按需 | 红队审稿 | redteam_review.md | README 演示显式化（v2 净增强） |
| 按需 | 变量抽取 | variable_extract.md | variable-extraction/extraction.ts |

## 7. 进度可见性（规则保留，协议加固）

1. 每个阶段开始先播报将做什么；2. 每步结果实时打印（run_pipeline 已内置）；
3. 接缝触发、门禁结果、轮次 delta 必须可见；4. 失败必须附反馈重派，不静默重试；
5. 交付必须展示 report.json 摘要与真实用例输出；6. 用户看不见 = 没发生；
7. 不 resume 就没有 report——协议本身使"跳步"无法产出交付物。

## 8. 文件地图

```
prompt-optimizer/
  SKILL.md                  本文件（v2 编排协议）
  USAGE.md                  技能使用文档（场景全景，用户向）
  DEEP_ANALYSIS_AND_PLAN.md 技术实现方案 v2.1（as-built，权威技术文档）
  CAPABILITY_PARITY.md      与原项目的源码级能力对照
  KNOWN_ISSUES.md           实战问题清单（能力边界，用前必读）
  templates/                强约束模板库（11 个，移植自原项目）
  agents/optimize_prompt.md 两张子代理卡（Fulfil 改写 + 盲评 Judge）执行说明
  references/               参考文档（何时读，写在每行末尾）
                            ├ langgpt_template.md   LangGPT 骨架 + 常见反模式表
                            │                       —— 改写 / 自查时可参照
                            ├ workflow.md           引擎契约速查（命令、退出码、
                            │                       门禁）—— 批量 / 自动化调用前必读，
                            │                       v2.2 已重写（勿再引用旧版"主代理直出"）
                            └ paradigm.md           虚拟客户端范式的设计决策
                                                    （为何零 LLM SDK、契约即边界）
  scripts/
    contract.py             结果契约（11 形状，含 SHAPE_BLIND_REVIEW）
    virtual_llm_client.py   接缝内核（call/chat/memoize/realizer）
    stubs.py                确定性规则器 + G7/G9 + patchPlan 构造
    optimizer_adapter.py    渲染 + 全部接缝（模板装载与路由、judge 消息）
    run_pipeline.py         协议编排器（--dispatch / AWAIT_JUDGE / exit 10/0/1）
    report_gen.py           可视化报告生成器（--report，确定性，零 LLM）
    verify_delivery.py      G1–G10 交付门禁
    optimize_cli.py         命令行入口
  runs/                     每次运行留档（state/report 可复现）
                            ├ dispatch/  每轮子代理任务卡
                            ├ fulfil/    每轮子代理产出 + judge_r<N>.json 盲评归档
                            ├ report.json  结论摘要
                            └ report.html  可视化报告（--report 生成）
```

## 9. 失败处理

- 契约校验 / G7-G9 失败 → 附反馈重新派发 fulfil 子代理覆盖同一文件，每轮 ≤3 次，耗尽 exit 1。
- judge.json 契约失败 → 重新派发 judge 子代理，≤2 次；盲评二次冲突 → exit 1。
- 子代理偏离模板 → 回派并附"只输出终稿本身"约束，≤3 次。
- issues 停滞或轮数耗尽（含达全局 `max_iterations`）→ exit 1 并明示剩余问题；禁止"看着差不多"就交付；仍按 Plan A 选优交付历史最优轮。
- 诚实边界：G7 拦已知互斥模式；语义级矛盾由盲评 judge 兜底，双层仍可能双漏
  （KNOWN_ISSUES C4）；真实用例验收（第 3 节第 8 步）是最终关口。

## 10. 可视化报告（v2.2）

`report.html` 是**零依赖单文件**，双击即可打开，全部内容由脚本确定性渲染，
不含任何模型推断——报告只呈现留痕，不替用户下"看起来不错"的结论。

| 区块 | 内容 |
|------|------|
| 总览 | **盲评质量分（LLM-as-judge · 0-100，全系统唯一对外分数，对齐上游 evaluation/content.ts）**、诊断问题数轨迹（收敛=清零）、G1–G10 门禁表、结论徽标 |
| 每轮改进 | 分数/字符变化、**改前→改后 diff**、本轮 patchPlan、**改进评价**（有效改进 / 结构提升 / 空转 / 退步）、已消除与新增的问题、门禁 G7–G9 |
| 独立盲评 | 逐次盲评的评分条（每维 0-100）、优点/缺点、blocking_conflicts、patchPlan、综合分（judge overall 优先，缺失回退各维平均并标注口径） |
| 运行跟踪 | 每轮**可复跑的脚本命令**、派发时刻、**子代理任务卡原文**、**子代理产出原文**、卡→产出间隔（<1s 会告警"疑似非子代理执行"） |
| 原文/终稿 | 两版全文对照 |

**改进评价的判定口径**（`report_gen.evaluate_round`，纯规则）：
- 分数升且问题数降 → 有效改进；分数升但问题未清 → 结构提升
- 分数与问题数均无变化 → **空转**（对齐 B4：第 2 轮幂等是设计如此）
- 分数降 → **退步**，并自动归因（若同时新增模糊词，提示"先查规则器假阳性再改稿"）
