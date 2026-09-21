# -*- coding: utf-8 -*-
"""
对话语料库构建 · 步骤1：从任意小说文本提取对话单元与对话场景
用法:
  python dialog_pipeline.py --input <小说.txt|章节目录> --out-dir <输出目录> [--title 书名]
输入:
  - 单个 .txt/.md 文件: 自动按「第X章」标题切分章节; 无章节标记则整体作为一章
  - 目录: 其中每个 .md/.txt 文件视为一章
输出 (写入 --out-dir):
  - dialogue_units.json   对话单元 (角色/话轮类型/六维单元指标)
  - dialogue_scenes.json  对话场景 (participants/turns/switch_rate/tags 等)
  - 对话场景清单.md / 对话特点归纳.md
仅依赖标准库; 依赖同目录 dialog_extract.py。
"""
import re, os, json, glob, sys, math, argparse
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dialog_extract as de

SCENE_GAP = 500  # 两引号间叙述超过此字符数则切分为新场景

CH_RE = re.compile(r'^\s*(第[0-9零一二三四五六七八九十百千两]+章[^\n]*)$')

def split_single_file(text):
    """单文件按「第X章」行切分; 无标记则整体一章。返回 [(ch_no, ch_num, ch_title, body)]"""
    lines = text.split('\n')
    marks = []  # (line_idx, title)
    for i, ln in enumerate(lines):
        m = CH_RE.match(ln.strip())
        if m:
            marks.append((i, m.group(1).strip()))
    if len(marks) < 2:
        return [(1, '1', '', text)]
    chunks = []
    bounds = [0] + [i for i, _ in marks]
    if marks[0][0] > 0:
        chunks.append((1, '1', '', '\n'.join(lines[:marks[0][0]])))
    for k, (i, title) in enumerate(marks):
        end = marks[k+1][0] if k+1 < len(marks) else len(lines)
        num = re.match(r'第([0-9零一二三四五六七八九十百千两]+)章', title)
        chunks.append((k+1, num.group(1) if num else str(k+1), title, '\n'.join(lines[i:end])))
    return chunks

def chapter_meta(fname):
    """目录模式: 从文件名解析 (章号, 章号文本, 章题); 解析失败返回 None"""
    m = re.match(r'(\d+)[_\- ]*第(.+?)章(.*)\.(md|txt)$', fname)
    if m:
        return int(m.group(1)), m.group(2), m.group(3).strip()
    m = re.match(r'(\d+)[_\- ]*(.*)\.(md|txt)$', fname)
    if m:
        return int(m.group(1)), None, m.group(2).strip()
    return None, None, None

def cn_to_int(s):
    try:
        return int(s)
    except ValueError:
        return None

