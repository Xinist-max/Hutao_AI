"""关键词记忆：只保存近期话题关键词，每次投递前注入，替代携带完整会话历史。

设计动机（成本）：
  完整会话历史会让每次投递重复携带数万 tokens；改为"关键词滚动记忆"后，
  投递只带最近 N 个话题关键词（几十 tokens），会话可安全地按天轮换/重置。

用法：
  mem = KeywordMemory(path, limit=15)
  mem.add("客卿赊账", chars=["钟离"])      # 每次投递/回执后调用
  mem.summary()                            # "钟离·客卿赊账、香菱·万民堂新菜…"
"""

from __future__ import annotations

import json
import os
import time
from typing import List, Optional


class KeywordMemory:
    def __init__(self, path: str, limit: int = 15):
        self.path = path
        self.limit = limit
        self._topics: List[str] = []
        self.load()

    # ---- 持久化 ----
    def load(self) -> None:
        if not os.path.exists(self.path):
            self._topics = []
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._topics = list(data.get("topics", []))[-self.limit:]
        except (OSError, json.JSONDecodeError):
            self._topics = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "topics": self._topics},
                      f, ensure_ascii=False, indent=2)

    # ---- 读写 ----
    def add(self, topic: str, chars: Optional[List[str]] = None) -> None:
        """记录一个话题关键词（可带角色名），去重并保留最近 limit 条。"""
        if not topic:
            return
        key = topic.strip()
        if chars:
            key = f"{'/'.join(chars)}·{key}"
        if key in self._topics:
            self._topics.remove(key)
        self._topics.append(key)
        self._topics = self._topics[-self.limit:]
        self.save()

    def summary(self, max_items: int = 10) -> str:
        """近期话题摘要（供投递消息前缀注入）。"""
        if not self._topics:
            return ""
        items = self._topics[-max_items:]
        return "、".join(items)
