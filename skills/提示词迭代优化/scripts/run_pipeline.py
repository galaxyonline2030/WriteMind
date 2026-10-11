"""全流程可视化编排器 v2.1（三段式文件协议 + 结构性子代理参与，确定性，零 LLM）
==========================================================================

DEEP_ANALYSIS_AND_PLAN.md §1 的落地。引擎换位：agent-first——
改写/迭代/盲评是生成式接缝，必须由**独立子代理** fulfil；主代理只做文件
编排（spawn 子代理、传文件），脚本只做前处理（诊断+patchPlan）与后门禁
（契约+G1–G10），并通过「state.json + 退出码」与代理握手。

子代理参与是协议的结构性环节，不是可选项：
  ① 每一轮改写由无上下文 fulfil 子代理执行（--dispatch 生成任务卡）；
  ② 收敛后强制盲评轮（AWAIT_JUDGE）：第二个无上下文 judge 子代理只看
     终稿文本打分，blocking_conflicts 非空 → 有界返工回灌（G10 门禁：
     没有盲评结论就不允许 PASS 交付）。

用法：
  python run_pipeline.py "@raw.txt" [system|user] [rounds] [scenario] \
      [--min-blind-score N] [--quality-reworks N] [--max-iterations N]
      Phase A：诊断 → patchPlan → 写 runs/<ts>/state.json
      退出码 10 = AWAIT_FULFILL

  可选 flag：
    --min-blind-score N   盲评质量目标（0=关闭质量门禁），默认 85（未校准临时值）
    --quality-reworks N   质量返工次数上限，默认 2
    --max-iterations  N   全局最大改写轮次硬上限（结构循环+返工合计），默认 5

  python run_pipeline.py --dispatch runs/<ts>
      按 state.phase 渲染子代理任务卡：
        AWAIT_FULFILL → dispatch/roundN.md（fulfil 子代理）
        AWAIT_JUDGE   → dispatch/judge.md（盲评 judge 子代理）
      主代理用 Agent 工具把它原样交给子代理执行。退出码 10。

  python run_pipeline.py --resume runs/<ts>
      按 state.phase 分派：
        AWAIT_FULFILL → Phase B：读 fulfil/roundN.txt → 接缝+契约 →
                        G7/G9 → evaluate → 收敛判定
        AWAIT_JUDGE   → 读 fulfil/judge.json（盲评结论）→ 无冲突 →
                        G10 PASS 收尾；有冲突 → 一次有界返工回灌
      退出码：10 = 交接（fulfil / judge / 返工）；0 = PASS；1 = FAIL

  python run_pipeline.py --legacy "@raw.txt" "@final.md" [rounds] [mode]
      旧式一步到位门禁（G10 记 SKIP）。

  python run_pipeline.py --status runs/<ts>

退出码约定：10 不是错误，是脚本→主代理的交接信号；state.json 是唯一事实源。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contract import (  # noqa: E402
    SHAPE_BLIND_REVIEW,
    SHAPE_ITERATE_DIRECTED,
    SHAPE_REWRITE_INFORMED,
)
from optimizer_adapter import (  # noqa: E402
    analyze,
    compare,
    evaluate,
    iterate_directed,
    informed_rewrite,
    load_template,
    pick_template,
    render_optimize_messages,
    render_iterate_messages,
    render_informed_rewrite_messages,
    render_judge_messages,
)
from stubs import (  # noqa: E402
    analyze_stub,
    build_patch_plan,
    compare_stub,
    consistency_lint,
    missing_variables,
    rewrite_stub,
    score_stub,
)
from virtual_llm_client import (  # noqa: E402
    AgentRealizationRequired,
    ContractViolation,
    VirtualLLMClient,
)
from verify_delivery import run as verify_run  # noqa: E402

W = 62
EPS = 5          # delta < EPS 视为停滞
MAX_RETRIES = 3  # 每轮门禁失败的重派上限（对齐 llm-client-sim 失败处理）
JUDGE_RETRIES = 2  # 盲评 judge.json 契约校验失败的重新派发上限
SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_FULFILL = 0, 1, 2, 10

# ---- 质量收敛层（v2 增强 #6：盲评分作为迭代终止条件）-----------------------
# DEFAULT_MIN_BLIND_SCORE = 85 是【临时值 provisional】：校准数据 n=1
#（2026-10-10 简历稿终稿盲评 85）。按「绝对阈值必须用领域内公认好样本校准」
# 原则，正式启用前应拿 2-3 份公认好稿测 judge 分数地板，再定稳定阈值。
# 调用方可通过 --min-blind-score 覆盖（传 0 关闭质量门禁，退回纯结构收敛）。
DEFAULT_MIN_BLIND_SCORE = 85
DEFAULT_QUALITY_REWORKS = 2   # 质量返工次数上限（默认 2，与 max_rounds=2 对齐；每次配额 +1，防过拟合 judge 噪声）
DIM_DANGER_LINE = 60          # 危险线：judge 模板语义自带「<60 的维度必须写明原因」
DEFAULT_MAX_ITERATIONS = 5    # 全局最大迭代（改写）轮次硬上限：结构循环 + 返工 合计不超过此数；可用 --max-iterations 覆盖


def _resolve(arg: str) -> str:
    if arg.startswith("@") and len(arg) > 1:
        with open(arg[1:], encoding="utf-8") as f:
            return f.read()
    return arg


def _client(provider: str = "run-pipeline") -> VirtualLLMClient:
    c = VirtualLLMClient(provider=provider)
    # 只注册确定性分析类形状；rewrite_informed / iterate_directed 故意不注册——
    # 生成式接缝必须走 realizer（B1 修复：stub 不再冒充优化器）。
    c.register_deterministic("analysis", analyze_stub)
    c.register_deterministic("score", score_stub)
    c.register_deterministic("compare", compare_stub)
    return c


def hdr(title: str) -> None:
    print("\n" + "─" * W)
    print(f"  {title}")
    print("─" * W)


def ok(msg: str) -> None:
    print(f"        ✓ {msg}")


def info(msg: str) -> None:
    print(f"        ⓘ {msg}")


def warn(msg: str) -> None:
    print(f"        ✗ {msg}")


def _save_state(rundir: str, state: dict) -> None:
    with open(os.path.join(rundir, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _rundir(arg: str) -> str:
    """解析 run 目录：原样优先，其次相对 SKILL_DIR。

    修 2026-10-07 实战：脚本播报的是 `runs/<ts>`，但 rundir 实际建在 SKILL_DIR
    下；从 scripts/ 里照抄播报命令会得到 FileNotFoundError。这里做一次兜底解析，
    让「照抄播报」永远可用。
    """
    if os.path.isdir(arg):
        return os.path.normpath(arg)
    cand = os.path.normpath(os.path.join(SKILL_DIR, arg))
    if os.path.isdir(cand):
        return cand
    return os.path.normpath(arg)


def _load_state(rundir: str) -> dict:
    with open(os.path.join(rundir, "state.json"), encoding="utf-8") as f:
        return json.load(f)


def _latency_sec(state: dict, path: str) -> float | None:
    """任务卡派发 → 产出文件落盘 的间隔秒数（无派发留痕或文件不存在则 None）。

    脚本自身耗时 <1s，因此间隔显著大于 1s 可作为「中间有子代理在工作」的客观留痕。
    """
    at = state.get("dispatch_at")
    if not at or not os.path.exists(path):
        return None
    try:
        dispatched = datetime.fromisoformat(at).timestamp()
    except ValueError:
        return None
    return round(os.path.getmtime(path) - dispatched, 1)


def _write_report(rundir: str, state: dict, gate: str, extra: dict) -> str:
    report = {
        "run_dir": rundir,
        "mode": state["mode"],
        "rounds_completed": len(state["history"]),
        "gate": gate,
        "score_before": state["baseline"]["score"],
        "score_final": state["history"][-1]["score_after"] if state["history"] else None,
        "remaining_issues": state["history"][-1]["issues"] if state["history"] else [],
        "final": state.get("delivered_final") or state.get("last_final", ""),
        "template": state["template"],
        "history": state["history"],
        **extra,
    }
    path = os.path.join(rundir, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# Phase A：诊断 + patchPlan + state 落盘 → exit 10
# ---------------------------------------------------------------------------
def phase_a(raw_path: str, mode: str, rounds: int, scenario: str,
            min_blind_score: int | None = DEFAULT_MIN_BLIND_SCORE,
            quality_reworks: int = DEFAULT_QUALITY_REWORKS,
            max_iterations: int = DEFAULT_MAX_ITERATIONS) -> None:
    raw = _resolve(raw_path)
    c = _client("phase-a")

    hdr(f"Phase A · 诊断与改写指令   模式={mode}   上限={rounds} 轮   场景={scenario}")
    print(f"  原始：{raw_path}（{len(raw)} 字符）")

    t = time.perf_counter()
    a_raw = analyze(c, raw)
    for iss in a_raw["issues"]:
        print(f"          · {iss}")
    ok(f"基线诊断：{len(a_raw['issues'])} 条问题"
       f"（结构要素分仅内部衬底，不作分数呈现；质量分以盲评为准）")

    plan = build_patch_plan(a_raw["issues"], a_raw["suggestions"])
    ok(f"patchPlan 就绪：{len(plan)} 条改写指令（issues 回灌管道）")
    for p in plan:
        print(f"          · [{p['target']}] → {p['action']}")

    template = pick_template(mode, scenario)
    ok(f"路由模板：templates/{template}")

    # 修 2026-10-09 回归 BUG-01：原实现用秒级时间戳 + exist_ok=True，
    # 并发 / 同秒启动的两次运行会静默复用同一 run 目录——state.json 与 fulfil/
    # 互相覆盖，且并发 open('w') 会抛 PermissionError。改为「已存在即顺延序号」，
    # 保证每次运行独占一个目录（exist_ok=False 让冲突显式暴露而非静默串味）。
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    seq = 0
    while True:
        ts = base if seq == 0 else f"{base}-{seq}"
        rundir = os.path.join(SKILL_DIR, "runs", ts)
        try:
            os.makedirs(os.path.join(rundir, "fulfil"), exist_ok=False)
            break
        except FileExistsError:
            seq += 1
            if seq > 100:
                raise RuntimeError(f"run 目录连续冲突 100 次，放弃：{base}")
    if seq:
        print(f"  ⓘ 同秒已存在同名 run 目录，本次顺延为 runs/{ts}（BUG-01 防护）")
    with open(os.path.join(rundir, "raw.md"), "w", encoding="utf-8") as f:
        f.write(raw)

    state = {
        "version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_path": raw_path,
        "mode": mode,
        "max_rounds": rounds,
        "max_iterations": max(1, int(max_iterations)),
        "scenario": scenario,
        "template": template,
        "round": 1,
        "retries": 0,
        "phase": "AWAIT_FULFILL",
        "raw": raw,
        "baseline": a_raw,
        "pending_plan": plan,
        "last_final": "",
        "history": [],
        # 质量收敛层：盲评分作为迭代终止条件（两层收敛 = 结构清零 且 盲评达标）
        "quality_target": (None if not min_blind_score else int(min_blind_score)),
        "quality_rework_max": max(0, int(quality_reworks)),
        "quality_rework_count": 0,
        "conflicts_rework_done": False,
    }
    _save_state(rundir, state)

    hdr("AWAIT_FULFILL · 派发 fulfil 子代理")
    print(f"  1. python run_pipeline.py --dispatch {rundir}"
          f"  → 生成 dispatch/round1.md（子代理任务卡）")
    print(f"  2. 用 Agent 工具 spawn 一个**独立子代理**，把任务卡原文交给它执行")
    print(f"     （子代理按模板改写并把终稿写入 fulfil/round1.txt；"
          f"主代理不得代写，防上下文污染）")
    print(f"  3. 子代理完成后重跑：python run_pipeline.py --resume runs/{ts}")
    print(f"\n  退出码 10（交接信号，非错误）  耗时 {time.perf_counter() - t:.2f}s")
    raise SystemExit(EXIT_FULFILL)


# ---------------------------------------------------------------------------
# Phase B：fulfil 注入 → 契约 → 门禁 → 评估 → 收敛判定
# ---------------------------------------------------------------------------
def phase_b(rundir: str) -> None:
    state = _load_state(rundir)
    rnd = state["round"]
    mode = state["mode"]
    raw = state["raw"]
    fulfil_path = os.path.join(rundir, "fulfil", f"round{rnd}.txt")
    c = _client("phase-b")

    hdr(f"Phase B · 第 {rnd} 轮校验   模式={mode}   重试 {state['retries']}/{MAX_RETRIES}")

    if state["phase"] != "AWAIT_FULFILL":
        warn(f"state.phase={state['phase']}，无待 fulfil 任务；用 --status 查看。")
        raise SystemExit(EXIT_USAGE)

    if not os.path.exists(fulfil_path):
        info(f"未找到 {fulfil_path}")
        print("\n  派发 fulfil 子代理：")
        print(f"  1. python run_pipeline.py --dispatch {rundir}"
              f"  → 生成 dispatch/round{rnd}.md")
        print("  2. 用 Agent 工具 spawn 独立子代理执行任务卡"
              + ("（informed 改写）" if rnd == 1 else "（iterate_directed 定向迭代）"))
        print("  3. 必须逐条落实 pending_plan 的每一条 action；"
              "逐字保留原文全部 {{var}}（G9 门禁）")
        print(f"  4. 子代理写完 {fulfil_path} 后重跑 --resume")
        raise SystemExit(EXIT_FULFILL)

    with open(fulfil_path, encoding="utf-8") as f:
        fulfil_text = f.read()

    latency = _latency_sec(state, fulfil_path)
    if latency is not None:
        tag = "（脚本自身耗时 <1s，间隔即子代理工作耗时）" if latency > 1 else \
              "（⚠ 间隔 <1s：产出疑似非子代理执行，请核对）"
        ok(f"派发留痕：{state.get('dispatch_card')} → {os.path.basename(fulfil_path)}"
           f" 间隔 {latency}s {tag}")

    # ---- 生成式接缝：realizer = 代理产出，必须过接缝 + 契约（B1/B3 修复）----
    t = time.perf_counter()
    current = raw if rnd == 1 else state["last_final"]
    try:
        c_agent = VirtualLLMClient(provider="agent-seam")
        c_agent.set_realizer(lambda _c, _m, _s: fulfil_text)
        if rnd == 1:
            msgs = render_informed_rewrite_messages(raw, state["pending_plan"],
                                                    state["template"])
            admitted = c_agent.call(msgs, SHAPE_REWRITE_INFORMED)
            seam = "rewrite_informed"
        else:
            requirement = "；".join(p["action"] for p in state["pending_plan"])
            msgs = render_iterate_messages(current, requirement)
            admitted = c_agent.call(msgs, SHAPE_ITERATE_DIRECTED)
            seam = "iterate_directed"
        ok(f"已过生成式接缝 [{seam}]（realizer=代理产出），契约校验通过，"
           f"{len(admitted)} 字符（耗时 {time.perf_counter() - t:.3f}s）")
    except ContractViolation as e:
        state["retries"] += 1
        state["fail_feedback"] = f"契约校验失败：{e}"
        _save_state(rundir, state)
        warn(f"{e}")
        if state["retries"] >= MAX_RETRIES:
            warn("重派次数耗尽 → FAIL")
            path = _write_report(rundir, state, "FAIL",
                                 {"abort": "contract_violation", "detail": str(e)})
            print(f"  报告：{path}")
            raise SystemExit(EXIT_FAIL)
        info(f"请修正后覆盖写入 {fulfil_path}，再重跑 --resume（重派 "
             f"{state['retries']}/{MAX_RETRIES}）")
        raise SystemExit(EXIT_FULFILL)

    final = admitted

    # ---- 门禁三件套（每轮都跑，退步当场拦截）--------------------------
    t = time.perf_counter()
    lint = consistency_lint(final)
    missing_vars = missing_variables(raw, final)
    gate_round = not (lint or missing_vars)
    if lint:
        for x in lint:
            warn(f"G7 {x}")
    if missing_vars:
        warn(f"G9 缺失变量：{missing_vars}")
    if gate_round:
        ok("G7 自洽 / G9 变量完整 全部通过")
    print(f"        （耗时 {time.perf_counter() - t:.3f}s）")

    if not gate_round:
        state["retries"] += 1
        feedback = "；".join(lint +
                            [f"缺失变量 {v}" for v in missing_vars])
        state["fail_feedback"] = feedback
        _save_state(rundir, state)
        if state["retries"] >= MAX_RETRIES:
            warn("重派次数耗尽 → FAIL")
            path = _write_report(rundir, state, "FAIL", {"abort": "round_gates"})
            print(f"  报告：{path}")
            raise SystemExit(EXIT_FAIL)
        info(f"请按上述反馈修正后覆盖写入 {fulfil_path}，再重跑 --resume（重派 "
             f"{state['retries']}/{MAX_RETRIES}）")
        raise SystemExit(EXIT_FULFILL)

    # ---- 评估与收敛判定（全部确定性）---------------------------------
    t = time.perf_counter()
    a_fin = analyze(c, final)
    score_before = state["baseline"]["score"] if rnd == 1 else state["history"][-1]["score_after"]
    score_after = a_fin["score"]
    cmp = compare(c, current, final)
    delta = score_after - score_before
    state["history"].append({
        "round": rnd,
        "input": current,
        "output": final,
        "fulfil_file": os.path.relpath(fulfil_path, rundir),
        "seam": seam if rnd > 1 else "rewrite_informed",
        "score_before": score_before,
        "score_after": score_after,
        "delta": delta,
        "compare": cmp,
        "dispatch_card": state.get("dispatch_card"),
        "fulfil_latency_sec": latency,
        # 本轮落实的改写指令（可视化报告逐轮展示"改了什么、为何改"）
        "patch_plan": state.get("pending_plan", []),
        "retries": state["retries"],
        "issues": a_fin["issues"],
        "gates": {"G7": "PASS" if not lint else "FAIL",
                  "G9": "PASS" if not missing_vars else "FAIL"},
    })
    prev_issues_n = (len(state["baseline"]["issues"]) if rnd == 1
                     else len(state["history"][-1]["issues"]))
    ok(f"剩余 issues {prev_issues_n} → {len(a_fin['issues'])} 条"
       f"（收敛判定 = 清零；结构分仅内部衬底，不作分数呈现）")

    if a_fin["issues"]:
        for iss in a_fin["issues"]:
            print(f"          · {iss}")

    converged = not a_fin["issues"]
    plateau = (len(state["history"]) >= 2
               and state["history"][-1]["delta"] < EPS
               and state["history"][-2]["delta"] < EPS)
    out_of_rounds = (rnd >= state["max_rounds"]
                     or rnd >= state.get("max_iterations", DEFAULT_MAX_ITERATIONS))

    if converged:
        # 收敛 ≠ 可交付：强制盲评轮（C3 独立性 / G10 门禁）。
        # 主代理不得自己打分——运动员不能兼裁判，必须派第二个无上下文子代理。
        state["last_final"] = final
        state["phase"] = "AWAIT_JUDGE"
        _save_state(rundir, state)
        info("issues 清零，进入盲评阶段 AWAIT_JUDGE（C3：运动员不能兼裁判）")
        print("\n  派发盲评 judge 子代理：")
        print(f"  1. python run_pipeline.py --dispatch {rundir}"
              f"  → 生成 dispatch/judge.md")
        print("  2. 用 Agent 工具 spawn 一个**全新的、无本会话上下文**的子代理，")
        print("     把任务卡原文交给它——judge 只看终稿文本，看不到原始稿与历史分数")
        print("  3. judge 把评审 JSON 写入 fulfil/judge.json 后重跑 --resume")
        print("     （blocking_conflicts 非空 → 一次有界返工回灌；二次冲突 → FAIL）")
        print(f"\n  （耗时 {time.perf_counter() - t:.3f}s）")
        raise SystemExit(EXIT_FULFILL)
    elif plateau or out_of_rounds:
        state["last_final"] = final
        _save_state(rundir, state)
        reason = "delta 连续 2 轮停滞" if plateau else "达到轮数上限"
        warn(f"{reason}，但仍有 {len(a_fin['issues'])} 条 issues → FAIL（G4 不清零不交付）")
        _finish(rundir, state, raw, mode, "FAIL", t,
                {"stop_reason": reason, "remaining_issues": a_fin["issues"]},
                blind_expected=False)
    else:
        plan = build_patch_plan(a_fin["issues"], a_fin["suggestions"])
        state["round"] = rnd + 1
        state["retries"] = 0
        state["pending_plan"] = plan
        state["last_final"] = final
        state["phase"] = "AWAIT_FULFILL"
        # 结构回灌 = 离开返工语境：清返工标记与质量反馈，防泄漏进后续任务卡
        state.pop("rework_kind", None)
        state.pop("quality_feedback", None)
        _save_state(rundir, state)
        info(f"issues 回灌为第 {rnd + 1} 轮 patchPlan（{len(plan)} 条指令）")
        print(f"\n  派发下一轮 fulfil 子代理：--dispatch {rundir}"
              f"  → dispatch/round{rnd + 1}.md")
        print(f"  （iterate_directed 定向融合：修改上一轮终稿，不执行需求）"
              f"  耗时 {time.perf_counter() - t:.3f}s")
        raise SystemExit(EXIT_FULFILL)


# ---------------------------------------------------------------------------
# --dispatch：渲染子代理任务卡（结构性子代理参与，非可选）
# ---------------------------------------------------------------------------
FULFIL_CARD = """# 子代理任务卡 · Fulfil（第 {rnd} 轮 · {kind}）

