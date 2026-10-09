# 技术实现方案 v2.1（as-built）

> 2026-10-07 修订。状态：**已全部实施并通过端到端验证**（真实 Agent 工具 spawn
> fulfil 与盲评两个子代理，36 → 96 全门禁 PASS；盲评冲突回灌与二次冲突 FAIL 分支实测通过）。
> 本文件是技能的权威技术方案。能力逐项对照见 `CAPABILITY_PARITY.md`；
> 实战问题与能力边界见 `KNOWN_ISSUES.md`——本文不再重复这两份内容。

---

## 1. 架构定位（设计依据，一段讲清）

原项目 `linshenkx/prompt-optimizer` 的全部智能物化在 **20+ 个内置模板**（专家扩写、
定向迭代、LLM-as-judge、变量抽取）× 真实 LLM × 编排流水线中。在 WorkBuddy 内复刻它，
关键认知是：**WorkBuddy 的 agent 本身就是一个真实 LLM**——缺的从来不是引擎，而是
① 把原项目强度的模板交给独立子代理执行；② 让诊断结果流进改写（数据闭环）；
③ 让评审与改写互相隔离（独立性）。因此：

- **引擎换位（agent-first）**：确定性脚本只做前处理（诊断 + patchPlan）与后门禁
  （契约 + G1–G10）；改写、迭代、评审是生成式接缝，由**独立子代理**执行。
- **主代理只做编排**：spawn 子代理、传任务卡、收文件——不代写、不改稿、不打分。
- **红线**：`scripts/` 内绝不 import / 调用任何 LLM SDK（llm-client-sim 范式）。

## 2. 总体流程

```
Phase A（脚本，确定性，exit 10）
  诊断 raw → 基线分 → patchPlan（issues 逐条转改写指令）→ 路由模板
  → 写 runs/<ts>/state.json {phase: AWAIT_FULFILL}

--dispatch（脚本）→ 渲染自包含任务卡 dispatch/roundN.md → exit 10
Fulfil 子代理①（独立、无会话上下文）→ 按模板改写 → 写 fulfil/roundN.txt

Phase B（--resume，脚本）：fulfil 注入 realizer → 契约校验 → G7/G8/G9
  → analyze/compare → 收敛判定：
    issues 清零   → phase=AWAIT_JUDGE → exit 10
    仍有 issues   → 回灌为下一轮 patchPlan → exit 10（回到 --dispatch）
    停滞/轮数耗尽 → exit 1

--dispatch → 渲染 dispatch/judge.md → exit 10
盲评 Judge 子代理②（全新、无上下文，只看终稿文本）→ 写 fulfil/judge.json

--resume（脚本，AWAIT_JUDGE 裁决）：
    blocking_conflicts 为空 → G10 PASS → 全量 G1–G10 → exit 0
    有冲突且未返工过        → 冲突 + judge patchPlan 回灌，配额 +1 → exit 10
    返工后二次冲突          → exit 1（不允许带病交付）
```

## 3. 文件协议（state.json + 退出码 + 任务卡）

- **state.json 是唯一事实源**，字段：`version / phase(AWAIT_FULFILL|AWAIT_JUDGE) /
  round / max_rounds / retries / judge_retries / rework_done / mode / scenario /
  template / raw / baseline / pending_plan / last_final / history[] / blind_review /
  fail_feedback / judge_feedback`。
- **退出码**：`10` = 脚本→主代理交接信号（非错误）；`0` = 可交付；`1` = FAIL；
  `2` = 用法错误。
- **任务卡自包含**：模板正文 + patchPlan JSON + 待处理文本 + 绝对输出路径 + 红线。
  子代理不需要任何会话上下文；写完回复 `DONE` 即结束。主代理 spawn 时用任务卡
  **原文**作 prompt，不附加会话信息（保子代理独立性）。
- **主代理红线**：不代写 fulfil 内容、不代替 judge 打分、不绕过 fulfil 文件直接交付
  （旁路会被契约校验拦下）、不跳过盲评（G10 强制）。

## 4. 模板资产库 `templates/`（11 个，移植自原项目模板家族）

