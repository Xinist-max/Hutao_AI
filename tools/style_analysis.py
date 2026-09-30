#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说话风格体检：**只输出数字**，不打印任何原文。

## 隐私约定

默认模式下本脚本**不输出任何对话内容**——没有片段、没有例句、没有 n-gram 文本，
只有计数、占比与分布指标。这些数字进模型上下文没有敏感性。

原文只有在你**自己**加 `--show-text` 时才会打印到你的终端；
那个开关的存在是为了让你自己核对，**不是给模型看的**。

本脚本不写任何文件、不落盘。

## 为什么"只看数字"就够

"模板化"的本质是**分布**问题，不是样本问题：人读三段会觉得"还好"，
但统计上可能 60% 的段落都以动作提示开头。判断与验证修复效果都只需要数字。

输出七块（全部为数值）：
  ① 规模      —— 回复数、段落数、平均段落数
  ② 开头集中度 —— 不同开头比例、最高占比、HHI
  ③ 动作提示   —— 总量、不同种类、集中度、以动作开头的段落占比
  ④ 片段重复率 —— 有多少段落含有"在别处也反复出现"的片段
  ⑤ 结构形态   —— 段落数分布、问句占比、结尾标点分布、长度分位
  ⑥ 段落相似度 —— 相邻段落 4-gram Jaccard 均值（越高越像同一套模板）
  ⑦ 时序列     —— 按月/周的集中度变化，判断是"一直如此"还是"最近变差"

用法：
    python3 tools/style_analysis.py                    # 纯数字（默认）
    python3 tools/style_analysis.py --trend            # 只看时间趋势
    python3 tools/style_analysis.py --show-text        # ⚠️ 自己看原文时才用
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

ACTION_RE = re.compile(r"[（(]([^）)]{1,24})[）)]")
INJECT_MARK = "[璃月港事件]"


def her_replies(agent: str, min_replies: int = 10) -> list:
    """返回 [(时间戳字符串, 回复文本)]，只取 assistant、跳过引擎注入的轮次。

    ⚠️ `min_replies` 是**必须的语料过滤器**，不是可选项。实测：不过滤时会把
    同一目录下几十个**测试会话**（`authprobe` / `fmt-*` / `base1-3` … 每个 1~5 条）
    全算进来，于是一批不含中文的噪声段落污染整份统计——
    "最高的那一种开头占 18%" 全是被污染出来的假信号，过滤后真实值只有 3%。
    真实对话（微信直聊、会话合并前的历史、liyue-scene 按天会话）都 ≥10 条。
    """
    out = []
    pat = os.path.expanduser(f"~/.openclaw/agents/{agent}/sessions/*.jsonl")
    for path in glob.glob(pat):
        if path.endswith(".trajectory.jsonl"):
            continue
        got = []
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"role": "assistant"' not in line and '"role":"assistant"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = rec.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content")
                    if isinstance(content, list):
                        content = "\n".join(str(c.get("text", "")) for c in content
                                            if isinstance(c, dict) and c.get("type") == "text")
                    text = str(content or "").strip()
                    if text and INJECT_MARK not in text:
                        got.append((str(rec.get("timestamp") or ""), text))
        except OSError:
            continue
        if len(got) >= min_replies:
            out += got
    return out


def paragraphs(replies: list) -> list:
    """拆段落，并滤掉**不含汉字**的段——实测有 28% 的"段落"是会话里的
    非回复内容（工具输出、标识符等），它们会把开头集中度算得虚高好几倍。"""
    out = []
    for _ts, r in replies:
        for p in re.split(r"\n\s*\n", r):
            p = p.strip()
            if p and re.search(r"[\u4e00-\u9fff]", p):
                out.append(p)
    return out


def hhi(counter) -> float:
    """赫芬达尔指数：0=完全分散，1=全挤在同一项。用来量化"就那几套"。"""
    total = sum(counter.values())
    if not total:
        return 0.0
    return sum((c / total) ** 2 for c in counter.values())


