"""确定性衬底（Deterministic Stubs，零 LLM）
=======================================

prompt-optimizer 优化流里每个 LLM 接缝的「规则化替身」。
它们不调用任何模型，只用正则/规则把 messages 映射成对应形状的结果，
从而让整条优化链在「完全不依赖外部 LLM / SDK client」的前提下端到端跑通。

约定（与 optimizer_adapter 的模板渲染对齐）：
- 优化 / 分析 / 评估类的 user 消息里，原始提示词被包在  <<ORIGINAL>> ... <<END>>  之间
- 对比类的 user 消息里，前后提示词分别包在  <<BEFORE>> ... <<END>>  和  <<AFTER>> ... <<END>>

这些替身用于「证明路径成立」，不是生产级评分器；真实部署时同一形状可被
llm-client-sim 的「生成式替身（子代理）」替换，而调用方代码保持不变。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

Message = Dict[str, str]

# 模糊词表：提示词工程里常见的"不可执行"表述（双语，修 A2/A3——词表可绕过是
# 规则器的固有局限，此处只当下限；语义级兜底由 evaluation_judge 承担）
VAGUE_WORDS = [
    "一些", "某些", "合适", "相关", "等等", "之类的", "尽可能", "大概",
    "比较好", "尽量", "某种", "适当", "不错", "较好", "很多", "几个",
    "若干", "适度", "酌情", "大量", "众多",
    "一定程度上", "必要时", "视情况", "相应",
]
# 结构性碰撞例外：这些组合是 LangGPT 术语，不是模糊表述（分析器假阳性修正）
VAGUE_EXCEPTIONS = {"基本": r"基本(?!原则)"}
VAGUE_WORDS_EN = [
    "some", "several", "various", "appropriate", "relevant", "etc",
    "roughly", "about", "try to", "as much as possible", "a few",
    "moderate", "basically", "part of", "many", "lots of", "nice",
    "good enough", "if needed", "as appropriate", "certain",
]

# 结构特征正则（双语，修 A3：英文/中英混排不再被系统性低估）
RE_ROLE = r"#\s*Role|##\s*Role|角色|身份|Role\s*:"
RE_STEPS = r"步骤|工作流|流程|Workflow|Step|步骤 \d|\d+\.\s"
RE_CONSTRAINTS = r"约束|Constraints|要求|必须|不要|禁止|Rules|限制"
RE_GOALS = r"目标|Goals|Goal|Goal:|Objectives"


def _last_user(messages: List[Message]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return messages[-1].get("content", "") if messages else ""


def _extract(text: str) -> str:
    """从 <<ORIGINAL>> ... <<END>> 中抽取原始提示词，失败时回退到整段文本。"""
    m = re.search(r"<<ORIGINAL>>\s*(.*?)\s*<<END>>", text, re.S)
    return (m.group(1).strip() if m else text.strip())


def _extract_block(text: str, label: str) -> str:
    m = re.search(rf"<<{label}>>\s*(.*?)\s*<<END>>", text, re.S)
    return m.group(1).strip() if m else ""


# 禁用语境屏蔽（修 2026-10-07 实战假阳性，A2 同类）：
# 高质量终稿会写「终稿中不出现「一些、尽量、大概、若干…」等表述」——它是在**禁用**
# 这些词，词表却照常判为命中，把 70 分的稿子直接打到 26 分。
# 规则：引号串前 18 字内出现禁用类提示词（不出现/禁止/避免/消除…）则该引串不计分。
_RE_PROHIBIT_CUE = (r"(不出现|不包含|不含|不得出现|不得使用|禁用|禁止使用|禁止"
                    r"|避免|消除|删除|去除|不用|不使用|杜绝|排除|移除|去掉|改写掉"
                    r"|不把|不得把|不要把|避免把|不写|不得写|避免写|不留下|不保留)")
_RE_QUOTED = r"[「“\"'『]([^」”\"'』]{1,60})[」”\"'』]"

# 词表声明 / 禁用枚举形态（修 2026-10-09 回归 BUG-06，A8 第三次复发）：
# 终稿写「受限词清单为：若干、一些、部分、适当…」时，这些词是**被禁用**的对象。
# 旧实现只挖引号串，而这行用的是裸词 + 顿号枚举，一个引号都没有 → 屏蔽完全失效，
# 正在禁用模糊词的高质量稿被从 96 分打到 6 分（KNOWN_ISSUES F.6：分数暴跌先疑仪器）。
_RE_WORDLIST_DECL = re.compile(
    r"(受限词|禁用词|模糊词|禁用表述|受限表述|黑名单词|负面清单|禁止词|避免词|禁用清单)"
    r"[^。；;\n]{0,40}?(清单|词表|列表|如下|包括|含|为)\s*[:：]?"
)
_RE_ENUM_SEP = re.compile(r"[、,，]")
_RE_PROHIBIT_CUE_EXTRA = r"(不输出|不得输出|不产生|不可使用|不写)"


def _is_wordlist_line(line: str) -> bool:
    """整行是否为「禁用 / 受限词清单」的声明或枚举（BUG-06）。

    判定成立 → 该行所有模糊词一律不计命中：此时这些词是**被禁止的对象**，
    不是稿子正在使用的模糊表述。
    """
    if _RE_WORDLIST_DECL.search(line):
        return True
    # 禁用 cue + 顿号枚举（≥2 个分隔符）：
    # 「不输出…限定词…：若干、一些、适当、必要时…」这类裸词清单
    if re.search(_RE_PROHIBIT_CUE + "|" + _RE_PROHIBIT_CUE_EXTRA, line) \
            and len(_RE_ENUM_SEP.findall(line)) >= 2:
        return True
    return False


def _mask_one_line(line: str) -> str:
    """单行内的禁用语境屏蔽。

    三道判定，任一成立即挖空（不计入模糊词命中）：
    0. 词表声明 / 禁用枚举行（BUG-06）：整行挖空。
    1. 行级 cue：整行含「不出现 / 禁止 / 避免 / 消除…」等禁用线索。
    2. 引串紧邻连排：两个及以上引号串之间无其它字符（「A」「B」「C」词表列举形态）。
       —— 词表列举里混进真模糊词（举例说明候选人会写「团队若干人」）也不该算稿子在用。
    """
    def _is_listing() -> bool:
        spans = [m.span() for m in re.finditer(_RE_QUOTED, line)]
        if len(spans) < 2:
            return False
        for (_, e1), (s2, _) in zip(spans, spans[1:]):
            if line[e1:s2] != "":
                return False
        return True

    if _is_wordlist_line(line):
        return ""   # 整行都是被禁用的词表，全部挖空
    if not (re.search(_RE_PROHIBIT_CUE, line) or _is_listing()):
        return line
    # 行级判定一旦成立，整行的引号串无差别挖空：此时整句都处在「禁用 / 词表列举」
    # 语境里，逐 match 再判一次前缀窗口只会漏掉窗口边缘的词（曾漏掉「大概」「适度」
    # 「尽可能」——cue「不出现」在第 11 字，窗口从 start-18 起算时被切成两半）。
    return re.sub(_RE_QUOTED, "", line)


def mask_prohibited_terms(text: str) -> str:
    """把「禁用语境」下的引号词表挖空，避免把被禁止的词当成正在使用的词。

    修 2026-10-07 复发：整行是「4. 模糊词清零：全文不出现「尽量」「适当」「一些」
    「若干」「大概」」这种**长词表 + 行首 cue** 形态，原来的「前 18 字窗口」会被
    前面的引号串挤占，把「不出现」挤出窗口 → 判定失败 → 被禁用的词反被判命中，
    高质量稿子被扣 4 分（60→56）。现改为**整行**为判定单位，窗口逻辑仅作补充。
    """
    return "\n".join(_mask_one_line(line) for line in text.split("\n"))


def vague_hits_cn(text: str) -> set:
    """中文模糊词命中（带结构术语例外，如「基本原则」不算「基本」）。"""
    text = mask_prohibited_terms(text)
    hits = set()
    for w in VAGUE_WORDS:
        pat = VAGUE_EXCEPTIONS.get(w, re.escape(w))
        if re.search(pat, text):
            hits.add(w)
    return hits


def vague_hits_en(text: str) -> set:
    """英文模糊词命中（同样跳过禁用语境里的词表）。"""
    lower = mask_prohibited_terms(text).lower()
    return set(w for w in VAGUE_WORDS_EN
               if re.search(rf"\b{re.escape(w)}\b", lower))


def _structure_score(prompt: str) -> int:
    """内部确定性衬底：结构要素分（双语正则，地板 60 / 满分 96）。

    ⚠ 定位（2026-10-10 一致性改造）：上游 linshenkx/prompt-optimizer
    没有任何对应机制（其唯一评分 = LLM-as-judge evaluation，0-100 + overall）。
    本函数只服务于 analyze/compare 的 issues 引擎与内部诊断，
    **禁止**作为质量分输出到日志、报告或对外口径——质量分的唯一来源
    是盲评 judge 子代理（templates/evaluation_judge.md）的 0-100 / overall。
    """
    score = 60  # 基线
    if len(prompt) >= 40:
        score += 8
    if re.search(RE_ROLE, prompt, re.I):
        score += 8
    if re.search(RE_STEPS, prompt, re.I):
        score += 8
    if re.search(RE_CONSTRAINTS, prompt, re.I):
        score += 8
    if re.search(RE_GOALS, prompt, re.I):
        score += 4
    # 模糊词扣分（中英合并计数；禁用语境已屏蔽）
    vagueness = len(vague_hits_cn(prompt)) + len(vague_hits_en(prompt))
    score -= 10 * vagueness
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# 接缝替身
# ---------------------------------------------------------------------------
def analyze_stub(messages: List[Message]) -> Dict[str, Any]:
    """SHAPE_ANALYSIS：诊断原始提示词的问题（双语）。"""
    orig = _extract(_last_user(messages))
    issues: List[str] = []
    if len(orig) < 20:
        issues.append("提示词过短，缺乏上下文、角色与约束")
    for w in sorted(vague_hits_cn(orig)):
        issues.append(f"含模糊词「{w}」，建议量化为具体可执行标准")
    for w in sorted(vague_hits_en(orig)):
        issues.append(f"含模糊表述 \"{w}\"，建议量化为具体可执行标准")
    if not re.search(RE_ROLE, orig, re.I):
        issues.append("缺少角色 / 身份设定（建议用 LangGPT 风格明确 # Role）")
    if not re.search(RE_STEPS, orig, re.I):
        issues.append("缺少明确的执行步骤 / 工作流")
    if not re.search(RE_CONSTRAINTS, orig, re.I):
        issues.append("缺少输出约束（格式、边界、禁忌）")
    score = _structure_score(orig)
    suggestions = [f"针对问题：{i}" for i in issues] or ["结构已较完整，可补充具体示例与验收标准"]
    return {"issues": issues, "score": score, "suggestions": suggestions}


def score_stub(messages: List[Message]) -> float:
    """SHAPE_SCORE：给提示词打 0-100 分。"""
    orig = _extract(_last_user(messages))
    return float(_structure_score(orig))


def compare_stub(messages: List[Message]) -> Dict[str, Any]:
    """SHAPE_COMPARE：对比优化前后。"""
    text = _last_user(messages)
    before = _extract_block(text, "BEFORE")
    after = _extract_block(text, "AFTER")
    sb = _structure_score(before)
    sa = _structure_score(after)
    delta = sa - sb
    if delta > 0:
        winner, reason = "after", f"优化后结构评分 {sa} 高于优化前 {sb}，提升 {delta} 分"
    elif delta < 0:
        winner, reason = "before", f"优化后评分 {sa} 低于优化前 {sb}，下降 {abs(delta)} 分"
    else:
        winner, reason = "tie", f"优化前后评分持平（均为 {sa} 分）"
    return {"winner": winner, "delta": delta, "reason": reason}


def rewrite_stub(messages: List[Message]) -> str:
    """SHAPE_OPTIMIZED_PROMPT：把原始提示词改写为 LangGPT 风格结构化提示词。

    幂等性：若输入已经是结构化提示词（含 # Role 与多个 ## 节），视为已收敛，
    原样返回，避免多轮迭代时把已结构化提示词再包一层导致嵌套。
    """
    orig = _extract(_last_user(messages)).strip()
    if re.search(r"#\s*Role", orig) and orig.count("## ") >= 3:
        return orig
    goal = orig[:120] + ("…" if len(orig) > 120 else "")
    return f"""# Role: 任务执行专家

