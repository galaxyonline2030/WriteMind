# -*- coding: utf-8 -*-
"""
对话语料库构建 · 步骤3：结构签名聚类 + 每簇采样落盘
方法论:
  - 聚类主键 = 结构签名 (说话人数 P × 轮数 T × 切换率 S)，不靠饱和 tag
  - 省略号率只作簇内描述符，不进聚类键
  - 原文只落盘到 per-cluster 文件，供隔离子代理读取，不进主上下文
用法:
  python pattern_cluster.py --scenes <dialogue_scenes.json> --out-dir <语料库目录> \
      [--source 书名] [--samples-per-cluster 16]
  # 整合多书: 传多个 --scenes（各配一个 --source，逗号对应）
输出:
  - _cluster_01..NN.json  每簇抽样 (含 source 来源标记)
  - _cluster_index.json   簇索引 (规模/均轮/均切换/均省略/主导tag/来源分布)
"""
import json, os, random, argparse
from collections import Counter, defaultdict

def pc_band(p):
    # 粗分：2 人(对决) vs 3+(群像)
    return 1 if len(set(p)) == 2 else 2
def turn_band(t):
    return 1 if t <= 6 else (2 if t <= 12 else 3)
def sw_band(s):
    return 1 if s < 0.5 else 2
def ell_band(e):
    return 1 if e < 0.2 else (2 if e < 0.4 else 3)

def load_scenes(spec):
    """spec = "path[:source]"（注意 Windows 盘符冒号：仅当冒号前是存在的文件时才视为来源标记）"""
    path, src = spec, None
    if ':' in spec:
        cand_p, cand_s = spec.rsplit(':', 1)
        if os.path.isfile(cand_p) and '/' not in cand_s and '\\' not in cand_s:
            path, src = cand_p, cand_s
    with open(path, encoding='utf-8') as f:
        scenes = json.load(f)
    if src is None:
        src = os.path.basename(os.path.dirname(path)) or 'src'
    for s in scenes:
        s.setdefault('source', src)
    return scenes, src

def main():
    ap = argparse.ArgumentParser(description='结构签名聚类')
    ap.add_argument('--scenes', required=True, nargs='+',
                    help='一个或多个 dialogue_scenes.json（可写 路径:来源名 标记来源）')
    ap.add_argument('--out-dir', required=True, help='语料库输出目录 (patterns/)')
    ap.add_argument('--samples-per-cluster', type=int, default=16)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    random.seed(args.seed)

    all_scenes = []
    for spec in args.scenes:
        scenes, src = load_scenes(spec)
        print(f"载入 [{src}]: {len(scenes)} 场景")
        all_scenes.extend(scenes)

    # 合格候选：多轮 + 多说话人
    elig = [s for s in all_scenes if s.get('turns', 0) >= 4 and len(set(s.get('participants', []))) >= 2]
    print(f"合格候选: {len(elig)} / {len(all_scenes)}")

    groups = defaultdict(list)
    for s in elig:
        sig = f"P{pc_band(s['participants'])}T{turn_band(s['turns'])}S{sw_band(s['switch_rate'])}"
        groups[sig].append(s)

    order = sorted(groups.keys(), key=lambda k: -len(groups[k]))
    index = []
    for i, sig in enumerate(order, 1):
        members = groups[sig]
        tagc, srcc = Counter(), Counter()
        for s in members:
            for t in s.get('tags', []):
                tagc[t] += 1
            srcc[s.get('source', '?')] += 1
        dom = [t for t, _ in tagc.most_common(4)]
        # 多样性采样：按 turns 排序后等距取
        members_sorted = sorted(members, key=lambda x: x['turns'])
        n = min(len(members_sorted), args.samples_per_cluster)
        step = max(1, len(members_sorted) // n)
        sampled = members_sorted[::step][:n]
        cfile = os.path.join(args.out_dir, f"_cluster_{i:02d}.json")
        payload = {
            'cluster_id': i, 'signature': sig, 'size': len(members),
            'dominant_tags': dom,
            'source_counts': dict(srcc),
            'samples': [
                {'scene_id': s['scene_id'], 'source': s.get('source', '?'),
                 'title': s.get('title', ''),
                 'participants': s.get('participants', []), 'tags': s.get('tags', []),
                 'turns': s['turns'], 'switch_rate': s['switch_rate'],
                 'ellipsis_rate': s['ellipsis_rate'], 'excerpt': s.get('excerpt', [])}
                for s in sampled
            ],
        }
        json.dump(payload, open(cfile, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        index.append({'cluster_id': i, 'signature': sig, 'size': len(members),
                      'dominant_tags': dom, 'sample_file': os.path.basename(cfile),
                      'source_counts': dict(srcc),
                      'avg_turns': round(sum(s['turns'] for s in members)/len(members), 1),
                      'avg_switch': round(sum(s['switch_rate'] for s in members)/len(members), 3),
                      'avg_ellipsis': round(sum(s['ellipsis_rate'] for s in members)/len(members), 3)})

    json.dump(index, open(os.path.join(args.out_dir, '_cluster_index.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    print(f"\n簇总数: {len(order)}")
    print(f"{'#':>2} {'签名':<10} {'规模':>6} {'均轮':>5} {'均切换':>6} {'均省略':>6}  来源  主导tag")
    for it in index:
        srcs = ','.join(f"{k}:{v}" for k, v in it['source_counts'].items())
        print(f"{it['cluster_id']:>2} {it['signature']:<10} {it['size']:>6} "
              f"{it['avg_turns']:>5} {it['avg_switch']:>6} {it['avg_ellipsis']:>6}  {srcs}  {','.join(it['dominant_tags'][:3])}")
    print(f"\n已落盘 {len(order)} 个 per-cluster 文件 + _cluster_index.json → {args.out_dir}")

if __name__ == '__main__':
    main()
