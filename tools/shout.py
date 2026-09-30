#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扬声器喊话：需要人工介入时在 Mac 上说出来。

用户睡觉前交代的："有事儿拿扬声器喊话"。
所以只在**真的需要他起来处理**时才用，别拿它报进度。

用法：
    python3 tools/shout.py "本地模型修好了"
    python3 tools/shout.py "需要你看一眼" --repeat 2
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def shout(text: str, voice: str = "", repeat: int = 1, rate: int = 175) -> None:
    """用系统 say 念出来。失败不抛异常——喊话本身不该拖垮调用方。"""
    for i in range(max(1, repeat)):
        cmd = ["say", "-r", str(rate)]
        if voice:
            cmd += ["-v", voice]
        cmd.append(text)
        try:
            subprocess.run(cmd, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
        if i + 1 < repeat:
            time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="扬声器喊话（有事才用）")
    ap.add_argument("text")
    ap.add_argument("--voice", default="", help="语音名，留空用系统默认")
    ap.add_argument("--repeat", type=int, default=2, help="念几遍（默认 2，怕你睡沉了）")
    ap.add_argument("--rate", type=int, default=175)
    args = ap.parse_args()
    shout(args.text, args.voice, args.repeat, args.rate)
    print(f"[shout] 已喊话：{args.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
