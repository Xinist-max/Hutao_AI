"""事件模板：定义"什么条件下、以什么概率生成什么事件"。

模板即"场景剧本"，全部放外部配置（events.json / events.yaml），改模板无需改代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

TIER_ORDER = ["stranger", "familiar", "close"]


@dataclass
class EventTemplate:
    id: str
    type: str                    # environment / time / relationship / surprise / user_state
    title: str = ""              # 事件标题（OpenClaw 通道用；留空则用 id）
    description: str = ""        # 描述模板，支持 {location} {weather} 占位符
    instruction: str = ""        # 注入 OpenClaw 的指令（要求以角色身份主动开口）
    probability: float = 0.5     # 条件满足后的触发概率（0~1）
    priority: str = "medium"     # low / medium / high
    hours_min: Optional[float] = None
    hours_max: Optional[float] = None
    weathers: Optional[List[str]] = None
    locations: Optional[List[str]] = None
    character_states: Optional[List[str]] = None
    tier_min: Optional[str] = None          # 好感度档位门槛：stranger / familiar / close
    affinity_min: Optional[float] = None
    affinity_max: Optional[float] = None
    user_inactive_hours_min: Optional[float] = None  # user_state 类事件：用户空闲多久后触发
    days_mod: Optional[int] = None          # 节日/周期事件：第 N 天倍数触发
    cooldown_minutes: float = 60.0
    affinity_delta: float = 0.0             # 触发后对好感度的影响（±）
    tags: List[str] = field(default_factory=list)
    characters: List[str] = field(default_factory=list)  # 需要在场的 NPC（钟离/魈/温迪…）
    # 可选：这条事件希望你配一张什么画面。填了它，**即使模型忘了写 `[[img_gen: ...]]`**，
    # 引擎也会按这句话补一张图（见 scene/delivery.py 的 _fallback_image）。
    #
    # 为什么这个字段以前是死的：`_fallback_image` 读 `payload["event"]["image"]`，
    # 但 `SceneEvent` 没有这个字段、`build_stimulus` 也不透传，events.json 里更是一个
    # 都没填——于是那条"本地模型丢标记时引擎兜底"的逻辑**从未触发过**，
    # 而 docs/本地生成.md 却把它记成已修。现在链路打通，模板想用就能用。
    image: Optional[str] = None
    idle: bool = False                      # 日常闲聊事件：防沉默机制触发时强制候选
    conversational: bool = False            # 抛话题型事件：回执 replied 后开启多轮主动会话
    follow_up: bool = False                 # 续聊型事件：会话话题开启后由引擎强制触发（{topic} 占位符）

    def matches(self, state, user_inactive_minutes: float) -> bool:
        s = state
        if self.hours_min is not None and s.hour < self.hours_min:
            return False
        if self.hours_max is not None and s.hour >= self.hours_max:
            return False
        if self.weathers and s.weather not in self.weathers:
            return False
        if self.locations and s.location not in self.locations:
            return False
        if self.character_states and s.character_state not in self.character_states:
            return False
        if self.tier_min and TIER_ORDER.index(s.tier) < TIER_ORDER.index(self.tier_min):
            return False
        if self.affinity_min is not None and s.affinity < self.affinity_min:
            return False
        if self.affinity_max is not None and s.affinity >= self.affinity_max:
            return False
        if self.user_inactive_hours_min is not None:
            if user_inactive_minutes / 60.0 < self.user_inactive_hours_min:
                return False
        if self.days_mod is not None and s.day % self.days_mod != 0:
            return False
        if self.characters and not all(c in s.present for c in self.characters):
            return False
        return True


def load_templates(path: str) -> List[EventTemplate]:
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            data = json.load(f)
        else:  # .yaml / .yml
            import yaml  # 延迟导入，JSON 场景下零依赖

            data = yaml.safe_load(f)
    return [EventTemplate(**d) for d in data]
