#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作记忆：让她的身体动作在时间上连贯，不重复、不自相矛盾。

## 要解决什么

她把动作写在全角括号里（`（叉腰）`、`（把帽子摘下来）`），但**模型只把它当装饰性文字**，
不当成"状态改变"。于是：

  · **重复**：同一段对话里反复出现同一个动作——实测历史里 `眯着眼笑` 出现 13 次、
    `更小声` 与 `声音轻下去` 各 9 次
  · **自相矛盾**：上一条刚"把帽子摘下来"，下一条又"扶正帽檐"——帽子根本没戴回去

## 怎么解决

两件事：

1. **最近动作**：记录最近若干条用过的动作，注入给模型并明确"别重复"
2. **持续状态**：识别会改变身体状态的动作（摘帽/戴帽、坐下/起身），
   跨消息保持，并告诉模型"帽子现在是摘下的"——避免"摘了又摘""没戴却说扶正"

状态靠关键词规则推断，**规则表是可扩展的**（见 `_STATE_RULES`）：中文动作表达千变万化，
正则做不到全懂，所以只覆盖最明确、最容易出洋相的那几种，其余靠"最近动作"去重兜住。
"""

from __future__ import annotations

import json
import os
import re
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(HERE, "logs", "action_memory.json")

# 动作抽取：她写在全角/半角括号里的内容
_ACTION_RE = re.compile(r"[（(]([^）)]{1,24})[）)]")

# 持续状态规则：(状态名, 目标值, 匹配正则)
# 只收"会改变身体状态、且说错了很明显"的那几类。
#
# ⚠️ 两条踩过的坑：
# ① **语序**。原来只写"名词在前、动词在后"（`帽子.{0,6}摘`），可中文最自然的说法是
#    动宾结构——实测 `（扶正帽檐）`（正是本文件开头自称要防的例子）、`（戴上帽子）`、
#    `（摘下帽子）`、`（脱了帽子）` **全部零状态变化**。所以两个方向都要认。
# ② **窗口不能跨小句**。原来是 `.{0,6}`，实测 `（帽子歪着，摘了颗蜜枣吃）` 会被判成
#    "帽子摘下"——把"摘蜜枣"算到帽子头上。改成不跨标点的 `[^，。；！？]{0,2}`。
_HAT = r"(?:帽子|泰卦帽|帽檐)"
_OFF = r"(?:摘|脱|取下|扔|丢)"
_ON = r"(?:戴|扶正|扣上|正了正|拉了拉)"

_STATE_RULES = [
    ("帽子", "摘下",
     r"(?:(?:把|将)?" + _HAT + r"[^，。；！？]{0,2}" + _OFF + r")"
     r"|(?:" + _OFF + r"(?:掉|下|了|下来)?(?:把|将)?" + _HAT + r")"),
    ("帽子", "戴着",
     r"(?:(?:把|将)?" + _HAT + r"[^，。；！？]{0,2}" + _ON + r")"
     r"|(?:" + _ON + r"(?:上|好|正|起来)?(?:把|将)?" + _HAT + r")"),
    ("姿态", "坐着", r"(坐下|坐在|坐回|蹲在|蹲下|趴在|趴着|坐了|坐回)"),
    # "站着"要认得全：只写「站起来/起身」时，`（从椅子上弹起来）` 会被漏掉，
    # 于是状态还停在"坐着"，hint 就会输出"刚用过：弹起来；当前坐着"这种自相矛盾。
    ("姿态", "站着",
     r"(站起来|站起身|站起|起身|弹起来|直起身|立起来|蹦起来|跳起来|站起身来)"),
    ("亲近", "抱着", r"(抱住|搂住|搂着|抱着)"),
    ("亲近", "松手", r"(松开|放开|撒手)"),
]


class ActionMemory:
    """动作的近期去重 + 持续状态跟踪。存成一个小 JSON，跨进程保持。"""

    def __init__(self, path: str = DEFAULT_PATH, recent_keep: int = 10,
                 state_ttl_hours: float = 6.0, recent_ttl_minutes: float = 120.0):
        self.path = path
        self.recent_keep = recent_keep
        self.state_ttl_hours = state_ttl_hours   # 身体状态的有效期：隔太久就不该再断言
        self.recent_ttl_minutes = recent_ttl_minutes  # "刚用过"的有效期
        self.recent: list = []
        self.recent_ts: list = []                # 与 recent 一一对应
        self.state: dict = {}
        self.state_ts: dict = {}
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.recent = [str(x) for x in (d.get("recent") or [])][-self.recent_keep:]
            self.recent_ts = [float(x) for x in (d.get("recent_ts") or [])][-self.recent_keep:]
            self.state = dict(d.get("state") or {})
            self.state_ts = dict(d.get("state_ts") or {})
        except (OSError, json.JSONDecodeError):
            self.recent, self.recent_ts, self.state, self.state_ts = [], [], {}, {}
        while len(self.recent_ts) < len(self.recent):
            # 老数据/回填数据缺时间戳：当成"很久以前"，下次 _expire 就会清掉，
            # 否则这些动作会永远被当成"刚用过"。
            self.recent_ts.insert(0, 0.0)
        now0 = time.time()
        for cat in self.state:
            # 状态同理：缺时间戳就当成"现在设置的"，让它有个正常的有效期，
            # 而不是因为 ts 为空被 `if ts and ...` 判成永不过期。
            self.state_ts.setdefault(cat, now0)
        self._expire()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"recent": self.recent[-self.recent_keep:],
                           "recent_ts": self.recent_ts[-self.recent_keep:],
                           "state": self.state, "state_ts": self.state_ts},
                          f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ---------- 记账 ----------
    def record(self, text: str) -> list:
        """从一条回复里抽出动作并记账。返回本次抽到的动作列表。"""
        found = [m.group(1).strip() for m in _ACTION_RE.finditer(text or "")]
        found = [a for a in found if a]
        now = time.time()
        for a in found:
            self.recent.append(a)
            self.recent_ts.append(now)
        self.recent = self.recent[-self.recent_keep:]
        self.recent_ts = self.recent_ts[-self.recent_keep:]
        # 更新持续状态
        #
        # ⚠️ 两个坑都在这里：
        # ① 覆盖顺序必须是**文本顺序**（后出现的覆盖先出现的），不能是规则表顺序。
        #    原来的双层循环外层是 _STATE_RULES，于是"姿态:站着"永远压过"姿态:坐着"——
        #    实测「（站起身来又坐了回去）」被判成"站着"（其实最后是坐下了）。
        # ② 必须记录命中位置，取**最靠后**的那条。
        hits = []                       # (命中位置, 类别, 值)
        for a in found:
            base = (text or "").find(a)
            for cat, val, pat in _STATE_RULES:
                m = re.search(pat, a)
                if m:
                    # 位置要精确到**规则在动作串内部的匹配点**，不能只用整串的位置：
                    # 「站起身来又坐了回去」是一个动作串，两条规则都能命中，
                    # 只用整串位置的话两者相同、排序退化成规则表顺序（站着会赢）。
                    hits.append((base + m.start(), cat, val))
        # 按命中位置**升序**迭代：后赋值的即最靠后的动作，也就是最终状态。
        for _pos, cat, val in sorted(hits, key=lambda h: h[0]):
            if self.state.get(cat) == val:
                continue                           # 状态没变就不刷新时间戳
            self.state[cat] = val
            self.state_ts[cat] = time.time()
        self.save()
        return found

    def _expire(self) -> None:
        """清掉过期的身体状态。

        为什么要过期：她说"坐着"是那一刻的事，隔了一天再说"当前坐着"就是错的——
        模型会据此写出奇怪的连贯（例如明明该在街上走，却"从坐着的地方站起来"）。
        """
        now = time.time()
        # 最近动作同样会"过时"：隔了几小时还说"刚用过"没有意义
        if self.recent_ttl_minutes:
            keep = [(a, t) for a, t in zip(self.recent, self.recent_ts)
                    if t and (now - t) / 60.0 <= self.recent_ttl_minutes]
            self.recent = [a for a, _ in keep]
            self.recent_ts = [t for _, t in keep]
        if not self.state_ttl_hours:
            return
        for cat in list(self.state):
            ts = self.state_ts.get(cat)
            if ts and (now - ts) / 3600.0 > self.state_ttl_hours:
                self.state.pop(cat, None)
                self.state_ts.pop(cat, None)

    # ---------- 给模型的提示 ----------
    def hint(self, max_chars: int = 96) -> str:
        """生成一行简短提示：别重复哪些动作、当前身体状态是什么。

        **必须短**：这条每次注入都会写进会话历史（见 docs/上下文连贯性.md 的成本说明），
        所以只给"最近 3 个动作 + 明确状态"，不铺开。
        """
        self._expire()
        parts = []
        seen, uniq = set(), []
        for a in reversed(self.recent):            # 最近的优先
            key = a[:6]
            if key not in seen:
                seen.add(key)
                uniq.append(a)
            if len(uniq) >= 3:
                break
        if uniq:
            # 每个动作截到 10 字：完整描述（如"转头看你，梅花瞳里全是星星"）太长，
            # 写进历史每条都占额度，而"别重复"只需要认出是哪个动作。
            short = [a[:10] + ("…" if len(a) > 10 else "") for a in uniq]
            parts.append("刚用过（别重复）：" + "、".join(short))
        st = self.state

        def _stale(cat: str, val: str) -> bool:
            """当前状态是否已被**之后出现的、互斥的动作**推翻。

            为什么要这道防线：规则表不可能收全中文的所有说法——实测
            `（从椅子上弹起来）` 就不在"站着"的规则里，于是状态还停在"坐着"，
            而 hint 会同时输出「刚用过：从椅子上弹起来…；当前坐着（要站得先写起身）」，
            同一行自相矛盾，模型只能二选一。
            判定方式：recent 里有没有比状态时间戳更晚、且与 val 互斥的动作。
            有 → 这个状态不可信，**宁缺勿错，直接不断言**。
            """
            ts = self.state_ts.get(cat, 0)
            for a, t in zip(self.recent, self.recent_ts):
                if not t or t <= ts:
                    continue
                for c2, other, pat in _STATE_RULES:
                    if c2 == cat and other != val and re.search(pat, a):
                        return True
            return False

        if st.get("帽子") == "摘下" and not _stale("帽子", "摘下"):
            parts.append("当前帽子已摘下（别再摘，要戴得先写戴回去）")
        elif st.get("帽子") == "戴着" and not _stale("帽子", "戴着"):
            parts.append("当前帽子戴着")
        if st.get("姿态") == "坐着" and not _stale("姿态", "坐着"):
            parts.append("当前坐着（要站得先写起身）")
        elif st.get("姿态") == "站着" and not _stale("姿态", "站着"):
            parts.append("当前站着")
        out = "；".join(parts)
        return out[:max_chars]


_SINGLETON = None


def tracker() -> ActionMemory:
    """进程内单例（引擎每轮都会用，没必要反复读盘）。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ActionMemory()
    return _SINGLETON
