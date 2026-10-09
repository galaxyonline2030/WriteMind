# 范式：LLM 接缝虚拟客户端化（对齐 llm-client-sim）

本技能把 `linshenkx/prompt-optimizer`（纯前端 TS+Vue，直连多家模型 API）的 LLM 接缝，
替换为「虚拟客户端」范式——证明提示词优化流可以**不依赖任何外部 LLM SDK client** 运行，
且生成式接缝整体可由子代理 fulfil。

## 1. 对齐的真实接缝

`packages/core/src/services/llm/service.ts`：

```ts
class LLMService implements ILLMService {
  async sendMessage(messages: Message[], provider: string): Promise<string>
  async sendMessageStructured(messages, provider): Promise<LLMResponse>
}
```

即：**给定一组由模板渲染的 messages，调用模型，拿回文本 / 结构化结果。**
这正是 `llm-client-sim` 的 `client.complete(prompt, shape)` 同源抽象。

## 2. 三条硬约束（来自 llm-client-sim）

1. **不直接 import / 调用任何 LLM SDK**（openai / anthropic / …）。模型行为由替身提供。
2. **确定性替身**（正则 / 规则，零 LLM）走本地注册表；**生成式替身必须由编排层 / 主代理注入
   realizer**——脚本本身不 spawn 子代理，只在无人 fulfil 时抛 `AgentRealizationRequired`。
3. **结果契约**是唯一约定：无论确定性还是生成式，返回前必过 `contract.py` 校验。

## 3. 分流顺序（`VirtualLLMClient.call`）

```
① 形状名在确定性注册表 -> 跑确定性替身（零 LLM）
② 否则有 realizer     -> 交给编排层 fulfil（生成式替身 / 子代理）
③ 否则                -> 抛 AgentRealizationRequired，由上游处理
```

## 4. 与 llm-client-sim 的映射

| llm-client-sim | 本技能落地 |
|----------------|-----------|
| `client.complete(prompt, shape)` | `VirtualLLMClient.call(messages, shape)` |
| 结果契约（Result Contract） | `contract.py` 的 `Str/Score/Json` + `validate()` |
| 确定性替身（零 LLM） | `stubs.py` 的 `analyze/score/compare/rewrite_stub` |
| 生成式替身（子代理 + 强约束 prompt） | `agents/optimize_prompt.md`（编排层注入 realizer） |
| 硬约束：脚本不 spawn 子代理 | 脚本只抛 `AgentRealizationRequired`，realizer 由外部提供 |
| memoize / 缓存 | `VirtualLLMClient._cache` |
| 失败处理：校验 + 重试 ≤3 + 降级 | 契约失败抛 `ContractViolation`；上抛由编排层决定重试 / 降级 |

## 5. 关键设计决策

- **按形状而非模型解耦**：调用方只声明"要什么形状"，不关心背后是规则还是子代理；
  替换底层不影响 `optimizer_adapter` 代码。
- **确定性幂等**：`rewrite_stub` 检测到输入已结构化时原样返回，使多轮迭代自然收敛，避免嵌套。
- **诚实上抛**：生成式接缝无 realizer 时**不假装能产出**，而是抛 `AgentRealizationRequired`，
  把 fulfil 责任明确上抛——这是与"脚本内伪造 LLM"的本质区别。
- **契约即边界**：返回前必过形状校验，保证"脚本产物"与"真实 LLM 产物"可互换。
