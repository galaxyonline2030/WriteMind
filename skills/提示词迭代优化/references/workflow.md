# 引擎契约 · v2.2（写给要批量 / 自动化调用的人）

> 本文件是 `scripts/` 的**调用契约速查**。协议的完整规则以 [`SKILL.md`](../SKILL.md) 为权威，
> 两者冲突时以 SKILL.md 为准。
> ⚠ v1 版的本文曾描述「主代理直出」与 `optimize` stub 管线——这两条**已废弃**，
> 见 KNOWN_ISSUES.md B1 / B3（stub 是摆设、直出会绕开接缝校验）。

## 0. 结果契约（Result Contract）

每个接缝只约定「返回形状」，与产出方式解耦。形状定义在 `scripts/contract.py`：

| 形状名 | 类型 | 含义 | 由谁 fulfil |
|--------|------|------|------------|
| `analysis` | json{issues,score,suggestions} | 原始提示词诊断 | 确定性（零 LLM） |
| `score` | float [0,100] | 结构化评分 | 确定性（零 LLM） |
| `compare` | json{winner,delta,reason} | 前后对比结论 | 确定性（零 LLM） |
| `rewrite_informed` | str（非空，LangGPT） | 第 1 轮改写 | **独立 fulfil 子代理**（无确定性替身） |
| `iterate_directed` | str（非空） | 第 2 轮起定向融合 | **独立 fulfil 子代理**（无确定性替身） |
| `blind_review` | json{summary,scores,…,blocking_conflicts} | 盲评结论 | **独立 judge 子代理** |

**硬规则**：`rewrite_informed` / `iterate_directed` / `blind_review` **没有确定性替身**，
脚本永远不产内容——无人 fulfil 时上抛，绝不假装能产出。
（`contract.py` 共定义 11 个形状；管线实际用到上表 6 个，其余见 `contract.py`。）

## 1. 确定性命令（零 LLM，可放心批量跑）

```bash
python scripts/optimize_cli.py analyze   "@prompt.txt"               # 诊断 + patchPlan
python scripts/optimize_cli.py patchplan "@prompt.txt"               # issues → 改写指令
python scripts/optimize_cli.py lint      "@prompt.txt"               # G7 自洽性（exit 1=有冲突）
python scripts/optimize_cli.py vars      "@prompt.txt"               # {{var}} 抽取（含中文变量名）
python scripts/optimize_cli.py evaluate  "@prompt.txt"               # 打分 0-100
python scripts/optimize_cli.py compare   "@before.txt" "@after.txt"  # winner / delta / reason
```

## 2. 三段式协议（改写必须走这条，exit 10 = 交接信号，不是错误）

```bash
cd scripts
python run_pipeline.py "@raw.txt" system 3 general   # Phase A → exit 10（AWAIT_FULFILL）
python run_pipeline.py --dispatch runs/<ts>          # 渲染子代理任务卡 → exit 10
#   ↑ 用 Agent 工具 spawn 独立子代理，把 dispatch/roundN.md 原文交给它执行；
#     子代理把终稿写入 fulfil/roundN.txt。主代理不得代写（B3 已封死旁路）。
python run_pipeline.py --resume runs/<ts>            # 校验+评分；收敛 → AWAIT_JUDGE → exit 10
#   ↑ 盲评同理：spawn 全新无上下文 judge 子代理执行 dispatch/judge.md → fulfil/judge.json
python run_pipeline.py --status  runs/<ts>           # 进度：round=X/Y、各轮 delta、盲评结论
python run_pipeline.py --report  runs/<ts>           # 生成 runs/<ts>/report.html 可视化报告
python run_pipeline.py --legacy "@raw.txt" "@final.md"   # 旧式一步门禁（G10 记 SKIP，勿当交付）
```

## 3. 退出码与状态机

| 退出码 | 含义 |
|---|---|
| `0` | PASS，可交付（或 --status / --report 正常结束） |
| `1` | FAIL——issues 未清零 / 轮数耗尽 / delta 停滞 / 盲评二次冲突。**不得带病交付** |
| `2` | 用法错误 |
| `10` | 交接信号：主代理该 spawn 子代理了（fulfil 或 judge） |

状态机：`AWAIT_FULFILL →（fulfil 子代理写入）→ --resume 校验 → 收敛 → AWAIT_JUDGE`
`→（judge 写入）→ 裁决 → PASS / 有界返工（回 AWAIT_FULFILL，配额 +1）/ FAIL`
唯一事实源是 `runs/<ts>/state.json`；手工介入时 `phase` 必须用 **`AWAIT_FULFILL`（两个 L）**。

## 4. 交付门禁 G1–G10

G1 基线诊断 / G2 迭代管线 / G3 终稿复检 / G4 issues 清零 / G5 分数提升 /
G6 非模板空壳 / G7 自洽 lint / G8 信息保持 / G9 变量完整 / G10 盲评 PASS。
**任何一项 FAIL 都不允许交付**；`--legacy` 模式 G10 记 SKIP，不可用于正式交付。

## 5. 边界（别踩）

- 分数与门禁只是**下限**：交付前必须拿 ≥1 个真实输入跑一遍终稿（真实用例验收）。
- 分数暴跌但内容明显变好 → 先查规则器假阳性（见 KNOWN_ISSUES.md A7/A8），别改稿。
- 批量跑请保留 `runs/` 留档：`state.json` / `report.json` / `report.html` 可复现、可审计。
