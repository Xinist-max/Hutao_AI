#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把手动下载的图导入图库。

什么时候用它：wiki 限流时 `fetch_gallery.py` 抓不动，但**浏览器能正常打开页面**——
你在浏览器里右键存图（或整页另存再挑图），然后用这个脚本把文件收进图库、补上关键词。

它会做三件事：
  1. 内容去重（sha256）：同一张图重复导入不会产生副本
  2. 修名字：wiki 直链的文件名是一串乱码（`6x5q4v3ovlgtd5pk7xlqxjdqv1gvf0a.png`），
     这种会被重命名为 `<角色><类别><序号>.<ext>`；**带中文的名字原样保留**
  3. 补关键词：`--tag` 的词写进 gallery/tags.json，这样 `[[img: 甘雨 打招呼]]` 能命中

用法：
    # 存图时如果保留了中文名（右键「图片另存为」通常不会），直接导入即可
    python3 tools/import_gallery.py --char 甘雨 --category 立绘 ~/Downloads/*.png

    # 乱码名的图，最好给个名字
    python3 tools/import_gallery.py --char 甘雨 --category 立绘 --name 甘雨立绘 ~/Downloads/xxx.png

    # 加关键词，之后她就能用自然词取图
    python3 tools/import_gallery.py --char 甘雨 --category 立绘 --tag 打招呼 --tag 微笑 ~/Downloads/a.png ~/Downloads/b.png

    # 先看看会怎么处理，不实际写入
    python3 tools/import_gallery.py --char 甘雨 --category 立绘 --dry-run ~/Downloads/*
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GALLERY_DIR = os.path.join(PROJECT_ROOT, "gallery")
TAGS_FILE = os.path.join(GALLERY_DIR, "tags.json")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
CATEGORIES = ("立绘", "表情", "节日", "剧情", "动作")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _meaningless(stem: str) -> bool:
    """wiki 直链的文件名是一串无意义字符：没有中文且长度 ≥16。"""
    return not _CJK_RE.search(stem) and len(stem) >= 16


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _existing_hashes() -> dict:
    """图库里已有文件的内容指纹 → 路径，用于去重。"""
    out = {}
    for root, _dirs, files in os.walk(GALLERY_DIR):
        for name in files:
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                p = os.path.join(root, name)
                try:
                    out[_sha256(p)] = os.path.relpath(p, GALLERY_DIR).replace(os.sep, "/")
                except OSError:
                    pass
    return out


def _expand_inputs(paths: list) -> list:
    """展开目录，返回图片文件列表。"""
    out = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in sorted(os.listdir(p))
                    if os.path.splitext(f)[1].lower() in IMAGE_EXTS]
        elif os.path.isfile(p):
            out.append(p)
        else:
            print(f"  ⚠️ 跳过（不存在）{p}", file=sys.stderr)
    return out


def _load_tags() -> dict:
    if os.path.exists(TAGS_FILE):
        try:
            with open(TAGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_tags(tags: dict) -> None:
    with open(TAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="把手动下载的图导入图库")
    ap.add_argument("files", nargs="+", help="图片文件或目录")
    ap.add_argument("--char", required=True, help="角色名（决定落到 gallery/<角色>/）")
    ap.add_argument("--category", default="立绘", choices=CATEGORIES, help="类别目录（默认 立绘）")
    ap.add_argument("--name", default=None, help="指定文件名（单文件时用；可带扩展名）")
    ap.add_argument("--tag", action="append", default=[], help="额外关键词，可重复")
    ap.add_argument("--move", action="store_true", help="移动而不是复制（默认复制，保留原文件）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    args = ap.parse_args()

    srcs = _expand_inputs(args.files)
    if not srcs:
        print("没有找到可导入的图片")
        return 1

    dest_dir = os.path.join(GALLERY_DIR, args.char, args.category)
    known = _existing_hashes()
    tags = _load_tags()
    added, skipped, planned = [], [], []
    taken = set()   # 本批次已占用的目标路径（os.path.exists 看不到同一批里的）

    for i, src in enumerate(srcs, 1):
        ext = os.path.splitext(src)[1].lower()
        if ext not in IMAGE_EXTS:
            continue
        try:
            digest = _sha256(src)
        except OSError as e:
            print(f"  ❌ 读不了 {src}: {e}")
            continue
        if digest in known:
            skipped.append((src, known[digest]))
            continue

        stem = os.path.splitext(os.path.basename(src))[0]
        if args.name:
            base = os.path.splitext(args.name)[0]
            if len(srcs) > 1:
                base = f"{base}{i}"
        elif _meaningless(stem):
            base = f"{args.char}{args.category}{i:02d}"
        else:
            base = stem
        dest = os.path.join(dest_dir, f"{base}{ext}")
        n = 1
        while os.path.exists(dest) or dest in taken:   # taken=本批次已占用的目标
            dest = os.path.join(dest_dir, f"{base}_{n}{ext}")
            n += 1
        planned.append((src, dest, digest))
        taken.add(dest)

    if args.dry_run:
        print(f"将导入 {len(planned)} 张到 {os.path.relpath(dest_dir, PROJECT_ROOT)}/：")
        for src, dest, _ in planned:
            print(f"  {os.path.basename(src)}  →  {os.path.basename(dest)}")
        if skipped:
            print(f"将跳过 {len(skipped)} 张重复：")
            for src, dup in skipped:
                print(f"  {os.path.basename(src)}  （图库已有 {dup}）")
        return 0

    os.makedirs(dest_dir, exist_ok=True)
    for src, dest, digest in planned:
        if args.move:
            shutil.move(src, dest)
        else:
            shutil.copy2(src, dest)
        rel = os.path.relpath(dest, GALLERY_DIR).replace(os.sep, "/")
        known[digest] = rel
        if args.tag:
            merged = list(dict.fromkeys(list(tags.get(rel, [])) + args.tag))
            tags[rel] = merged
        print(f"  ✅ {os.path.basename(src)}  →  {rel}")

    for src, dup in skipped:
        print(f"  ⏭️  重复跳过 {os.path.basename(src)}（图库已有 {dup}）")

    if args.tag and planned:
        _save_tags(tags)

    print(f"\nDONE 导入 {len(planned)} 张、跳过 {len(skipped)} 张")
    print(f"          图库目录 {dest_dir}")
    if args.tag:
        print(f"          关键词 {'、'.join(args.tag)} 已写入 gallery/tags.json")
    print("下一步：python3 tools/gallery.py stats"
          f"　然后试 python3 tools/gallery.py pick \"{args.char} {args.tag[0] if args.tag else args.category}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