def build_scenes(units, fname, ch_no, ch_num, ch_title, full_text):
    scenes, cur = [], None
    for u in units:
        if cur is None:
            cur = {'units': [u]}
            scenes.append(cur)
            continue
        prev = cur['units'][-1]
        if u['char_start'] - prev['char_end'] > SCENE_GAP:
            cur = {'units': [u]}
            scenes.append(cur)
        else:
            cur['units'].append(u)
    scene_list = []
    for idx, sc in enumerate(scenes, 1):
        us = sc['units']
        sp_set = set()
        for u in us:
            sp = u['speaker']
            if sp and not sp.startswith('（') and sp not in ('（未知）',):
                sp_set.add(sp)
        first, last = us[0], us[-1]
        excerpt = [f"{u['speaker']}：{u['content'][:42]}" for u in us[:10]]
        tags = []
        types = [u['type'] for u in us]
        if types.count('疑问') >= 2:
            tags.append('质询/对抗')
        seg = full_text[first['char_start']:last['char_end']+1]
        if re.search(r'逻辑|推理|因为|所以|如果|那么|前提|证明|推导|线索|矛盾', seg):
            tags.append('推理推演')
        if '……' in seg or seg.count('…') > 3:
            tags.append('留白/潜台词')
        if re.search(r'威胁|杀|死|要了你|命|闭嘴|住口', seg):
            tags.append('威胁/冲突')
        if re.search(r'别怕|安抚|解释|放心|没事', seg):
            tags.append('安抚/解释')
        if len(sp_set) >= 4:
            tags.append('群像多角色')
        if len(sp_set) == 1:
            tags.append('独白/对质')
        n = len(us)
        switches = sum(1 for i in range(1, n) if us[i]['speaker'] != us[i-1]['speaker'])
        ell_rate = sum(1 for u in us if u['ellipsis']) / n if n else 0
        tens_mean = sum(u['lexicon_adv'] for u in us) / n if n else 0  # 未验证词典代理，非张力
        sym_rate = sum(1 for u in us if u['symbolic']) / n if n else 0
        lengths = [u['length'] for u in us]
        if lengths:
            mx = max(lengths); buckets = 10
            hist = [0]*(buckets+1)
            for L in lengths:
                hist[min(L*buckets//(mx+1), buckets)] += 1
            ent = -sum((h/n)*math.log2(h/n) for h in hist if h > 0)
        else:
            ent = 0
        scene_list.append({
            'scene_id': f"CH{ch_no:04d}-S{idx:02d}",
            'chapter': ch_no,
            'chapter_label': f"第{ch_num}章" if ch_num else '',
            'title': ch_title,
            'file': fname,
            'start_line': first['line'], 'end_line': last['line'],
            'char_start': first['char_start'], 'char_end': last['char_end'],
            'turns': n,
            'participants': sorted(sp_set),
            'tags': tags,
            'excerpt': excerpt,
            'switch_rate': round(switches/n, 3) if n else 0,
            'ellipsis_rate': round(ell_rate, 3),
            'tension_mean': round(tens_mean, 3),
            'symbolic_rate': round(sym_rate, 3),
            'len_entropy': round(ent, 3),
            'avg_len': round(sum(lengths)/n, 1) if n else 0,
        })
    return scene_list

def main():
    ap = argparse.ArgumentParser(description='小说对话单元/场景提取')
    ap.add_argument('--input', required=True, help='小说文本: 单个 .txt/.md 文件或章节目录')
    ap.add_argument('--out-dir', required=True, help='输出目录')
    ap.add_argument('--title', default=None, help='书名 (默认取输入文件/目录名)')
    args = ap.parse_args()
    title = args.title or os.path.basename(args.input.rstrip('/\\'))
    os.makedirs(args.out_dir, exist_ok=True)

    # 收集章节: (ch_no, ch_num, ch_title, fname, text)
    chapters = []
    if os.path.isdir(args.input):
        files = sorted(glob.glob(os.path.join(args.input, '*.md')) + glob.glob(os.path.join(args.input, '*.txt')))
        print(f"发现章节文件: {len(files)}")
        for fi, fpath in enumerate(files, 1):
            fname = os.path.basename(fpath)
            ch_no, ch_num, ch_title = chapter_meta(fname)
            if ch_no is None:
                ch_no = fi
            with open(fpath, encoding='utf-8') as f:
                chapters.append((ch_no, ch_num, ch_title, fname, f.read()))
    else:
        with open(args.input, encoding='utf-8') as f:
            text = f.read()
        for ch_no, ch_num, ch_title, body in split_single_file(text):
            label = ch_title or (f"第{ch_num}章" if ch_num else "全文")
            fname = f"{ch_no:04d}_{label}.md"
            chapters.append((ch_no, ch_num if (ch_title or ch_num) else None, ch_title, fname, body))
        print(f"单文件输入: 切分为 {len(chapters)} 章")

    all_units, all_scenes, per_chapter = [], [], []
    for fi, (ch_no, ch_num, ch_title, fname, text) in enumerate(chapters, 1):
        full_text, units = de.extract_file_text(fname, text)
        for u in units:
            u['chapter'] = ch_no
            u['chapter_label'] = f"第{ch_num}章" if ch_num else ''
            u['title'] = ch_title
            u['file'] = fname
            all_units.extend([u])
        scenes = build_scenes(units, fname, ch_no, ch_num, ch_title, full_text)
        all_scenes.extend(scenes)
        per_chapter.append({'chapter': ch_no, 'label': f"第{ch_num}章" if ch_num else '',
                            'title': ch_title, 'file': fname, 'units': len(units), 'scenes': len(scenes)})
        if fi % 100 == 0:
            print(f"  已处理 {fi}/{len(chapters)} 章，累计对话 {len(all_units)} 句 / 场景 {len(all_scenes)}")
    print(f"完成。总对话句数: {len(all_units)}，总场景数: {len(all_scenes)}")

    with open(os.path.join(args.out_dir, 'dialogue_units.json'), 'w', encoding='utf-8') as f:
        json.dump(all_units, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out_dir, 'dialogue_scenes.json'), 'w', encoding='utf-8') as f:
        json.dump(all_scenes, f, ensure_ascii=False, indent=1)
    write_scene_md(all_scenes, per_chapter, args.out_dir, title)
    write_features_md(all_units, all_scenes, per_chapter, args.out_dir, title)
    print("已写出 dialogue_units.json / dialogue_scenes.json / 对话场景清单.md / 对话特点归纳.md")

def write_scene_md(scenes, per_chapter, out_dir, title):
    lines = [f"# 《{title}》对话场景清单\n"]
    lines.append(f"> 自动提取（共 {len(per_chapter)} 章），对话单元 {sum(s['turns'] for s in scenes)} 句，聚合场景 {len(scenes)} 个。\n")
    lines.append("---")
    for s in scenes:
        lines.append(f"\n## {s['scene_id']} ｜ {s['chapter_label']}《{s['title']}》\n")
        lines.append(f"- **位置**：第 {s['start_line']}–{s['end_line']} 行")
        lines.append(f"- **参与角色**：{('、'.join(s['participants']) if s['participants'] else '（未识别/独白）')}")
        lines.append(f"- **对话轮数**：{s['turns']} ｜ **特征**：{('、'.join(s['tags']) if s['tags'] else '—')}")
        lines.append("- **摘要**：")
        for ex in s['excerpt']:
            lines.append(f"  - {ex}")
    with open(os.path.join(out_dir, '对话场景清单.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

def write_features_md(units, scenes, per_chapter, out_dir, title):
    from collections import Counter
    N = len(units)
    M = {'N': N}
    tc = Counter(u['type'] for u in units)
    mc = Counter(u['mode'] for u in units)
    n_ell = sum(1 for u in units if u['ellipsis'])
    n_rth = sum(1 for u in units if u['rhetorical'])
    n_sym = sum(1 for u in units if u['symbolic'])
    n_adv = sum(1 for u in units if u['adversarial'])
    n_avo = sum(1 for u in units if u['avoidance'])
    lengths = [u['length'] for u in units]
    short_rate = sum(1 for L in lengths if L <= 10) / N if N else 0
    avg_len = sum(lengths)/N if N else 0
    if lengths:
        mx = max(lengths); buckets = 10
        hist = [0]*(buckets+1)
        for L in lengths:
            hist[min(L*buckets//(mx+1), buckets)] += 1
        glob_ent = -sum((h/N)*math.log2(h/N) for h in hist if h > 0) if N else 0
    else:
        glob_ent = 0
    n_sc = len(scenes)
    avg_switch = sum(s['switch_rate'] for s in scenes)/n_sc if n_sc else 0
    avg_scell = sum(s['ellipsis_rate'] for s in scenes)/n_sc if n_sc else 0
    avg_sten = sum(s['tension_mean'] for s in scenes)/n_sc if n_sc else 0

    lines = [f"# 《{title}》对话特点归纳（六维能力指标版）\n"]
    lines.append(f"> 基于 {N} 句对话、{n_sc} 个对话场景的量化统计。\n")
    lines.append(">\n> **口径与限制**：仅提取中文弯引号" "配对的对话；说话人代词回指桶为作者续接手法或识别兜底，占比见下。对抗词典分为【未验证代理】，非张力度量。\n")

    lines.append("\n## 一、话轮类型分布\n")
    for k in ('陈述', '疑问', '感叹'):
        lines.append(f"- {k}：{tc.get(k,0)}（{tc.get(k,0)*100//max(1,N)}%）")

    lines.append("\n## 二、说话人识别模式\n")
    for k, v in mc.most_common():
        lines.append(f"- {k}：{v}")

    sp_cnt = Counter(u['speaker'] for u in units if not u['speaker'].startswith('（'))
    lines.append("\n## 三、高频说话人 Top30（标注称谓属性）\n")
    for sp, c in sp_cnt.most_common(30):
        sym = '符号化' if de.is_symbolic_speaker(sp) else '本名'
        lines.append(f"- {sp}（{sym}）：{c} 句")

    lines.append("\n## 四、六维能力指标基线（L1/L2 实测）\n")
    lines.append("| 维度 | 指标 | 数值 | 用途 |")
    lines.append("|------|------|------|------|")
    lines.append(f"| 潜台词 | 省略号率 | {n_ell/N*100:.1f}% | 留白潜台词代理 |")
    lines.append(f"| 潜台词 | 反问密度 | {n_rth/N*100:.1f}% | 质询/反诘代理 |")
    lines.append(f"| 张力 | 对抗词汇占比（未验证词典代理） | {n_adv/N*100:.1f}% | 仅存在性标记 |")
    lines.append(f"| 经济性 | 短句率（≤10字） | {short_rate*100:.1f}% | 字均承载 |")
    lines.append(f"| 节奏 | 平均句长 | {avg_len:.1f} 字 | 呼吸感 |")
    lines.append(f"| 节奏 | 句长分布熵 burstiness | {glob_ent:.3f} | 节奏均匀度 |")
    lines.append(f"| 称谓 | 符号化率 | {n_sym/N*100:.1f}% | 声口模糊/AI腔反向指标 |")
    lines.append(f"| 群像 | 场景切换率均值 TSC | {avg_switch:.3f} | 轮转密度 |")
    lines.append(f"| 潜台词 | 回避率 | {n_avo/N*100:.1f}% | 未正面作答代理 |")

    tagc = Counter()
    for s in scenes:
        for t in s['tags']:
            tagc[t] += 1
    lines.append("\n## 五、场景特征标签分布\n")
    for t, c in tagc.most_common():
        lines.append(f"- {t}：{c} 个场景")

    lines.append("\n## 六、章节对话密度 Top30\n")
    for p in sorted(per_chapter, key=lambda x: -x['units'])[:30]:
        lines.append(f"- {p['label'] or p['file']}《{p['title']}》：{p['units']} 句 / {p['scenes']} 场景")

    lines.append("\n## 七、对话写作特点归纳\n")
    q_pct = tc.get('疑问', 0) * 100 // max(1, N)
    lines.append(f"1. **对话即推演**：疑问占 {q_pct}%，反问密度 {n_rth/N*100:.1f}%。")
    lines.append(f"2. **白描驱动**：平均句长 {avg_len:.1f} 字、短句率 {short_rate*100:.1f}%。")
    lines.append(f"3. **潜台词与留白**：省略号率 {n_ell/N*100:.1f}%、回避率 {n_avo/N*100:.1f}%。")
    lines.append(f"4. **称谓符号化**：符号化率 {n_sym/N*100:.1f}%。")
    lines.append(f"5. **同说话人续接**：代词+续接占识别模式约 {(mc.get('代词',0)+mc.get('续接',0))*100//max(1,N)}%。")
    lines.append(f"6. **群像轮转**：场景切换率均值 {avg_switch:.3f}。")
    with open(os.path.join(out_dir, '对话特点归纳.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

if __name__ == '__main__':
    main()
