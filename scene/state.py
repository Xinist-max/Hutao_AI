"""数字化场景状态：纯逻辑数据 + 持久化（无图形、无 3D）。"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
from dataclasses import MISSING, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

# 好感度档位（数值区间）
AFFINITY_TIERS = [
    ("stranger", 0, 30),    # 陌生
    ("familiar", 30, 70),   # 熟悉
    ("close", 70, 101),     # 亲密
]

WEATHERS = ["sunny", "cloudy", "drizzle", "rain", "snow", "windy"]

# 天气枚举 → 中文显示名（用于事件描述/指令文案）
WEATHER_CN = {
    "sunny": "晴空",
    "cloudy": "多云",
    "drizzle": "小雨",
    "rain": "大雨",
    "snow": "飞雪",
    "windy": "大风",
}

# 璃月港场景地点
LOCATIONS = ["yard", "hall", "street", "tavern", "harbor", "terrace"]
LOCATION_CN = {
    "yard": "往生堂后院",
    "hall": "往生堂",
    "street": "绯云坡",
    "tavern": "三碗不过港",
    "harbor": "璃月码头",
    "terrace": "玉京台",
}


def tier_of(affinity: float) -> str:
    for name, lo, hi in AFFINITY_TIERS:
        if lo <= affinity < hi:
            return name
    # 兜底给**最高档**而不是最低：原来写 "stranger" 时，任何 ≥101 的值
    # （手工编辑、旧版 state.json）会被静默降级成陌生人档，方向正好反了。
    return AFFINITY_TIERS[-1][0]


@dataclass
class SceneState:
    """场景的数字化状态。

    hour 为 0~24 的小时（可带小数），day 为第几天；weather/location/character_state
    是有限枚举；affinity 是好感度（0~100）；cooldowns 是事件冷却表；
    last_user_message_ts 是用户最后一次发消息的真实时间戳（驱动"不插嘴"规则）。
    """

    day: int = 1
    hour: float = 8.0
    weather: str = "sunny"
    location: str = "yard"          # yard / hall / street / tavern / harbor / terrace
    character_state: str = "idle"   # idle / busy / sleeping / out
    affinity: float = 40.0
    cooldowns: dict = field(default_factory=dict)  # event_id -> 剩余冷却分钟
    last_user_message_ts: float = 0.0
    # 最近一次**任一方**开口的真实时间戳（用户或她自己都算）。
    # 与 last_user_message_ts 的区别：那个只记用户，用户沉默几分钟就被当成"人走了"；
    # 这个用来判断"对话是否还热着"——驱动主动发言的**对话缓冲**
    # （长对话中途不许冷不丁插一句见闻，见 scene/decision.py）。
    last_conversation_ts: float = 0.0
    last_decay_ts: float = 0.0                              # 上次亲密度衰减的真实时间戳（避免重复扣）
    present: List[str] = field(default_factory=list)        # 当前在场的 NPC（钟离/魈/温迪）
    present_reason: dict = field(default_factory=dict)      # NPC 在场原因（如温迪为什么出现）
    receipts: dict = field(default_factory=dict)            # 事件回执：event_id -> {status, reply...}
    receipt_meta: dict = field(default_factory=dict)        # 回执反哺：template_id -> 最近已回执的场景时刻(分钟)
    conversation: dict = field(default_factory=dict)        # 主动会话话题：{template_id, topic, minute, rounds}
    sync_to_utc8: bool = False                              # 场景时间与 UTC+8 真实时间同步（作息/饭点/出殡统一）
    _epoch_date: Optional[str] = None                       # 同步模式的纪元日期（ISO），day 以此为第 1 天

    def scene_minutes(self) -> float:
        """场景时间轴（分钟），用于回执反哺与冷却的比较（与真实时间无关）。"""
        return self.day * 1440 + self.hour * 60

    @property
    def tier(self) -> str:
        return tier_of(self.affinity)

    # ---------- 亲密度动态：可升也可降 ----------
    def decay_affinity(self, idle_hours: float, baseline: float = 40.0,
                       rate_per_hour: float = 0.002) -> float:
        """用户长期不互动 → 亲密度向 baseline 回落（"关系会冷"）。返回实际变化量。

        **为什么必须有衰减**：原来 47 个事件模板的 affinity_delta 全是正的、且没有任何衰减，
        于是亲密度单调涨到 100 就锁死。而模型路由按亲密度切云端/本地——
        结果就是只能「云→本」，**永远回不到云端**（实测：26 天从 49.5 涨到 100 后不动）。

        衰减按"超出 baseline 的那部分"每小时回落一个小比例，因此：
          · 越是刚建立的关系掉得越快，越深厚的越经得住冷落
          · 无论多久不互动，都不会掉到 baseline 以下（不至于变回陌生人）

        默认 0.002/小时：affinity=100、baseline=40 时约 2.9/天，
        约 2 天跌破 95（切回云端的阈值）、约 5 天跌破 88。
        """
        if idle_hours <= 0 or self.affinity <= baseline:
            return 0.0
        before = self.affinity
        self.affinity = max(baseline, self.affinity - (self.affinity - baseline) * rate_per_hour * idle_hours)
        return self.affinity - before

    def add_affinity(self, delta: float, ceiling: float = 100.0) -> float:
        """互动加成：用户真的回话/主动找来时，关系往前走一步。返回实际变化量。"""
        before = self.affinity
        self.affinity = max(0.0, min(ceiling, self.affinity + delta))
        return self.affinity - before

    def tick_minutes(
        self, minutes: float, rng: random.Random, real_minutes: Optional[float] = None
    ) -> None:
        """推进场景时间，演化天气与角色作息。

        sync_to_utc8=True 时，场景时间锚定 UTC+8 真实时钟（胡桃作息、饭点、出殡全部
        与真实时间统一）；否则按分钟推进（快进模式）。
        """
        if self.sync_to_utc8:
            self._sync_to_real_time()
        else:
            self.hour += minutes / 60.0
            while self.hour >= 24.0:
                self.hour -= 24.0
                self.day += 1
        # 天气演化按真实流逝时长（同步模式 tick 为真实 30 秒 ≈ 0.5 分钟，天气变化平缓）
        weather_min = (real_minutes if real_minutes is not None else (0.5 if self.sync_to_utc8 else minutes))
        self._evolve_weather(weather_min, rng)
        self._evolve_character()

    def _sync_to_real_time(self) -> None:
        now = datetime.now(timezone(timedelta(hours=8)))  # UTC+8
        if not self._epoch_date:
            self._epoch_date = now.date().isoformat()
            self.day = 1
        else:
            try:
                self.day = (now.date() - date.fromisoformat(self._epoch_date)).days + 1
            except ValueError:
                self.day = 1
        self.hour = now.hour + now.minute / 60.0

    def _evolve_weather(self, minutes: float, rng: random.Random) -> None:
        # 简单马尔可夫：每次 tick 以与时间成正比的小概率迁移天气
        if rng.random() < min(0.9, minutes / 240.0):
            self.weather = rng.choice(WEATHERS)

    def _evolve_character(self) -> None:
        """胡桃作息表（与 UTC+8 统一）：白天营业、晚间出殡、深夜休息。"""
        h = self.hour
        if h < 7.0:
            self.character_state = "sleeping"     # 深夜休息
        elif h < 8.0:
            self.character_state = "idle"         # 晨起梳洗
        elif h < 12.0:
            self.character_state = "busy"         # 上午工作（往生堂营业）
        elif h < 13.0:
            self.character_state = "idle"         # 午饭
        elif h < 17.0:
            self.character_state = "busy"         # 下午工作
        elif h < 18.0:
            self.character_state = "idle"         # 晚饭
        elif h < 22.0:
            self.character_state = "busy"         # 晚间工作（含出殡）
        else:
            self.character_state = "idle"         # 收尾休息

    # ---- 持久化 ----
    def save(self, path: str) -> None:
        """原子落盘：先写临时文件再 os.replace，避免写一半被杀留下坏状态。

        为什么不用 `asdict(self)`：万一某个字段的实例属性缺失（历史坏状态，
        或旧版本写过 `del obj.field` 这类代码），`asdict` 会直接 AttributeError
        ——连落盘都失败，状态就一路丢下去。这里逐字段取值并兜底。
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {}
        for f in fields(self):
            try:
                data[f.name] = getattr(self, f.name)
            except AttributeError:
                # 显式兜底：default → default_factory → None。
                # （用不了 Field.get_default——本机 Python 3.14 的 dataclasses 没这个方法。）
                if f.default is not MISSING:
                    data[f.name] = f.default
                elif f.default_factory is not MISSING:      # type: ignore[misc]
                    data[f.name] = f.default_factory()
                else:
                    data[f.name] = None
        # 用 mkstemp 而不是固定的 `{path}.tmp`：同时跑两个引擎（README 就教了
        # `--config config.live2d.json` 另起一路）时，两者会写同一个临时文件，
        # os.replace 可能把半截内容替换进去 → 状态损坏。
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)          # 原子替换：要么旧文件、要么新文件，不会有半个

    @classmethod
    def load(cls, path: str) -> "SceneState":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            # 状态文件坏了不能让它把引擎拖死——退回全新状态，至少还能跑起来。
            # 但**绝不能静默**：day/好感度/回执/冷却会一起归零（26 天的关系看起来像
            # 全新开局），而且 10 轮之后就会被新状态覆盖掉。所以先留一份现场再告警，
            # 让人有机会捞回来。
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = f"{path}.corrupt-{stamp}"
                shutil.copy2(path, backup)
                print(f"[state] ❌ 状态文件损坏（{type(exc).__name__}: {exc}）"
                      f"，已备份到 {backup}，本次以**全新状态**启动"
                      f"（第 {cls().day} 天 / 好感度 {cls().affinity:.0f}）")
            except OSError:
                print(f"[state] ❌ 状态文件损坏且无法备份（{type(exc).__name__}: {exc}）")
            return cls()
        if not isinstance(data, dict):
            return cls()
        allowed = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in allowed})
