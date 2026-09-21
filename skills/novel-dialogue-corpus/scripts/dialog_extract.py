# -*- coding: utf-8 -*-
"""
《十日终焉》对话提取器 v3（含六维能力指标计算）
- 中文弯引号配对
- 说话人识别：以人物名词结尾的短语（山羊头/女人/男人/青年/白大褂…），
  取最近(最左且非宾语)匹配；无动词倒装、同说话人连续引号兜底
- 单元级指标：句长 / 省略号率 / 反问密度 / 称谓符号化 / 对抗张力 / 回避率 / 功能词向量
- 输出结构化数据供场景清单与特点归纳使用
"""
import re, os, json, sys, math

# 场景切分阈值（与 pipeline 保持一致）：两引号间叙述 > 此字符数则视为新场景
SCENE_GAP = 500

# ============ 六维能力指标 · L1/L2 表层与词典近似计算 ============
# 功能词表（中文虚词/小品词），用于角色声音差异 JSD
FUNCTION_WORDS = ['的','了','是','在','我','你','他','她','它','们','就','也','都','不','没',
    '还','很','又','要','会','能','着','过','给','和','与','但','却','因','为','从','向',
    '对','以','之','其','而','此','后','前','中','里','外','上','下','这','那','个','些',
    '把','被','让','叫','比','更','最','再','才','正','等','像','如','若','吗','呢','吧',
    '啊','呀','哦','嘛','地','得','及','并','或','虽','然','于','将','已','仍','皆','均','自己','什么','怎么','为什么','怎么','如何','怎么']

# 称谓符号化：描述性/代号判定
# 修复(R-D1)：原 SYMBOLIC_TAIL 缺「龙/雀/武」，导致四象代号 青龙/朱雀/玄武 漏检；
# 同时 齐夏/陈俊南/乔家劲/楚天秋 等真实姓名因尾字(夏/南/劲/秋)命中 tail 被误判。
# 方案：① 扩充 tail 含 龙/雀/武；② 四象前缀 青/朱/玄 入 SYMBOLIC_PREFIX；
#       ③ 真实姓名拦截表 KNOWN_REAL_NAMES 强制 False；④ 判定前先做描述性清洗。
SYMBOLIC_TAIL = re.compile(r'(头|女人|男人|青年|少年|老者|老头|老太|面具|警官|医生|队员|主|王|神|妇|爷|婆|哥|弟|姐|妹|生|者|年轻|家伙|褂|汉|伙|辈|徒|客|妖|魔|兵|探|警|犯|友|敌|将|士|仆|佣|农|商|工|师|傅|翁|妪|婴|童|郎|娘|媳|婿|侄|孙|鬼|魂|灵|仙|佛|道|儒|侠|盗|匪|卒|吏|臣|君|皇|帝|妃|侯|伯|怪|物|雄|霸|圣|精|影|长|东|板|龙|雀|武)$')
SYMBOLIC_PREFIX = re.compile(r'^(地|天|人|神|阴|阳|青|白|朱|玄|黑|红|清冷|花臂|纹着|戴着|穿着|蒙面|独眼|断臂|瘸腿|胖|瘦|高|矮|年轻|中年|老年|小|老|络腮|光头|秃|瞎|哑|瘸)')
CODE_NAME = re.compile(r'^(地|天|人|神|阴|阳)[甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥龙猴兔蛇羊狗鸡猪鼠牛虎马]')
DIGIT_CODE = re.compile(r'^[赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳][一二三四五六七八九十百零0-9]+$')
# 真实角色全名拦截表（命中则强制非符号化）。需随语料扩展。
KNOWN_REAL_NAMES = {
    '齐夏','陈俊南','乔家劲','楚天秋','云瑶','江若雪','苏闪','林檎','韩一墨','甜甜',
    '楚天枢','楚天秋','白羊','郑英雄','田鼠','许流年','戴琳','甜甜','苏文','林然',
    '楚天阔','楚天枢','齐夏','陈俊南','乔家劲',
}

