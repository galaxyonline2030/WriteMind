# -*- coding: utf-8 -*-
"""
对话语料库构建 · 步骤2.5：语料质量抽检
用法:
  python corpus_qa.py --units <dialogue_units.json> [--top 40] [--pronoun-sample 50]
检查项:
  1) top-speakers : Top-N 说话人频次 + 称谓符号化判定 (识别质量总览)
  2) pronoun-bucket: "（她·回指）"类代词桶抽样验证 — 检查同场景前驱是否有真实
     说话人名, 判定是"作者同说话人续接手法"(合理) 还是"说话人识别失败"(可疑)
仅依赖标准库。
"""
import json, os, random, argparse
from collections import Counter
import dialog_extract as de

def qa_top_speakers(units, top):
    u = [x for x in units if not x['speaker'].startswith('（')]
    spc = Counter(x['speaker'] for x in u)
    print(f'=== Top{top} 说话人 + 称谓符号化判定 (有效说话人 {len(spc)}) ===')
    for s, c in spc.most_common(top):
        print('  %-16s %6d  sym=%s' % (s, c, de.is_symbolic_speaker(s)))

def qa_pronoun_bucket(units, n_sample):
    N = len(units)
    pro_idx = [i for i, x in enumerate(units) if ('代词' in x['mode']) or ('回指' in x['mode'])]
    print(f'\n=== 代词桶检查 ===')
    print(f'代词桶总数: {len(pro_idx)} (占全体 {100*len(pro_idx)/N:.1f}%)' if N else '无单元')
    if not pro_idx:
        return
    random.seed(42)
    sample = random.sample(pro_idx, min(n_sample, len(pro_idx)))
    ok = susp = 0
    for idx in sample:
        x = units[idx]
        prev = None
        for j in range(idx-1, -1, -1):
            if units[j]['chapter'] != x['chapter']:
                break
            if not units[j]['speaker'].startswith('（'):
                prev = units[j]['speaker']
                break
        if prev:
            ok += 1
        else:
            susp += 1
            if susp <= 10:
                print(f'  [可疑] 行{x["line"]} 「{x["speaker"]}」 {x["content"][:36]}')
    print(f'抽样 {len(sample)}: 合理续接 {ok} / 可疑 {susp}')
    if susp > len(sample) * 0.3:
        print('⚠ 可疑比例 >30%: 建议检查说话人识别阈值或语料引号格式')
    else:
        print('✓ 代词桶主要为"同说话人续接"手法, 识别质量可接受')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='语料质量抽检')
    ap.add_argument('--units', required=True, help='dialogue_units.json 路径')
    ap.add_argument('--top', type=int, default=40)
    ap.add_argument('--pronoun-sample', type=int, default=50)
    args = ap.parse_args()
    units = json.load(open(args.units, encoding='utf-8'))
    qa_top_speakers(units, args.top)
    qa_pronoun_bucket(units, args.pronoun_sample)