def sim(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def grams_of(text: str, n: int = 4) -> set:
    clean = re.sub(r"[^\u4e00-\u9fff]", "", ACTION_RE.sub("", text))
    return {clean[i:i+n] for i in range(max(0, len(clean) - n + 1))}


def main() -> None:
    ap = argparse.ArgumentParser(description="说话风格体检（默认只输出数字）")
    ap.add_argument("--agent", default="main")
    ap.add_argument("--prefix", type=int, default=4)
    ap.add_argument("--show-text", action="store_true",
                    help="⚠️ 打印原文片段——只给自己看，别贴给模型")
    ap.add_argument("--trend", action="store_true", help="只看时间趋势")
    ap.add_argument("--min-replies", type=int, default=10,
                    help="只统计回复数≥N 的会话（默认10，用来排除测试会话）")
    ap.add_argument("--tail", type=int, default=0,
                    help="只看最近 N 条（0=全部）")
    args = ap.parse_args()

    replies = her_replies(args.agent, min_replies=args.min_replies)
    if args.tail:
        replies = replies[-args.tail:]
    if not replies:
        print("没有读到她的回复（检查 agent 名与会话目录）")
        return
    paras = paragraphs(replies)

    # ⑦ 时间趋势（先算，--trend 时直接输出）
    if args.trend:
        buckets = collections.defaultdict(list)
        for ts, r in replies:
            buckets[ts[:7] or "未知"].append(r)
        print("时间趋势（按年月）：")
        print(f"{'月份':10s} {'回复数':>6s} {'段落数':>6s} {'开头种类':>8s} {'最高开头占比':>12s}")
        for k in sorted(buckets):
            ps = paragraphs([(k, r) for r in buckets[k]])
            if not ps:
                continue
            heads = collections.Counter(p[:args.prefix] for p in ps)
            top_share = heads.most_common(1)[0][1] / len(ps)
            print(f"{k:10s} {len(buckets[k]):6d} {len(ps):6d} {len(heads):8d} {top_share:11.0%}")
        return

    print(f"① 规模")
    print(f"   她的回复        {len(replies)} 条")
    print(f"   拆出段落        {len(paras)} 段")
    print(f"   平均每条        {len(paras)/max(len(replies),1):.1f} 段")

    # ② 开头集中度
    heads = collections.Counter(p[:args.prefix] for p in paras)
    top1 = heads.most_common(1)[0][1] / len(paras)
    multi = sum(c for _h, c in heads.items() if c >= 5) / len(paras)
    multi3 = sum(c for _h, c in heads.items() if c >= 3) / len(paras)
    print(f"\n② 段落开头集中度（前 {args.prefix} 字）")
    print(f"   不同开头        {len(heads)} 种 / {len(paras)} 段"
          f"（多样性 {len(heads)/len(paras):.2f}，1.0=全不一样）")
    print(f"   最高那一种占    {top1:.0%}")
    print(f"   出现≥5 次的合计占 {multi:.0%}")
    print(f"   出现≥3 次的合计占 {multi3:.0%}")
    print(f"   集中度 HHI      {hhi(heads):.4f}（越高越挤在少数几种）")

    # ③ 动作提示
    acts = collections.Counter(a.strip() for p in paras for a in ACTION_RE.findall(p))
    lead = sum(1 for p in paras if ACTION_RE.match(p)) / len(paras)
    print(f"\n③ 动作提示（括号动作）")
    print(f"   总量            {sum(acts.values())} 处")
    print(f"   不同种类        {len(acts)} 种"
          f"（多样性 {len(acts)/max(sum(acts.values()),1):.2f}）")
    if acts:
        print(f"   最高那一种占    {acts.most_common(1)[0][1]/sum(acts.values()):.0%}")
        print(f"   集中度 HHI      {hhi(acts):.4f}")
        print(f"   出现≥5 次的合计占 {sum(c for _a,c in acts.items() if c>=5)/sum(acts.values()):.0%}")
    print(f"   以动作开头的段落占 {lead:.0%}")

    # ④ 片段重复率
    grams = collections.Counter()
    gsets = []
    for p in paras:
        g = grams_of(p)
        gsets.append(g)
        for x in g:
            grams[x] += 1
    rep_grams = {g for g, c in grams.items() if c >= 5}
    para_with_rep = sum(1 for gs in gsets if gs & rep_grams) / len(paras)
    print(f"\n④ 片段重复率（4 字片段）")
    print(f"   不同片段        {len(grams)} 种")
    print(f"   出现≥5 次的片段 {len(rep_grams)} 种"
          f"（占全部片段 {len(rep_grams)/max(len(grams),1):.1%}）")
    print(f"   含这类片段的段落占 {para_with_rep:.0%}")

    # ⑤ 结构形态
    n_para = collections.Counter(len(re.split(r"\n\s*\n", r)) for _ts, r in replies)
    q = sum(1 for p in paras if p.rstrip().endswith(("？", "?"))) / len(paras)
    tail = collections.Counter(p.rstrip()[-1] for p in paras if p.rstrip())
    lens = sorted(len(re.sub(r"\s", "", p)) for p in paras)
    print(f"\n⑤ 结构形态")
    print("   每条段落数分布  " + "、".join(f"{k}段×{v}" for k, v in sorted(n_para.items())[:8]))
    print(f"   问号结尾段落占  {q:.0%}")
    print("   结尾标点分布    " + "、".join(f"{k}×{v}" for k, v in tail.most_common(6)))
    if lens:
        print(f"   段落长度        中位 {lens[len(lens)//2]} 字"
              f"、p10={lens[len(lens)//10]}、p90={lens[len(lens)*9//10]}")

    # ⑥ 段落相似度
    consec = [sim(gsets[i], gsets[i+1]) for i in range(len(gsets)-1)]
    sampled = [sim(gsets[i], gsets[j]) for i in range(0, len(gsets), 5)
               for j in range(i+5, len(gsets), 7)]
    avg_c = sum(consec)/max(len(consec),1)
    avg_s = sum(sampled)/max(len(sampled),1)
    print(f"\n⑥ 段落相似度（4-gram Jaccard，越高越像同一套）")
    print(f"   相邻段落均值    {avg_c:.3f}")
    print(f"   随机抽样均值    {avg_s:.3f}（这个是关键：远高于 0.05 就说明复用在跨段落发生）")

    if args.show_text:
        print("\n" + "=" * 60)
        print("⚠️ 以下为原文片段，仅供你自己核对，请勿贴给模型")
        print("=" * 60)
        print("\n出现最多的段落开头：")
        for h, c in heads.most_common(10):
            print(f"   {c:4d}× {h}")
        print("\n出现最多的动作提示：")
        for a, c in acts.most_common(10):
            print(f"   {c:4d}× （{a}）")


if __name__ == "__main__":
    main()