你是被编排脚本派生的**独立改写子代理**。你与主代理、与后续评估/盲评环节互相
隔离：你只看到本任务卡里的材料，看不到任何对话历史。主代理只做文件编排，
**不会替你写内容**；你的产出经 fulfil 文件进入生成式接缝并接受契约与门禁校验。

## 强约束指令（模板正文，逐条遵守）

```markdown
{template}
```

## 改写指令 patchPlan（必须逐条落实，每条都要能在终稿中找到对应改动）

```json
{plan}
```

## 待处理文本

```text
{source}
```
{feedback}
## 输出要求（红线）

1. **只把终稿提示词本身**写入文件：{out_path}（UTF-8）。
   不要写解释、对话、评分、进度说明——文件里只能是提示词。
2. 逐字保留待处理文本中的全部 `{{{{variable}}}}` 占位符（G9 门禁机器校验）。
3. 消除全部模糊词（一些/尽量/大概/若干/适度…），约束写成可判断真假的句子。
4. 模板中的输出结构硬性要求必须满足；禁引导词；领域知识写实，不用占位泛称。
5. 写完文件后，回复一行 `DONE <字符数>`，然后立即结束。不要做其他任何事。
"""

JUDGE_CARD = """# 子代理任务卡 · 盲评 Judge（C3 独立性）

