# 子代理执行卡（v2.1 · 两张卡：Fulfil 改写 + 盲评 Judge）

> 本文件描述协议中**两个结构性子代理触点**的执行说明。脚本永远不产内容（B1），
> 主代理只做文件编排、**不代写不改稿不打分**（用户实测纠正：子代理必须真实参与）。
> 子代理卡由 `--dispatch` 渲染到 `runs/<ts>/dispatch/`，主代理用 Agent 工具把
> 任务卡**原文**交给子代理即可——任务卡自包含，子代理不需要任何会话上下文。

## 协议中的位置

```
Phase A（exit 10）
  → 主代理: --dispatch → dispatch/roundN.md
  → 子代理①(Fulfil): 按卡改写 → fulfil/roundN.txt
Phase B（--resume: 接缝+契约+G7/G9+评估）
  → 收敛 → AWAIT_JUDGE（exit 10）
  → 主代理: --dispatch → dispatch/judge.md
  → 子代理②(Judge): 只看终稿 → fulfil/judge.json
Resume（盲评裁决）
  → 无冲突且盲评达标（≥目标线且无<60低维）→ G10 PASS 交付
  → 有冲突 → 一次有界返工（回到子代理①）；二次冲突 → FAIL
  → 无冲突但未达标 → 质量返工（weaknesses/patchPlan 回灌，≤--quality-reworks 次，
    默认 1；分数无改善 plateau 提前停）；耗尽未达标 → FAIL（BELOW_TARGET）
```

---

## 卡① Fulfil 改写子代理

**独立性要求**：spawn 全新子代理，不带本轮优化任务的任何对话上下文。

### 第 1 轮（informed 改写）

1. 读任务卡 `dispatch/round1.md`——模板正文、patchPlan、原始稿都在卡里。
2. 模板（`templates/<state.template>`）是强约束指令：输出结构硬性要求必须满足。
3. 逐条落实 patchPlan 的每一条 action；每条都必须能在终稿中找到对应改动。
4. 领域知识写进 Profile 的 background / personality / expertise / target_audience，
   禁止"某领域资深执行者"式占位泛称。
5. 逐字保留原文全部 `{{variable}}` 占位符（G9 会机器校验）。
6. 消除全部模糊词（一些/尽量/大概/若干/适度…），约束写成可判断真假的句子。
7. **只把终稿本身**写入 `runs/<ts>/fulfil/round1.txt`（不要解释、不要对话），
   回复一行 `DONE <字符数>` 后立即结束。

### 第 2 轮起（iterate_directed 定向融合）

1. 按 `templates/iterate_directed.md` 执行：**修改上一轮终稿**，把 patchPlan
   的需求作为新约束融入；不要重新生成，不要执行需求。
2. 保持原稿核心意图、语言风格与结构；做精准修改，避免过度调整。
3. 保留全部 `{{variable}}`；输出前逐一自检（脚本会用 G9 复检）。
4. 写入 `runs/<ts>/fulfil/roundN.txt`，回复 `DONE`。

### 被重派时（retries 1-3 / 3）

- 脚本在 `--resume` 输出中给出具体失败反馈（G7 互斥冲突 /
  G9 缺失变量 / 契约错误），主代理把它追加到重新渲染的任务卡尾部传给子代理。
- 子代理按反馈**覆盖写入同一个 fulfil 文件**，主代理重跑 `--resume`。
- 3 次耗尽 → exit 1，向用户如实报告，不得降级交付。

---

## 卡② 盲评 Judge 子代理

**独立性要求（C3）**：必须 spawn 一个**全新的、无本会话上下文**的子代理。
judge 只看到 `dispatch/judge.md` 里那段终稿文本——没有原始稿、没有历史分数、
没有迭代过程。主代理绝不自己打分：运动员不能兼裁判。

1. 读任务卡 `dispatch/judge.md`：评审指令 = `templates/evaluation_judge.md`，
   评审对象 = 仅终稿文本。
2. 按模板输出严格 JSON，必含字段：`summary`、`scores`（structure / specificity /
   executability / constraint_integrity / self_consistency）、`strengths`、
   `weaknesses`、`patchPlan`、`blocking_conflicts`。
3. `blocking_conflicts`：互斥约束、使约束失效的元约束等「不返工就不能交付」的
   问题逐条列出；没有则 `[]`。这是 G10 门禁的裁决字段。
4. **只把 JSON 本体**写入 `runs/<ts>/fulfil/judge.json`（不要附加解释），
   回复一行 `DONE` 后立即结束。
5. 判卷后果由脚本裁决：无冲突且盲评达标（overall ≥ 质量目标线，且无单维
   <60）→ G10 PASS；有冲突 → 一次有界返工回灌（judge 的 patchPlan 一并注入
   下一轮 patchPlan）；返工后二次冲突 → FAIL；无冲突但未达标 → 质量返工
   （weaknesses/patchPlan 回灌，受 `--quality-reworks` 上限保护），耗尽或
   分数无改善 → FAIL（BELOW_TARGET）。

---

## 主代理红线（编排者，不是引擎）

- 不代写 fulfil 内容、不代替 judge 打分——两个触点都必须是真实 spawn 的子代理。
- 不绕过 dispatch/fulfil 文件直接把内容当交付（B3：旁路会被契约校验拦下）。
- 不跳过盲评：G10 门禁下，没有 `fulfil/judge.json` 且 verdict=PASS，就没有 PASS。
- spawn 时用任务卡原文作 prompt，不要附加会话上下文（会污染子代理独立性）。
