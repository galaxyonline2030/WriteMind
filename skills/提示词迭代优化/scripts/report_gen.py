"""可视化报告生成器（确定性，零 LLM）
=====================================

把一次 run 的全部留痕渲染成一份**零依赖、可双击打开**的 HTML 报告：

  1. 总览       —— 分数轨迹、门禁 G1–G10、关键指标、结论
  2. 每轮改进   —— 逐轮「改了什么（diff）+ 为何改（patchPlan）+ 改进评价」
  3. 盲评       —— 每次盲评的分数、优点、缺点、blocking_conflicts、修改指令
  4. 运行跟踪   —— 每轮跑的脚本命令、派发时刻、子代理任务卡全文、子代理产出全文
  5. 原始稿 / 终稿全文

判定全部由脚本算出（difflib + stubs 的确定性规则器），不含任何模型推断，
因此报告可复现、可审计——**报告本身不接受"看起来不错"这种理由**。

用法：
    python run_pipeline.py --report runs/<ts>
    python report_gen.py runs/<ts> [--out runs/<ts>/report.html]
"""

from __future__ import annotations

import difflib
import html
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import vague_hits_cn, vague_hits_en  # noqa: E402

W = 62


# ---------------------------------------------------------------------------
# 读取与工具
# ---------------------------------------------------------------------------
def _j(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _t(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def _dt(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 改进评价（确定性规则，不靠模型打分）
# ---------------------------------------------------------------------------
def evaluate_round(prev_out: str, curr_out: str,
                   prev_issues: list, curr_issues: list,
                   score_before: int, score_after: int,
                   gates: dict, retries: int) -> dict:
    """给一轮迭代下结论：有效改进 / 改进 / 部分改善 / 空转 / 引入新问题。

    定性口径 = 诊断问题清单的消长（确定性引擎），与上游一致：
    迭代质量由问题解决情况与盲评衡量，不使用结构分 delta。
    （score_before/score_after 仅作内部诊断入参，不参与定性。）
    """
    issue_delta = len(curr_issues or []) - len(prev_issues or [])
    solved = [i for i in (prev_issues or []) if i not in (curr_issues or [])]
    added = [i for i in (curr_issues or []) if i not in (prev_issues or [])]

    v_prev = set(vague_hits_cn(prev_out)) | set(vague_hits_en(prev_out))
    v_curr = set(vague_hits_cn(curr_out)) | set(vague_hits_en(curr_out))
    vague_killed = sorted(v_prev - v_curr)
    vague_new = sorted(v_curr - v_prev)

    if curr_issues == [] and solved:
        verdict, tone = "有效改进", "good"
    elif solved and not added:
        verdict, tone = "改进（仍有剩余问题）", "good"
    elif added and not solved:
        verdict, tone = "引入新问题", "bad"
    elif solved and added:
        verdict, tone = "部分改善", "warn"
    else:
        verdict, tone = "空转（幂等，未带来变化）", "warn"

    reasons = []
    if solved:
        reasons.append(f"消除 {len(solved)} 条问题")
    if added:
        reasons.append(f"新增 {len(added)} 条问题")
    if vague_killed:
        reasons.append("模糊词减少：" + "、".join(vague_killed))
    if vague_new:
        reasons.append("⚠ 模糊词增加：" + "、".join(vague_new))
    if added and vague_new:
        reasons.append("疑似主因：新增内容引入了模糊词（先查规则器假阳性再改稿）")
    if retries:
        reasons.append(f"本轮重派 {retries} 次后才通过门禁")
    gates_failed = [k for k, v in (gates or {}).items() if v != "PASS"]
    if gates_failed:
        reasons.append("门禁失败：" + "、".join(gates_failed))

    return {
        "verdict": verdict, "tone": tone, "delta": issue_delta,
        "solved": solved, "added": added,
        "vague_killed": vague_killed, "vague_new": vague_new,
        "reasons": reasons,
        "char_delta": len(curr_out) - len(prev_out),
        "line_delta": len(curr_out.splitlines()) - len(prev_out.splitlines()),
    }


def diff_html(before: str, after: str, limit: int = 400) -> str:
    """渲染两版提示词的差异（+ 增 / - 删），超长截断。"""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    lines = list(difflib.unified_diff(
        a, b, fromfile="改前", tofile="改后", lineterm="", n=1))
    if not lines:
        return '<p class="muted">两版内容完全一致（本轮未产生改动）。</p>'
    out, shown = [], 0
    for ln in lines:
        if ln.startswith(("---", "+++", "@@")):
            out.append(f'<div class="dmeta">{esc(ln)}</div>')
            continue
        cls = "dadd" if ln.startswith("+") else ("ddel" if ln.startswith("-") else "dctx")
        if cls != "dctx":
            shown += 1
        if shown > limit:
            out.append('<div class="dmeta">… 差异过多，已截断</div>')
            break
        out.append(f'<div class="{cls}">{esc(ln)}</div>')
    return "".join(out)


# ---------------------------------------------------------------------------
# 片段渲染
# ---------------------------------------------------------------------------
def _bar(v, vmax=10, color="#3b82f6") -> str:
    w = 0 if not v else max(3, int(round(float(v) / vmax * 100)))
    return (f'<span class="bar"><i style="width:{w}%;background:{color}"></i>'
            f'</span><b>{v}</b>')


def _gate_badge(status: str) -> str:
    cls = {"PASS": "ok", "FAIL": "bad"}.get(status, "skip")
    return f'<span class="badge {cls}">{esc(status)}</span>'


def _tone(t: str) -> str:
    return {"good": "ok", "warn": "warn", "bad": "bad"}.get(t, "skip")


def _score_chart(points: list) -> str:
    """分数轨迹 SVG（浅色主题）。"""
    if len(points) < 2:
        return ""
    vals = [p[1] for p in points]
    lo, hi = min(vals + [0]), max(vals + [100])
    span = max(1, hi - lo)
    W_, H_ = 620, 150
    pad = 26
    def xy(i, v):
        x = pad + i * (W_ - 2 * pad) / (len(points) - 1)
        y = H_ - pad - (v - lo) / span * (H_ - 2 * pad)
        return x, y
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                   (xy(i, v) for i, v in enumerate(vals)))
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#3b82f6"/>'
        f'<text x="{x:.1f}" y="{y - 9:.1f}" class="svgt">{val}</text>'
        for (x, y, val) in ((*xy(i, v), v) for i, v in enumerate(vals)))
    return (f'<svg viewBox="0 0 {W_} {H_}" class="chart" role="img">'
            f'<polyline points="{pts}" fill="none" stroke="#3b82f6" '
            f'stroke-width="2.5"/>{dots}</svg>')


def _cmds(rundir: str, rnd: int, raw_path: str, mode: str,
          max_rounds: int, scenario: str) -> list:
    """重建每一轮实际执行的命令序列（可复制复跑）。"""
    base = f"python run_pipeline.py --resume {rundir}"
    if rnd == 1:
        return [
            f'python run_pipeline.py "@{raw_path}" {mode} {max_rounds} {scenario}',
            f"python run_pipeline.py --dispatch {rundir}",
            "# ← 用 Agent 工具 spawn 独立子代理，把 dispatch/round1.md 原文交给它",
            base,
        ]
    return [
        f"python run_pipeline.py --dispatch {rundir}",
        "# ← 用 Agent 工具 spawn 独立子代理（iterate_directed 定向迭代）",
        base,
    ]


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------
def build(rundir: str, out_path: str | None = None) -> str:
    state = _j(os.path.join(rundir, "state.json"))
    if not state:
        raise SystemExit(f"找不到 state.json：{rundir}")
    rep = _j(os.path.join(rundir, "report.json"), {}) or {}
    hist = state.get("history", [])
    blinds = state.get("blind_reviews") or []
    if not blinds and state.get("blind_review"):
        blinds = [state["blind_review"]]
    # 修 2026-10-10 既有瑕疵：verdict/low_dims 写入发生在 blind_reviews append
    # 之后（浅拷贝快照不回传），报告读到的 verdict 恒为空。从最新的
    # state["blind_review"] 回填最后一条（两者总是同一轮盲评）。
    _last_br = state.get("blind_review") or {}
    if blinds and _last_br.get("verdict") and not blinds[-1].get("verdict"):
        blinds[-1]["verdict"] = _last_br["verdict"]
        if _last_br.get("low_dims") and not blinds[-1].get("low_dims"):
            blinds[-1]["low_dims"] = _last_br["low_dims"]

    base_issues = state["baseline"].get("issues") or []
    final_issues = hist[-1]["issues"] if hist else base_issues

    # 盲评质量分（LLM-as-judge，对齐上游 evaluation 机制）：取最近一次盲评的综合分。
    # 这是全系统对外的唯一分数——与上游一致（上游唯一评分机制即 LLM 评审
    # 0-100 + overall）。结构要素分只是确定性 issues 引擎的内部衬底，
    # 上游无对应机制，不作为分数呈现（2026-10-10 一致性改造）。
    blind_final, blind_verdict = None, None
    for br in reversed(blinds):
        if br.get("blind_score") is not None:
            blind_final = br["blind_score"]
            blind_verdict = br.get("verdict", "—")
            break
    blind_src = (blinds[-1].get("blind_score_source") if blinds else None) \
        if blinds else None

    gate = rep.get("gate", "—")
    checks = (rep.get("delivery_gate") or {}).get("checks", [])

    blind_chip = [(f"盲评{i}", br.get("blind_score"))
                  for i, br in enumerate(blinds, 1)
                  if br.get("blind_score") is not None]

    # ---- 每轮改进卡 ----
    round_cards = []
    for idx, h in enumerate(hist):
        prev_out = h["input"]
        curr_out = h["output"]
        prev_issues = state["baseline"]["issues"] if idx == 0 else hist[idx - 1]["issues"]
        ev = evaluate_round(prev_out, curr_out, prev_issues, h["issues"],
                            h["score_before"], h["score_after"],
                            h.get("gates", {}), h.get("retries", 0))
        plan = h.get("patch_plan") or []
        card_file = os.path.join(rundir, "dispatch", f"round{h['round']}.md")
        out_file = os.path.join(rundir, "fulfil", f"round{h['round']}.txt")
        card_txt = _t(card_file)
        out_txt = _t(out_file)

        plan_html = "".join(
            f'<li><code>{esc(p.get("target", ""))}</code>'
            f'<div class="act">{esc(p.get("action", ""))}</div></li>'
            for p in plan) or '<li class="muted">本轮指令未留档（历史运行）</li>'

        solved_html = "".join(f'<li class="okli">{esc(s)}</li>' for s in ev["solved"]) \
            or '<li class="muted">无</li>'
        added_html = "".join(f'<li class="badli">{esc(s)}</li>' for s in ev["added"]) \
            or '<li class="muted">无</li>'
        reasons_html = "".join(f"<li>{esc(r)}</li>" for r in ev["reasons"]) \
            or '<li class="muted">问题清单无变化</li>'

        lat = h.get("fulfil_latency_sec")
        lat_html = (f'{lat}s' if lat is not None else "—")
        lat_note = ""
        if lat is not None:
            lat_note = ('<span class="ok">子代理执行</span>' if lat > 1
                        else '<span class="warn">⚠ 间隔过短，疑似非子代理执行</span>')

        round_cards.append(f"""
<section class="card" id="r{h['round']}">
  <h3>第 {h['round']} 轮 · {esc(h.get('seam',''))}
      <span class="badge {_tone(ev['tone'])}">{esc(ev['verdict'])}</span></h3>
  <div class="kv">
    <span>问题 <b>{len(prev_issues)}</b> → <b>{len(h['issues'])}</b> 条
      （{'+' if ev['delta'] >= 0 else ''}{ev['delta']}）</span>
    <span>字符 {len(curr_out)}（{'+' if ev['char_delta'] >= 0 else ''}{ev['char_delta']}）</span>
    <span>剩余问题 {len(h['issues'])}</span>
    <span>门禁 G7 {_gate_badge(h.get('gates',{}).get('G7','—'))}
          G9 {_gate_badge(h.get('gates',{}).get('G9','—'))}</span>
    <span>任务卡→产出 间隔 {lat_html} {lat_note}</span>
  </div>
  <div class="two">
    <div><h4>本轮要落实的指令（patchPlan）</h4><ol class="plan">{plan_html}</ol></div>
    <div><h4>改进评价</h4>
      <ul class="reasons">{reasons_html}</ul>
      <h4>问题变化</h4>
      <div class="two">
        <div><b class="okli">已消除</b><ul>{solved_html}</ul></div>
        <div><b class="badli">新增</b><ul>{added_html}</ul></div>
      </div>
    </div>
  </div>
  <details><summary>改动明细（改前 → 改后 diff）</summary>
    <pre class="diff">{diff_html(prev_out, curr_out)}</pre></details>
  <details><summary>运行跟踪 · 脚本命令</summary>
    <pre class="cmd">{esc(chr(10).join(_cmds(rundir, h['round'], state.get('raw_path',''),
                                        state.get('mode','system'),
                                        state.get('max_rounds',2),
                                        state.get('scenario','general'))))}</pre></details>
  <details><summary>运行跟踪 · 交给子代理的任务卡（{esc(h.get('dispatch_card') or f"dispatch/round{h['round']}.md")}，{len(card_txt)} 字符）</summary>
    <pre class="raw">{esc(card_txt) or '（未留档）'}</pre></details>
  <details><summary>运行跟踪 · 子代理产出（fulfil/round{h['round']}.txt，{len(out_txt)} 字符）</summary>
    <pre class="raw">{esc(out_txt) or '（未留档）'}</pre></details>
</section>""")

    # ---- 盲评卡 ----
    blind_cards = []
    for i, br in enumerate(blinds, 1):
        sc = br.get("scores") or {}
        sc_html = "".join(
            f'<div class="scrow"><span>{esc(k)}</span>{_bar(v)}</div>'
            for k, v in sc.items() if isinstance(v, (int, float)))
        st = "".join(f"<li>{esc(x)}</li>" for x in (br.get("strengths") or []))
        wk = "".join(f"<li>{esc(x)}</li>" for x in (br.get("weaknesses") or []))
        cf = br.get("blocking_conflicts") or []
        cf_html = "".join(f'<li class="badli">{esc(c)}</li>' for c in cf) \
            or '<li class="okli">无阻塞冲突（verdict=PASS）</li>'
        pp = br.get("patch_plan") or []
        pp_html = "".join(
            f'<li><code>{esc(p.get("target","") if isinstance(p, dict) else "指令")}</code>'
            f'<div class="act">{esc(p.get("action", p) if isinstance(p, dict) else p)}</div></li>'
            for p in pp) or '<li class="muted">无</li>'
        verdict = br.get("verdict", "—")
        cls = "ok" if verdict == "PASS" else "bad"
        blind_cards.append(f"""
<section class="card" id="j{i}">
  <h3>第 {i} 次盲评 · 轮次 r{br.get('round','—')}
      <span class="badge {cls}">{esc(verdict)}</span>
      <span class="badge {'ok' if not cf else 'bad'}">冲突 {len(cf)} 条</span></h3>
  <div class="kv"><span>综合分 <b>{br.get('blind_score','—')}</b>
    <span class="muted">（{'judge overall' if br.get('blind_score_source') == 'overall' else ('各维平均回退' if br.get('blind_score_source') else '口径未记录，按均值展示')}）</span></span>
    <span>产出文件 {esc(br.get('judge_file') or '—')}</span>
    <span>归档 {esc(br.get('archive') or '—')}</span>
    <span>卡→产出 间隔 {br.get('fulfil_latency_sec','—')}s</span></div>
  <div class="two">
    <div><h4>评分</h4>{sc_html or '<p class="muted">—</p>'}</div>
    <div><h4>blocking_conflicts（不返工不能交付）</h4><ul>{cf_html}</ul></div>
  </div>
  <div class="two">
    <div><h4>优点 {len(br.get('strengths') or [])} 条</h4><ul>{st or '<li class="muted">—</li>'}</ul></div>
    <div><h4>缺点 {len(br.get('weaknesses') or [])} 条</h4><ul>{wk or '<li class="muted">—</li>'}</ul></div>
  </div>
  <details><summary>盲评给出的修改指令（patchPlan，{len(pp)} 条）</summary>
    <ol class="plan">{pp_html}</ol></details>
  <p class="sum">{esc(br.get('summary',''))}</p>
</section>""")

    # ---- 门禁表 ----
    gate_rows = "".join(
        f'<tr><td><code>{esc(c.get("id"))}</code></td><td>{esc(c.get("name"))}</td>'
        f'<td>{_gate_badge(c.get("status"))}</td><td>{esc(c.get("detail"))}</td></tr>'
        for c in checks) or '<tr><td colspan="4" class="muted">未跑全量门禁</td></tr>'

    blind_traj = "".join(
        f'<span class="chip" style="background:#dcfce7">{esc(n)} · '
        f'<b>{v}</b>（质量分）</span>' for n, v in blind_chip) \
        or '<span class="muted">暂无盲评记录（盲评在收敛后进行）</span>'
    # 质量收敛层标注：目标线 / 危险线 / 历史最优轮（数据来自 state.quality_target
    # 与逐次 blind_reviews，全部脚本留档，无手工填写）
    qt = state.get("quality_target")
    _scores_all = [b.get("blind_score") for b in blinds
                   if b.get("blind_score") is not None]
    if _scores_all:
        _best_i = max(range(len(_scores_all)), key=lambda i: _scores_all[i])
        blind_traj += (
            f'<div class="sub" style="margin-top:6px">'
            + (f'质量目标 <b>{qt}</b> · ' if qt else '')
            + f'危险线（单维）{60} · '
            + f'历史最优 <b>{_scores_all[_best_i]}</b>'
            + f'（盲评{_best_i + 1}，产物见其对应 fulfil/roundN.txt）'
            + (f' · <b style="color:{"var(--ok)" if blind_final is not None and qt and blind_final >= qt else "var(--bad)"}">'
               f'{"达标" if blind_final is not None and qt and blind_final >= qt else "未达标"}</b>'
            if qt else '')
            + '</div>')

    raw_txt = _t(os.path.join(rundir, "raw.md")) or state.get("raw", "")
    final_txt = (state.get("delivered_final")
                 or state.get("last_final", "")
                 or (hist[-1]["output"] if hist else ""))

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>提示词优化报告 · {esc(os.path.basename(rundir))}</title>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--ink:#1f2328;--mut:#6b7280;--line:#e5e7eb;
--ok:#15803d;--okbg:#dcfce7;--bad:#b91c1c;--badbg:#fee2e2;--warn:#a16207;
--warnbg:#fef3c7;--acc:#3b82f6;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px 20px 80px}}
h1{{font-size:22px;margin:0 0 4px}}
h2{{font-size:17px;margin:34px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--line)}}
h3{{font-size:15px;margin:0 0 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
h4{{font-size:13px;margin:12px 0 6px;color:var(--mut)}}
.sub{{color:var(--mut);font-size:13px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:14px 0}}
.hero{{display:flex;gap:28px;flex-wrap:wrap;align-items:center}}
.big{{font-size:34px;font-weight:700;letter-spacing:-.5px}}
.muted{{color:var(--mut)}}
.badge{{font-size:12px;padding:2px 9px;border-radius:999px;font-weight:600;
background:#eef2f7;color:#374151}}
.badge.ok{{background:var(--okbg);color:var(--ok)}}
.badge.bad{{background:var(--badbg);color:var(--bad)}}
.badge.warn{{background:var(--warnbg);color:var(--warn)}}
.badge.skip{{background:#eef2f7;color:#6b7280}}
.kv{{display:flex;gap:16px;flex-wrap:wrap;color:var(--mut);font-size:13px;
margin:8px 0 4px}}
.kv b{{color:var(--ink)}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:820px){{.two{{grid-template-columns:1fr}}}}
ul,ol{{margin:4px 0;padding-left:20px}}
li{{margin:3px 0}}
.okli{{color:var(--ok)}} .badli{{color:var(--bad)}}
.plan li{{margin-bottom:8px}}
.plan code{{background:#f3f4f6;padding:1px 6px;border-radius:4px;font-size:12px}}
.act{{color:#374151;font-size:13px}}
.reasons li{{color:#374151}}
details{{margin:10px 0;border:1px solid var(--line);border-radius:8px;
background:#fcfcfd}}
summary{{cursor:pointer;padding:8px 12px;font-size:13px;color:var(--acc)}}
pre{{margin:0;padding:12px;overflow:auto;max-height:420px;font-size:12px;
line-height:1.6;background:#fff;border-top:1px solid var(--line)}}
pre.cmd{{background:#0f172a;color:#e2e8f0}}
pre.raw{{white-space:pre-wrap;word-break:break-word}}
.diff{{white-space:pre-wrap;word-break:break-word}}
.dadd{{background:var(--okbg)}} .ddel{{background:var(--badbg)}}
.dctx{{color:var(--mut)}} .dmeta{{color:#8b5cf6;font-size:11px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td,th{{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left}}
th{{color:var(--mut);font-weight:600}}
.chip{{display:inline-block;background:#eef2f7;border-radius:6px;
padding:3px 10px;margin:3px 6px 3px 0;font-size:12px;color:#374151}}
.chart{{width:100%;max-width:640px;background:#fff;border:1px solid var(--line);
border-radius:8px}}
.svgt{{font-size:10px;fill:#374151;text-anchor:middle}}
.bar{{display:inline-block;width:120px;height:8px;background:#eef2f7;
border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:8px}}
.bar i{{display:block;height:100%}}
.scrow{{display:flex;align-items:center;gap:4px;font-size:13px}}
.scrow span:first-child{{width:150px;color:var(--mut)}}
.sum{{background:#f9fafb;border-left:3px solid var(--acc);padding:10px 12px;
font-size:13px;color:#374151;border-radius:0 6px 6px 0}}
footer{{color:var(--mut);font-size:12px;margin-top:40px;text-align:center}}
</style></head><body><div class="wrap">

<h1>提示词优化 · 全程可视化报告</h1>
<div class="sub">运行目录 <code>{esc(rundir)}</code> · 创建 {esc(state.get('created_at','—'))}
 · mode={esc(state.get('mode'))} / scenario={esc(state.get('scenario'))}
 · 模板 <code>{esc(state.get('template'))}</code></div>

<div class="card hero">
  <div><div class="sub">盲评质量分（LLM-as-judge · 0-100）</div>
    <div class="big">{'—' if blind_final is None else blind_final}</div>
    <div class="sub">verdict {esc(blind_verdict or '—')}
      · {'judge overall' if blind_src == 'overall' else ('各维平均回退' if blind_src else '—')}
      · 共 {len(blinds)} 次盲评
      · {'质量目标 ' + str(qt) if qt else '质量门禁未启用'}</div></div>
  <div><div class="sub">诊断问题数（确定性引擎 · 收敛=清零）</div>
    <div class="big">{len(base_issues)} <span class="muted">→</span> {len(final_issues)}</div>
    <div class="sub">问题清单消长是迭代定性口径；质量结论以盲评为准</div></div>
  <div>{_score_chart(blind_chip)}</div>
  <div><div class="sub">交付门禁</div>
    <div class="big"><span class="badge {'ok' if gate == 'PASS' else 'bad'}">{esc(gate)}</span></div>
    <div class="sub">{len(checks)} 项检查</div></div>
</div>

<div class="card"><h3>盲评质量分轨迹<span class="sub">（LLM-as-judge · 0-100，
  对齐上游唯一评分机制 evaluation/content.ts）</span></h3>{blind_traj}</div>

<h2>一、交付门禁 G1–G10</h2>
<div class="card"><table>
<tr><th>门禁</th><th>名称</th><th>状态</th><th>说明</th></tr>
{gate_rows}</table></div>

<h2>二、每轮改进点与改进评价</h2>
{''.join(round_cards) or '<div class="card muted">无迭代记录</div>'}

<h2>三、独立盲评（无上下文 judge 子代理）</h2>
{''.join(blind_cards) or '<div class="card muted">未跑盲评</div>'}
{'' if state.get('blind_reviews') else
 '<div class="card muted">注：本运行早于 judge 归档功能，逐次盲评结论已被覆盖，'
 '此处仅保留最后一次。自 v2.2 起每次盲评均归档为 fulfil/judge_r&lt;N&gt;.json。</div>'}

<h2>四、原文与终稿</h2>
<div class="card">
  <details><summary>原始稿（{len(raw_txt)} 字符）</summary>
    <pre class="raw">{esc(raw_txt)}</pre></details>
  <details open><summary>终稿（{len(final_txt)} 字符）</summary>
    <pre class="raw">{esc(final_txt)}</pre></details>
</div>

<footer>由 scripts/report_gen.py 确定性生成 · 门禁/诊断来自脚本留痕，无模型推断；
质量分（盲评）来自独立 judge 子代理留痕（对齐上游 LLM-as-judge 机制）</footer>
</div></body></html>"""

    out = out_path or os.path.join(rundir, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out


def main() -> None:
    args = sys.argv[1:]
    out = None
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1]
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    p = build(args[0], out)
    print(f"  ✓ 可视化报告已生成：{p}（{os.path.getsize(p)} 字节）")


if __name__ == "__main__":
    main()
