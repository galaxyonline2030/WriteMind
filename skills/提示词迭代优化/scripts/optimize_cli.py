"""优化流命令行入口 v2（确定性 + 生成式接缝探测）
================================================

确定性命令（零 LLM，可直接跑）：
  python optimize_cli.py analyze   "@prompt.txt"                 # 诊断（含 patchPlan）
  python optimize_cli.py patchplan "@prompt.txt"                 # issues → 改写指令
  python optimize_cli.py lint      "@prompt.txt"                 # G7 自洽性检查
  python optimize_cli.py vars      "@prompt.txt"                 # {{var}} 正则抽取
  python optimize_cli.py evaluate  "@prompt.txt"                 # 打分 0-100
  python optimize_cli.py compare   "@before.txt" "@after.txt"    # 前后对比

生成式接缝命令（agent-first：无 realizer 时抛 AgentRealizationRequired）：
  python optimize_cli.py optimize  "@prompt.txt" [mode]          # stub 冒烟用（v1 兼容）
  python optimize_cli.py informed  "@prompt.txt" "@plan.json"    # informed 改写接缝
  python optimize_cli.py iterate   "@last.txt" "@requirement.txt" # 定向迭代接缝
  python optimize_cli.py redteam   "@prompt.txt"                 # 红队接缝
  python optimize_cli.py pipeline  "@prompt.txt" [rounds] [mode] # v1 stub 管线（冒烟）

约定：参数以 @ 开头表示从文件读取（适合长文本）；直接传字符串也可。
"""

from __future__ import annotations

import json
import sys

import contract as _contract
from optimizer_adapter import (
    analyze,
    compare,
    evaluate,
    informed_rewrite,
    iterate_directed,
    optimize,
    optimize_with_evaluation,
    redteam_review,
    render_informed_rewrite_messages,
    render_iterate_messages,
    render_redteam_messages,
)
from stubs import (
    analyze_stub,
    build_patch_plan,
    compare_stub,
    consistency_lint,
    extract_variables,
    rewrite_stub,
    score_stub,
)
from virtual_llm_client import AgentRealizationRequired, VirtualLLMClient


def _resolve(arg: str) -> str:
    if arg.startswith("@") and len(arg) > 1:
        with open(arg[1:], encoding="utf-8") as f:
            return f.read()
    return arg


def _client(*, with_rewrite_stub: bool = False) -> VirtualLLMClient:
    c = VirtualLLMClient(provider="prompt-optimizer-cli")
    c.register_deterministic("analysis", analyze_stub)
    c.register_deterministic("score", score_stub)
    c.register_deterministic("compare", compare_stub)
    if with_rewrite_stub:
        c.register_deterministic("optimized_prompt", rewrite_stub)  # v1 兼容/冒烟
    return c


def _dump(result) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)


def _seam_attempt(client: VirtualLLMClient, messages, shape, hint: str, do_call) -> None:
    """生成式接缝探测：无 realizer 时给出协议指引（退出码 10 = 交接信号）。"""
    try:
        result = do_call(client)
        print(_dump(result))
    except AgentRealizationRequired as e:
        print(f"[接缝 {e.shape.name}] 需要 fulfil（无确定性替身，脚本不产内容）。")
        print("  协议：python run_pipeline.py \"@raw.txt\" 启动 Phase A，")
        print("  按提示把代理产出写入 fulfil 文件后 --resume。")
        print(f"  （{hint}）")
        raise SystemExit(10)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)

    cmd = sys.argv[1]
    a1 = _resolve(sys.argv[2])

    if cmd == "analyze":
        c = _client()
        r = analyze(c, a1)
        r["patch_plan"] = build_patch_plan(r["issues"], r["suggestions"])
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "patchplan":
        c = _client()
        r = analyze(c, a1)
        print(json.dumps(build_patch_plan(r["issues"], r["suggestions"]),
                         ensure_ascii=False, indent=2))
    elif cmd == "lint":
        conflicts = consistency_lint(a1)
        print("PASS：无互斥约束" if not conflicts
              else "FAIL：\n  - " + "\n  - ".join(conflicts))
        raise SystemExit(0 if not conflicts else 1)
    elif cmd == "vars":
        print(json.dumps(extract_variables(a1), ensure_ascii=False))
    elif cmd == "evaluate":
        print(evaluate(_client(), a1))
    elif cmd == "compare":
        if len(sys.argv) < 4:
            print("compare 需要 before 与 after 两个参数", file=sys.stderr)
            raise SystemExit(2)
        print(json.dumps(compare(_client(), a1, _resolve(sys.argv[3])),
                         ensure_ascii=False, indent=2))
    elif cmd == "optimize":  # v1 兼容：stub 冒烟，勿当交付
        mode = sys.argv[3] if len(sys.argv) > 3 else "system"
        print(optimize(_client(with_rewrite_stub=True), a1, mode))
    elif cmd == "pipeline":  # v1 兼容：stub 冒烟
        rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 2
        mode = sys.argv[4] if len(sys.argv) > 4 else "system"
        report = optimize_with_evaluation(_client(with_rewrite_stub=True), a1, mode, rounds)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif cmd == "informed":
        plan = json.loads(_resolve(sys.argv[3])) if len(sys.argv) > 3 else []

        def call(c: VirtualLLMClient):
            return informed_rewrite(c, a1, plan)

        _seam_attempt(_client(), render_informed_rewrite_messages(a1, plan),
                      _contract.SHAPE_REWRITE_INFORMED,
                      "informed 改写需要 patchPlan，配合 run_pipeline 三段式协议使用", call)
    elif cmd == "iterate":
        if len(sys.argv) < 4:
            print("iterate 需要 last 与 requirement 两个参数", file=sys.stderr)
            raise SystemExit(2)
        req = _resolve(sys.argv[3])

        def call(c: VirtualLLMClient):
            return iterate_directed(c, a1, req)

        _seam_attempt(_client(), render_iterate_messages(a1, req),
                      _contract.SHAPE_ITERATE_DIRECTED,
                      "定向迭代由 run_pipeline --resume 在第 2 轮起自动派发", call)
    elif cmd == "redteam":
        def call(c: VirtualLLMClient):
            return redteam_review(c, a1)

        _seam_attempt(_client(), render_redteam_messages(a1),
                      _contract.SHAPE_REDTEAM_REVIEW,
                      "红队审稿也可由代理直接按 templates/redteam_review.md 执行", call)
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
