#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""去掉她历史回复**开头**的括号动作，断开"她模仿自己"的循环。

## 为什么需要这个（实测依据）

她最近 20 条回复 **100% 以「（动作）」开头**，而且 31 条历史回复全在上下文里。
模型在**延续这个模式**——实测：

    上下文里她的回复原样      → 新回复括号开头 100%   (n=8)
    上下文里去掉开头括号      → 新回复括号开头  25%   (n=8)

排除了别的解释之后（Q2_K 量化、num_ctx、模板拍平、采样参数、系统提示词长度、
**以及所有提示词层面的干预** —— 一条措辞强到"看到自己前面这么写，你偏不这么写"
的指令，12 次生成一次都没被遵守），**改上下文是唯一有效的办法**。

## 关键：这不动用户在微信里看到的历史

改的是 OpenClaw 的**会话转录**（`~/.openclaw/agents/*/sessions/<id>.jsonl`），
那只是**给模型看的上下文**。微信里的消息记录由微信自己保存，不受影响。

## 安全措施

- 默认 **dry-run**，加 `--apply` 才真写
- 写之前留时间戳备份 `<file>.bak-style-<ts>`
- **只删每条回复开头的括号动作**，正文一个字不动
- 原子写（tmp + os.replace），写完校验 JSONL 可解析
- `--restore` 可列出备份并一键回滚

用法：
    python3 tools/style_rewrite.py                 # 看会改什么（不写盘）
    python3 tools/style_rewrite.py --apply         # 真改（自动备份）
    python3 tools/style_rewrite.py --restore       # 列出备份
    python3 tools/style_rewrite.py --restore <备份文件>
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# 行首的括号动作：全角/半角括号，最多 40 字，后面允许跟空白与分隔标点
LEAD_ACTION = re.compile(r"^\s*[（(][^）)]{1,40}[）)]\s*[\n，,、：:。]*\s*")
# 行首的省略号（…… / ⋯ / ...）——**它和括号动作是同一个模板的两段**
LEAD_ELLIPSIS = re.compile(r"^\s*[….．]{2,}\s*|^\s*\.{3,}\s*")


def strip_leading_actions(text: str) -> tuple:
    """剥掉开头的「公式」：括号动作 **和** 省略号（最多连剥 4 层）。

    ⚠️ 两段必须一起剥 —— 这是实测纠正过来的：
    改写**前**她的 48 条回复里 88% 以「（动作）」开头、**0%** 以「……」开头，
    但 **69% 剥掉括号后紧跟一个「……」**。也就是说模板一直是两段式：

        （叉腰）……哼，巧了。

    第一版只剥括号，结果「……」从第二段变成第一段、看起来像"换了个套路"——
    其实不是换了，是**露出来了**。

    返回 (新文本, 剥掉几层)。
    """
    out, n = text or "", 0
    for _ in range(4):
        nxt = LEAD_ACTION.sub("", out, count=1)
        if nxt == out:
            nxt = LEAD_ELLIPSIS.sub("", out, count=1)
        if nxt == out:
            break
        out, n = nxt, n + 1
    return out.strip(), n


def session_files() -> list:
    pat = os.path.expanduser("~/.openclaw/agents/*/sessions/*.jsonl")
    return [p for p in glob.glob(pat)
            if not p.endswith(".trajectory.jsonl")
            and ".bak" not in p and ".compacted" not in p and ".tmp" not in p]


