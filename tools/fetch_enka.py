#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Enka.Network 抓官方游戏内素材到本地图库。

比抓 wiki 页面好在三点：
  1. **不限流**（wiki 是阿里云 WAF 的 IP 级封禁，连刷几页就「请求已被拦截」，浏览器也救不了）
  2. **是游戏内原始素材**，不是 wiki 转存的缩略图，胡桃就是胡桃本人的官方图
  3. 地址可由角色 ID 直接拼出来，不需要解析页面

资产类型（Enka 路径 /ui/<文件名>.png）：

  | 文件名                        | 内容               | 归类 | 本地命名          |
  |-------------------------------|--------------------|------|-------------------|
  | UI_Gacha_AvatarImg_<ID>       | 抽卡立绘（全身大图）| 立绘 | <角色>立绘.png     |
  | UI_Gacha_AvatarIcon_<ID>      | 抽卡头像（半身）    | 立绘 | <角色>半身.png     |
  | UI_AvatarIcon_<ID>            | 圆形头像            | 表情 | <角色>头像.png     |
  | UI_NameCardPic_<ID>_P         | 角色名片            | 表情 | <角色>名片.png     |

用法：
    python3 tools/fetch_enka.py                 # 抓全部阵容
    python3 tools/fetch_enka.py --char 胡桃 甘雨  # 只抓指定角色
    python3 tools/fetch_enka.py --dry-run
