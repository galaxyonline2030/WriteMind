# -*- coding: utf-8 -*-
"""
对话语料库构建 · 步骤5：把新语料整合进已有语料库
用法:
  python merge_corpus.py --corpus-dir <已有语料库目录> --scenes <新书dialogue_scenes.json> \
      --source <新书名> [--old-scenes <旧书dialogue_scenes.json>] [--max-samples 8] [--cap 32]
两种模式:
  A. 场景级 (提供 --old-scenes, 推荐): 新旧场景合并后整体重聚类, 簇/签名可能重排;
     旧簇文件与索引备份到 corpus_dir/backup_<时间戳>/, 模式卡需按新旧签名映射人工核对。
  B. 样本级 (默认): 保留旧簇不动, 把新书的合格场景按签名归入对应簇 (补抽样),
     新签名则新建簇; 旧簇文件与索引同样先备份。
输出:
  - 更新后的 _cluster_XX.json / _cluster_index.json (样本带 source 来源标记)
  - merge_report.json 合并报告 (各签名新增量 / 新建簇 / 备份位置)
"""
import json, os, glob, shutil, argparse
from collections import Counter, defaultdict
from datetime import datetime
import pattern_cluster as pc


def eligible(scenes):
    return [s for s in scenes if s.get('turns', 0) >= 4 and len(set(s.get('participants', []))) >= 2]

def sig_of(s):
    return f"P{pc.pc_band(s['participants'])}T{pc.turn_band(s['turns'])}S{pc.sw_band(s['switch_rate'])}"

