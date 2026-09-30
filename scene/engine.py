"""场景模拟引擎：推进状态、演化 NPC 在场、按事件模板生成随机事件（一次 tick 至多一个）。

防沉默机制：长时间没有事件触发时，强制从 idle 事件池（日常闲聊）选一条符合条件的，
实现角色"没事儿就分享"的效果。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .events import EventTemplate
from .state import LOCATIONS, LOCATION_CN, WEATHER_CN, SceneState


@dataclass
class SceneEvent:
    template_id: str
    type: str
    title: str
    description: str
    instruction: str
    priority: str
    tags: List[str]
    affinity_delta: float
    characters: List[str]
    image: Optional[str] = None      # 事件想要的画面；引擎据此兜底配图（模板没填就是 None）


def _in_hours(hour: float, hours: list) -> bool:
    """支持跨午夜区间（如 [19, 5] 表示 19 点~次日 5 点）。"""
    lo, hi = hours
    if lo <= hi:
        return lo <= hour < hi
    return hour >= lo or hour < hi


class SceneEngine:
    def __init__(
        self,
        state: SceneState,
        templates: List[EventTemplate],
        rng: Optional[random.Random] = None,
        cast: Optional[Dict] = None,
        anti_silence_minutes: float = 0.0,
        follow_up_minutes: float = 180.0,
        event_rate_multiplier: float = 1.0,
        quiet_buffer_minutes: float = 0.0,
        news_hold_minutes: float = 0.0,
    ):
        self.state = state
        self.templates = templates
        self.rng = rng or random.Random()
        self.cast = cast or {}                       # cast.json：全部角色的在场规则与认知反差提示
        self.anti_silence_minutes = anti_silence_minutes   # 场景时间超过该时长无事件 → 强制日常闲聊（0=关闭）
        self.follow_up_minutes = follow_up_minutes   # 话题开启后间隔该时长（场景分钟）→ 引擎触发续聊
        self.event_rate_multiplier = event_rate_multiplier   # 事件生成频率倍率（<1 降频）
        # 对话缓冲：双方任一方最近开口后 N 分钟内，**一条主动事件都不产生**（0=关闭）。
        #
        # 为什么必须放在引擎里、而不是决策层（scene/decision.py）：
        # `_fire()` 是**先扣冷却再交给决策层**的——被决策层挡下的事件，
        # 冷却已经消耗掉了，等于"没发出去但报废了"。时令见闻的冷却是 3~7 天，
        # 挡一次就等于**永久丢掉这条见闻**。所以硬闸必须在开火之前拦。
        self.quiet_buffer_minutes = quiet_buffer_minutes
        # 见闻静默：带 `news` 标签的模板，在对话结束后再压这么久（0=关闭）。
        # 同样是"不选它"而不是"选了再挡"，冷却不会白扣。
        self.news_hold_minutes = news_hold_minutes
        self.idle_templates = [t for t in templates if t.idle]
        self.follow_up_templates = [t for t in templates if t.follow_up]
        self._last_event_minutes = state.scene_minutes()   # 最近一次事件发生的场景时刻
        self._rare_day: Dict[str, int] = {}          # 稀有角色每天至多出现一次
        self._undo: Optional[dict] = None            # 上一次开火的回滚点（决策层挡下时还原）

    # ---------- 对话缓冲 ----------

    def minutes_since_activity(self) -> float:
        """距"任一方最后一次开口"过了多少分钟；从未开口过返回一个很大的数。

        任一方 = 用户发言 或 她自己的发言。只看用户的话，用户沉默几分钟就被
        当成"人走了"，于是在长对话中途把一条见闻推出去——这正是要修的问题。
        """
        s = self.state
        last = max(float(s.last_user_message_ts or 0.0),
                   float(getattr(s, "last_conversation_ts", 0.0) or 0.0))
        if last <= 0:
            return float("inf")
        return max(0.0, (time.time() - last) / 60.0)

    def _in_quiet_buffer(self) -> bool:
        if self.quiet_buffer_minutes <= 0:
            return False
        return self.minutes_since_activity() < self.quiet_buffer_minutes


    # ---------- 主循环 ----------

    def tick(
        self,
        minutes: float = 30.0,
        user_inactive_minutes: float = 0.0,
        real_minutes: Optional[float] = None,
    ) -> List[SceneEvent]:
        s = self.state
        s.tick_minutes(minutes, self.rng, real_minutes=real_minutes)
        self._evolve_location(minutes)
        self._evolve_presence(minutes)

        # 冷却递减：必须按**真实经过的时间**扣，不能按 tick 的场景分钟步长。
        # 同步模式下 tick 间隔 30 秒却推进 30 场景分钟（场景时间 = 真实北京时间），
        # 按步长扣等于快了 60 倍：设为 720 分钟（12 小时）的冷却 12 分钟就耗光。
        # 实测后果：welcome_back 投递 187 次、间隔中位数 24 分钟，153 次间隔不足 1 小时——
        # 她反复说「你终于回来了」，上下文连贯性被彻底破坏。
        elapsed = real_minutes if (getattr(s, "sync_to_utc8", False) and real_minutes) else minutes
        for k in list(s.cooldowns):
            s.cooldowns[k] -= elapsed
            if s.cooldowns[k] <= 0:
                del s.cooldowns[k]

        scene_now = s.scene_minutes()
        # ── 对话缓冲：任一方刚开口，本轮一条主动事件都不产生 ──
        # 放在这里而不是函数最开头：时钟、天气、在场、冷却的推进都在上面做完了，
        # 所以缓冲期内**场景照常演化**，只是不开口（否则世界会跟着冻住）。
        # 也放在"常规事件"之前：连选都不选，冷却就不会被白扣。
        if self._in_quiet_buffer():
            return []
        # 常规事件（命中即返回，一次 tick 至多一个；续聊模板只由话题机制触发）
        for tpl in self.templates:
            if tpl.follow_up:
                continue
            if self._suppressed(tpl, scene_now, user_inactive_minutes):
                continue
            if self.rng.random() > tpl.probability * self.event_rate_multiplier:
                continue
            return [self._fire(tpl)]

        # 多轮话题续聊：上次抛话题已被 OpenClaw 回应（conversation 开启）且间隔足够 → 触发续聊
        # 用 getattr 兜底：万一 conversation 属性缺失（历史 state.json、或旧版本
        # 写下的坏状态），也只是这一轮不续聊，而不是让整个引擎崩掉。
        conv = getattr(s, "conversation", None) or {}
        if conv and (scene_now - conv.get("minute", scene_now)) >= self.follow_up_minutes:
            for tpl in self.follow_up_templates:
                if not self._suppressed(tpl, scene_now, user_inactive_minutes):
                    return [self._fire_follow_up(tpl, conv)]

        # 防沉默：长时间没有事件 → 强制触发一条日常闲聊（跳过概率，只按条件筛选）
        if self.anti_silence_minutes > 0 and (scene_now - self._last_event_minutes) >= self.anti_silence_minutes:
            for tpl in self.idle_templates:
                if not self._suppressed(tpl, scene_now, user_inactive_minutes):
                    return [self._fire(tpl)]
        return []

    # ---------- 判定与触发 ----------

    def _suppressed(self, tpl: EventTemplate, scene_now: float, user_inactive_minutes: float) -> bool:
        s = self.state
        if s.cooldowns.get(tpl.id, 0) > 0:
            return True
        # 见闻静默：对话刚结束也不能立刻换个话题讲时令见闻。
        # 这里返回 True 是"根本没选它"，冷却不会被消耗，等静默期过了照样能发。
        if (self.news_hold_minutes > 0 and "news" in (tpl.tags or [])
                and self.minutes_since_activity() < self.news_hold_minutes):
            return True
        # 回执反哺：同类事件已有 replied/processed 回执且仍在冷却期内 → 不再触发
        last_r = s.receipt_meta.get(tpl.id)
        if last_r is not None and (scene_now - last_r) < tpl.cooldown_minutes:
            return True
        return not tpl.matches(s, user_inactive_minutes)

    # ---------- 开火与回滚 ----------

    def _snapshot(self, tpl: EventTemplate) -> None:
        """记下开火前的现场，供决策层挡下时回滚。

        为什么要回滚：引擎是**先开火（扣冷却、加好感度）再交决策层**的。
        决策层挡下的事件等于"没送出去但冷却已报废"——实测白扣率高达 70%，
        240 分钟冷却的模板实际变成 ~857 分钟，内容多样性被无声压缩 3 倍多；
        好感度也跟着虚涨（没送出去的事件照样加分，而好感度是模型切换的判据）。
        """
        s = self.state
        # 注意：快照必须是**独立副本**。写成
        #     s.cooldowns = dict(s.cooldowns); undo = {"cooldowns": s.cooldowns}
        # 会让 undo 和 s.cooldowns 指向同一个 dict，开火写入直接污染快照，回滚就失效了。
        self._undo = {
            "template_id": tpl.id,
            "cooldowns": dict(s.cooldowns),
            "affinity": s.affinity,
            "last_event_minutes": self._last_event_minutes,
            "conversation": dict(getattr(s, "conversation", None) or {}),
        }

    def rollback_last_fire(self) -> bool:
        """决策层挡下时调用：撤销上一次开火造成的冷却/好感度/会话轮次副作用。

        返回是否真的回滚了。没有待回滚的现场时返回 False（不报错）。
        """
        undo = getattr(self, "_undo", None)
        if not undo:
            return False
        s = self.state
        s.cooldowns = undo["cooldowns"]
        s.affinity = undo["affinity"]
        self._last_event_minutes = undo["last_event_minutes"]
        if getattr(s, "conversation", None) is not None or undo["conversation"]:
            s.conversation = undo["conversation"]
        self._undo = None
        return True

    def commit_last_fire(self) -> None:
        """决策层放行（事件真的要投递）时调用：丢弃回滚点，让副作用生效。"""
        self._undo = None

    def _fire(self, tpl: EventTemplate) -> SceneEvent:
        s = self.state
        self._snapshot(tpl)
        s.cooldowns[tpl.id] = tpl.cooldown_minutes
        s.affinity = max(0.0, min(100.0, s.affinity + tpl.affinity_delta))
        self._last_event_minutes = s.scene_minutes()
        fmt = {
            "location": LOCATION_CN.get(s.location, s.location),
            "weather": WEATHER_CN.get(s.weather, s.weather),
        }
        return SceneEvent(
            template_id=tpl.id,
            type=tpl.type,
            title=tpl.title or tpl.id,
            description=tpl.description.format(**fmt),
            instruction=tpl.instruction.format(**fmt),
            priority=tpl.priority,
            tags=list(tpl.tags),
            affinity_delta=tpl.affinity_delta,
            characters=list(tpl.characters),
            image=tpl.image,
        )

    def _fire_follow_up(self, tpl: EventTemplate, conv: dict) -> SceneEvent:
        """续聊：把当前话题 {topic} 织入续聊模板，推进会话轮次，收尾后关闭话题。"""
        s = self.state
        self._snapshot(tpl)                  # 含会话快照：挡下时轮次要退回去
        s.cooldowns[tpl.id] = tpl.cooldown_minutes
        s.affinity = max(0.0, min(100.0, s.affinity + tpl.affinity_delta))
        self._last_event_minutes = s.scene_minutes()

        conv["rounds"] = conv.get("rounds", 0) + 1
        if conv["rounds"] >= 2:
            # ⚠️ 必须是赋值空字典，**不能写 `del s.conversation`**。
            # SceneState 是 dataclass，conversation 用 default_factory=dict——
            # 删掉实例属性后类上并没有同名默认值可回落，再次访问直接
            # AttributeError。而 tick() 里第一件事就是 `s.conversation`，
            # 于是**下一个 tick 必崩**（连 state.save() 都会崩），
            # main.py 的 engine.tick() 没有 try 包裹、又没有 LaunchAgent
            # 自动拉起 → 引擎进程直接死掉，角色永久沉默。实测确认。
            s.conversation = {}
        else:
            conv["minute"] = s.scene_minutes()   # 还有一轮，重置计时

        topic = conv.get("topic", "刚才那件事")
        return SceneEvent(
            template_id=tpl.id,
            type="follow_up",
            title=tpl.title or tpl.id,
            description=tpl.description.format(topic=topic),
            instruction=tpl.instruction.format(topic=topic),
            priority=tpl.priority,
            tags=list(tpl.tags),
            affinity_delta=tpl.affinity_delta,
            characters=[],
        )

    # ---------- 场景演化 ----------

    def _evolve_location(self, minutes: float) -> None:
        """角色在璃月港各处游走（按时段加权：工作偏堂内/街市，晚间偏街头出殡路线，深夜偏院内）。"""
        if self.rng.random() < 0.08 * (minutes / 30.0):
            weights = self._location_weights()
            self.state.location = self.rng.choices(LOCATIONS, weights=weights, k=1)[0]

    def _location_weights(self) -> List[float]:
        h = self.state.hour
        base = [1.0] * len(LOCATIONS)
        idx = {loc: i for i, loc in enumerate(LOCATIONS)}
        if 8 <= h < 12 or 13 <= h < 17:      # 工作时段：往生堂 + 街市
            base[idx["hall"]] += 2.0
            base[idx["street"]] += 1.0
        elif 12 <= h < 13:                    # 午饭：万民堂一带
            base[idx["street"]] += 1.5
            base[idx["tavern"]] += 1.0
        elif 17 <= h < 19:                    # 晚饭
            base[idx["tavern"]] += 1.5
            base[idx["street"]] += 1.0
        elif 19 <= h < 22:                    # 晚间出殡路线：街头/码头
            base[idx["street"]] += 2.5
            base[idx["harbor"]] += 1.5
        else:                                 # 深夜：院内/堂内
            base[idx["yard"]] += 1.5
            base[idx["hall"]] += 1.0
        return base

    def _evolve_presence(self, minutes: float) -> None:
        """按 cast.json 的 presence 规则演化全部 NPC 在场（数据驱动，加角色只改配置）。

        模式：
          standard   —— 常规在场（默认概率 0.6/tick）
          conditional—— 条件在场（附加 min_affinity 等门槛）
          rare       —— 稀有在场（低概率 + 每天至多一次 + 带出现原因）
        """
        s = self.state
        scale = minutes / 30.0

        # 在场 NPC 有概率离开
        for c in list(s.present):
            if self.rng.random() < 0.3 * scale:
                s.present.remove(c)
                s.present_reason.pop(c, None)

        if not self.cast:
            return

        MAX_PRESENT = 6
        for name, member in self.cast.items():
            if name in s.present or len(s.present) >= MAX_PRESENT:
                continue
            p = member.get("presence", {})
            mode = p.get("mode", "standard")
            if not _in_hours(s.hour, p.get("hours", [8, 22])):
                continue
            if s.location not in p.get("locations", []):
                continue
            if mode == "conditional" and s.affinity < p.get("min_affinity", 0):
                continue
            if mode == "rare" and s.day == self._rare_day.get(name, -1):
                continue
            base = p.get("base_probability", {"standard": 0.6, "conditional": 0.3, "rare": 0.08}.get(mode, 0.2))
            if self.rng.random() >= base * scale:
                continue
            reason = None
            if mode == "rare" and p.get("reasons"):
                reason = self.rng.choice(p["reasons"])
            s.present.append(name)
            if reason:
                s.present_reason[name] = reason
            if mode == "rare":
                self._rare_day[name] = s.day
