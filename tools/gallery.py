#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地图库取图：按关键词挑一张现成的官方图。

为什么需要它：生图模型不认识「胡桃」，画出来的是路人脸（详见 docs/输出自然化.md 5.1）。
官方图 100% 保真、0 成本、0 延迟，适合「表达自己情绪 / 打招呼 / 撒娇」这类
**画面主体就是胡桃本人**的场合；而「分享见闻」这类需要具体场景的场合仍走 [[img_gen:]] 生图。

零依赖（仅标准库），与项目其余部分一致。

用法：
    python3 tools/gallery.py pick "胡桃 打招呼"     # 打印 GALLERY_OK <路径> 或 GALLERY_MISS <关键词>
    python3 tools/gallery.py list                  # 列出图库索引
    python3 tools/gallery.py stats                 # 图库统计

关键词匹配规则（全部来自文件系统，不需要手工维护索引）：
  · 相对路径的每一段目录名都是关键词     gallery/胡桃/表情/xx.png  →  命中「胡桃」「表情」
  · 文件名按 _ - 空格 数字 拆词           hutao_打招呼_01.png       →  命中「hutao」「打招呼」
  · gallery/tags.json 可补额外关键词     {"胡桃/表情/xx.png": ["开心", "得意"]}
"""

import argparse
import json
import os
import random
import re
import sys
import time

GALLERY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gallery")
TAGS_FILE = os.path.join(GALLERY_DIR, "tags.json")
USAGE_FILE = os.path.join(GALLERY_DIR, ".usage.json")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
HISTORY_MAX = 100          # 发送历史保留条数（用于轮换，避免连着发同一张）
_SPLIT_RE = re.compile(r"[_\W\d]+")

# 类别优先级：只在**分数打平**时起作用（比如她只写了「胡桃」）。
# 数字越小越优先——立绘最适合当日常配图，动作 GIF 有 7MB 不适合天天发。
CATEGORY_RANK = {"立绘": 1, "节日": 2, "剧情": 3, "表情": 4, "动作": 5}


def _norm(text: str) -> str:
    """归一化：全角空格/常见标点→空格，转小写，压空白。"""
    text = (text or "").lower()
    for ch in "，,。.、/|·:：;；!！?？'\"":
        text = text.replace(ch, " ")
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _tokens_of(rel_path: str, extra: list) -> set:
    """一个文件的关键词集合：目录段 + 文件名拆词 + 去掉角色名前缀 + tags.json 补充词。"""
    parts = rel_path.replace(os.sep, "/").split("/")
    stem = os.path.splitext(parts[-1])[0]
    tokens = set()
    for p in parts[:-1]:                     # 目录名整段
        tokens.add(_norm(p))
    for w in _SPLIT_RE.split(stem):          # 文件名拆词
        if w.strip():
            tokens.add(_norm(w))
    for p in parts[:-1]:
        # 「甘雨半身.png」在 甘雨/ 目录下 → 额外得到「半身」这个关键词。
        # 文件名用中文连写时没有分隔符，拆词拆不出来，靠这条补上。
        if stem.startswith(p) and len(stem) > len(p):
            rest = _norm(stem[len(p):])
            if rest:
                tokens.add(rest)
    for w in extra or []:
        tokens.add(_norm(w))
    return {t for t in tokens if t}


def build_index() -> list:
    """扫描图库目录，返回 [{path, rel, tokens}, ...]。"""
    if not os.path.isdir(GALLERY_DIR):
        return []
    extra_map = {}
    if os.path.exists(TAGS_FILE):
        try:
            with open(TAGS_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            extra_map = {k.replace(os.sep, "/"): v for k, v in raw.items()
                         if not k.startswith("_") and isinstance(v, list)}
        except (OSError, json.JSONDecodeError) as e:
            print(f"[gallery] tags.json 解析失败，已忽略：{e}", file=sys.stderr)

    index = []
    for root, _dirs, files in os.walk(GALLERY_DIR):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, GALLERY_DIR).replace(os.sep, "/")
            index.append({"path": full, "rel": rel,
                          "tokens": _tokens_of(rel, extra_map.get(rel))})
    return index


def _load_usage() -> list:
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            return json.load(f).get("recent", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def _save_usage(recent: list) -> None:
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"recent": recent[-HISTORY_MAX:]}, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _match_score(word: str, item: dict) -> int:
    """单个查询词对一个文件的匹配分：目录名 5 > 文件名词 4 > 宽松子串 2，不中 0。"""
    if word in item["dirs"]:
        return 5
    if word in item["tokens"]:
        return 4
    if any(word in t or t in word for t in item["tokens"] if len(word) >= 2):
        return 2
    return 0


def pick(query: str, rng: random.Random = None) -> tuple:
    """按关键词挑一张图，返回 (路径 或 None, 说明)。

    **第一个词是主题词，必须命中**（她写「钟离 立绘」时不能拿「立绘」这一半去匹配、
    结果发回一张胡桃图——主题词不中就直接 MISS，让上层记一条失败说明）。
    其余词只用来在同一主题内排序，不中也不影响（`[[img: 胡桃 打招呼]]` 会退化成给立绘）。

    同分时按类别优先级裁决：立绘 > 节日 > 剧情 > 表情 > 动作。
    轮换：在最高分候选里永远挑「最久没发过」的那一档，候选 ≥2 张就不会连发同一张。
    """
    rng = rng or random.Random()
    index = build_index()
    if not index:
        return None, "图库为空"

    words = [w for w in _norm(query).split(" ") if w]
    if not words:
        return None, "关键词为空"

    for item in index:
        item["dirs"] = {_norm(p) for p in item["rel"].split("/")[:-1]}

    subject = words[0]
    pool = [it for it in index if _match_score(subject, it)]
    if not pool:
        return None, f"图库没有「{subject}」"

    scored = []
    for item in pool:
        score = sum(_match_score(w, item) for w in words[1:])
        rank = next((r for c, r in CATEGORY_RANK.items() if f"/{c}/" in item["rel"]), 9)
        scored.append((score, rank, item))

    best = max(s for s, _, _ in scored)
    top = [(r, it) for s, r, it in scored if s == best]
    best_rank = min(r for r, _ in top)
    cands = [it for r, it in top if r == best_rank]

    # 候选池内 LRU 轮换：挑「最久没发过」的那一档，池子至少 2 张就不会连发
    recent = _load_usage()

    def last_used(p: str) -> int:
        hits = [i for i, x in enumerate(recent) if x == p]
        return hits[-1] if hits else -1

    cands.sort(key=lambda it: last_used(it["path"]))
    oldest = last_used(cands[0]["path"])
    tier = [c for c in cands if last_used(c["path"]) == oldest]
    chosen = rng.choice(tier)

    _save_usage(recent + [chosen["path"]])
    note = f"命中 {len(pool)} 张中的 {len(cands)} 张，可选 {len(tier)} 张"
    return chosen["path"], note


def main() -> int:
    ap = argparse.ArgumentParser(description="本地图库取图")
    ap.add_argument("cmd", choices=["pick", "list", "stats"])
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--seed", type=int, default=None, help="固定随机种子（调试用）")
    args = ap.parse_args()

    if args.cmd == "pick":
        path, note = pick(args.query, random.Random(args.seed))
        if path:
            print(f"GALLERY_OK {path}")
            print(f"# {note}", file=sys.stderr)
            return 0
        print(f"GALLERY_MISS {args.query}  # {note}")
        return 1

    index = build_index()
    if args.cmd == "stats":
        print(f"图库目录: {GALLERY_DIR}")
        print(f"图片总数: {len(index)}")
        print(f"最近发送: {len(_load_usage())} 条记录")
        return 0

    for it in index:
        print(f"{it['rel']}\t{' '.join(sorted(it['tokens']))}")
    print(f"# 共 {len(index)} 张", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