def diverse_sample(members, n):
    ms = sorted(members, key=lambda x: x['turns'])
    step = max(1, len(ms) // max(1, n))
    return ms[::step][:n]

def sample_payload(s):
    return {'scene_id': s['scene_id'], 'source': s.get('source', '?'),
            'title': s.get('title', ''),
            'participants': s.get('participants', []), 'tags': s.get('tags', []),
            'turns': s['turns'], 'switch_rate': s['switch_rate'],
            'ellipsis_rate': s['ellipsis_rate'], 'excerpt': s.get('excerpt', [])}

def backup_corpus(corpus_dir):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    bdir = os.path.join(corpus_dir, f'backup_{ts}')
    os.makedirs(bdir, exist_ok=True)
    n = 0
    for f in glob.glob(os.path.join(corpus_dir, '_cluster_*.json')):
        shutil.copy2(f, bdir)
        n += 1
    print(f"已备份 {n} 个簇文件 → {bdir}")
    return bdir

def load_index(corpus_dir):
    p = os.path.join(corpus_dir, '_cluster_index.json')
    if os.path.exists(p):
        return json.load(open(p, encoding='utf-8')), True
    return [], False

def scene_level(args):
    old_scenes, _ = pc.load_scenes(args.old_scenes)
    new_scenes, _ = pc.load_scenes(f"{args.scenes}:{args.source}")
    combined = old_scenes + new_scenes
    tmp = os.path.join(args.corpus_dir, '_merge_tmp')
    os.makedirs(tmp, exist_ok=True)
    json.dump(combined, open(os.path.join(tmp, 'combined.json'), 'w', encoding='utf-8'),
              ensure_ascii=False)
    backup_corpus(args.corpus_dir)
    # 复用 pattern_cluster 主逻辑整体重聚类
    import sys
    old_argv = sys.argv
    sys.argv = ['pattern_cluster.py', '--scenes', os.path.join(tmp, 'combined.json'),
                '--out-dir', args.corpus_dir, '--samples-per-cluster', str(args.samples_per_cluster)]
    try:
        pc.main()
    finally:
        sys.argv = old_argv
    shutil.rmtree(tmp, ignore_errors=True)
    report = {'mode': 'scene-level', 'old_scenes': len(old_scenes), 'new_scenes': len(new_scenes),
              'source': args.source,
              'note': '簇已整体重聚类; 模式卡与签名的对应关系需人工核对后再更新卡文件。'}
    json.dump(report, open(os.path.join(args.corpus_dir, 'merge_report.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    return report

def normalize_legacy(corpus_dir, index, legacy_source):
    """旧簇样本可能没有 source 字段（旧版语料）：回填 legacy_source，
    并把旧簇全量场景记入 source_counts[legacy_source]，保证合并后来源可追溯。"""
    for e in index:
        cf = os.path.join(corpus_dir, os.path.basename(e['sample_file']))
        if not os.path.isfile(cf):
            continue
        cl = json.load(open(cf, encoding='utf-8'))
        changed = False
        for sm in cl.get('samples', []):
            if 'source' not in sm:
                sm['source'] = legacy_source
                changed = True
        sc = Counter(cl.get('source_counts', {}))
        if legacy_source not in sc:
            sc[legacy_source] = cl.get('size', e['size'])
            changed = True
        if changed:
            cl['source_counts'] = dict(sc)
            json.dump(cl, open(cf, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        e['source_counts'] = dict(sc)

def sample_level(args):
    new_scenes, _ = pc.load_scenes(f"{args.scenes}:{args.source}")
    elig = eligible(new_scenes)
    print(f"新书合格场景: {len(elig)} / {len(new_scenes)}")
    by_sig = defaultdict(list)
    for s in elig:
        by_sig[sig_of(s)].append(s)

    backup = backup_corpus(args.corpus_dir)
    index, has_index = load_index(args.corpus_dir)
    sig2entry = {e['signature']: e for e in index}
    normalize_legacy(args.corpus_dir, index, args.legacy_source)

    added = Counter()
    new_clusters = []
    for sig, members in sorted(by_sig.items(), key=lambda kv: -len(kv[1])):
        if sig in sig2entry:
            e = sig2entry[sig]
            # 始终按 corpus-dir 解析簇文件（旧索引可能存过绝对路径，强制归一化，杜绝外部引用）
            cfile = os.path.join(args.corpus_dir, os.path.basename(e['sample_file']))
            cluster = json.load(open(cfile, encoding='utf-8'))
            have = {sm['scene_id'] for sm in cluster['samples']}
            picks = [s for s in diverse_sample(members, args.max_samples) if s['scene_id'] not in have]
            cluster['samples'] = (cluster['samples'] + [sample_payload(s) for s in picks])[:args.cap]
            cluster['size'] = cluster.get('size', e['size']) + len(members)
            sc = Counter(cluster.get('source_counts', {})); sc[args.source] += len(members)
            cluster['source_counts'] = dict(sc)
            json.dump(cluster, open(cfile, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            e['size'] = cluster['size']
            esc = Counter(e.get('source_counts', {})); esc[args.source] += len(members)
            e['source_counts'] = dict(esc)
            e.setdefault('avg_note', '场景级均值未随合并重算, 仅为旧语料近似')
        else:
            cid = (max((x['cluster_id'] for x in index), default=0)) + 1
            cfile = os.path.join(args.corpus_dir, f'_cluster_{cid:02d}.json')
            cluster = {'cluster_id': cid, 'signature': sig, 'size': len(members),
                       'dominant_tags': [t for t, _ in Counter(
                           t for s in members for t in s.get('tags', [])).most_common(4)],
                       'source_counts': {args.source: len(members)},
                       'samples': [sample_payload(s) for s in diverse_sample(members, args.max_samples)]}
            json.dump(cluster, open(cfile, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            index.append({'cluster_id': cid, 'signature': sig, 'size': len(members),
                          'dominant_tags': cluster['dominant_tags'], 'sample_file': cfile,
                          'source_counts': cluster['source_counts'],
                          'avg_turns': round(sum(s['turns'] for s in members)/len(members), 1),
                          'avg_switch': round(sum(s['switch_rate'] for s in members)/len(members), 3),
                          'avg_ellipsis': round(sum(s['ellipsis_rate'] for s in members)/len(members), 3)})
            new_clusters.append({'cluster_id': cid, 'signature': sig, 'size': len(members)})
        added[sig] += len(members)

    # 保持 cluster_id 与文件名一致（不重编号），仅按规模排序展示
    index.sort(key=lambda x: -x['size'])
    json.dump(index, open(os.path.join(args.corpus_dir, '_cluster_index.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    report = {'mode': 'sample-level', 'source': args.source,
              'backup_dir': backup,
              'added_by_signature': dict(added),
              'new_clusters': new_clusters,
              'total_index': len(index)}
    json.dump(report, open(os.path.join(args.corpus_dir, 'merge_report.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f"\n各签名新增: {dict(added)}")
    if new_clusters:
        print(f"新建簇: {[(c['cluster_id'], c['signature']) for c in new_clusters]}")
    print(f"合并报告 → {os.path.join(args.corpus_dir, 'merge_report.json')}")
    return report

def main():
    ap = argparse.ArgumentParser(description='新语料并入已有语料库')
    ap.add_argument('--corpus-dir', required=True, help='已有语料库目录 (含 _cluster_XX.json)')
    ap.add_argument('--scenes', required=True, help='新书 dialogue_scenes.json')
    ap.add_argument('--source', required=True, help='新书来源名 (写入样本 source 字段)')
    ap.add_argument('--old-scenes', default=None, help='旧书 dialogue_scenes.json (提供则走场景级重聚类)')
    ap.add_argument('--max-samples', type=int, default=8, help='每签名补充抽样上限 (样本级模式)')
    ap.add_argument('--cap', type=int, default=32, help='每簇样本总数上限')
    ap.add_argument('--samples-per-cluster', type=int, default=16, help='场景级重聚类每簇抽样上限')
    ap.add_argument('--legacy-source', default='legacy', help='旧簇无 source 样本的回填来源名')
    args = ap.parse_args()

    if not os.path.isdir(args.corpus_dir):
        raise SystemExit(f'语料库目录不存在: {args.corpus_dir}')
    if args.old_scenes:
        scene_level(args)
    else:
        sample_level(args)

if __name__ == '__main__':
    main()