def _clean_symbolic_input(sp):
    """判定前清洗：去首尾'的'，截断尾部情绪/动作动词（防御提取噪声如'小眼镜高兴'）。"""
    sp = sp.replace('的', '').strip().strip('的').strip()
    # 截断尾部谓语/修饰起词（防'山羊头高兴'类误带）
    tail_cut = ['高兴','生气','点头','摇头','笑了','笑','道','说','问','想','看','站','坐','走','转','低','抬','皱','愣','呆','沉','默','怒','怕','惊','慌','急','忧','叹','喊','叫']
    for c in tail_cut:
        if sp.endswith(c) and len(sp) > len(c):
            sp = sp[:-len(c)]
    return sp

def is_symbolic_speaker(sp):
    if not sp or sp.startswith('（'):
        return False
    sp = _clean_symbolic_input(sp)
    if not sp or sp.startswith('（'):
        return False
    if sp in KNOWN_REAL_NAMES:
        return False
    if CODE_NAME.match(sp) or DIGIT_CODE.match(sp):
        return True
    if SYMBOLIC_PREFIX.match(sp):
        return True
    if SYMBOLIC_TAIL.search(sp):
        return True
    return False

# 对抗/张力词典（L2 lexicon 近似）—— 重要：此为【未验证的粗粒度词典代理】，
# 仅统计暴力/威胁类词汇出现次数，并不测量真正的戏剧张力（tension）。
# 经对抗评估(F1)确认：AUR 的 ~53% 仅由「死/命/滚」触发，且单字 token 造成子串误匹配
# （计划/信念/使命→'命'中招；绝对/绝望→'绝'中招；小狗→'狗'中招；阴谋→'阴'中招；毒药→'毒'中招）。
# B4 修复（09 系统性修复）：① 删除单字风险 token '命/绝/狗/阴/毒'，根除上述子串误匹配；
# ② 加 EXCLUDE_SUBSTRINGS 撤销"死"在 送死/饿死鬼/吓死 等非威胁语境的误触发；
# ③ 09 C1/F1：本函数【不得作为复检闸门或张力基线】，仅作"对抗词素存在性 L1"标记。
# 真正张力度量须建 L3 gold（McKee 三要素）后另立，当前无可靠 gold。
ADV_WORDS = ['威胁','杀','死','要了你','性命','粉肠','冚家铲','识相','我劝','不准','闭嘴','滚','揭短','揭穿','反驳','质疑','冷笑','嘲笑','讥讽','讥笑','怒','狠','逼问','逼','拷问','惩罚','教训','信不信','弄死','宰','砍','剁','撕','灭','镇压','挑衅','威吓','恫吓','算计','出卖','背叛','诡计','谎言','耍','休想','妄想','做梦','呸','啐','放肆','杂种','畜生','废物','垃圾','怂','孬','欺','恐吓','逼我','算账','报复','宰了','弄死你']
# B4 排除集：这些非威胁语境含"死"等核心字，撤销其计数（整词误触发修正）
EXCLUDE_SUBSTRINGS = ['送死','饿死鬼','吓死']
NEG_WORDS = ['不','没','别','勿','非','无','未','莫','休','甭','不要','不敢','不会']

def adv_lexicon_score(content):
    """【未验证词典代理·B4整词/排除集修正】返回对抗词素计数分（0~5）。
    仅"对抗词素存在性 L1"标记，非张力度量，绝不作复检闸门。"""
    s = 0.0
    for w in ADV_WORDS:
        if w in content:
            s += 1.0
    # B4：撤销非威胁语境的子串误触发（如 送死/饿死鬼/吓死 中的"死"）
    for ex in EXCLUDE_SUBSTRINGS:
        if ex in content:
            s -= 1.0
    if any(n in content for n in NEG_WORDS) and ('？' in content):
        s += 0.5
    if '！' in content:
        s += 0.5
    return round(max(min(s, 5.0), 0.0), 2)

