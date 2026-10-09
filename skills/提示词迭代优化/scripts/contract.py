"""结果契约（Result Contract）
======================

对应 llm-client-sim 的核心约定：**结果契约唯一约定的是「结果形状」**。

在 prompt-optimizer 的 LLM 接缝上，每个调用点都应声明它期望返回什么形状：
- optimize   -> 一段改写后的提示词文本（str）
- analyze    -> 结构化诊断（dict: issues / score / suggestions）
- evaluate   -> 一个 0-100 的分数（float）
- compare    -> 前后对比结论（dict: winner / delta / reason）

VirtualLLMClient 在拿到确定性/生成式替身的结果后，一律用契约校验，
保证「脚本产出的东西」和「真实 LLM 产出的东西」在形状上可互换。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class ContractViolation(Exception):
    """返回结果不满足契约。"""

    def __init__(self, shape: str, detail: str):
        self.shape = shape
        self.detail = detail
        super().__init__(f"契约违反 [{shape}]: {detail}")


class Contract:
    """结果契约基类。子类实现 validate。"""

    name: str = "base"

    def validate(self, result: Any) -> Any:
        raise NotImplementedError


class StrContract(Contract):
    """期望一段非空字符串（如优化后的提示词）。"""

    def __init__(self, name: str = "text"):
        self.name = name

    def validate(self, result: Any) -> Any:
        if not isinstance(result, str) or not result.strip():
            raise ContractViolation(self.name, f"期望非空字符串，实际 {type(result).__name__}: {result!r}")
        return result


class ScoreContract(Contract):
    """期望一个 [lo, hi] 区间内的数值（如 0-100 评分）。"""

    def __init__(self, name: str = "score", lo: float = 0.0, hi: float = 100.0):
        self.name = name
        self.lo = lo
        self.hi = hi

    def validate(self, result: Any) -> Any:
        try:
            v = float(result)
        except (TypeError, ValueError):
            raise ContractViolation(self.name, f"期望数值，实际 {type(result).__name__}: {result!r}")
        if not (self.lo <= v <= self.hi):
            raise ContractViolation(self.name, f"数值 {v} 超出区间 [{self.lo}, {self.hi}]")
        return v


class JsonContract(Contract):
    """期望一个 dict，并可声明必填字段。"""

    def __init__(self, name: str, required: Optional[List[str]] = None, optional: Optional[List[str]] = None):
        self.name = name
        self.required = required or []
        self.optional = optional or []

    def validate(self, result: Any) -> Any:
        if not isinstance(result, dict):
            raise ContractViolation(self.name, f"期望 dict，实际 {type(result).__name__}")
        missing = [k for k in self.required if k not in result]
        if missing:
            raise ContractViolation(self.name, f"缺少必填字段 {missing}")
        return result


# ---------------------------------------------------------------------------
# 预定义形状：与 optimizer_adapter 的调用点一一对应
# ---------------------------------------------------------------------------
SHAPE_OPTIMIZED_PROMPT = StrContract("optimized_prompt")
SHAPE_ANALYSIS = JsonContract("analysis", required=["issues", "score", "suggestions"])
SHAPE_SCORE = ScoreContract("score")
SHAPE_COMPARE = JsonContract("compare", required=["winner", "delta", "reason"])
# 生成式接缝示例：需要「创意增强」改写，没有确定性替身，必须由子代理 fulfil
SHAPE_CREATIVE = StrContract("creative_enhancement")

# ---------------------------------------------------------------------------
# v2 新增形状（DEEP_ANALYSIS_AND_PLAN.md §3）
# ---------------------------------------------------------------------------
# 分析 v2：在 issues/score/suggestions 基础上增加 patch_plan（诊断→改写的回灌管道）
SHAPE_ANALYSIS_V2 = JsonContract(
    "analysis_v2", required=["issues", "score", "suggestions", "patch_plan"]
)
# informed 改写：输入含原始提示词 + patchPlan 的生成式改写（agent-first 主接缝）
SHAPE_REWRITE_INFORMED = StrContract("rewrite_informed")
# 定向迭代：输入含上一轮终稿 + 改写需求的融合式修改（对齐原项目 iterate 语义）
SHAPE_ITERATE_DIRECTED = StrContract("iterate_directed")
# 红队审稿：漏洞 / 风险 / 隐含假设
SHAPE_REDTEAM_REVIEW = JsonContract(
    "redteam_review", required=["loopholes", "risks", "hidden_assumptions"]
)
# 变量抽取：对齐原项目 variable-extraction 的 JSON 契约
SHAPE_VARIABLES = JsonContract("variables", required=["variables"])
# 盲评 judge：独立无上下文子代理只看终稿文本给出的评审（C3 独立性）
SHAPE_BLIND_REVIEW = JsonContract(
    "blind_review",
    required=["summary", "scores", "strengths", "weaknesses", "patchPlan",
              "blocking_conflicts"],
)
