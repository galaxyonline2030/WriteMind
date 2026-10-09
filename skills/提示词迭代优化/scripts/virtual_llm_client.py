"""VirtualLLMClient —— prompt-optimizer 的 LLM 接缝虚拟客户端
=========================================================

把 llm-client-sim 的「虚拟客户端」范式落到 prompt-optimizer 的 LLM 接缝上。

prompt-optimizer 的真实接缝（packages/core/src/services/llm/service.ts）：

    class LLMService implements ILLMService {
      async sendMessage(messages: Message[], provider: string): Promise<string>
      async sendMessageStructured(messages, provider): Promise<LLMResponse>
    }

也就是：**给定一组 messages（system + user，由模板渲染而成），调用模型，拿回文本/结构化结果。**
本模块用纯 Python 等价实现该接缝，使整条优化流可以不依赖任何外部 LLM SDK client 运行。

三条与 llm-client-sim 对齐的硬约束
----------------------------------
1. 不直接 import / 调用任何 LLM SDK（openai / anthropic / ...）。所有「模型行为」由替身提供。
2. 确定性替身（正则/规则，零 LLM）走本地注册表；生成式替身必须由「编排层 / 主代理」注入
   realizer——**脚本本身不 spawn 子代理**，只在上游未提供 fulfil 时抛出 AgentRealizationRequired。
3. 结果契约（contract.Contract）是唯一约定：无论确定性还是生成式，返回前都必须过校验。

分流顺序（call）：
    1) 形状名在确定性注册表 -> 跑确定性替身（零 LLM）
    2) 否则有 realizer -> 交给编排层 fulfil（生成式替身）
    3) 否则 -> 抛 AgentRealizationRequired，由上游处理
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from contract import Contract, ContractViolation, StrContract

# 一条消息：{"role": "system"|"user"|"assistant", "content": "..."}
Message = Dict[str, str]

# 确定性替身：输入 messages，产出符合某形状的结果（零 LLM）
DeterministicFn = Callable[[List[Message]], Any]
# 生成式 realizer（由编排层/主代理注入，生产环境即「子代理调用」）：
#   (client, messages, shape) -> 符合 shape 的结果
RealizerFn = Callable[["VirtualLLMClient", List[Message], Contract], Any]


class AgentRealizationRequired(Exception):
    """生成式接缝没有可用 realizer，必须由编排层 / 主代理用子代理 fulfil。

    这是接缝层的「诚实信号」：脚本承认自己产不出这个生成结果，
    把 fulfil 的责任上抛给编排层。生产环境里，编排层会向一个强约束 prompt 的子代理派发该任务。
    """

    def __init__(self, shape: Contract, messages: List[Message], note: str = ""):
        self.shape = shape
        self.messages = messages
        self.note = note
        super().__init__(f"需要子代理 fulfil 生成式接缝 [{shape.name}]: {note}")


class VirtualLLMClient:
    def __init__(
        self,
        provider: str = "virtual",
        *,
        deterministic: Optional[Dict[str, DeterministicFn]] = None,
        realizer: Optional[RealizerFn] = None,
        cache: bool = True,
    ):
        self.provider = provider
        self._det: Dict[str, DeterministicFn] = dict(deterministic or {})
        self._realizer: Optional[RealizerFn] = realizer
        self._cache_enabled = cache
        self._cache: Dict[str, Any] = {}
        # 多轮会话历史（对齐 prompt-optimizer 的「多轮会话测试」）
        self._sessions: Dict[str, List[Message]] = {}

    # ---- 配置接口 ----------------------------------------------------------
    def register_deterministic(self, shape_name: str, fn: DeterministicFn) -> None:
        self._det[shape_name] = fn

    def set_realizer(self, fn: RealizerFn) -> None:
        self._realizer = fn

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---- 内部 --------------------------------------------------------------
    def _key(self, messages: List[Message], shape_name: str) -> str:
        payload = json.dumps(
            {"m": messages, "s": shape_name},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---- 主接缝（对齐 sendMessageStructured） ------------------------------
    def call(
        self,
        messages: List[Message],
        shape: Contract,
        *,
        session: Optional[str] = None,
    ) -> Any:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages 必须是非空列表")
        key = self._key(messages, shape.name)
        if self._cache_enabled and key in self._cache:
            return self._cache[key]

        # 1) 确定性替身：零 LLM
        if shape.name in self._det:
            result = self._det[shape.name](messages)
        # 2) 生成式替身：交给编排层 realizer（即子代理）
        elif self._realizer is not None:
            result = self._realizer(self, messages, shape)
        # 3) 无人 fulfil -> 上抛，由编排层/主代理处理
        else:
            raise AgentRealizationRequired(shape, messages, "未注册确定性替身且未提供 realizer")

        # 结果契约校验（确定性 / 生成式都必须过）
        shape.validate(result)

        if self._cache_enabled:
            self._cache[key] = result
        return result

    # ---- 兼容 prompt-optimizer 的 sendMessage 命名 -------------------------
    def send_message(self, messages: List[Message], provider: Optional[str] = None) -> str:
        """对齐 LLMService.sendMessage：返回纯文本。

        注意：若未为 'text' 形状注册确定性替身且无 realizer，会抛 AgentRealizationRequired。
        """
        return self.call(messages, StrContract("text"))

    # ---- 多轮会话（对齐「多轮会话测试」） ---------------------------------
    def chat(self, session: str, msg: str, shape: Contract) -> Any:
        """在 session 内追加一条用户消息，调用模型，并把结果回写历史。

        与 call 的区别：call 是「一次性 messages」，chat 维持跨轮次上下文。
        """
        hist = self._sessions.setdefault(session, [])
        user_msg: Message = {"role": "user", "content": msg}
        messages = hist + [user_msg]
        result = self.call(messages, shape)
        hist.append(user_msg)
        hist.append({
            "role": "assistant",
            "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
        })
        return result

    def get_session(self, session: str) -> List[Message]:
        return list(self._sessions.get(session, []))
