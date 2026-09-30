#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原神最新资讯 —— 抓取、查看、翻译成世界内事件。

数据源：米游社官方公告 API（CN 服，无需鉴权）。
核心逻辑在 `scene/news.py`，本文件只是命令行外壳。

用法：
    python3 tools/genshin_news.py                # 拉最新资讯并显示
    python3 tools/genshin_news.py --force        # 忽略缓存，强制重拉
    python3 tools/genshin_news.py --events       # 只看翻译出来的世界内事件
    python3 tools/genshin_news.py --events -v    # 连注入给她的 instruction 一起看
    python3 tools/genshin_news.py --offline      # 只读缓存，不联网
    python3 tools/genshin_news.py --json         # 输出原始 JSON
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from scene import news as N  # noqa: E402

CACHE = os.path.join(ROOT, "logs", "genshin_news.json")


def show_news(n: dict) -> None:
    src = n.get("source", "?")
    flag = {"api": "🌐 官方 API", "cache": "💾 缓存", "none": "❌ 无数据"}.get(src, src)
    extra = ""
    if n.get("stale"):
        extra = f"  ⚠️ 缓存已过期（{n.get('error', '拉取失败')}）"
    print(f"数据来源：{flag}　拉取于 {n.get('fetched_at') or '—'}　公告 {n.get('ann_count', 0)} 条{extra}")
    print()

    ver = n.get("version") or {}
    if ver:
        print(f"📖 版本　　{ver.get('no')}「{ver.get('name')}」")
    if n.get("anniversary"):
        print(f"🎉 周年　　{n['anniversary']}周年")

    fest = n.get("festival") or {}
    if fest:
        who = "璃月本地的节" if fest.get("liyue") else "外地的节"
        print(f"🏮 节日　　{fest.get('name')}（{who}{'，按日期推导' if fest.get('approx') else ''}）")
    else:
        print("🏮 节日　　当前不在任何节庆窗口内")

    for b in n.get("banners") or []:
        chars = "、".join(f"{c.get('name','?')}（{c.get('element','?')}）" for c in b.get("characters") or [])
        print(f"🎴 角色池　「{b.get('pool','?')}」→ {chars or '、'.join(b.get('up') or [])}")
    for b in n.get("weapon_banners") or []:
        print(f"⚔️  武器池　「{b.get('pool','?')}」→ {'、'.join(b.get('up') or [])}")

    st = n.get("story") or {}
    if st:
        print(f"📜 剧情　　{st.get('kind')}「{st.get('chapter')}」")
    for q in n.get("world_quests") or []:
        print(f"🗺️  世界任务「{q}」")
    for r in n.get("regions") or []:
        print(f"🏔️  新区域　「{r}」")

    evs = n.get("events") or []
    if evs:
        print(f"🎪 活动　　{'、'.join(evs)}")


def show_events(evs, verbose: bool) -> None:
    if not evs:
        print("（没有生成任何事件——资讯为空，或全部进了缓存降级）")
        return
    print(f"翻译成 {len(evs)} 条世界内事件：\n")
    for e in evs:
        print(f"● [{e.priority}] {e.title}")
        print(f"    id={e.id}  概率={e.probability}  冷却={int(e.cooldown_minutes)}分钟  标签={e.tags}")
        print(f"    描述：{e.description}")
        if verbose:
            print(f"    指令：{e.instruction}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description="原神最新资讯 → 世界内事件")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重拉")
    ap.add_argument("--offline", action="store_true", help="只读缓存，不联网")
    ap.add_argument("--events", action="store_true", help="显示翻译出的世界内事件")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="事件模式下列出完整指令")
    args = ap.parse_args()

    if args.offline:
        if not os.path.exists(CACHE):
            print(f"缓存不存在：{CACHE}", file=sys.stderr)
            sys.exit(1)
        with open(CACHE, encoding="utf-8") as f:
            n = json.load(f)
        n["source"] = "cache"
        # stale 要按**真实年龄**判，不能无条件置 True。
        # 原来无条件 True，于是刚拉完的缓存也会显示"⚠️ 缓存已过期（拉取失败）"——
        # 明明是好的却报故障，比不报还糟。
        age_h = (time.time() - float(n.get("fetched_ts") or 0)) / 3600.0
        n["stale"] = age_h > 6.0
        if n["stale"]:
            n["error"] = f"缓存已 {age_h:.1f} 小时未更新"
    else:
        # cache_hours=0 配合 force，保证一定走网络
        n = N.fetch(CACHE, cache_hours=0 if args.force else 6, force=args.force)

    if args.json:
        print(json.dumps(n, ensure_ascii=False, indent=2))
        return

    if args.events:
        show_events(N.world_events(n), args.verbose)
        return

    show_news(n)
    print()
    print(f"（要看待翻译出的世界内事件：--events）")


if __name__ == "__main__":
    main()
