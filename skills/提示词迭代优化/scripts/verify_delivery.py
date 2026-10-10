"""交付前门禁（Delivery Gate，确定性，零 LLM）
==============================================

把 SKILL.md 里「容易靠嘴说、实际被跳过」的步骤做成机器门禁：
不跑完 analyze / pipeline / analyze(最终稿) / compare，就拿不到 PASS，
拿不到 PASS 就不允许出交付。

用法：
  python verify_delivery.py "@raw.txt" "@final.md" [rounds] [system|user]
  python verify_delivery.py "@raw.txt" "@final.md" --json

退出码：0 = PASS（允许交付）；1 = FAIL（必须回去修）。

门禁项：
  G1 基线诊断已跑         analyze(raw)
  G2 迭代管线已跑         pipeline(raw, rounds)（记录每轮 delta；协议模式从 state.json 取证）
  G3 最终稿已复检         analyze(final)   ← 上一轮就是漏了这条
  G4 剩余 issue 清零      analyze(final).issues 必须为空
  G5 分数确实提升         compare(raw, final) winner=after 且 delta>0
  G6 非通用模板产物       final != rewrite_stub(raw)（防止直接交付空壳模板）
  G7 自洽性 lint          consistency_lint(final)（互斥约束启发式，下限检查）
  G9 变量完整性           原文全部 {{var}} 必须逐字保留（对齐原项目 iterate 自检）
  G10 盲评已通过          独立无上下文 judge 子代理的盲评结论 verdict=PASS，
                          且（若设置质量目标）盲评分 ≥ 目标线且无 <60 低维
                          （协议模式必查：运动员不能兼裁判；legacy 记 SKIP 不阻断）
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer_adapter import (  # noqa: E402
    analyze,
    compare,
    evaluate,
    optimize_with_evaluation,
    render_optimize_messages,
)
from stubs import (  # noqa: E402
    consistency_lint,
    extract_variables,
    missing_variables,
    rewrite_stub,
)
from virtual_llm_client import VirtualLLMClient  # noqa: E402

OK, FAIL = "PASS", "FAIL"


def _resolve(arg: str) -> str:
    if arg.startswith("@") and len(arg) > 1:
        with open(arg[1:], encoding="utf-8") as f:
            return f.read()
    return arg


def _client() -> VirtualLLMClient:
    from stubs import analyze_stub, compare_stub, rewrite_stub as rw, score_stub

    c = VirtualLLMClient(provider="verify-delivery")
    c.register_deterministic("optimized_prompt", rw)
    c.register_deterministic("analysis", analyze_stub)
    c.register_deterministic("score", score_stub)
    c.register_deterministic("compare", compare_stub)
    return c


def run(raw: str, final: str, rounds: int = 2, mode: str = "system",
        protocol_history: list | None = None,
        blind_review: dict | None = None,
        blind_expected: bool = True) -> dict:
    """protocol_history 非空时（文件协议模式），G2 从 state.json 取证，
    G10 校验盲评结论；否则走旧式 stub 管线（legacy 兼容，G10 记 SKIP）。

    blind_expected=False 表示本次运行**未收敛**（轮数耗尽 / delta 停滞），
    本就没有触发盲评轮——此时 G10 记 SKIP 而非 FAIL，避免与真实原因
    （G4 残留 issues）并列造成归因噪音（修 2026-10-09 回归 BUG-05）。
    """
    c = _client()
    checks = []

    a_raw = analyze(c, raw)
    checks.append({
        "id": "G1", "name": "基线诊断已跑", "status": OK,
        "detail": f"score={a_raw['score']}，issues={len(a_raw['issues'])} 条",
        "issues": a_raw["issues"],
    })

    if protocol_history:
        rounds_info = [
            {
                "round": h.get("round", i + 1),
                "score_before": h.get("score_before"),
                "score_after": h.get("score_after"),
                "delta": (h.get("score_after", 0) - h.get("score_before", 0)),
                "source": "protocol(state.json)",
            }
            for i, h in enumerate(protocol_history)
        ]
        g2_detail = (f"文件协议 {len(rounds_info)} 轮：" + " / ".join(
            f"r{r['round']} {r['score_before']}→{r['score_after']}" for r in rounds_info
        ))
    else:
        rep = optimize_with_evaluation(c, raw, mode, rounds)
        rounds_info = [
            {
                "round": h["round"],
                "score_before": h["score_before"],
                "score_after": h["score_after"],
                "delta": h["score_after"] - h["score_before"],
                "idempotent_noop": h["output"].strip() == h["input"].strip(),
            }
            for h in rep["history"]
        ]
        g2_detail = f"legacy {rounds} 轮：" + " / ".join(
            f"r{r['round']} {r['score_before']}→{r['score_after']}" for r in rounds_info
        )
    checks.append({
        "id": "G2", "name": "迭代管线已跑", "status": OK,
        "detail": g2_detail,
        "rounds": rounds_info,
    })

    a_fin = analyze(c, final)
    checks.append({
        "id": "G3", "name": "最终稿已复检", "status": OK,
        "detail": f"score={a_fin['score']}，issues={len(a_fin['issues'])} 条",
        "issues": a_fin["issues"],
    })

    remaining = a_fin["issues"]
    checks.append({
        "id": "G4", "name": "剩余 issue 清零",
        "status": OK if not remaining else FAIL,
        "detail": "无剩余问题" if not remaining else "仍有 " + "；".join(remaining),
        "remaining": remaining,
    })

    cmp = compare(c, raw, final)
    score_final = evaluate(c, final)
    passed5 = cmp["winner"] == "after" and cmp["delta"] > 0
    checks.append({
        "id": "G5", "name": "分数确实提升",
        "status": OK if passed5 else FAIL,
        "detail": f"{a_raw['score']} → {score_final}（delta={cmp['delta']}，winner={cmp['winner']}）",
    })

    stub_out = rewrite_stub(render_optimize_messages(raw, mode))
    is_stub = final.strip() == stub_out.strip()
    checks.append({
        "id": "G6", "name": "非通用模板产物",
        "status": FAIL if is_stub else OK,
        "detail": "最终稿等于 rewrite_stub 空壳模板，不可交付" if is_stub else "含领域内容，非模板",
    })

    # ---- v2 新增门禁（DEEP_ANALYSIS_AND_PLAN.md §4）----------------------
    lint = consistency_lint(final)
    checks.append({
        "id": "G7", "name": "自洽性 lint",
        "status": OK if not lint else FAIL,
        "detail": "无互斥约束" if not lint else "；".join(lint),
        "conflicts": lint,
    })

    missing_vars = missing_variables(raw, final)
    n_raw_vars = len(extract_variables(raw))
    # 修 2026-10-09 回归 BUG-04：原文案把「一个都没缺」写成「原文无 {{var}} 占位符」，
    # 会让读报告的人误以为 G9 空转（KNOWN_ISSUES F.8 同型）。改为如实报告数量。
    if missing_vars:
        g9_detail = f"缺失 {len(missing_vars)} 个：{missing_vars}"
    elif n_raw_vars:
        g9_detail = f"原文 {n_raw_vars} 个 {{{{var}}}} 全部逐字保留"
    else:
        g9_detail = "原文无 {{var}} 占位符（无需保留）"
    checks.append({
        "id": "G9", "name": "变量完整性",
        "status": OK if not missing_vars else FAIL,
        "detail": g9_detail,
        "missing": missing_vars,
    })

    # ---- G10 盲评门禁（C3 独立性：主代理不打分，judge 子代理必须真实参与）--
    if protocol_history is not None and not blind_expected:
        # 未收敛（轮数耗尽 / delta 停滞）：本就没进 AWAIT_JUDGE，不属 G10 失职
        checks.append({"id": "G10", "name": "盲评已通过", "status": "SKIP",
                       "detail": "未收敛，未触发盲评轮（真实阻塞原因见 G4）"})
    elif protocol_history is not None:
        verdict = (blind_review or {}).get("verdict")
        if verdict == "PASS":
            # 质量收敛层复核（防裁决块与门禁脱节）：verdict=PASS 还须
            # 盲评分 ≥ 质量目标 且 无低于危险线的维度（与 run_pipeline 对齐）。
            qt = (blind_review or {}).get("quality_target")
            bs = (blind_review or {}).get("blind_score")
            low_dims = (blind_review or {}).get("low_dims") or []
            if qt and (bs is None or bs < qt or low_dims):
                g10_status = FAIL
                g10_detail = (f"质量门禁未达标：盲评 {bs} < 目标 {qt}"
                              f" 或低维 {low_dims}——G10 不允许 PASS")
            else:
                g10_status, g10_detail = OK, (
                    f"独立 judge 盲评通过（综合 {bs}"
                    + (f"≥目标 {qt}" if qt else "")
                    + "，无 blocking_conflicts）")
        elif verdict == "BELOW_TARGET":
            g10_status = FAIL
            g10_detail = (
                f"盲评 {(blind_review or {}).get('blind_score')} 未达质量目标 "
                f"{(blind_review or {}).get('quality_target')}"
                f"（低维 {(blind_review or {}).get('low_dims') or '—'}）"
                "——质量返工耗尽/无改善，不允许带病交付")
        else:
            g10_status = FAIL
            if blind_review:
                g10_detail = (f"盲评 verdict={verdict}，conflicts="
                              f"{len(blind_review.get('blocking_conflicts') or [])}")
            else:
                g10_detail = "协议模式但无盲评结论（fulfil/judge.json 缺失）——" \
                             "收敛后必须经过 AWAIT_JUDGE 盲评轮"
        checks.append({"id": "G10", "name": "盲评已通过",
                       "status": g10_status, "detail": g10_detail})
    else:
        checks.append({"id": "G10", "name": "盲评已通过", "status": "SKIP",
                       "detail": "legacy 模式无盲评轮（结构性盲评仅在协议模式强制）"})

    gate = OK if all(x["status"] == OK for x in checks
                     if x["status"] != "SKIP") else FAIL
    return {
        "gate": gate,
        "score_before": a_raw["score"],
        "score_final": score_final,
        "delta": score_final - a_raw["score"],
        "checks": checks,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if len(args) < 2:
        print(__doc__)
        raise SystemExit(2)

    rounds = int(args[2]) if len(args) > 2 else 2
    mode = args[3] if len(args) > 3 else "system"
    result = run(_resolve(args[0]), _resolve(args[1]), rounds, mode)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["gate"] == OK else 1)

    print(f"\n交付门禁：{result['gate']}   （{result['score_before']} → {result['score_final']}，"
          f"delta={result['delta']}）")
    print("-" * 60)
    for c in result["checks"]:
        mark = "✓" if c["status"] == OK else ("○" if c["status"] == "SKIP" else "✗")
        print(f"  {mark} {c['id']} {c['name']:<14} {c['detail']}")
    print("-" * 60)
    if result["gate"] == FAIL:
        print("  未通过，禁止交付。修完重跑本命令。\n")
        raise SystemExit(1)
    print("  全部通过，允许交付。\n")


if __name__ == "__main__":
    main()