| 文件 | 移植自 | 要点 |
|---|---|---|
| `optimize_general.md` | general-optimize.ts | Profile 六字段 / Rules 三类 / 变量逐字保留 / 禁引导词 |
| `optimize_analytical.md` | analytical-optimize.ts | 分析型策略 |
| `optimize_output_format.md` | output-format-optimize.ts | 格式强化策略 |
| `user_optimize_{basic,planning,professional}.md` | user-optimize/* | 用户提示词三档 |
| `iterate_directed.md` | iterate.ts v3.0.0 | **定向融合**语义 + 3 个 few-shot 反例 + 变量输出前自检 |
| `evaluation_judge.md` | evaluation/content.ts v5.0 | 多维分 + strengths/weaknesses + patchPlan + 自洽性检查清单 |
| `evaluation_rewrite.md` | evaluation-rewrite/generic.ts | 拿 patchPlan 定向改写 |
| `redteam_review.md` | 红队审稿（净增强） | 漏洞/风险/隐含假设 |
| `variable_extract.md` | extraction.ts | 变化点四标准 + JSON 契约 |

路由：`pick_template(mode, scenario)`（system/user × 场景 → 模板）；第 2 轮起固定
`iterate_directed.md`。模板正文即子代理的强约束 system prompt。

## 5. 接缝形状 `contract.py`（11 个）

```python
# 确定性接缝
SHAPE_OPTIMIZED_PROMPT / SHAPE_ANALYSIS / SHAPE_SCORE / SHAPE_COMPARE
# v2 生成式与闭环接缝
SHAPE_ANALYSIS_V2        # {issues, score, suggestions, patch_plan}
SHAPE_REWRITE_INFORMED   # informed 改写（非空 str）
SHAPE_ITERATE_DIRECTED   # 定向迭代（非空 str）
SHAPE_REDTEAM_REVIEW     # {loopholes, risks, hidden_assumptions}
SHAPE_VARIABLES          # {variables: [...]}
# v2.1 盲评接缝
SHAPE_BLIND_REVIEW       # {summary, scores, strengths, weaknesses,
                         #  patchPlan, blocking_conflicts}
```

子代理产出（fulfil 文件 / judge.json）一律经契约校验才能进入管线；
judge.json 解析宽容（容忍 ```json 围栏与前后杂散文字）。

## 6. 确定性引擎（定位 = 下限检查器，不是质量引擎）

- `analyze / score / compare`：零 LLM 规则器，双语模糊词表 + 双语结构正则。
- **G7 自洽性 lint**：互斥约束启发式（否定输出 vs 要求输出、数量冲突、元约束失效）。
- **G8 信息保持**：抽取原文数字/量词/引号串锚点，校验终稿保留率；含
  `_NUM_ANCHOR_EXCEPTIONS` 例外表（冠词性「一个/一名」不入锚点——E2E 实测发现的
  A1 同类假阳性，按"修规则不绕门禁"处理）。
- **G9 变量完整性**：`{{var}}` 正则逐字校验。
- **patchPlan 构造**：issues+suggestions 确定性转为 `{target, action}` 指令列表
  （诊断→改写回灌管道）。
- 诚实定位：规则器只拦已知模式，语义级矛盾由盲评 judge 兜底；双层仍可能双漏。

## 7. 迭代与盲评语义

- **迭代 = 定向融合修改**（对齐 iterate.ts）：每轮输入 = 上一轮终稿 + 剩余 issues
  （作为 iterateInput），**修改原稿而非执行需求、不整稿重写**。
- **盲评裁决（C3 独立性 / G10）**：judge 与 fulfil 是两个互相隔离的子代理；
  `blocking_conflicts` 是"不返工就不能交付"的裁决字段。返工有界：一次，
  配额 +1；二次冲突即 FAIL。

## 8. 交付门禁 G1–G10（`verify_delivery.py`）

| 门禁 | 含义 | 失败动作 |
|---|---|---|
| G1–G3 | 基线诊断 / 迭代管线（协议模式从 state.json 取证）/ 最终稿复检 已跑 | 禁止交付 |
| G4/G5 | 剩余 issues 清零 / 分数确实提升（winner=after 且 delta>0） | 禁止交付 |
| G6 | 非通用模板产物（≠ rewrite_stub 空壳） | 禁止交付 |
| G7 | 自洽性 lint（互斥约束） | 重派 ≤3 |
| G8 | 关键信息保持（数字/量词/引述锚点 + 例外表） | 重派 ≤3 |
| G9 | 变量完整性（{{var}} 逐字保留） | 重派 ≤3 |
| G10 | 盲评已通过（独立 judge verdict=PASS）；legacy 模式记 SKIP 不阻断 | 派 judge / 返工 / FAIL |

回归基线：`examples/false_positive_demo.txt` 必须持续被 G7/G6/G5 拦截（exit 1）。

## 9. 持久化 `runs/<ts>/`

```
runs/<ts>/
  raw.md                     原始稿
  state.json                 协议唯一事实源
  dispatch/roundN.md judge.md 子代理任务卡（留档可复现 spawn 过程）
  fulfil/roundN.txt          Fulfil 子代理产出
  fulfil/judge.json          盲评结论
  report.json                分数轨迹 + 门禁明细 + 盲评结论 + 剩余 issues
```

每次运行即一个版本，全程可复现；打包分发时清空 runs/。

## 10. 交付验收协议（强制，脚本不能代劳）

宣告完成前的最后关口：拿 ≥1 个**真实输入**跑一遍终稿，把输出留档到 run 目录并向
用户展示。脚本层只能提醒与留档，无法代替——这是"LLM 自验天花板"下的诚实边界。

## 11. 失败处理与重试预算

- 契约 / G7–G9 失败 → 附反馈重派 fulfil 子代理覆盖同一文件，每轮 ≤3 次，耗尽 exit 1。
- judge.json 契约失败 → 重新派发 judge，≤2 次。
- 盲评 blocking_conflicts → 一次有界返工（配额 +1），二次冲突 exit 1。
- issues 停滞（连续 2 轮 delta<5）或轮数耗尽 → exit 1 并明示剩余问题。

## 12. 边界（诚实声明）

- 质量上限 = WorkBuddy 运行时 LLM × 模板强度；规则器只当下限与回归门禁。
- 子代理独立性靠"spawn 时只给自包含任务卡"保证；同会话反复测试时 judge 仍可能从
  prompt 措辞间接推断意图（已知残差）。
- 不引入任何 SDK / HTTP 直连；不覆盖图像提示词、Function Calling、多端部署、MCP Server
  （接缝形状可扩展，按需另立方案）。