def process(path: str, apply: bool) -> dict:
    """处理一个会话文件。返回统计。"""
    lines, changed, backups = [], 0, 0
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except OSError as e:
        return {"file": path, "error": str(e)}
    for line in raw:
        s = line.strip()
        if not s:
            lines.append(line)
            continue
        try:
            rec = json.loads(s)
        except json.JSONDecodeError:
            lines.append(line)                 # 坏行原样保留，不碰
            continue
        msg = rec.get("message") or {}
        if msg.get("role") != "assistant":
            lines.append(line)
            continue
        content = msg.get("content")
        # ⚠️ 会话里 assistant 的 content 有两种形态：**字符串**，或 OpenClaw 内部的
        # **列表**（`[{"type":"text","text":"…"}]`）。第一版只处理了字符串，
        # 结果在真实会话上"改了 0 条"——因为她的回复全是列表形态。
        if isinstance(content, str):
            new, n = strip_leading_actions(content)
            if n:
                msg["content"] = new
                rec["message"] = msg
                lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
                changed += 1
                continue
        elif isinstance(content, list):
            n = 0
            for blk in content:                     # 只剥**第一个**文本块的开头
                if not isinstance(blk, dict) or blk.get("type") != "text":
                    continue
                t = blk.get("text")
                if not isinstance(t, str):
                    continue
                nt, k = strip_leading_actions(t)
                if k:
                    blk["text"] = nt
                    n = k
                break                           # 开头只可能在一个文本块上
            if n:
                msg["content"] = content
                rec["message"] = msg
                lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
                changed += 1
                continue
        lines.append(line)
    out = {
        "file": os.path.basename(path), "总行数": len(raw), "改了几条回复": changed,
    }
    if not changed:
        out["动作"] = "无需改动"
        return out
    if not apply:
        out["动作"] = "dry-run（未写盘）"
        return out
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-style-{stamp}"
    try:
        shutil.copy2(path, backup)
        backups += 1
    except OSError as e:
        out["error"] = f"备份失败，未改动: {e}"
        return out
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        # 写完先校验：逐行解析，任何一行坏掉就不替换
        with open(tmp, encoding="utf-8") as f:
            for i, ln in enumerate(f, 1):
                if ln.strip():
                    try:
                        json.loads(ln)
                    except json.JSONDecodeError as e:
                        raise RuntimeError(f"第 {i} 行不是合法 JSON: {e}")
        os.replace(tmp, path)
    except (OSError, RuntimeError) as e:
        out["error"] = f"写盘失败，原文件未动: {e}"
        try:
            os.remove(tmp)
        except OSError:
            pass
        return out
    out["动作"] = f"已改写（备份 {os.path.basename(backup)}）"
    return out


def list_backups() -> list:
    return sorted(glob.glob(os.path.expanduser("~/.openclaw/agents/*/sessions/*.bak-style-*")))


def main() -> int:
    ap = argparse.ArgumentParser(description="去掉历史回复开头的括号动作（断开自我模仿）")
    ap.add_argument("--apply", action="store_true", help="真写盘（默认只看）")
    ap.add_argument("--restore", nargs="?", const="__list__", default=None,
                    help="回滚：不带参数列出备份，带备份路径则恢复")
    ap.add_argument("--session", default="", help="只处理文件名含该串的会话")
    args = ap.parse_args()

    if args.restore is not None:
        bks = list_backups()
        if args.restore == "__list__":
            if not bks:
                print("没有找到备份")
                return 0
            print("可回滚的备份：")
            for b in bks:
                print(f"  {b}  ({os.path.getsize(b)} 字节)")
            print("\n恢复：python3 tools/style_rewrite.py --restore <上面某一行>")
            return 0
        src = args.restore
        if not os.path.exists(src):
            print(f"备份不存在：{src}", file=sys.stderr)
            return 1
        target = src.split(".bak-style-")[0]
        try:
            shutil.copy2(target, target + ".before-restore")
            shutil.copy2(src, target)
        except OSError as e:
            print(f"恢复失败：{e}", file=sys.stderr)
            return 1
        print(f"已从 {os.path.basename(src)} 恢复 {os.path.basename(target)}"
              f"（恢复前的版本存为 {os.path.basename(target)}.before-restore）")
        return 0

    files = [p for p in session_files() if args.session in p]
    if not files:
        print("没找到会话文件")
        return 0
    print(f"{'模式：' + ('APPLY（会写盘）' if args.apply else 'DRY-RUN（只看不改）')}\n")
    total = 0
    for p in files:
        r = process(p, args.apply)
        if r.get("改了几条回复"):
            total += r["改了几条回复"]
            print(f"  {r['file'][:40]:42s} {r['总行数']:5d} 行  改 {r['改了几条回复']:3d} 条  {r.get('动作','')}")
            if r.get("error"):
                print(f"      ⚠️ {r['error']}")
    print(f"\n合计改动 {total} 条回复")
    if not args.apply and total:
        print("（加 --apply 才会真写盘；写前会自动备份）")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def prune_backups(keep: int = 3) -> int:
    """每个会话只留最近 keep 个 style 备份，避免长期运行把磁盘塞满。"""
    import collections
    groups = collections.defaultdict(list)
    for b in list_backups():
        groups[b.split(".bak-style-")[0]].append(b)
    removed = 0
    for _target, bks in groups.items():
        for old in sorted(bks)[:-keep] if keep > 0 else sorted(bks):
            try:
                os.remove(old)
                removed += 1
            except OSError:
                pass
    return removed


def rewrite_session(path: str, keep_backups: int = 3) -> dict:
    """给引擎调用的入口：改写单个会话并顺手清理旧备份。"""
    r = process(path, apply=True)
    if r.get("改了几条回复"):
        prune_backups(keep_backups)
    return r
