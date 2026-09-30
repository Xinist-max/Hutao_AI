#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看图：用**有视觉能力的模型**读图，不受对话模型路由影响。

## 为什么需要单独一个工具

亲密度路由会把胡桃的对话模型切到本地（`ollama/local`），而本地模型与所有 deepseek 模型的
`input` 都只有 `["text"]`——**只有 `moonshot/kimi-k2.6` 支持 `["text","image"]`**。
所以一旦切到本地，她就看不了图。把"看图"和"聊天"绑在同一个开关上是错的，这里拆开：
不管对话跑在哪，看图都显式指定一个有视觉的模型。

失败时**绝不猜**：读不到就返回失败，不输出"按经验推测"这种编造的答案。

    python3 tools/ask_vision.py 图.png "描述这张图"
    python3 tools/ask_vision.py 图.png 图2.png "对比这两张"
    python3 tools/ask_vision.py --list-models          # 看哪些模型支持图像输入
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def vision_models() -> list:
    """列出配置里声明了 image 输入的模型（形如 provider/model）。"""
    try:
        with open(OPENCLAW_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for pname, p in (cfg.get("models", {}).get("providers") or {}).items():
        for m in (p.get("models") or []):
            if "image" in (m.get("input") or []):
                mid = m.get("id") or m.get("name")
                out.append(f"{pname}/{mid}")
    return out


def pick_model(preferred: str = "") -> str:
    """选一个有视觉的模型：优先配置里指定的，其次第一个可用的。"""
    avail = vision_models()
    if not avail:
        return ""
    if preferred and preferred in avail:
        return preferred
    if preferred:
        # 允许只给模型名（如 kimi-k2.6）
        hit = next((m for m in avail if m.endswith("/" + preferred.split("/")[-1])), None)
        if hit:
            return hit
    return avail[0]


VISION_STAGE = os.path.expanduser("~/.openclaw/workspace/images/_vision")


def stage(paths: list) -> tuple:
    """把图复制到 agent 工作区里。

    agent 的 `image` 工具有**目录白名单**——只让读自己工作区内的文件，
    `/tmp/xxx.png` 会返回「路径不在允许访问的目录里」。所以必须先暂存进去，
    这是这个工具最容易踩的坑（看起来像"模型没有视觉能力"，其实是路径被拦）。
    """
    import shutil
    os.makedirs(VISION_STAGE, exist_ok=True)
    out = []
    for p in paths:
        if not os.path.exists(p):
            return [], f"文件不存在：{p}"
        # ⚠️ 暂存名不能只用 basename：多图对比时两张都叫 shot.png 会互相覆盖，
        # 模型拿到的是同一个路径 → 给出自信而错误的对比结论，调用方却收到 VISION_OK。
        # 加内容哈希前 8 位：既唯一，又能看出两张其实是不是同一张图。
        import hashlib
        try:
            with open(p, "rb") as _f:
                _h = hashlib.sha1(_f.read()).hexdigest()[:8]
        except OSError:
            _h = "nohash"
        name = os.path.basename(p)
        dest = os.path.join(VISION_STAGE, f"{_h}_{name}")
        if os.path.abspath(p) != os.path.abspath(dest):
            shutil.copy2(p, dest)
        out.append(dest)
    return out, ""


def ask(paths: list, question: str, model: str = "", timeout: int = 240) -> tuple:
    """让 agent 看图回答问题。返回 (成功, 回答或失败原因)。"""
    use = pick_model(model)
    if not use:
        return False, "配置里没有任何声明 image 输入的模型"
    staged, err = stage(paths)
    if err:
        return False, err

    files = "\n".join(f"- {p}" for p in staged)
    msg = (f"[看图] 用 image 工具读下面这些文件，然后回答问题。\n{files}\n\n"
           f"问题：{question}\n\n"
           f"**读不到图就直接说读不到，不要凭经验推测，也不要编。**")
    parts = ["openclaw", "agent", "--agent", "main", "--model", use,
             "--session-key", f"agent:main:vision-{int(time.time())}",
             "--message", msg]
    try:
        proc = subprocess.run(parts, capture_output=True, text=True, timeout=timeout)
    except subprocess.SubprocessError as e:
        return False, f"{type(e).__name__}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return False, ((proc.stderr or out).strip()[:200] or "无输出")
    # 兜底：模型自称读不到时明确标出来，别让调用方误以为拿到了有效结论
    if "读不到" in out or "看不到" in out:
        return False, out
    return True, out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="用有视觉能力的模型看图",
        epilog='例：python3 tools/ask_vision.py a.png b.png -q "对比这两张"')
    ap.add_argument("images", nargs="*", help="图片路径")
    # 问题必须是选项：images 用 nargs="*" 时，位置参数形式的问题会被贪婪地当成图片路径
    ap.add_argument("-q", "--question", default="描述这张图", help="要问的问题")
    ap.add_argument("--model", default="", help="指定模型（默认取配置里第一个支持图像的）")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()

    if args.list_models:
        avail = vision_models()
        print(f"支持图像输入的模型 {len(avail)} 个：")
        for m in avail:
            print(f"   {m}")
        return 0 if avail else 1

    if not args.images:
        print("用法：ask_vision.py <图片...> [-q 问题]")
        return 2
    use = pick_model(args.model)
    print(f"VISION_MODEL {use}")
    ok, text = ask(args.images, args.question, args.model)
    print(("VISION_OK\n" if ok else "VISION_FAIL ") + text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