def is_rhetorical(content):
    if '？' not in content and '?' not in content:
        return False
    if '难道' in content or '岂' in content or '何不' in content or '何必' in content:
        return True
    if any(n in content for n in NEG_WORDS) and ('吗' in content or '呢' in content):
        return True
    return False

def is_ellipsis(content):
    return ('……' in content) or content.endswith('…') or content.endswith('…"') or ('…' in content and len(content) > 2)

def func_counts(content):
    return {w: content.count(w) for w in FUNCTION_WORDS}

def jsd(p, q):
    """Jensen-Shannon 散度（比特），p/q 为功能词计数字典"""
    words = FUNCTION_WORDS
    sp = sum(p[w] for w in words); sq = sum(q[w] for w in words)
    if sp == 0 or sq == 0:
        return 0.0
    P = [p[w]/sp for w in words]; Q = [q[w]/sq for w in words]
    M = [(a+b)/2 for a, b in zip(P, Q)]
    def kl(a, b):
        s = 0.0
        for x, y in zip(a, b):
            if x > 0 and y > 0:
                s += x * math.log2(x / y)
        return s
    return round(0.5*kl(P, M) + 0.5*kl(Q, M), 4)

# 说话动词
VERB_LIST = r'说话了|说话|说道|问道|问|说|回答|回道|开口|骂|喊|叫|嚷|轻声|低声|打断|接话|附和|反驳|感叹|喃喃|讲道|提议|应道|答道|嘀咕|嘟囔|笑道|冷笑|沉声|高声|大声|应|唤|安抚|劝|叹|嘀咕|咕哝|哼|嗤|讥|嘲|赞|夸|谢|求|恳|哀|呼|嘶|吼|咆哮|厉声|厉色'
VERB_RE = re.compile(VERB_LIST)
# 人物名词结尾（称谓/角色/生肖代号/常见身份）—— 已剔除 处/后/中/女/男/口/子 等方位虚词
PERSON_TAIL = (r'头|人|女人|男人|青年|少年|少女|女孩|孩子|儿童|男童|女童|老者|老头|老太|大叔|大妈|'
    r'面具|警官|医生|队员|主|王|神|妇|爷|婆|哥|弟|姐|妹|生|者|年轻|家伙|'
    r'夏|南|秋|劲|龙|猴|兔|蛇|羊|狗|鸡|猪|鼠|牛|虎|马|官|长|东|板|'
    r'褂|汉|伙|辈|徒|客|妖|魔|兵|探|警|犯|友|敌|将|士|仆|佣|农|商|工|师|傅|'
    r'翁|妪|婴|童|郎|娘|媳|婿|侄|孙|鬼|魂|灵|仙|佛|道|儒|侠|盗|匪|卒|吏|臣|'
    r'君|皇|帝|后|妃|侯|伯|怪|物|雄|霸|圣|精|影|'
    r'甲|乙|丙|丁|戊|己|庚|辛|壬|癸|子|丑|寅|卯|辰|巳|午|未|申|酉|戌|亥')
# 禁止说话人短语以这些词开头（介词/代词/连词/方位）
FORBID_START = r'对在向给叫让替为跟和从把被与和即也都就又还而但并且因由当这那该各某任每另我你他她它们其此于及或等之以有是'
NAME_CAND = re.compile(r'(?<![对在向给叫让替为跟和从把被与和即也都就又还而但并且因由当这那该各某任每另我你他她它们其此于及或等之以有是])'
                        r'(?:一个|这个|那个|一位|一名|名叫|身着|穿着|戴着|脸上|嘴里|被称|自称|自称是|外号叫|人称)?'
                        r'([一-龥a-zA-Z]{1,10}(?:' + PERSON_TAIL + r'))')
PRO_RE = re.compile(r'(他|她|它|众人|大家|我们|你们|他们|她们|我|你)')

