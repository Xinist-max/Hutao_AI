"""相关性过滤：只把"对角色有影响"的事件放行到主动发言决策器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .engine import SceneEvent
from .state import SceneState


@dataclass
class FilterConfig:
    persona_tags: List[str] = field(default_factory=list)  # 角色"在意"的事件标签
    require_persona_tag: bool = True


def relevance_filter(event: SceneEvent, state: SceneState, cfg: FilterConfig):
    """返回 (是否放行, 原因)。"""
    # 1) 人设相关：事件标签与角色在意的标签有交集
    if cfg.require_persona_tag and cfg.persona_tags:
        if not (set(event.tags) & set(cfg.persona_tags)):
            return False, f"人设不相关 tags={event.tags}"
    # 2) 角色状态：睡觉时不打扰（除非高优先级事件）
    if state.character_state == "sleeping" and event.priority != "high":
        return False, "角色在睡觉，不打扰"
    # 3) 关系门槛兜底：亲密类高优事件在陌生档位丢弃
    if event.priority == "high" and state.tier == "stranger":
        return False, "关系档位过低（stranger）"
    return True, "通过"