"""

import argparse
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GALLERY_DIR = os.path.join(PROJECT_ROOT, "gallery")
BASE = "https://enka.network/ui"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# 中文名 → 游戏内资产 ID（Enka 用的就是游戏内部命名）
CAST = {
    "胡桃": "Hutao", "钟离": "Zhongli", "魈": "Xiao", "温迪": "Venti",
    "甘雨": "Ganyu", "刻晴": "Keqing", "凝光": "Ningguang", "北斗": "Beidou",
    "香菱": "Xiangling", "行秋": "Xingqiu", "重云": "Chongyun", "辛焱": "Xinyan",
    "申鹤": "Shenhe", "夜兰": "Yelan", "云堇": "Yunjin", "白术": "Baizhu",
    "瑶瑶": "Yaoyao", "嘉明": "Gaming", "闲云": "Xianyun",
}

# (资产名模板, 类别, 本地文件名模板)
ASSETS = [
    ("UI_Gacha_AvatarImg_{id}",   "立绘", "{char}立绘.png"),
    ("UI_Gacha_AvatarIcon_{id}",  "立绘", "{char}半身.png"),
    ("UI_AvatarIcon_{id}",        "表情", "{char}头像.png"),
    ("UI_NameCardPic_{id}_P",     "表情", "{char}名片.png"),
]

MIN_BYTES = 20 * 1024        # 小于 20KB 的基本是占位图

# Enka 缺这两个角色的资产（全部 404），用 genshin.jmp.blue 社区镜像兜底。
# 它给的是 webp，用 macOS 自带 sips 转成 png（微信对 webp 兼容性没保证）。
JMP_FALLBACK = {"白术": "baizhu", "闲云": "xianyun"}
JMP_ASSETS = [
    ("gacha-splash", "立绘", "{char}立绘"),
    ("portrait",     "立绘", "{char}半身"),
    ("card",         "表情", "{char}名片"),
]


def _to_png(src: str, dest: str) -> bool:
    """webp → png（sips 是 macOS 自带，没有就保留原格式）。"""
    proc = subprocess.run(["sips", "-s", "format", "png", src, "--out", dest],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dest):
        return False
    os.remove(src)
    return True


def fetch_jmp(char: str, slug: str) -> tuple:
    """兜底源：genshin.jmp.blue。返回 (成功数, 缺失数)。"""
    ok = miss = 0
    for path, category, name_tpl in JMP_ASSETS:
        url = f"https://genshin.jmp.blue/characters/{slug}/{path}"
        dest_dir = os.path.join(GALLERY_DIR, char, category)
        dest = os.path.join(dest_dir, name_tpl.format(char=char) + ".png")
        if os.path.exists(dest):
            print(f"  ⏭️  已存在 {os.path.relpath(dest, GALLERY_DIR)}")
            ok += 1
            continue
        os.makedirs(dest_dir, exist_ok=True)
        tmp = dest + ".webp"
        proc = subprocess.run(["curl", "-sL", "--max-time", "60", "-A", UA,
                               "-o", tmp, "-w", "%{http_code}", url],
                              capture_output=True, text=True)
        code = (proc.stdout or "").strip()
        if code != "200" or not os.path.exists(tmp) or os.path.getsize(tmp) < MIN_BYTES:
            if os.path.exists(tmp):
                os.remove(tmp)
            print(f"  ❌ HTTP {code or '???'}（兜底源也没有 {path}）")
            miss += 1
            continue
        size = os.path.getsize(tmp)
        if not _to_png(tmp, dest):
            os.rename(tmp, dest[:-4] + ".webp")
            dest = dest[:-4] + ".webp"
        print(f"  ✅ {size / 1024:.0f}KB(webp)  {os.path.relpath(dest, GALLERY_DIR)}")
        ok += 1
    return ok, miss


def fetch(url: str, dest: str, timeout: int = 60) -> tuple:
    """下载到 dest，返回 (ok, 说明)。"""
    proc = subprocess.run(["curl", "-sL", "--max-time", str(timeout), "-A", UA,
                           "-o", dest, "-w", "%{http_code}", url],
                          capture_output=True, text=True)
    code = (proc.stdout or "").strip()
    if code != "200" or not os.path.exists(dest):
        if os.path.exists(dest):
            os.remove(dest)
        return False, f"HTTP {code or '???'}"
    size = os.path.getsize(dest)
    if size < MIN_BYTES:
        os.remove(dest)
        return False, f"过小({size}B)，大概是占位图"
    return True, f"{size / 1024:.0f}KB"


def main() -> int:
    ap = argparse.ArgumentParser(description="从 Enka.Network 抓官方游戏内素材")
    ap.add_argument("--char", nargs="*", default=None, help="只抓指定角色（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="只列出计划")
    ap.add_argument("--overwrite", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    chars = args.char or list(CAST)
    unknown = [c for c in chars if c not in CAST]
    if unknown:
        print(f"不认识的角色：{'、'.join(unknown)}（可选：{'、'.join(CAST)}）")
        return 1

    ok = miss = 0
    failed_chars = []
    for char in chars:
        cid = CAST[char]
        print(f"########## {char}  ({cid})")
        char_ok = 0
        for tpl, category, name_tpl in ASSETS:
            asset = tpl.format(id=cid)
            url = f"{BASE}/{asset}.png"
            dest_dir = os.path.join(GALLERY_DIR, char, category)
            dest = os.path.join(dest_dir, name_tpl.format(char=char))
            if args.dry_run:
                print(f"  [dry] {url}\n        → {os.path.relpath(dest, GALLERY_DIR)}")
                continue
            if os.path.exists(dest) and not args.overwrite:
                print(f"  ⏭️  已存在 {os.path.relpath(dest, GALLERY_DIR)}")
                ok += 1
                continue
            os.makedirs(dest_dir, exist_ok=True)
            good, note = fetch(url, dest)
            if good:
                ok += 1
                char_ok += 1
                print(f"  ✅ {note}  {os.path.relpath(dest, GALLERY_DIR)}")
            else:
                miss += 1
                print(f"  ❌ {note}  {asset}")
        if not args.dry_run and char_ok == 0:
            failed_chars.append(char)

    if args.dry_run:
        return 0

    # Enka 没有的角色，换社区镜像兜底
    for char in failed_chars:
        slug = JMP_FALLBACK.get(char)
        if not slug:
            print(f"########## {char}：Enka 没有它的资产，也没有登记兜底源")
            continue
        print(f"########## {char} 兜底源 genshin.jmp.blue/{slug}")
        o, m = fetch_jmp(char, slug)
        ok += o
        miss += m

    print(f"\nDONE 成功 {ok}、缺失 {miss}")
    print("下一步：python3 tools/gallery.py stats")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
