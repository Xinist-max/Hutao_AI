"""主动发言决策器：决定"此情此景，角色是否应该主动开口"。

规则层：勿扰时段、用户活跃抑制（不插嘴）、频率上限、概率门（主动度 × 优先级）。
语义层（可选，后续）：接一个小模型判断，接口不变。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import List

from .engine import SceneEvent
from .state import SceneState

PRIORITY_WEIGHT = {"low": 0.3, "medium": 0.6, "high": 1.0}

# 主动度参数：mult 为概率乘数，cap_per_hour 为每小时主动发言上限，dnd_mult 为勿扰时段乘数
PROACTIVENESS = {
    "reserved": {"mult": 0.4, "cap_per_hour": 1, "dnd_mult": 0.0},
    "balanced": {"mult": 1.0, "cap_per_hour": 3, "dnd_mult": 0.3},
    "lively": {"mult": 1.6, "cap_per_hour": 6, "dnd_mult": 0.7},
}


@dataclass
class DecisionConfig:
    proactiveness: str = "balanced"                 # reserved / balanced / lively
    dnd_hours: tuple = (0, 7)                       # 勿扰时段（场景时间）
    suppress_after_user_minutes: float = 3.0        # 用户刚发消息 N 分钟内不插嘴
    # 对话缓冲：双方**任一方**最近开口后 N 分钟内，任何主动发言一律压住。
    # ⚠️ **实际执法在 SceneEngine，不在本文件**——原因见下：
    #    引擎是"先扣冷却再交决策层"，在这里挡下的事件冷却已经消耗掉了；
    #    时令见闻的冷却是 3~7 天，挡一次就等于永久丢掉那条见闻。
    #    所以硬闸必须在开火之前（engine.tick / engine._suppressed）拦。
    # 这两个值仍在本配置块里（只留一处调参入口），由 main.py 传给引擎。
    conversation_buffer_minutes: float = 20.0
    # 见闻静默：带 `news` 标签的事件（原神实时资讯翻译出的时令事件）
    # 额外压这么久——对话刚结束也不能立刻跳出来换个话题。
    news_hold_minutes: float = 60.0
    recent_window_minutes: float = 60.0
    min_interval_minutes: float = 20.0              # 推送周期下界（分钟）
    max_interval_minutes: float = 90.0              # 推送周期上界：每条发出后随机抽取下次可发言时间（可变周期）


@dataclass
class DecisionMaker:
    cfg: DecisionConfig
    _spoken: List[float] = field(default_factory=list)  # 最近主动发言的真实时间戳
    _next_allowed: float = 0.0                          # 下次允许发言的时间戳（随机周期）

    def decide(self, event: SceneEvent, state: SceneState, now: float):
        """返回 (是否开口, 原因)。"""
        prof = PROACTIVENESS[self.cfg.proactiveness]

        # 1) 用户活跃抑制：用户刚发过消息则让路（高优先级除外）
        if state.last_user_message_ts > 0:
            idle_min = (now - state.last_user_message_ts) / 60.0
            if idle_min < self.cfg.suppress_after_user_minutes and event.priority != "high":
                return False, f"用户刚发消息({idle_min:.0f}min)，不插嘴"

        # 2) 可变推送周期：上次发言后随机抽取"下次可发言时间"，到点前一律不打扰
        if now < self._next_allowed:
            return False, f"距下次分享还有{(self._next_allowed - now) / 60:.0f}min（周期可变）"

        # 3) 频率上限：滑动窗口内主动发言次数（硬上限）
        window_start = now - self.cfg.recent_window_minutes * 60
        recent = [t for t in self._spoken if t > window_start]
        if len(recent) >= prof["cap_per_hour"]:
            return False, f"已达频率上限({prof['cap_per_hour']}/小时)"

        # 4) 概率门：主动度 × 优先级权重（勿扰时段打折）
        p = prof["mult"] * PRIORITY_WEIGHT[event.priority]
        in_dnd = self.cfg.dnd_hours[0] <= state.hour < self.cfg.dnd_hours[1]
        if in_dnd:
            p *= prof["dnd_mult"]
        if random.random() > p:
            return False, f"随机未通过(p={p:.2f})"

        self._spoken.append(now)
        lo = max(1.0, self.cfg.min_interval_minutes)
        hi = max(lo, self.cfg.max_interval_minutes)
        self._next_allowed = now + random.uniform(lo, hi) * 60   # 周期可变：20~90 分钟随缘
        return True, f"该说（下次约 {random.randint(int(lo), int(hi))} 分钟后）"