def clean_name(sp):
    sp = re.sub(r'^(?:一个|这个|那个|一位|一名|名叫|身着|穿着|戴着|脸上|嘴里|被称|自称|自称是|外号叫|人称)', '', sp)
    # 截断首个说话动词/宾语标记之后的部分（防止「清冷的女人开口对山羊头」整句吞入）
    m = VERB_RE.search(sp)
    if m and m.start() > 0:
        sp = sp[:m.start()]
    for cut in '对向给叫让替为跟和':
        i = sp.find(cut)
        if i > 0:
            sp = sp[:i]
            break
    # 截断首个谓语/修饰起词（防止「山羊头没有点头」「李警官瞪着乔家劲」整段吞入名字）
    # 前缀词：切在其后（保留主体）；中缀词：切在其前（保留主语）
    PREFIX_CUT = ['只见', '按照', '当', '从', '在场的', '在', '被', '给', '让']
    INFIX_CUT = ['没有', '也没有', '也', '又', '已经', '大抵', '了解', '知道', '看到', '听到', '明白',
           '着急', '不断', '听后', '只听到', '眼前', '伸手', '捂', '捂住', '瞪', '冷声', '冷冷',
           '心中', '暗道', '暗自', '喝道', '摇', '点', '看着', '看向', '回', '指了', '扯了',
           '扯', '眉头', '点', '用笔', '将', '缓缓', '慢慢', '微微', '轻声', '低声', '高声',
           '猛地', '忽然', '突然', '终于', '随即', '，', '。', '、']
    for c in PREFIX_CUT:
        i = sp.find(c)
        if i >= 0:
            sp = sp[i+len(c):]
            break
    for c in INFIX_CUT:
        i = sp.find(c)
        if i > 0:
            sp = sp[:i]
            break
    sp = sp.replace('的', '').strip()
    # 归一：去掉内部"的"以合并「清冷的女人」/「清冷女人」等变体
    sp = sp.strip('的').strip()
    return sp or '（未知）'

def clause_before(seg, pos):
    """取 seg 中 pos 之前、最近一个句末标点/换行之后的子句（限 90 字符），并剥离引号片段"""
    sub = seg[max(0, pos-90):pos]
    # 剥离引号片段，避免在上句引语内容里误匹配说话人
    sub = re.sub(r'“[^”]*”', '', sub)
    sub = re.sub(r'「[^」]*」', '', sub)
    idx = -1
    for i in range(len(sub)-1, -1, -1):
        if sub[i] in '。！？\n':
            idx = i
            break
    if idx >= 0:
        sub = sub[idx+1:]
    return sub

def clause_after(seg, pos):
    sub = seg[pos:pos+90]
    for i, ch in enumerate(sub):
        if ch in '。！？\n':
            sub = sub[:i]
            break
    return sub

def pick_name(seg):
    names = list(NAME_CAND.finditer(seg))
    # 取最左（捕获完整描述短语），且不被宾语标记前置
    for nm in names:
        before = seg[:nm.start()][-1:] if nm.start() > 0 else ''
        if before in '对向给叫让替为跟和':
            continue
        return nm.group(1)
    if names:
        return names[0].group(1)
    return None

def find_speaker(text, s, e, prev_speaker):
    pre = text[max(0, s-220):s]
    post = text[e: e+160]
    L = len(pre)
    # 1) 顺装：pre 中距左引号 <=28 的说话动词（就近优先，跳过被：/引号隔断的）
    cand_list = []
    for m in VERB_RE.finditer(pre):
        d = L - m.end()
        if 0 <= d <= 28:
            cand_list.append(m)
    cand_list.sort(key=lambda x: -x.start())  # 最近优先
    for cand_v in cand_list:
        between = pre[cand_v.end():s]
        if '“' in between or '”' in between:
            continue
        seg = clause_before(pre, cand_v.start())
        nm = pick_name(seg)
        if nm:
            return clean_name(nm), '顺装'
    # 2) 倒装：post 中距右引号 <=28 的最近说话动词
    for m in VERB_RE.finditer(post):
        if m.start() <= 28:
            seg = clause_before(post, m.start())
            nm = pick_name(seg)
            if nm:
                return clean_name(nm), '倒装'
            break
    # 3) 倒装无动词：右引号后紧跟人物名词短语（<=30）后接标点/的/地
    seg = clause_after(post, 0)
    m2 = re.match(r'^[”"”]?\s*([一-鿿]{1,8}(?:'+PERSON_TAIL+r'))', seg.lstrip())
    if m2:
        return clean_name(m2.group(1)), '倒装无动词'
    # 4) 代词回指
    pm = list(PRO_RE.finditer(pre[-45:]))
    if pm:
        return '（' + pm[-1].group(1) + '·回指）', '代词'
    # 5) 同说话人连续引号兜底
    if prev_speaker and prev_speaker not in ('（未知）', '（ unknown ）'):
        return prev_speaker, '续接'
    return '（未知）', '未知'

