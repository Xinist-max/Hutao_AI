#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模型基准测试：速度、思维链是否开启、是否退化成重复。

用来对比不同本地模型/量化档次的**实际表现**，判断该选哪个参数量——
光看"参数量大"没用，M5 上是 MoE 还是稠密、量化到几位、有没有开思维链，
都会让同一台机器上的体验差好几倍。

    python3 tools/bench_local.py                    # 默认测 ollama/local
    python3 tools/bench_local.py --model local9b    # 对比旧模型
    python3 tools/bench_local.py --model local --think false
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:11434"

# 用真实场景里会出现的输入，而不是"你好"这种测不出问题的
CASES = [
    ("短问句", "你好"),
    ("字数约束", "用一句话说说海灯节，20字以内"),
    ("角色扮演", "你是胡桃，往生堂第七十七代堂主。请用一句话跟旅行者打招呼，自称本堂主。"),
    ("场面描写", "雪夜里的璃月港码头，成百上千盏天灯正在升空。用两三句话转述这个场面。"),
]


def gen(model: str, prompt: str, think: bool | None, num_predict: int = 400,
        timeout: int = 300) -> dict:
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": {"num_predict": num_predict}}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(f"{HOST}/api/generate",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    d["_wall"] = time.time() - t0
    return d


def repetition_ratio(text: str) -> float:
    """重复度：最长重复子串的占比（退化时会接近 1）。粗略但够用。"""
    t = re.sub(r"\s+", "", text or "")
    if len(t) < 20:
        return 0.0
    best = 0
    for n in range(4, min(30, len(t) // 3)):
        for i in range(len(t) - n * 2 + 1):
            if t.count(t[i:i + n]) >= 2:
                best = max(best, n)
                break
        else:
            continue
    return best / len(t)


def main() -> int:
    ap = argparse.ArgumentParser(description="本地模型基准测试")
    ap.add_argument("--model", default="local")
    ap.add_argument("--think", choices=["true", "false", "auto"], default="false")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()
    think = None if args.think == "auto" else (args.think == "true")

    results = []
    if not args.json:
        print(f"模型 {args.model}（think={args.think}）\n")
        print(f"{'用例':<10}{'耗时':>8}{'输出tok':>9}{'速度':>10}{'思维链':>8}{'重复度':>8}")
        print("-" * 60)
    for name, prompt in CASES:
        try:
            d = gen(args.model, prompt, think)
        except (urllib.error.URLError, TimeoutError) as e:
            if not args.json:
                print(f"{name:<10}  调用失败：{type(e).__name__}")
            results.append({"case": name, "error": str(e)})
            continue
        resp = (d.get("response") or "").strip()
        th = (d.get("thinking") or "").strip()
        n = d.get("eval_count") or 0
        dur = (d.get("eval_duration") or 0) / 1e9
        tps = n / dur if dur else 0
        rep = repetition_ratio(resp)
        results.append({"case": name, "seconds": round(d["_wall"], 1), "tokens": n,
                        "tps": round(tps, 1), "thinking_chars": len(th),
                        "repeat_ratio": round(rep, 3), "response": resp[:200]})
        if not args.json:
            flag = " ⚠️退化" if rep > 0.3 else ""
            print(f"{name:<10}{d['_wall']:>7.1f}s{n:>9}{tps:>8.1f}t/s{len(th):>8}{rep:>8.2f}{flag}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    ok = [r for r in results if "error" not in r]
    if ok:
        avg = sum(r["tps"] for r in ok) / len(ok)
        print(f"\n平均生成速度 {avg:.1f} tok/s")
        bad = [r for r in ok if r["repeat_ratio"] > 0.3]
        if bad:
            print(f"⚠️ {len(bad)} 个用例出现明显重复退化："
                  f"{'、'.join(r['case'] for r in bad)}")
            print("   低比特量化（Q2/IQ2）常见此问题——考虑提高量化档或关掉思维链")
        if any(r["thinking_chars"] > 50 for r in ok):
            print("⚠️ 部分用例产生了思维链——陪伴对话要的是直接说人话，建议关掉")
        print("\n示例输出：")
        for r in ok[:2]:
            print(f"  [{r['case']}] {r['response'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