## Profile
- 身份：本领域资深执行者
- 版本：v2（经优化）
- 依据：用户原始意图——「{goal}」

## Skills
1. 准确理解用户意图，拆解为可执行步骤
2. 遇到歧义时基于上下文做合理默认假设并显式说明
3. 输出结构清晰、可直接落地

## Goals
- 高质量完成用户诉求：{orig}

## Workflow
1. 复述并确认任务目标与约束
2. 拆解为 2-4 个有序步骤
3. 逐步执行并给出中间产出
4. 汇总最终结果，标注关键假设

## Constraints
- 不编造事实；不确定处明确告知
- 优先具体、可量化，避免模糊表述
- 一次只聚焦一个核心目标

## Initialization
作为任务执行专家，依据上述设定开始执行用户请求。
"""


# 供 demo_subagent 使用的「子代理 fulfil 替身」（演示用，仍零 LLM）
def subagent_realizer(client, messages: List[Message], shape) -> Any:
    """编排层注入的生成式 realizer（生产环境即子代理调用）。

    这里用确定性增强作为子代理 fulfil 的*演示替身*：真实部署时，此处应改为
    向一个强约束 prompt 的子代理派发任务，并把子代理返回值按 shape 校验后回传。
    """
    text = _extract(_last_user(messages)) if messages else ""
    base = rewrite_stub([{"role": "user", "content": f"<<ORIGINAL>>\n{text}\n<<END>>"}])
    if shape.name == "creative_enhancement":
        return base + "\n\n> 创意增强（子代理补充）：已加入「反例约束」与「验收评分标准」两节，降低跑题概率。"
    # 兜底：当作普通优化处理
    return base


# ---------------------------------------------------------------------------
# v2 规则器（DEEP_ANALYSIS_AND_PLAN.md §4）：G7 自洽 / G9 变量完整
# 定位 = 下限检查器；语义级矛盾仍由 evaluation_judge（生成式）兜底
# G8 信息保持已于 2026-10-10 对抗评估后移除：对自然语言草稿类输入恒返回空
# （无数字/引号锚点可丢），且不校验「用户需求是否被静默丢弃」，实测对语义
# 漂移全漏，属无效护栏——真正的语义守护应交给 G10 盲评（建议加软阈值）。
# ---------------------------------------------------------------------------
# 互斥约束规则（G7 自洽 linter，修 A1）：每条 = (正则A, 正则B, 说明)
# 注意：第 1 条「禁止输出 vs 要求输出」已下沉为带范围判定的 _negation_is_global()，
# 见下方说明——无宾语范围的禁止才是真冲突，有具体宾语的是合法范围限定。
_CONFLICT_RULES = [
    (r"(只输出一句话|只用一句话|仅一句话|输出一句话|只有一句话|一句话$|one sentence only)",
     r"(包含五个|5 个部分|五个部分|多个部分|分五个|five parts|several parts)",
     "「单句输出」与「多部分结构」数量冲突"),
    (r"(不要解释|无需解释|禁止解释)",
     r"(解释每一步|说明理由并解释|详细解释|附上解释)",
     "「禁止解释」与「要求解释」冲突"),
    (r"(不要使用列表|禁止列表|不要分点)",
     r"(以列表形式|分点列出|使用列表|列出以下)",
     "「禁止列表」与「要求列表」冲突"),
]


# 「禁止输出」类：只有**无宾语范围**的禁止才与「要求输出」冲突。
# 修 2026-10-07 实战假阳性（G7 把合法稿判死）：
#   「第一轮只输出体检结论…不输出改写稿」+「每条含…写明…」被原规则误判为冲突，
#   实际这是「不输出 X / 输出 Y」的范围限定，完全自洽。
#   真冲突是「不要输出任何内容」这类全局禁止——宾语为空或含 任何/全部/所有/一切。
NEG_OUTPUT_RE = (r"(不要输出|不输出|禁止输出|无需输出|不要回复"
                 r"|不要生成|不要给出|不要提供)")
REQ_OUTPUT_RE = (r"(必须输出|必须包含|必须给出|必须提供|请输出"
                 r"|请提供|包含以下|列出|写明)")
# 「任何 / 全部 / 所有 / 一切」只有修饰**泛称宾语**时才表示全局禁止：
#   「不输出任何内容 / 任何东西 / 任何信息」  → 全局禁止（真冲突）
#   「不输出任何引导词 / 所有解释性说明」    → 限定范围的禁止（自洽，非冲突）
# 修 2026-10-09 回归 BUG-02：原实现把「任何」一律当作全局标志，
# 使「不输出任何引导词、解释性说明」+「输出一份报告」被误判为互斥，合法终稿被判死。
_GENERIC_QUANT_RE = re.compile(r"^(?:的)?(任何|全部|所有|一切)")
_GENERIC_NOUN_RE = re.compile(r"^(?:的)?(内容|东西|信息|输出|结果|文字|话|答复|回复|字)")
_PUNCT_ONLY_RE = re.compile(r"^[\s，。；、：:,.!！?？）)\]】》\"'」』]*$")


# 中文动词常带**前置宾语**：「第一项简历正文与第二项待确认问题一律不输出」里
# 宾语在「不输出」左边。原实现只往动词右边找宾语，右边是空 → 误判为全局禁止，
# 把合法稿判死（2026-10-07 复发，A7 同族）。故左侧也要找宾语。
_RE_ADV_TAIL = re.compile(r"(一律|均|一并|概不|一律都|都|全|不再|绝不|也不|又|而已|罢了|不|没)\s*$")
_RE_LEFT_OBJECT = re.compile(r"(第[一二三四五六七八九十]项|正文|清单|对照表|成稿|回复|结果|说明|问题|内容|列表|表格)")


def _obj_is_generic(obj: str) -> bool:
    """禁止动词右侧的宾语是否属于「全局」而非限定范围。

    空宾语 → 全局；「内容 / 东西」类泛称 → 全局；
    「任何 + 具体名词」（任何引导词 / 所有解释性说明）→ 限定范围，不算全局。
    """
    if not obj:
        return True
    m = _GENERIC_QUANT_RE.match(obj)
    rest = obj[m.end():] if m else obj
    rest = re.sub(r"^的", "", rest).strip()
    if not rest or _PUNCT_ONLY_RE.match(rest):
        return True          # 「不输出任何」/「不输出。」→ 无限定，视为全局
    return bool(_GENERIC_NOUN_RE.match(rest))


def _negation_is_global(prompt: str) -> bool:
    """「禁止输出」是否不带具体宾语范围（= 全局禁止，与任何输出要求互斥）。

    宾语判定同时看动词两侧：右侧是常规后置宾语，左侧覆盖中文前置宾语
    （「…正文与清单一律不输出」）。两侧都找不到具体宾语才算全局禁止。
    """
    for m in re.finditer(NEG_OUTPUT_RE, prompt):
        # 宾语窗口先按句读切一刀：否则「不输出。\n必须列出三点」会被下一句
        # 的「必须列出三点」冒充成具体宾语，把全局禁止误判为范围限定。
        obj = re.split(r"[。；;！？!?\n]", prompt[m.end():m.end() + 14], 1)[0].strip()
        if not (obj and not _obj_is_generic(obj)):
            left = prompt[max(0, m.start() - 20):m.start()]
            if not _RE_LEFT_OBJECT.search(_RE_ADV_TAIL.sub("", left)):
                return True
    return False


def consistency_lint(prompt: str) -> List[str]:
    """G7：互斥约束启发式检查。返回冲突说明列表（空 = 通过）。

    已知覆盖：「只输出一句话」+「包含五个部分」+「不要输出任何内容」类互斥（A1 启发式）。
    语义级自相矛盾不在规则覆盖内，由 evaluation_judge 的自洽清单兜底。
    """
    conflicts: List[str] = []
    if _negation_is_global(prompt) and re.search(REQ_OUTPUT_RE, prompt):
        conflicts.append("自洽性冲突：「禁止输出」与「要求输出」同时出现"
                         "（全局禁止 vs 输出要求）")
    for pat_a, pat_b, desc in _CONFLICT_RULES:
        if re.search(pat_a, prompt) and re.search(pat_b, prompt):
            conflicts.append(f"自洽性冲突：{desc}")
    # 自由裁量冲突：「由你自行决定是否遵守」类元约束使所有约束失效
    if re.search(r"(自行决定是否遵守|决定是否遵守以上|可选择性地遵守)", prompt) and re.search(
        RE_CONSTRAINTS, prompt
    ):
        conflicts.append("自洽性冲突：约束可被自主忽略，等同无约束")
    return conflicts


# 修 A9（2026-10-07 实战发现）：原正则只认 ASCII 变量名，{{简历原文}}、{{目标岗位JD}}
# 这类中文占位符**一个都检测不到**，G9 全程空转并谎报"原文无 {{var}} 占位符"——
# 若子代理删掉占位符，门禁根本不会拦。改用 Unicode \w（Python3 \w 已含中日韩文字）。
_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def extract_variables(text: str) -> List[str]:
    """确定性变量抽取：{{name}} 正则（可靠、零 LLM）。"""
    return sorted(set(_VAR_RE.findall(text or "")))


def missing_variables(before: str, after: str) -> List[str]:
    """G9：变量完整性（对齐原项目 iterate 模板的输出前自检）。"""
    return [v for v in extract_variables(before) if v not in extract_variables(after)]


def build_patch_plan(issues: List[str], suggestions: List[str]) -> List[Dict[str, str]]:
    """把剩余 issues 确定性转换为 patchPlan（issues → 改写指令的回灌管道）。"""
    plan: List[Dict[str, str]] = []
    for i, issue in enumerate(issues):
        action = suggestions[i] if i < len(suggestions) else f"消除问题：{issue}"
        plan.append({
            "target": issue,
            "action": action if action.startswith(("针对", "消除")) else f"消除问题：{issue}",
        })
    return plan
