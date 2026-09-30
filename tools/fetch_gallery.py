#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从原神观测枢 wiki 页面批量抓取官方图，落到本地图库。

为什么用它：官方图 100% 保真（不像生图模型会画出路人脸），且 0 成本。
wiki 页里每张图都带**中文语义文件名**（`胡桃立绘.png`、`无背景-角色-胡桃.png`、
`生贺·胡桃·2025.png`），缩略图链接去掉 `/thumb` 与尺寸后缀就是全尺寸原图——
所以文件名本身就是最好的关键词标签，`tools/gallery.py` 不用另建索引。

用法：
    python3 tools/fetch_gallery.py                      # 默认抓胡桃
    python3 tools/fetch_gallery.py --char 钟离
    python3 tools/fetch_gallery.py --url <任意wiki页面> --char 胡桃 --dry-run

抓完跑 `python3 tools/gallery.py stats` 看图库统计。
"""

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GALLERY_DIR = os.path.join(PROJECT_ROOT, "gallery")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# alt 名 → 目标子目录（按顺序匹配，先命中先归类）
CATEGORY_RULES = [
    ("立绘", ("立绘", "无背景-角色", "角色卡", "头像框")),
    ("表情", ("表情", "头像", "卡牌-角色牌", "名片")),
    ("节日", ("生贺", "贺图", "海灯", "春节", "周年", "活动壁纸", "壁纸")),
    ("剧情", ("剧情", "PV", "截图", "任务")),
]
DEFAULT_CATEGORY = "立绘"

_IMG_RE = re.compile(r'<img[^>]+?alt="([^"]*)"[^>]+?src="([^"]+)"', re.S)
_THUMB_RE = re.compile(r'(/images/[^/]+)/thumb(/.*?\.(?:png|jpe?g|gif|webp))/[^/]*$', re.I)
_OK_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _full_size(url: str) -> str:
    """缩略图链接 → 全尺寸原图链接（去掉 /thumb 与结尾的 /NNpx-名字）。"""
    m = _THUMB_RE.search(url)
    if m:
        return url[: m.start()] + m.group(1) + m.group(2)
    return url


def _category_of(alt: str) -> str:
    for cat, keys in CATEGORY_RULES:
        if any(k in alt for k in keys):
            return cat
    return DEFAULT_CATEGORY


def _safe_name(alt: str, url: str) -> str:
    """用 alt 做文件名（可读、可当关键词）；alt 为空则退回 URL 末段。"""
    ext = os.path.splitext(url)[1].lower() or ".png"
    base = os.path.splitext(alt)[0].strip() if alt else ""
    base = re.sub(r"[/\\:*?\"<>|]", "_", base).strip(" .")
    if not base:
        base = os.path.splitext(os.path.basename(url))[0]
    return f"{base}{ext}"


def fetch_page(url: str, timeout: int = 40) -> str:
    """用 curl 取页面（走系统证书链，避开 python.org 版 Python 的 CA 缺失问题）。

    连刷多个角色页会被 wiki 限流，此时返回的是一个 7KB 左右、没有 <img> 的挑战页
    （正常页面 200KB+），这里识别出来当作抓取失败，避免静默「命中 0 张」。
    """
    proc = subprocess.run(["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
                          capture_output=True, text=True)
    page = proc.stdout or ""
    if len(page) < 30000 or "<img" not in page:
        return ""
    return page


def collect(page_html: str, char: str) -> list:
    """抽取候选图：alt 必须含角色名（滤掉导航图标、别的角色的图）。"""
    out, seen = [], set()
    # 「胡桃·安神」「钟离·天星」这类是技能/命座图标，不是能发出去的图
    skill_icon = re.compile(rf"^{re.escape(char)}·")
    for alt_raw, src in _IMG_RE.findall(page_html):
        alt = html.unescape(alt_raw).strip()
        if not alt or char not in alt:
            continue
        if skill_icon.match(alt):
            continue
        full = _full_size(html.unescape(src))
        if not full.lower().endswith(_OK_EXTS):
            continue
        # 站内小图标（导航/属性图标）直接跳过
        if "图书馆导航栏" in alt or "px-" in alt.split("/")[-1]:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append({"alt": alt, "url": full, "category": _category_of(alt)})
    return out


def download(item: dict, char: str, min_kb: int, max_kb: int = 0) -> tuple:
    """下载单张图，返回 (ok, 说明)。小于 min_kb 的当图标丢弃，大于 max_kb 的当大文件跳过。"""
    dest_dir = os.path.join(GALLERY_DIR, char, item["category"])
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, _safe_name(item["alt"], item["url"]))
    if os.path.exists(dest) and os.path.getsize(dest) > min_kb * 1024:
        return True, f"跳过（已存在）{os.path.relpath(dest, GALLERY_DIR)}"
    proc = subprocess.run(["curl", "-sL", "--max-time", "60", "-A", UA,
                           "-o", dest, item["url"]], capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dest):
        return False, f"下载失败 {item['alt']}"
    size_kb = os.path.getsize(dest) / 1024
    if size_kb < min_kb:
        os.remove(dest)
        return False, f"过小丢弃 {size_kb:.0f}KB {item['alt']}"
    if max_kb and size_kb > max_kb:
        os.remove(dest)
        return False, f"过大跳过 {size_kb / 1024:.1f}MB {item['alt']}"
    return True, f"{size_kb:.0f}KB  {os.path.relpath(dest, GALLERY_DIR)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="从 wiki 批量抓取官方图到本地图库")
    ap.add_argument("--char", default="胡桃", help="角色名（默认 胡桃）")
    ap.add_argument("--url", default=None, help="wiki 页面 URL（默认按角色名拼观测枢地址）")
    ap.add_argument("--html", default=None,
                    help="改用本地保存的页面 HTML（绕过 wiki 限流：浏览器能打开、curl 不能时用这个）")
    ap.add_argument("--min-kb", type=int, default=40, help="小于该体积视为图标丢弃（默认 40KB）")
    ap.add_argument("--max-kb", type=int, default=0,
                    help="大于该体积跳过（0=不限；建议 3000，可滤掉 6~8MB 的动作 GIF）")
    ap.add_argument("--limit", type=int, default=0, help="最多下载张数（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要下载的图")
    args = ap.parse_args()

    url = args.url or f"https://wiki.biligame.com/ys/{urllib.parse.quote(args.char)}"
    print(f"图源：{url}")

    if args.html:
        # 浏览器能过 wiki 的 JS 挑战、curl 不能——让用户存一份 HTML，图片仍从 CDN 拉（CDN 不限流）
        try:
            with open(os.path.expanduser(args.html), encoding="utf-8", errors="ignore") as f:
                page = f.read()
        except OSError as e:
            print(f"FETCH_FAIL 读不到 HTML：{e}")
            return 1
        print(f"本地 HTML：{args.html}（{len(page) // 1024} KB）")
        if "<img" not in page:
            print("FETCH_FAIL 这个 HTML 里没有 <img>，确认存的是「网页，仅 HTML」而不是其它页面")
            return 1
    else:
        page = fetch_page(url)
        if not page:
            print("FETCH_FAIL 页面抓取失败（多为连刷过快被 wiki 限流，歇几分钟再跑，")
            print("           或改用 --html：用浏览器打开页面另存为 HTML 后再跑，绕开限流）")
            return 1

    items = collect(page, args.char)
    if args.limit:
        items = items[: args.limit]
    print(f"命中 {len(items)} 张：")
    for it in items:
        print(f"  [{it['category']}] {it['alt']}")

    if args.dry_run:
        print("\n（--dry-run，未下载）")
        return 0

    if not shutil.which("curl"):
        print("FETCH_FAIL 缺少 curl")
        return 1

    ok = 0
    for it in items:
        good, note = download(it, args.char, args.min_kb, args.max_kb)
        ok += good
        print(("  ✅ " if good else "  ❌ ") + note)
        time.sleep(0.3)                        # 别把 wiki 当 CDN 猛刷

    print(f"\nDONE 成功 {ok}/{len(items)}，图库目录 {os.path.join(GALLERY_DIR, args.char)}")
    print("下一步：python3 tools/gallery.py stats")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
