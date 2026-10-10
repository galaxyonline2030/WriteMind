# 能力对照：v2 方案落地后 vs 原项目（源码级）

> 2026-10-07。方法：把 `DEEP_ANALYSIS_AND_PLAN.md` 的 v2 设计逐条映射回原项目
> `packages/core/src` 的具体实现文件，评估落地后的对齐程度。
> 评级：**对齐**（功能等价）/ **部分**（子集）/ **不覆盖**（形态取舍）/ **v2 增强**（原项目没有）。

## 1. 逐能力对照总表

| # | 能力 | 原项目源码实现 | v2 落地实现 | 评级 | 残差 |
|---|------|---------------|------------|------|------|
| 1 | 核心优化 | `default-templates/optimize/general-optimize.ts` v1.3.0：强指令让 LLM 按 LangGPT 结构专家扩写（Profile 六字段 background/personality/expertise/target_audience；Rules 三类）；`{{var}}` 逐字保留；`PromptService.optimizePrompt` → 渲染模板 → `sendMessage` → `validateResponse` | `templates/optimize_general.md` 逐字移植指令 → agent/子代理执行；确定性 pre-analysis + `SHAPE_REWRITE_INFORMED` 契约校验 + G9 变量完整门禁 | 对齐 | 无流式（原项目有 `sendMessageStream`）；无模板编辑 UI |
| 2 | 多策略优化 | `analytical-optimize` / `output-format-optimize` / `soul-hermes\|openclaw-compose` 模板家族，UI 手选 | 同名模板移植 + `pick_template(mode, scenario)` 规则路由 | 对齐 | 路由是规则匹配非语义理解；soul-* 人设合成列 P2 |
| 3 | 双模式 | optimize（system）与 `user-optimize`（basic / planning / professional 三档） | mode 参数路由 + 三档模板移植 | 对齐 | — |
| 4 | 迭代 | `iterate/iterate.ts` v3.0.0：**定向融合**语义（修改原稿而非执行需求）+ 3 个 few-shot 反例 + 变量输出前自检；输入 JSON `{lastOptimizedPrompt, iterateInput}`；MCP `iterate-prompt` 同链路 | `templates/iterate_directed.md` 忠实移植同语义；iterateInput 自动 = 剩余 issues 的 patchPlan（兼容用户手动需求）；**每轮过 G 门禁** | 对齐＋增强 | 迭代语义本就是"修改原稿"，v2 忠实移植且门禁每轮强制 |
| 5 | 评估（LLM-as-judge） | `evaluation/content.ts` v5.0：设计质量分析专家，多维分 + summary/improvements/**patchPlan**，支持 Focus Brief；basic/pro 双档 | `templates/evaluation_judge.md` 移植 + 自洽性检查清单；输出 `SHAPE_ANALYSIS_V2`；P1 先 generic 档 | 对齐 | pro-multi / pro-variable 档后补；judge 独立性见 §4-1 |
| 6 | 评估→改写闭环 | `evaluation-rewrite/generic.ts`（+pro-multi / pro-variable 变体）：消费 patchPlan 定向改写 | `SHAPE_REWRITE_INFORMED`：patchPlan 注入消息 → agent 改写 → 契约校验 | 对齐 | 同上 pro 变体 |
| 7 | 对比评估 | `evaluation-structured-compare/pair-judge.ts`（两两 LLM 评审）+ `synthesis.ts`（综合） | pair-judge 模板移植（P1）+ 确定性 compare（结构分差）作下限 | 对齐 | 确定性下限对齐原项目语义 |
| 8 | 变量 | `variable-extraction/extraction.ts`：变化点四标准、命名规则、JSON 契约（position/occurrence/reason/category）；另有 `variable-value-generation` 与变量管理 UI | `templates/variable_extract.md` 移植 + 确定性正则抽 `{{...}}` + `SHAPE_VARIABLES` + `.prompt.yaml` 产出；批量替换脚本化 | 基本对齐 | 变量值生成模板 P2 后补；无 UI |
| 9 | 红队 | **无独立模板**——README"红队审稿"是系统提示词优化后的*效果演示*（修正 Part I 的表述） | `templates/redteam_review.md` 显式化为独立接缝（`SHAPE_REDTEAM_REVIEW`：loopholes/risks/hidden_assumptions） | v2 增强 | 把隐式能力显式化，可单独调用 |
| 10 | 高级测试 | 上下文变量管理（批量替换/预览）、多轮会话测试、Function Calling（OpenAI/Gemini 工具） | chat()/sessions 多轮骨架（已有）；变量批量替换脚本化；FC 不覆盖 | 部分 | FC 缺失（接缝形状可扩展，本期不做） |
| 11 | 图像提示词 | `image-optimize` / `image-prompt-composition` / `image-prompt-migration` + 多图像模型集成 | 不覆盖（方案明示边界） | 不覆盖 | 可按接缝形状后补 |
| 12 | 资产与版本 | 智能收藏 / 版本历史 / 导入导出 / Prompt Garden 提示词库 | `runs/<ts>/` 留档（raw/analysis/state/fulfil/final/report/gates）= 最小版本历史 + 断言可复现 | 部分 | 无收藏、无库、无 UI |
| 13 | MCP / 多端 / 多模型 | Docker 自带 MCP Server（3 工具）；Web/桌面/插件/Docker 四端；12+ 供应商直连流式 | WorkBuddy 技能原生集成（零配置）；单一运行时 LLM；Lane B（stdlib HTTP 直连）默认关闭 | 不覆盖 | 形态取舍，非实现缺陷 |
| 14 | 响应校验 | `PromptService.validateResponse`（非空等基础校验） | `contract.py` Str/Score/Json 契约 + G1–G9 门禁 | v2 增强 | 契约是超集 |
| 15 | 质量保障 | 依赖用户在 UI 肉眼验收；无确定性门禁、无回归反例 | G1–G7、G9 + 自洽 linter（A1）+ 变量完整（G9）+ runs 留档 + Step7 真实用例验收 | v2 增强 | 原项目完全没有这一层（注：`false_positive_demo`/`G8 信息保持` 已于 2026-10-10 移除：前者目录从未落地、后者对抗实测无效） |

## 2. 统计

- **对齐（含 2 项带增强）**：8 项 —— #1–#8（优化、多策略、双模式、迭代、评估、闭环、对比、变量）
- **部分**：2 项 —— #10 高级测试、#12 资产（最小版）
- **不覆盖**：2 项 —— #11 图像、#13 MCP/多端/多模型
- **v2 独有增强**：4 项 —— #9 红队显式化、#14 契约超集、#15 质量保障层、#16 盲评分质量收敛（见 §3 第 4 条）

## 3. v2 独有增强（原项目没有的）

1. **确定性门禁层**：G1–G7、G9 每轮强制，退步当场拦截；原项目完全靠用户肉眼。
2. **变量完整门禁（G9）**：把原项目 iterate 模板里的"输出前自检"升级为机器门禁。
3. **全程留档可复现**：runs/ + state.json 使任意一轮可重放；断言不再靠记忆。
4. **盲评分质量收敛（2026-10-10，v2 增强 #6）**：盲评分从"只展示不控流"升级为
   **迭代终止条件**——两层收敛 = 结构层（issues 清零 + 无 conflicts）且 质量层
   （盲评 ≥ `--min-blind-score` 目标线 且 无单维 <60 危险线）。未达标触发有界
   质量返工（judge weaknesses/patchPlan 回灌，`--quality-reworks` 上限默认 1，
   plateau 无改善提前停，耗尽 → FAIL）。C3 独立性不变：judge 每轮仍只看终稿单
   文本。默认 85 为临时值（校准 n=1：2026-10-10 简历稿终稿盲评 85），上游无此
   机制（上游靠用户 UI 肉眼验收）——此前 G8 移除留下的"语义守护缺位"由此补上。

> 2026-10-10 对抗评估后移除两项原 §3 条目：
> - **回归反例**：文档曾称 `examples/false_positive_demo.txt` 进 CI 回归，但 `examples/` 目录从未创建，无可执行套件——属文档夸大，已纠正。
> - **信息保持检查（G8）**：规则器对自然语言草稿类输入恒返回空（无数字/引号锚点可丢），且不校验「用户需求是否被静默丢弃」，对抗实测对语义漂移全漏，属无效护栏，故移除；语义守护交 G10 盲评（软阈值已于同日实施，见 §3 第 4 条）。

## 4. v2 仍不如原项目的（诚实清单）

1. **评审独立性**：原项目用户可指定不同模型当 judge；v2 的 judge 与 rewriter 同为
   WorkBuddy 运行时 LLM（同源风险）。缓解 = 子代理隔离 + 盲评协议，但不可根除。
2. **无流式、无即时预览**：原项目 sendMessageStream + UI 实时反馈。
3. **无跨应用集成**：原项目 MCP Server 可被 Claude Desktop 等调用；v2 只活在 WorkBuddy 内。
4. **图像、Function Calling 不覆盖**。
5. **模板条件逻辑的位置**：原项目用 Handlebars helpers（`{{#hasExistingVariables}}`）在模板层
   表达条件；v2 由 Python 组装注入——等价但逻辑在代码里，模板移植需人工核对每个 helper。

## 5. 对 Part I 的一处修正

Part I 写过"原项目有专门 red-team 策略"——逐文件核对后修正：**红队审稿是 README 的
效果演示**（优化后的系统提示词让小模型转向批判式审查），原项目并无独立 red-team 模板。
v2 将其显式化为独立接缝属于净增强，而非移植。

## 6. 预期效果：KNOWN_ISSUES 修复度预估（P0+P1 落地后）

| 问题 | 预期 |
|------|------|
| B1 realizer 摆设 | **根除**（fulfil 文件协议，stub 不再注册 optimized_prompt） |
| B2 空壳交付 | **根除**（agent-first + general-optimize 强模板，终稿必含六字段 Profile） |
| B4 迭代空转 | **根除**（issues 回灌 + 定向融合，每轮 delta 可为正） |
| A1 假阳性 | **显著缓解，非根除**（G7 规则层拦已知互斥模式 + judge 语义层；双层仍可能双漏） |
| A2 词表绕过 | 缓解（双语化 + judge 不依赖词表） |
| A4 信息丢失 | 交由 G10 盲评语义把关（建议加软阈值）；确定性层不覆盖此风险（G8 已移除） |
| C1/C2 靠自觉 | 结构性缓解（不 resume 无 report、无 report 不许交付；彻底根治需用户配 hook） |
| D1 真实用例 | 流程强制（Step 7 前置），执行质量仍取决于真实跑 |

## 7. 结论

v2 落地后：**核心优化智能与流程闭环与原项目功能对齐**（同一强度的模板指令 × 等价的
LLM 执行引擎），**质量保障与可复现性超越原项目**（门禁/回归/留档是原项目没有的层），
**产品面能力（多端、多模型、图像、FC、UI 资产）不覆盖**——这是技能形态与产品形态的
边界，不是实现缺陷。

一句话定位：v2 后的技能 ≈ **"原项目优化引擎的无 UI 单机版 + 确定性质检层"**。
上限 = WorkBuddy 运行时 LLM × 移植模板强度；下限由 G 门禁兜住。
