"""情绪识别与双层情绪模型（瞬时情绪 + 持续心情）。

不依赖任何 API：基于中文语气词/标点/关键词的本地分类（零成本），
可选支持 OpenClaw 在回复中内嵌 `[[emotion:joy,intensity:0.8]]` 标签（精度更高，但会出现在正文里，
故默认关闭，仅在"不上微信、只上屏"的场景建议启用）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Tuple

EMOTIONS = [
    "joy", "amused", "smug", "shy", "surprise", "annoyed", "sad",
    "worried", "curious", "thinking", "sleepy", "serious", "excited", "neutral",
]

# 关键词 → 情绪（按优先级从上到下匹配；命中即返回）
_KEYWORDS: list = [
    ("excited",  ["太棒了", "太好了", "哇塞", "哇哦", "冲呀", "走啦走啦", "！！", "!！"]),
    ("surprise", ["哎呀", "诶？", "咦", "什么？", "居然", "竟然", "天呐", "不是吧", "怎么会"]),
    ("smug",     ["嘿嘿", "哼哼", "本堂主早就", "我就说吧", "早就知道", "看吧", "得意"]),
    ("amused",   ["哈哈", "嘿嘿嘿", "笑死", "逗", "乐死", "有趣", "好玩", "噗"]),
    ("shy",      ["害羞", "不好意思", "别夸", "脸红", "讨厌啦", "人家", "嘿嘿嘿…"]),
    ("annoyed",  ["哼", "烦", "讨厌", "真是的", "又来了", "受不了", "抱怨", "气死"]),
    ("sad",      ["难过", "伤心", "唉", "唉呀", "遗憾", "舍不得", "哭", "泪"]),
    ("worried",  ["担心", "小心", "注意", "别感冒", "当心", "要不", "劝你", "保重"]),
    ("curious",  ["？", "?", "你知道吗", "你猜", "为什么", "怎么", "是不是", "要不要"]),
    ("thinking", ["让我想想", "琢磨", "寻思", "说不定", "也许", "大概", "嗯……", "嗯..."]),
    ("sleepy",   ["困", "睡", "打哈欠", "晚安", "休息", "梦里"]),
    ("serious",  ["生死", "无常", "逝者", "送别", "出殡", "往生", "郑重", "安息"]),
    ("joy",      ["开心", "高兴", "愉快", "喜欢", "好耶", "真不错", "美滋滋", "～"]),
]

# 表情符号/标点辅助
_PUNCT_BOOST = {
    "！": "excited", "!": "excited",
    "……": "sad", "...": "sad",
    "？": "curious", "?": "curious",
}

_TAG_RE = re.compile(r"\[\[\s*emotion\s*:\s*([a-zA-Z_]+)\s*(?:,\s*intensity\s*:\s*([0-9.]+)\s*)?\]\]")


def parse_tag(text: str) -> Tuple[str | None, float | None, str]:
    """解析并剥离 `[[emotion:joy,intensity:0.8]]` 标签。返回 (情绪, 强度, 清洗后文本)。"""
    m = _TAG_RE.search(text or "")
    if not m:
        return None, None, text
    emo = m.group(1).lower()
    if emo not in EMOTIONS:
        emo = None
    inten = float(m.group(2)) if m.group(2) else 0.8
    return emo, inten, _TAG_RE.sub("", text).strip()


def classify(text: str) -> Tuple[str, float]:
    """本地关键词分类，返回 (情绪, 强度 0~1)。"""
    t = text or ""
    for emo, words in _KEYWORDS:
        for w in words:
            if w in t:
                # 命中词的"情绪浓度"：感叹号/重复语气词提高强度
                intensity = 0.6
                if "！" in t or "!" in t:
                    intensity = min(1.0, intensity + 0.2)
                if any(x in t for x in ("哈哈", "嘿嘿", "！！")):
                    intensity = min(1.0, intensity + 0.15)
                return emo, intensity
    for p, emo in _PUNCT_BOOST.items():
        if p in t:
            return emo, 0.5
    return "neutral", 0.3


def infer(text: str) -> Tuple[str, float, str]:
    """综合推断：优先标签，其次关键词。返回 (情绪, 强度, 清洗后文本)。"""
    emo, inten, clean = parse_tag(text)
    if emo:
        return emo, inten or 0.8, clean
    e, i = classify(text)
    return e, i, text


@dataclass
class MoodModel:
    """双层情绪：瞬时情绪 E_t（本条消息） + 持续心情 M_t（累积衰减）。"""

    decay: float = 0.85          # 心情衰减系数
    mood_weight: float = 0.35    # 心情在最终参数中的权重
    instant: Dict[str, float] = field(default_factory=dict)   # 当前瞬时情绪强度分布
    mood: Dict[str, float] = field(default_factory=dict)      # 持续心情分布

    def update(self, emotion: str, intensity: float = 0.8) -> None:
        """新消息到达：刷新瞬时情绪，并把情绪融入持续心情。"""
        self.instant = {e: 0.0 for e in EMOTIONS}
        self.instant[emotion] = max(0.0, min(1.0, intensity))
        # 心情：先衰减，再叠加新情绪
        for e in EMOTIONS:
            self.mood[e] = self.mood.get(e, 0.0) * self.decay
        self.mood[emotion] = min(1.0, self.mood.get(emotion, 0.0) + intensity * (1 - self.decay))

    def decay_step(self, dt: float = 1.0) -> None:
        """经过 dt 秒：心情按"每分钟衰减系数"衰减，瞬时情绪较快回落。

        decay 语义 = 每分钟保留比例（如 0.85 → 一分钟后剩 85%）。
        """
        if dt <= 0:
            return
        mood_factor = self.decay ** (dt / 60.0)      # 每分钟衰减
        instant_factor = 0.6 ** (dt / 1.5)           # 瞬时情绪约 1.5 秒显著回落
        for e in EMOTIONS:
            self.mood[e] = self.mood.get(e, 0.0) * mood_factor
        for e in list(self.instant):
            self.instant[e] *= instant_factor

    def blend(self, emotion_params: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        """把瞬时情绪与持续心情混合成具体参数目标值。

        最终参数 = (1-w) × Σ(瞬时强度·情绪参数) + w × Σ(心情强度·情绪参数)
        """
        out: Dict[str, float] = {}
        for emo, params in emotion_params.items():
            wi = self.instant.get(emo, 0.0) * (1 - self.mood_weight)
            wm = self.mood.get(emo, 0.0) * self.mood_weight
            w = wi + wm
            if w <= 0.001:
                continue
            for pid, val in params.items():
                if pid.startswith("_"):
                    continue
                out[pid] = out.get(pid, 0.0) + val * w
        return out

    def dominant(self) -> str:
        """当前主导情绪（用于选择表情文件）。"""
        best, best_w = "neutral", 0.0
        for e in EMOTIONS:
            w = self.instant.get(e, 0.0) * (1 - self.mood_weight) + self.mood.get(e, 0.0) * self.mood_weight
            if w > best_w:
                best, best_w = e, w
        return best
