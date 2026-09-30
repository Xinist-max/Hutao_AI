#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析每次调用喂给本地模型的提示词构成。

本地模型的瓶颈不是算力而是**提示词长度**：每轮都要重新预填充整个提示词，
长度直接决定延迟与内存。这个工具把最近一次调用的提示词拆开，
看清大头在哪，避免靠感觉乱砍。

    python3 tools/prompt_profile.py            # 分析最近的调用
    python3 tools/prompt_profile.py --top 15
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import collections

AGENTS_DIR = os.path.expanduser("~/.openclaw/agents")


def _as_text(v) -> str:
    """systemPrompt 有时是 str，有时是分块数组——统一成文本。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        out = []
        for blk in v:
            if isinstance(blk, str):
                out.append(blk)
            elif isinstance(blk, dict):
                out.append(blk.get("text") or "")
        return "\n".join(out)
    return ""


def latest_compiled(agent: str = "main") -> dict:
    """取最近一次 context.compiled 事件。"""
    best = None
    for p in glob.glob(os.path.join(AGENTS_DIR, agent, "sessions", "*.trajectory.jsonl")):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if best and mtime < best[0]:
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"context.compiled"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "context.compiled":
                        best = (mtime, d)
        except OSError:
            continue
    return best[1] if best else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="分析喂给本地模型的提示词构成")
    ap.add_argument("--agent", default="main")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    ev = latest_compiled(args.agent)
    if not ev:
        print("没找到 context.compiled 事件")
        return 1
    data = ev.get("data") or {}
    sp = _as_text(data.get("systemPrompt"))
    prompt = _as_text(data.get("prompt"))

    print(f"模型 {ev.get('provider')}/{ev.get('modelId')}")
    print(f"系统提示词 {len(sp)} 字符　用户提示词 {len(prompt)} 字符")
    print(f"合计约 {len(sp) + len(prompt)} 字符（中文约 1 字符≈0.6 token）\n")

    # 按 ## 分节统计系统提示词
    sections = re.split(r"\n(?=#{1,3} )", sp)
    counted = collections.Counter()
    for x in sections:
        head = x.split("\n", 1)[0].lstrip("# ").strip()[:46] or "(开头)"
        counted[head] += len(x)
    print(f"系统提示词分节 Top {args.top}：")
    for head, n in counted.most_common(args.top):
        bar = "█" * max(1, int(n / max(1, max(counted.values())) * 28))
        print(f"  {n:>6} 字  {bar}  {head}")

    # 工具定义单独算：通常以 <tool> / ### 工具名 或 JSON schema 形式出现
    tool_hits = re.findall(r"(<tool[^>]*>.*?</tool>|\"name\":\s*\"[a-z_]+\")", sp, re.S)
    if tool_hits:
        total_tool_chars = sum(len(h) for h in tool_hits)
        print(f"\n疑似工具定义 {len(tool_hits)} 处，约 {total_tool_chars} 字符")

    print(f"\n结论：大头是 "
          f"{'工具定义' if tool_hits and total_tool_chars > len(sp) * 0.3 else '人设/上下文分节'}"
          f"——优先从这里砍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
