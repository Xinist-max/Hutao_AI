#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片文件校验：判断一个文件是不是**真正的完整图片**。

## 为什么需要

生图链路原来只有"文件大于 N 字节"这一条校验：

    rc == 0 and os.path.exists(path) and os.path.getsize(path) > 1000

而 `curl` 不加 `-f` 时，4xx/5xx 的 HTTP 状态码**退出码仍是 0**。
实测拉一个不存在的 wiki 页面，拿到 139KB 的 HTML 错误页，
上面那条判定返回 True → 脚本打印 `IMAGE_OK` → 下游只看"文件存在"
就把它当成图片发到微信，**云端还照样计费**。

所以校验必须看**内容**：文件头魔数 + 文件尾结束标记。
两者都对才算完整图片（只查魔数会漏掉"下载被截断"——PNG 头部是对的，
只是后半截没了，历史上真出过这种"截断了却不报错"的情况）。

用法：
    from scene.imagery import is_valid_image, sniff_kind
    if not is_valid_image(path):
        os.remove(path); return False
"""

from __future__ import annotations

import os
from typing import Optional

# 各格式的 (头部魔数, 尾部结束标记)。尾部为 None 表示不校验结尾。
_MAGIC = (
    ("png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82"),
    ("jpeg", b"\xff\xd8\xff", b"\xff\xd9"),
    ("gif", b"GIF87a", b"\x3b"),
    ("gif", b"GIF89a", b"\x3b"),
    ("webp", b"RIFF", None),          # 还需在第 8~12 字节是 WEBP，单独判
)

# 允许当作"图片"发给微信的最小体积。太小的多半是占位图或错误页。
MIN_BYTES = 1024


def sniff_kind(path: str) -> Optional[str]:
    """只按文件头判断格式；不是已知图片格式返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    for kind, magic, _tail in _MAGIC:
        if head.startswith(magic):
            if kind == "webp" and head[8:12] != b"WEBP":
                continue
            return kind
    return None


def is_valid_image(path: str, min_bytes: int = MIN_BYTES) -> bool:
    """文件存在、够大、文件头是已知图片、且尾部有正确的结束标记。

    尾部标记是"完整下载"的证据：PNG 缺 IEND、JPEG 缺 FFD9 都说明被截断了。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size < min_bytes:
        return False
    kind = sniff_kind(path)
    if kind is None:
        return False
    tail = next((t for k, _m, t in _MAGIC if k == kind and t), None)
    if tail is None:
        return True                       # webp 不校验结尾
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - len(tail) - 64))     # 尾部标记可能带少量填充
            return tail in f.read()
    except OSError:
        return False


def describe(path: str) -> str:
    """给日志用的一句话描述（失败原因写清楚，别只说"失败"）。"""
    if not os.path.exists(path):
        return "文件不存在"
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return f"无法读取: {e}"
    kind = sniff_kind(path)
    if kind is None:
        try:
            with open(path, "rb") as f:
                head = f.read(16)
            hint = head[:12].decode("utf-8", "replace").replace("\n", " ")
        except OSError:
            hint = "?"
        return f"不是图片（{size} 字节，开头是 {hint!r}——疑似 HTML/错误页）"
    if size < MIN_BYTES:
        return f"{kind} 但只有 {size} 字节（小于 {MIN_BYTES}）"
    if not is_valid_image(path):
        return f"{kind} 头部正常但尾部缺结束标记（{size} 字节）——下载被截断"
    return f"{kind} {size} 字节 ✅"
