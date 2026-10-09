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
  python run_pipeline.py "@raw.txt" [system|user] [rounds] [scenario]
      Phase A：诊断 → 基线分 → patchPlan → 写 runs/<ts>/state.json
      退出码 10 = AWAIT_FULFILL

  python run_pipeline.py --dispatch runs/<ts>
      按 state.phase 渲染子代理任务卡：
        AWAIT_FULFILL → dispatch/roundN.md（fulfil 子代理）
        AWAIT_JUDGE   → dispatch/judge.md（盲评 judge 子代理）
      主代理用 Agent 工具把它原样交给子代理执行。退出码 10。

  python run_pipeline.py --resume runs/<ts>
      按 state.phase 分派：
        AWAIT_FULFILL → Phase B：读 fulfil/roundN.txt → 接缝+契约 →
                        G7/G8/G9 → evaluate → 收敛判定
        AWAIT_JUDGE   → 读 fulfil/judge.json（盲评结论）→ 无冲突 →
                        G10 PASS 收尾；有冲突 → 一次有界返工回灌
      退出码：10 = 交接（fulfil / judge / 返工）；0 = PASS；1 = FAIL

  python run_pipeline.py --legacy "@raw.txt" "@final.md" [rounds] [mode]
      旧式一步到位门禁（兼容 false_positive_demo 回归；G10 记 SKIP）。

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
    info_retention_missing,
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
        "final": state.get("last_final", ""),
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
def phase_a(raw_path: str, mode: str, rounds: int, scenario: str) -> None:
    raw = _resolve(raw_path)
    c = _client("phase-a")

    hdr(f"Phase A · 诊断与改写指令   模式={mode}   上限={rounds} 轮   场景={scenario}")
    print(f"  原始：{raw_path}（{len(raw)} 字符）")

    t = time.perf_counter()
    a_raw = analyze(c, raw)
    for iss in a_raw["issues"]:
        print(f"          · {iss}")
    ok(f"基线分 {a_raw['score']}/100，{len(a_raw['issues'])} 条问题")

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
    missing_info = info_retention_missing(raw, final)
    missing_vars = missing_variables(raw, final)
    gate_round = not (lint or missing_info or missing_vars)
    if lint:
        for x in lint:
            warn(f"G7 {x}")
    if missing_info:
        for x in missing_info:
            warn(f"G8 {x}")
    if missing_vars:
        warn(f"G9 缺失变量：{missing_vars}")
    if gate_round:
        ok("G7 自洽 / G8 信息保持 / G9 变量完整 全部通过")
    print(f"        （耗时 {time.perf_counter() - t:.3f}s）")

    if not gate_round:
        state["retries"] += 1
        feedback = "；".join(lint + missing_info +
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
                  "G8": "PASS" if not missing_info else "FAIL",
                  "G9": "PASS" if not missing_vars else "FAIL"},
    })
    ok(f"本轮 {score_before} → {score_after}（{'+' if delta >= 0 else ''}{delta}），"
       f"剩余 issues {len(a_fin['issues'])} 条")

    if a_fin["issues"]:
        for iss in a_fin["issues"]:
            print(f"          · {iss}")

    converged = not a_fin["issues"]
    plateau = (len(state["history"]) >= 2
               and state["history"][-1]["delta"] < EPS
               and state["history"][-2]["delta"] < EPS)
    out_of_rounds = rnd >= state["max_rounds"]

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
        feedback = ""
        if fb and state.get("judge_retries", 0) > 0:
            feedback = (
                "\n## ⚠ 上一版 judge.json 被契约校验拒绝（第 "
                f"{state['judge_retries']}/{JUDGE_RETRIES} 次重派）——必须修掉\n\n"
                f"```text\n{fb}\n```\n\n"
                "常见原因：字符串值里出现非法转义（如反引号前加反斜杠）、JSON 外"
                "包裹了说明文字。文件首字符必须是 `{`，且不要写任何反斜杠。\n"
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
    blind_score = None
    if isinstance(scores, dict) and scores:
        vals = [float(v) for v in scores.values()
                if isinstance(v, (int, float))]
        if vals:
            blind_score = round(sum(vals) / len(vals), 1)
    conflicts = review["blocking_conflicts"]
    state["blind_review"] = {
        "judge_file": os.path.relpath(judge_path, rundir),
        "summary": review["summary"],
        "scores": scores,
        "blind_score": blind_score,
        "strengths": review["strengths"],
        "weaknesses": review["weaknesses"],
        "patch_plan": review["patchPlan"],
        "blocking_conflicts": conflicts,
        "dispatch_card": state.get("dispatch_card"),
        "fulfil_latency_sec": latency,
        "archive": os.path.relpath(archive, rundir) if archive else None,
        "round": state["round"],
    }
    state.setdefault("blind_reviews", []).append(dict(state["blind_review"]))

    ok(f"盲评接收：综合 {blind_score}，strengths {len(review['strengths'])} 条 / "
       f"weaknesses {len(review['weaknesses'])} 条 / "
       f"blocking_conflicts {len(conflicts) if isinstance(conflicts, list) else '?'} 条")

    if conflicts and state.get("rework_done"):
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
        state["rework_done"] = True
        state["pending_plan"] = plan
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

    # 无冲突：G10 PASS，进入终态。
    state["blind_review"]["verdict"] = "PASS"
    _save_state(rundir, state)
    ok("盲评无 blocking_conflicts → G10 盲评独立性 PASS")
    _finish(rundir, state, state["raw"], state["mode"], "PASS",
            time.perf_counter())


def _finish(rundir: str, state: dict, raw: str, mode: str,
            expect: str, t: float, extra: dict | None = None,
            blind_expected: bool = True) -> None:
    """终态：跑全量 G1–G10 交付门禁（协议模式取证），写报告，给退出码。

    blind_expected=False（未收敛终止）时 G10 记 SKIP，避免与真实阻塞原因
    G4 并列造成归因噪音（修 2026-10-09 回归 BUG-05）。
    """
    blind = state.get("blind_review")
    res = verify_run(raw, state["last_final"], state["max_rounds"], mode,
                     protocol_history=state["history"], blind_review=blind,
                     blind_expected=blind_expected)
    for ch in res["checks"]:
        mark = "✓" if ch["status"] == "PASS" else ("○" if ch["status"] == "SKIP"
                                                   else "✗")
        print(f"          {mark} {ch['id']} {ch['name']:<12} {ch['detail']}")
    gate = res["gate"]
    path = _write_report(rundir, state, gate, {"delivery_gate": res, **(extra or {})})
    hdr("结论")
    print(f"  {res['score_before']} → {res['score_final']}  "
          f"(delta={res['delta']})    门禁：{gate}")
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
    print(f"\n交付门禁：{res['gate']}   （{res['score_before']} → {res['score_final']}，"
          f"delta={res['delta']}）")
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
    br = s.get("blind_review")
    if br:
        print(f"  盲评：verdict={br.get('verdict')}  综合={br.get('blind_score')}"
              f"  conflicts={len(br.get('blocking_conflicts') or [])}")
    raise SystemExit(EXIT_OK)


def main() -> None:
    args = sys.argv[1:]
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
        phase_a(raw_path, mode, rounds, scenario)
    else:
        print(__doc__)
        raise SystemExit(EXIT_USAGE)


if __name__ == "__main__":
    main()