def extract_file(path):
    return extract_file_text(os.path.basename(path), open(path, encoding='utf-8').read())

def extract_file_text(fname, text):
    """内存版抽取: 直接处理文本, 不读磁盘路径。"""
    lines = text.split('\n')
    line_starts = []
    acc = 0
    for ln in lines:
        line_starts.append(acc); acc += len(ln) + 1
    def char_to_line(pos):
        lo, hi = 0, len(line_starts)-1
        while lo < hi:
            mid = (lo+hi+1)//2
            if line_starts[mid] <= pos: lo = mid
            else: hi = mid-1
        return lo+1
    # 引号配对
    opens = [m.start() for m in re.finditer('“', text)]
    closes = [m.start() for m in re.finditer('”', text)]
    pairs = []
    stack = []
    for q in sorted(set(opens)|set(closes)):
        if q in set(opens): stack.append(q)
        else:
            if stack: pairs.append((stack.pop(), q))
    pairs.sort()
    units = []
    prev_sp = None
    prev_type = None
    prev_end = None
    for (s, e) in pairs:
        content = text[s+1:e].strip()
        if len(content) < 1:
            continue
        # R-D2：场景边界隔离——两引号间叙述超过 SCENE_GAP 视为新场景，
        # 重置 prev_type/prev_sp，避免跨场景的伪相邻污染回避率/续接判定。
        if prev_end is not None and (s - prev_end) > SCENE_GAP:
            prev_type = None
            prev_sp = None
        sp, mode = find_speaker(text, s, e, prev_sp)
        prev_sp = sp
        # 话轮类型
        if '？' in content or '?' in content:
            ttype = '疑问'
        elif '！' in content or '!' in content:
            ttype = '感叹'
        else:
            ttype = '陈述'
        # ---- 六维指标（L1/L2 表层+词典近似）----
        ell = is_ellipsis(content)
        rth = is_rhetorical(content)
        # 注意：tension 字段实为【未验证的对抗词典分】，非真正张力度量（见 adv_lexicon_score 注记）
        tens = adv_lexicon_score(content)
        adv = tens >= 1.0
        sym = is_symbolic_speaker(sp)
        # 回避率：前句为疑问，本句以疑问回掷 / 极短 / 省略号（未正面作答）
        # 注意：prev_type 已在场景边界重置，仅在同一场景内有效
        avoidance = (prev_type == '疑问') and (ttype == '疑问' or len(content) <= 4 or ell)
        units.append({
            'char_start': s, 'char_end': e,
            'line': char_to_line(s),
            'speaker': sp, 'mode': mode,
            'type': ttype, 'content': content,
            # 指标字段
            'length': len(content),
            'ellipsis': ell,
            'rhetorical': rth,
            'lexicon_adv': tens,   # 原 tension：改名为未验证词典分，避免误读为张力度量
            'adversarial': adv,
            'symbolic': sym,
            'avoidance': avoidance,
        })
        prev_type = ttype
        prev_end = e
    return text, units

if __name__ == '__main__':
    if len(sys.argv) > 1:
        p = sys.argv[1]
        _, units = extract_file(p)
        for i,u in enumerate(units):
            print(f"[{i+1}] 行{u['line']} 「{u['speaker']}」({u['mode']}/{u['type']})")
            print(f"     {u['content'][:60]}")
    else:
        print("用法: python dialog_extract.py <章节md路径>")