你是被编排脚本派生的**独立评审子代理**，与改写过程完全隔离：
你只能看到下面这一段终稿文本——没有原始稿、没有历史轮次、没有已有分数。
请以陌生评审的身份独立判断，不得猜测"作者想表达什么"。

## 评审指令（模板正文，逐条遵守）

```markdown
{template}
```

## 评审对象（仅此文本）

```text
{prompt}
```
{feedback}
## 输出要求（红线）

1. 按模板要求输出严格 JSON（放在 ```json 代码块中），必含字段：
   summary、scores、strengths、weaknesses、patchPlan、blocking_conflicts。
2. `blocking_conflicts`：若发现互斥约束、使约束失效的元约束等
   「不返工就不能交付」的问题，逐条列出（字符串数组）；没有则输出 `[]`。
3. 把 **JSON 本体**写入文件：{out_path}（UTF-8，不要附加解释文字）。
4. 写完文件后，回复一行 `DONE`，然后立即结束。不要做其他任何事。
"""


def _dispatch(rundir: str) -> None:
    """按 state.phase 渲染子代理任务卡到 dispatch/，exit 10 交回主代理。

    主代理的职责边界：spawn 子代理 + 传任务卡 + 收文件。任务卡是自包含的
    （模板正文 + 素材 + 输出路径 + 红线），子代理不需要任何会话上下文。
    """
    state = _load_state(rundir)
    phase = state["phase"]
    os.makedirs(os.path.join(rundir, "dispatch"), exist_ok=True)
    fb = state.get("fail_feedback") or ""  # 上一版被门禁/契约拒绝的原因（BUG-03）

    if phase == "AWAIT_FULFILL":
        rnd = state["round"]
        template = state["template"] if rnd == 1 else "iterate_directed.md"
        source = state["raw"] if rnd == 1 else state["last_final"]
        out_path = os.path.join(rundir, "fulfil", f"round{rnd}.txt")
        # 修 2026-10-09 回归 BUG-03：同轮重派必须带门禁反馈（SKILL.md §7.4），
        # 否则子代理拿到与首派相同的卡会产出同样的错误，配额耗尽即 FAIL。
        feedback = ""
        if fb and state.get("retries", 0) > 0:
            feedback = (
                "\n## ⚠ 上一版产出被门禁拒绝（第 "
                f"{state['retries']}/{MAX_RETRIES} 次重派）——必须逐条修掉\n\n"
                f"```text\n{fb}\n```\n\n"
                "修改不了就不要原样重交：同样的产出会被再次拒绝，"
                "重派配额耗尽后整轮 FAIL。\n"
            )
        # 质量返工卡：附上 judge 弱点判定，子代理知道本轮要补什么（C3 不受影响：
        # judge 与改写子代理依旧互相隔离，此处只是把 judge 结论回灌给改写方——
        # 对齐上游「评估→改写闭环」机制）。
        qfb = state.get("quality_feedback") or ""
        if qfb and state.get("rework_kind") == "quality":
            feedback += (
                "\n## 🎯 质量返工背景（盲评未达标）——本轮补强目标\n\n"
                f"```text\n{qfb}\n```\n\n"
                "以上是独立 judge 对上一版终稿的弱点判定，必须逐条针对性改进；"
                "同时保持全部 {{var}} 占位符与已达标的结构不变。\n"
            )
        card = FULFIL_CARD.format(
            rnd=rnd,
            kind="informed 改写" if rnd == 1 else "iterate_directed 定向迭代",
            template=load_template(template),
            plan=json.dumps(state["pending_plan"], ensure_ascii=False, indent=2),
            source=source,
            out_path=out_path,
            feedback=feedback,
        )
        out = os.path.join(rundir, "dispatch", f"round{rnd}.md")
    elif phase == "AWAIT_JUDGE":
        msgs = render_judge_messages(state["last_final"] or state["raw"])
        template = msgs[0]["content"]  # system 消息 = evaluation_judge 模板正文
        out_path = os.path.join(rundir, "fulfil", "judge.json")
        # 修 2026-10-09 回归 BUG-07（BUG-03 在 judge 路径的复发变体）：
        # judge 契约失败写在 state["judge_feedback"]，此处却读 fulfil 路径的
        # fail_feedback（fb）→ 条件永假，重派卡与首派逐字相同，judge 会重犯
        # 同样的契约错误直至配额耗尽。改为读 judge_feedback。
        feedback = ""
        jfb = state.get("judge_feedback") or ""
        if jfb and state.get("judge_retries", 0) > 0:
            feedback = (
                "\n## ⚠ 上一版 judge.json 被契约校验拒绝（第 "
                f"{state['judge_retries']}/{JUDGE_RETRIES} 次重派）——必须修掉\n\n"
                f"```text\n{jfb}\n```\n\n"
                "常见原因：字符串值里出现非法转义（如反引号前加反斜杠）、JSON 外"
                "包裹了说明文字、分值超出 0-100 契约范围。文件首字符必须是 `{`，"
                "且不要写任何反斜杠。\n"
            )
        card = JUDGE_CARD.format(
            template=template,
            prompt=state["last_final"],
            out_path=out_path,
            feedback=feedback,
        )
        out = os.path.join(rundir, "dispatch", "judge.md")
    else:
        warn(f"state.phase={phase}，无需派发；用 --status 查看。")
        raise SystemExit(EXIT_USAGE)

    with open(out, "w", encoding="utf-8") as f:
        f.write(card)

    # 派发留痕（修 C3「盲评/改写是否真由子代理执行」不可验证）：
    # 记录派发时刻与任务卡路径，--resume 时用产出文件 mtime 算出「卡渲染 → 产出落盘」
    # 的间隔。脚本自身耗时 <1s，间隔显著大于 1s 即证明中间有子代理在工作；
    # 这不能证明"是谁写的"，但把"有没有跑"从口头约定变成可复盘的机器留痕。
    state["dispatch_at"] = datetime.now().isoformat(timespec="seconds")
    state["dispatch_card"] = os.path.relpath(out, rundir)
    state["dispatch_out"] = os.path.relpath(out_path, rundir)
    _save_state(rundir, state)

    who = "fulfil 改写子代理" if phase == "AWAIT_FULFILL" else "盲评 judge 子代理"
    hdr(f"DISPATCH · {who} 任务卡就绪")
    print(f"  任务卡：{out}")
    print(f"  子代理产出应写入：{out_path}")
    print(f"  派发时刻：{state['dispatch_at']}（已写入 state.json，用于核算产出延迟）")
    print("\n  主代理动作（只做编排，不代写）：")
    print("  1. 用 Agent 工具 spawn 独立子代理，把任务卡**原文**作为 prompt 交给它")
    print("  2. 子代理写完产出文件后，重跑：python run_pipeline.py --resume "
          + rundir)
    raise SystemExit(EXIT_FULFILL)


# ---------------------------------------------------------------------------
# AWAIT_JUDGE：盲评裁决（C3 独立性 / G10 门禁）
# ---------------------------------------------------------------------------
def _load_json_lenient(path: str) -> dict:
    """宽容解析子代理写的 JSON：容忍 ```json 围栏与前后杂散文字。"""
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _normalize_plan(items: list) -> list:
    """把 judge 返回的 conflicts/patchPlan 归一为 {target, action} 指令列表。"""
    plan = []
    for it in items:
        if isinstance(it, str):
            plan.append({"target": "盲评冲突", "action": it})
        elif isinstance(it, dict) and it.get("action"):
            plan.append({"target": str(it.get("target", "盲评意见")),
                         "action": str(it["action"])})
    return plan


def _blind_score(review: dict) -> tuple:
    """综合盲评分：(score, source)。

    对齐上游 evaluation/content.ts 的 overall 语义：模型的整体质量判断
    **优先于**各维算术平均——上游把 overall 留给 judge 综合裁定，而不是
    代码求均值；仅当 judge 未给出 overall（旧版 judge.json）时回退均值。
    """
    overall = review.get("overall")
    if isinstance(overall, (int, float)) and not isinstance(overall, bool):
        return round(float(overall), 1), "overall"
    scores = review.get("scores")
    if isinstance(scores, dict) and scores:
        vals = [float(v) for v in scores.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if vals:
            return round(sum(vals) / len(vals), 1), "mean_fallback"
    return None, None


def phase_judge(rundir: str) -> None:
    state = _load_state(rundir)
    if state["phase"] != "AWAIT_JUDGE":
        warn(f"state.phase={state['phase']}，无待盲评任务；用 --status 查看。")
        raise SystemExit(EXIT_USAGE)

    hdr(f"AWAIT_JUDGE · 盲评裁决   轮次 {state['round']}/{state['max_rounds']}")
    judge_path = os.path.join(rundir, "fulfil", "judge.json")

    if not os.path.exists(judge_path):
        print("\n  派发盲评 judge 子代理：")
        print(f"  1. python run_pipeline.py --dispatch {rundir}"
              f"  → 生成 dispatch/judge.md")
        print("  2. 用 Agent 工具 spawn **无本会话上下文**的独立子代理执行任务卡")
        print(f"  3. judge 写完 {judge_path} 后重跑 --resume")
        raise SystemExit(EXIT_FULFILL)

    latency = _latency_sec(state, judge_path)
    if latency is not None:
        tag = "（脚本自身耗时 <1s，间隔即 judge 子代理工作耗时）" if latency > 1 else \
              "（⚠ 间隔 <1s：盲评疑似非子代理执行，G10 独立性存疑）"
        ok(f"派发留痕：{state.get('dispatch_card')} → judge.json 间隔 {latency}s {tag}")

    try:
        review = SHAPE_BLIND_REVIEW.validate(_load_json_lenient(judge_path))
    except (ContractViolation, ValueError) as e:
        state["judge_retries"] = state.get("judge_retries", 0) + 1
        state["judge_feedback"] = str(e)
        _save_state(rundir, state)
        warn(f"盲评结论契约校验失败：{e}")
        if state["judge_retries"] >= JUDGE_RETRIES:
            warn("judge 重派次数耗尽 → FAIL")
            path = _write_report(rundir, state, "FAIL",
                                 {"abort": "judge_contract", "detail": str(e)})
            print(f"  报告：{path}")
            raise SystemExit(EXIT_FAIL)
        info(f"请重新派发 judge 子代理（修正 JSON 形状后覆盖写入 "
             f"{judge_path}），再重跑 --resume（重派 "
             f"{state['judge_retries']}/{JUDGE_RETRIES}）")
        raise SystemExit(EXIT_FULFILL)

    # 盲评留痕：每次盲评结论归档为 judge_r<N>.json（原 judge.json 会被下一轮覆盖），
    # 供可视化报告按次展示每一轮盲评的分数/意见/冲突。
    try:
        archive = os.path.join(rundir, "fulfil", f"judge_r{state['round']}.json")
        with open(judge_path, encoding="utf-8") as src, \
                open(archive, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    except OSError:
        archive = None

    scores = review["scores"]
    blind_score, blind_src = _blind_score(review)
    # 刻度疑似误用告警：范围契约拦得住 >100 的越界，拦不住 0-10 刻度
    #（其值全部落在 0-100 内）。全维 ≤10 时提示人工核对（模板已显式要求 0-100）。
    if isinstance(scores, dict) and scores:
        _vals = [float(v) for v in scores.values()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if _vals and max(_vals) <= 10:
            warn("盲评各维全部 ≤10，疑似误用 0-10 刻度（契约要求 0-100，"
                 "对齐上游 content.ts）——请人工核对 judge.json")
    conflicts = review["blocking_conflicts"]
    state["blind_review"] = {
        "judge_file": os.path.relpath(judge_path, rundir),
        "summary": review["summary"],
        "scores": scores,
        "overall": review.get("overall"),
        "blind_score": blind_score,
        "blind_score_source": blind_src,
        "strengths": review["strengths"],
        "weaknesses": review["weaknesses"],
        "patch_plan": review["patchPlan"],
        "blocking_conflicts": conflicts,
        "quality_target": state.get("quality_target"),
        "dispatch_card": state.get("dispatch_card"),
        "fulfil_latency_sec": latency,
        "archive": os.path.relpath(archive, rundir) if archive else None,
        "round": state["round"],
    }
    state.setdefault("blind_reviews", []).append(dict(state["blind_review"]))

    ok(f"盲评接收：综合 {blind_score}"
       f"（{'judge overall' if blind_src == 'overall' else '各维平均回退'}），"
       f"strengths {len(review['strengths'])} 条 / "
       f"weaknesses {len(review['weaknesses'])} 条 / "
       f"blocking_conflicts {len(conflicts) if isinstance(conflicts, list) else '?'} 条")

    if conflicts and state.get("conflicts_rework_done"):
        # 二次冲突：有界返工已用尽，诚实失败（不允许带病交付）。
        state["blind_review"]["verdict"] = "REWORK_EXHAUSTED"
        _save_state(rundir, state)
        warn("返工后盲评仍有 blocking_conflicts → FAIL（不允许带病交付）")
        _finish(rundir, state, state["raw"], state["mode"], "FAIL",
                time.perf_counter(), {"abort": "judge_conflict_twice"})
        return

    if conflicts:
        # 一次有界返工：冲突 + judge patchPlan 回灌，扩一轮配额。
        plan = _normalize_plan(conflicts) + _normalize_plan(review["patchPlan"])
        state["conflicts_rework_done"] = True
        state["rework_kind"] = "conflicts"
        state.pop("quality_feedback", None)  # 防残留泄漏进 conflicts 返工卡
        state["pending_plan"] = plan
        # 全局最大迭代次数硬上限：再开一轮会越界则直接交付历史最优轮并终止
        if state["round"] >= state.get("max_iterations", DEFAULT_MAX_ITERATIONS):
            _finalize_by_cap(rundir, state)
            return
        state["round"] = state["round"] + 1
        state["max_rounds"] = state["max_rounds"] + 1
        state["retries"] = 0
        state["phase"] = "AWAIT_FULFILL"
        _save_state(rundir, state)
        warn(f"judge 判定 {len(conflicts)} 条 blocking_conflicts → 有界返工"
             f"（第 {state['round']} 轮，配额 +1）")
        print(f"\n  派发返工 fulfil 子代理：--dispatch {rundir}"
              f"  → dispatch/round{state['round']}.md")
        print("  返工后收敛将再次进入盲评；二次冲突即 FAIL。")
        raise SystemExit(EXIT_FULFILL)

    # ---- 质量收敛层（v2 增强 #6）：盲评分作为迭代终止条件 -------------------
    # 两层收敛 = 结构层（无 conflicts / issues 清零）且 质量层（盲评达标）。
    # 达标判定双保险：overall ≥ 目标线 且 各维 ≥ 危险线 DIM_DANGER_LINE。
    target = state.get("quality_target")
    if target:
        low_dims = [k for k, v in (scores.items() if isinstance(scores, dict) else [])
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                    and v < DIM_DANGER_LINE]
        state["blind_review"]["low_dims"] = low_dims
        if blind_score is None or blind_score < target or low_dims:
            # 本次盲评已被 append 进 blind_reviews，[:-1] = 历史最优（不含本次）
            prev_reviews = state.get("blind_reviews", [])[:-1]
            prev_best = max((b.get("blind_score") or -1) for b in prev_reviews) \
                if prev_reviews else None
            count = state.get("quality_rework_count", 0)
            exhausted = count >= state.get("quality_rework_max",
                                           DEFAULT_QUALITY_REWORKS)
            no_gain = (prev_best is not None and count > 0
                       and (blind_score or 0) <= prev_best)
            if exhausted or no_gain:
                # 耗尽语义 = FAIL（与 conflicts 二次冲突对称，不允许带病交付）。
                reason = ("质量返工次数耗尽" if exhausted else
                          f"返工后盲评 {blind_score} 未超过历史最优 {prev_best}"
                          "（plateau 护栏：防噪声空转）")
                state["blind_review"]["verdict"] = "BELOW_TARGET"
                _save_state(rundir, state)
                warn(f"盲评 {blind_score} 未达质量目标 {target}"
                     f"（低维 {low_dims or '—'}）→ {reason} → FAIL（不允许带病交付）")
                if prev_best is not None and prev_best > 0:
                    print(f"  ⓘ 历史最优盲评 {prev_best} 分的产物已留档于 fulfil/，"
                          "报告「盲评质量分轨迹」标注了最优轮次，可人工取用。")
                _finish(rundir, state, state["raw"], state["mode"], "FAIL",
                        time.perf_counter(),
                        {"abort": "quality_below_target", "quality_target": target,
                         "blind_score": blind_score, "low_dims": low_dims,
                         "best_blind_score": prev_best})
                return
            # 有界质量返工：judge weaknesses + patchPlan 回灌，扩一轮配额。
            plan = _normalize_plan(review["patchPlan"])
            for d in low_dims:
                plan.insert(0, {"target": f"维度 {d} 低于危险线 {DIM_DANGER_LINE}",
                                "action": f"补强盲评维度「{d}」：对照 judge weaknesses"
                                          "逐条落实，保持其余结构与全部 {{var}} 不变"})
            state["quality_rework_count"] = count + 1
            state["quality_feedback"] = "；".join(review.get("weaknesses") or [])
            state["rework_kind"] = "quality"
            state["pending_plan"] = plan
            # 全局最大迭代次数硬上限：再开一轮会越界则直接交付历史最优轮并终止
            if state["round"] >= state.get("max_iterations", DEFAULT_MAX_ITERATIONS):
                _finalize_by_cap(rundir, state)
                return
            state["round"] = state["round"] + 1
            state["max_rounds"] = state["max_rounds"] + 1
            state["retries"] = 0
            state["phase"] = "AWAIT_FULFILL"
            _save_state(rundir, state)
            warn(f"盲评 {blind_score} 未达质量目标 {target} → 质量返工"
                 f"（第 {state['quality_rework_count']}/"
                 f"{state['quality_rework_max']} 次，配额 +1）")
            print(f"\n  派发质量返工 fulfil 子代理：--dispatch {rundir}"
                  f"  → dispatch/round{state['round']}.md")
            print("  返工后重走结构门禁并再次盲评；分数耗尽未达标或无改善即 FAIL。")
            raise SystemExit(EXIT_FULFILL)

    # 无冲突且质量达标：G10 PASS，进入终态。
    state["blind_review"]["verdict"] = "PASS"
    _save_state(rundir, state)
    _q = (f"，质量 {blind_score}≥{target}"
          if target else "")
    ok(f"盲评无 blocking_conflicts{_q} → G10 盲评 PASS")
    _finish(rundir, state, state["raw"], state["mode"], "PASS",
            time.perf_counter())


def _select_best_delivery(rundir: str, state: dict) -> str:
    """Plan A：交付「盲评分最高且无 blocking_conflicts」的那一轮 fulfil 文本。

    找不到合格盲评记录时退回 state["last_final"]（最后一轮产出），
    保证任何终态都有可交付文本。选中的轮次与文本同时写入 state，供报告标注。
    """
    best_text, best_score, best_round = None, -1, None
    for rev in state.get("blind_reviews", []):
        if rev.get("blocking_conflicts"):
            continue  # 有互斥/失效约束的轮次绝不交付
        sc = rev.get("blind_score")
        if sc is None or sc <= best_score:
            continue
        rnd = rev.get("round")
        fp = os.path.join(rundir, "fulfil", f"round{rnd}.txt")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            continue
        best_text, best_score, best_round = txt, sc, rnd
    delivered = best_text if best_text is not None else state.get("last_final", "")
    state["delivered_final"] = delivered
    state["delivery_selected_round"] = best_round
    return delivered


def _finalize_by_cap(rundir: str, state: dict) -> None:
    """达到全局最大迭代次数：强制终止，交付历史最优轮（Plan A）。"""
    _select_best_delivery(rundir, state)
    _save_state(rundir, state)
    cap = state.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    sel = state.get("delivery_selected_round")
    sel_score = None
    if sel is not None:
        for rev in state.get("blind_reviews", []):
            if rev.get("round") == sel:
                sel_score = rev.get("blind_score")
                break
    if sel is not None:
        warn(f"达到最大迭代次数 {cap}，强制终止 → 交付历史最优轮"
             f"（轮次 r{sel}，盲评 {sel_score} 分）")
    else:
        warn(f"达到最大迭代次数 {cap}，强制终止 → 无合格盲评记录，退回最后一轮")
    blind_expected = bool(state.get("blind_reviews"))
    _finish(rundir, state, state["raw"], state["mode"], "FAIL",
            time.perf_counter(),
            {"stop_reason": f"达到最大迭代次数 {cap}",
             "delivery_selected_round": sel},
            blind_expected=blind_expected)


def _finish(rundir: str, state: dict, raw: str, mode: str,
            expect: str, t: float, extra: dict | None = None,
            blind_expected: bool = True) -> None:
    """终态：跑全量 G1–G10 交付门禁（协议模式取证），写报告，给退出码。

    blind_expected=False（未收敛终止）时 G10 记 SKIP，避免与真实阻塞原因
    G4 并列造成归因噪音（修 2026-10-09 回归 BUG-05）。
    """
    blind = state.get("blind_review")
    # Plan A：交付盲评分最高且无硬冲突的那一轮（_select_best_delivery 会写
    # state["delivered_final"] / ["delivery_selected_round"]，last_final 原样保留归档）。
    delivered = _select_best_delivery(rundir, state)
    res = verify_run(raw, delivered, state["max_rounds"], mode,
                     protocol_history=state["history"], blind_review=blind,
                     blind_expected=blind_expected)
    for ch in res["checks"]:
        mark = "✓" if ch["status"] == "PASS" else ("○" if ch["status"] == "SKIP"
                                                   else "✗")
        print(f"          {mark} {ch['id']} {ch['name']:<12} {ch['detail']}")
    gate = res["gate"]
    path = _write_report(rundir, state, gate, {"delivery_gate": res, **(extra or {})})
    hdr("结论")
    _brs = state.get("blind_reviews") or \
        ([state["blind_review"]] if state.get("blind_review") else [])
    _bq = next((b.get("blind_score") for b in reversed(_brs)
                if b.get("blind_score") is not None), None)
    print(f"  盲评质量分：{_bq if _bq is not None else '—'}（LLM-as-judge · 0-100）"
          f"    剩余问题：{len(res.get('remaining_issues') or [])} 条    门禁：{gate}")
    _sel = state.get("delivery_selected_round")
    if _sel is not None:
        print(f"  ⓘ 实际交付轮次：r{_sel}（Plan A 选优：盲评分最高且无硬冲突；"
              f"最后一轮 r{state['round']} 仅作归档）")
    print(f"  报告：{path}")
    print(f"  全流程耗时 {time.perf_counter() - t:.2f}s\n")
    raise SystemExit(EXIT_OK if gate == "PASS" else EXIT_FAIL)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _legacy(args: list) -> None:
    raw, final = _resolve(args[0]), _resolve(args[1])
    rounds = int(args[2]) if len(args) > 2 else 2
    mode = args[3] if len(args) > 3 else "system"
    res = verify_run(raw, final, rounds, mode)
    print(f"\n交付门禁：{res['gate']}"
          f"（质量分以盲评为准，结构分仅内部衬底；失败项见下方逐项）")
    for c_ in res["checks"]:
        mark = "✓" if c_["status"] == "PASS" else ("○" if c_["status"] == "SKIP"
                                                   else "✗")
        print(f"  {mark} {c_['id']} {c_['name']:<14} {c_['detail']}")
    raise SystemExit(EXIT_OK if res["gate"] == "PASS" else EXIT_FAIL)


def _status(rundir: str) -> None:
    s = _load_state(rundir)
    hdr(f"状态 · runs/{os.path.basename(rundir)}")
    print(f"  phase={s['phase']}  round={s['round']}/{s['max_rounds']}  "
          f"retries={s['retries']}  template={s['template']}")
    print(f"  基线分 {s['baseline']['score']}，history {len(s['history'])} 轮")
    for h in s["history"]:
        print(f"    r{h['round']}: {h['score_before']} → {h['score_after']} "
              f"(delta={h['delta']})  剩余 issues {len(h['issues'])}")
    qt = s.get("quality_target")
    print(f"  质量门禁：target={qt if qt else '关闭'}"
          f"  质量返工 {s.get('quality_rework_count', 0)}"
          f"/{s.get('quality_rework_max', DEFAULT_QUALITY_REWORKS)}")
    br = s.get("blind_review")
    if br:
        print(f"  盲评：verdict={br.get('verdict')}  综合={br.get('blind_score')}"
              f"  conflicts={len(br.get('blocking_conflicts') or [])}"
              f"  低维={br.get('low_dims') or '—'}")
    raise SystemExit(EXIT_OK)


def main() -> None:
    args = sys.argv[1:]
    # 剥离可选 flag：--min-blind-score N（盲评质量目标，0=关闭）、
    # --quality-reworks N（质量返工次数上限）。剩余按位置参数处理。
    min_blind_score, quality_reworks = DEFAULT_MIN_BLIND_SCORE, DEFAULT_QUALITY_REWORKS
    max_iterations = DEFAULT_MAX_ITERATIONS
    flags = {}
    i = 0
    while i < len(args):
        if args[i] == "--min-blind-score" and i + 1 < len(args):
            flags["min_blind_score"] = int(args[i + 1])
            del args[i:i + 2]
        elif args[i] == "--quality-reworks" and i + 1 < len(args):
            flags["quality_reworks"] = int(args[i + 1])
            del args[i:i + 2]
        elif args[i] == "--max-iterations" and i + 1 < len(args):
            max_iterations = int(args[i + 1])
            del args[i:i + 2]
        else:
            i += 1
    min_blind_score = flags.get("min_blind_score", min_blind_score)
    quality_reworks = flags.get("quality_reworks", quality_reworks)
    if args and args[0] == "--resume":
        if len(args) < 2:
            print(__doc__)
            raise SystemExit(EXIT_USAGE)
        # 按 state.phase 分派：盲评裁决 / fulfil 校验。
        rd = _rundir(args[1])
        if _load_state(rd)["phase"] == "AWAIT_JUDGE":
            phase_judge(rd)
        else:
            phase_b(rd)
    elif args and args[0] == "--dispatch":
        if len(args) < 2:
            print(__doc__)
            raise SystemExit(EXIT_USAGE)
        _dispatch(_rundir(args[1]))
    elif args and args[0] == "--report":
        if len(args) < 2:
            print(__doc__)
            raise SystemExit(EXIT_USAGE)
        from report_gen import build  # 确定性渲染，零 LLM
        path = build(_rundir(args[1]))
        print(f"  ✓ 可视化报告：{path}")
        raise SystemExit(EXIT_OK)
    elif args and args[0] == "--status":
        if len(args) < 2:
            print(__doc__)
            raise SystemExit(EXIT_USAGE)
        _status(_rundir(args[1]))
    elif args and args[0] == "--legacy":
        if len(args) < 3:
            print(__doc__)
            raise SystemExit(EXIT_USAGE)
        _legacy(args[1:])
    elif args:
        raw_path = args[0]
        mode = args[1] if len(args) > 1 else "system"
        rounds = int(args[2]) if len(args) > 2 else 2
        scenario = args[3] if len(args) > 3 else "general"
        phase_a(raw_path, mode, rounds, scenario,
                min_blind_score=min_blind_score, quality_reworks=quality_reworks,
                max_iterations=max_iterations)
    else:
        print(__doc__)
        raise SystemExit(EXIT_USAGE)


if __name__ == "__main__":
    main()
