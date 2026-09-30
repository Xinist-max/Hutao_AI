#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说话风格去模板化：记录她回复的**形状**，在她开始重复自己时给出提醒。

## 为什么需要（实测数据）

对真实对话语料（398 条回复 / 2784 个段落）做统计：

    最高的那一种段落开头只占          3%   —— 不存在"一个模板打天下"
    但出现 ≥12 次的开口合计占        40%   —— 几十个高频"起手式"吃掉四成段落
    含"在别处也反复出现"片段的段落占  74%
    段落两两相似度                   0.003 —— 不是逐字复读

结论：问题不是复读同一句，而是**一套固定的起手式在反复使用**。
所以对策也不是"禁止某一句"，而是**让引擎盯住她最近的起法，集中了就提醒换一个**。

## 设计要点

1. **只看形状，不存内容全文**：每条回复只留"开头指纹"（前 N 字）、段落数、
   结尾标点。这些是判别"是不是同一套起手"的最小信息。
2. **提醒要给出具体的"别再用这个"**：只泛泛说"换个说法"没用——模型需要知道
   它刚用过什么，这和 `scene/actions.py` 的「刚用过（别重复）」是同一个思路。
   提醒文本只进她的提示词，不进运行日志。
3. **只在真的集中时才提醒**：偶尔重复是自然的，天天提醒反而变成新的模板。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "logs", "style_memory.json")

# 和 tools/style_analysis.py 的判据保持一致：开头取前 4 字
HEAD_LEN = 4


def _head(text: str) -> str:
    """取"起法指纹"：第一个非空段落的前 N 字（去掉空白）。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s[:HEAD_LEN]
    return ""


def _para_count(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", text or "") if p.strip()])


def _tail(text: str) -> str:
    s = (text or "").rstrip()
    return s[-1] if s else ""


def _norm_for_cmp(t: str) -> str:
    """比对重复时把空白与括号动作归一化——"（抬头）在呢"和"（抬头看你一眼）在呢"
    算不算重复，取决于内容差异；这里只压掉空白，保留动作差异。"""
    return re.sub(r"\s+", "", t or "")


def similarity(a: str, b: str) -> float:
    """两条回复的相似度（0~1）。用 difflib，纯标准库。"""
    import difflib
    return difflib.SequenceMatcher(None, _norm_for_cmp(a), _norm_for_cmp(b)).ratio()


def find_duplicate(text: str, recent: list, threshold: float = 0.85) -> str:
    """在 recent 里找与 text 高度相似的一条，返回那条原文；没有则返回空串。

    为什么要这道守卫：实测真实日志里出现过**同一条回复被重复投递**的情况——
    2026-09-01 那个会话里她有 **52 次回复长度完全相同（360 字）**，
    63 对相邻回复中 49 对相似度 ≥95%。那不是模型在复读，是链路把同一句话发出去了。
    不管是模型复读还是重复投递，用户看到的都是"字一模一样"，
    所以在**发送前**拦一道是最省事、也最不依赖病因的修法。
    """
    if not text:
        return ""
    for prev in recent[-8:]:
        if not prev:
            continue
        # 完全一致直接判重（最常见的情况，不必算相似度）
        if _norm_for_cmp(prev) == _norm_for_cmp(text):
            return prev
        if similarity(prev, text) >= threshold:
            return prev
    return ""


class StyleTracker:
    """最近 N 条回复的形状记忆 + 集中度提醒。"""

    def __init__(self, path: str = DEFAULT_PATH, keep: int = 12,
                 head_repeat_limit: int = 3, para_repeat_limit: int = 5):
        self.path = path
        self.keep = keep                                   # 记最近多少条
        self.head_repeat_limit = head_repeat_limit         # 同一个起法出现几次算"扎堆"
        self.para_repeat_limit = para_repeat_limit         # 同一段落数连续几次算"呆板"
        self.items: List[dict] = []                        # [{ts, head, paras, tail}]
        self._hint_ts = 0.0
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = list(data.get("items", []))[-self.keep:]
        except (OSError, json.JSONDecodeError):
            self.items = []

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "items": self.items[-self.keep:]}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---------- 记账 ----------
    def record(self, text: str) -> None:
        """记下这一条的形状。不保存正文。"""
        head = _head(text)
        if not head:
            return
        self.items.append({"ts": time.time(), "head": head,
                           "paras": _para_count(text), "tail": _tail(text)})
        self.items = self.items[-self.keep:]
        self.save()

    # ---------- 提醒 ----------
    def concentration(self) -> tuple:
        """返回 (最集中的起法, 次数, 样本数)。次数<2 表示没有明显重复。"""
        if not self.items:
            return "", 0, 0
        counts: dict = {}
        for it in self.items:
            counts[it.get("head", "")] = counts.get(it.get("head", ""), 0) + 1
        head, n = max(counts.items(), key=lambda kv: kv[1])
        return head, n, len(self.items)

    def hint(self, max_chars: int = 110, cooldown_minutes: float = 90.0) -> str:
        """需要提醒时返回一句话，否则返回空串。

        只在她**最近这一批**明显扎堆时才说话，而且有冷却——否则提醒本身
        会变成每条都出现的新模板。
        """
        now = time.time()
        if self._hint_ts and (now - self._hint_ts) / 60.0 < cooldown_minutes:
            return ""
        parts = []

        head, n, total = self.concentration()
        if n >= self.head_repeat_limit and head:
            parts.append(f"最近 {total} 条里有 {n} 条用「{head}」起头，这条换个起法")

        # 段落数呆板：连续多条段数一样
        paras = [it.get("paras", 0) for it in self.items[-self.para_repeat_limit:]]
        if len(paras) >= self.para_repeat_limit and len(set(paras)) == 1 and paras[0] > 1:
            parts.append(f"最近 {len(paras)} 条都是 {paras[0]} 段，这条把长短打散")

        # 结尾标点呆板
        tails = [it.get("tail", "") for it in self.items[-6:]]
        if len(tails) >= 6 and len(set(tails)) == 1 and tails[0]:
            parts.append("最近几条结尾标点一模一样，换个收法")

        if not parts:
            return ""
        self._hint_ts = now
        return ("【说话方式·别落进套路】" + "；".join(parts) + "。")[:max_chars]

    # ---------- 体检 ----------
    def stats(self) -> dict:
        """纯数字的自检指标（供 tools/style_analysis.py 与体检脚本用）。"""
        if not self.items:
            return {}
        counts: dict = {}
        for it in self.items:
            counts[it.get("head", "")] = counts.get(it.get("head", ""), 0) + 1
        total = len(self.items)
        top = max(counts.values()) if counts else 0
        return {
            "样本": total,
            "不同起法": len(counts),
            "最高起法占比": round(top / total, 3),
            "出现≥3次的起法数": sum(1 for c in counts.values() if c >= 3),
        }


_tracker: Optional[StyleTracker] = None


def tracker() -> StyleTracker:
    """进程内单例（与 scene/actions.py 的 tracker() 同样的用法）。"""
    global _tracker
    if _tracker is None:
        _tracker = StyleTracker()
    return _tracker


def session_reply_similarity(session_key: str, n: int = 4) -> tuple:
    """读会话里她最近 n 条回复，返回 (相邻相似度列表, 她最近的回复列表)。

    **隐私**：这些文本只在**本机进程内存**里用于算相似度——不打印、不写文件、
    不进日志。外部只看到相似度数字。

    为什么要读会话：直聊的回复由 OpenClaw 直接生成发出，引擎拦不到。
    唯一能作用于那条链路的杠杆是**上下文本身**——实测复读时会出现
    "同一段 336 字隔 17 分钟逐字再现"，那是被整段记住的原文，
    得把那段上下文清掉（触发会话压缩）才能断开。
    """
    if not session_key:
        return [], []
    try:
        from .delivery import _session_jsonl_path
        path = _session_jsonl_path(session_key)
    except Exception:                                        # noqa: BLE001
        return [], []
    if not path:
        return [], []
    replies = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"role": "assistant"' not in line and '"role":"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = rec.get("message") or {}
                if m.get("role") != "assistant":
                    continue
                c = m.get("content")
                if isinstance(c, list):
                    c = "\n".join(str(x.get("text", "")) for x in c
                                   if isinstance(x, dict) and x.get("type") == "text")
                t = str(c or "").strip()
                if t:
                    replies.append(t)
    except OSError:
        return [], []
    replies = replies[-max(2, n):]
    sims = [similarity(replies[i - 1], replies[i]) for i in range(1, len(replies))]
    return sims, replies


def looks_like_repeating(session_key: str, threshold: float = 0.85, n: int = 4) -> tuple:
    """她是不是在复读？返回 (是否复读, 说明)。"""
    sims, replies = session_reply_similarity(session_key, n)
    if len(sims) < 2:
        return False, ""
    # 判据：最近 3 对里至少 2 对超阈值。
    # 用"2/3"而不是"最近 2 对都要超标"——实测真实的复读窗口是
    # `98% / 100% / 84%` 这种形态，最后一条掉到 84% 就漏判了。
    recent = sims[-3:]
    hits = [v for v in recent if v >= threshold]
    if len(hits) >= 2:
        return True, (f"最近 {len(recent)+1} 条回复里有 {len(hits)} 对高度相似："
                      f"{'/'.join(f'{v:.0%}' for v in recent)}（阈值 {threshold:.0%}）")
    return False, ""


# ── 动态注入：把"最近说过太多次的起手式"写进她会读到的文件 ──
#
# ## 为什么用这个办法（实测依据）
#
# OpenClaw 注入**固定清单**的工作区文件，实测整行命中率：
#   SOUL.md 168/168、IDENTITY.md 35/35、USER.md 21/21、MEMORY.md 40/40、
#   AGENTS.md 70/70、TOOLS.md 11/11
# 而 HEARTBEAT.md 是 0/14、自定义的 STYLE.md 是 0/3 —— **自定义文件不注入**。
# 所以要让直聊那条链路（引擎完全绕过）看见动态提醒，**只能写进那六个之一**。
#
# MEMORY.md 是"她的记忆"，放"最近老用同一个起手"语义上正好。
#
# ## 安全约束
#
# · 只改 `<!-- AUTO:STYLE BEGIN -->` 与 `<!-- AUTO:STYLE END -->` 之间的内容
# · 两个标记都不存在时**不创建**（避免动用户的文件结构，需人工先放好标记）
# · 块内只放"起手式片段 + 次数"，不放整段回复
_WS = os.path.expanduser("~/.openclaw/workspace")
STYLE_FILE = os.path.join(_WS, "MEMORY.md")
_BEGIN = "<!-- AUTO:STYLE BEGIN -->"
_END = "<!-- AUTO:STYLE END -->"


# 身体反应的"族"：同一族里换几个字（脸红/脸红透/脸瞬间红到耳根）**还是同一套**，
# 精确字符串匹配抓不到，必须按语义归类。这是实测出来的——
# 她 20 条回复里 `（脸瞬间` 5 次、`（深吸一` 4 次、`（耳朵瞬` 3 次，
# 但按 6 字精确匹配一个都凑不够 3 次，检测器全漏了。
_REACTION_FAMILIES = (
    ("脸红", ("脸", "红", "耳根", "发烫")),
    ("耳朵", ("耳朵", "耳尖", "耳根")),
    ("深吸气", ("深吸", "吸气", "呼气", "喘")),
    ("整个人一顿", ("整个人", "浑身", "僵", "愣", "顿住")),
    ("眯眼笑", ("眯", "笑", "勾唇", "嘴角")),
    ("抬眼", ("抬眼", "抬头", "低头", "垂眼")),
)


def opening_profile(session_key: str, n: int = 20) -> dict:
    """她最近 n 条回复的**开头形态**画像（纯统计）。

    重点看两个数：
      `punct_rate`   —— 有多少条以括号动作开头（真人不会每条都先垫一个动作）
      `family_rate`  —— 这些动作里有多少落在同一族身体反应上（脸红/耳朵/深吸气…）
    """
    _sims, replies = session_reply_similarity(session_key, n)
    if not replies:
        return {}
    import collections
    paren = 0
    fams = collections.Counter()
    heads = collections.Counter()
    for r in replies:
        h = _head_of(r, 8)
        if not h:
            continue
        if h.startswith(("（", "(")):
            paren += 1
            for fname, kws in _REACTION_FAMILIES:
                if any(k in h for k in kws):
                    fams[fname] += 1
                    break
        heads[_head_of(r, 4)] += 1
    top_head, top_n = (heads.most_common(1)[0] if heads else ("", 0))
    return {
        "样本": len(replies),
        "括号开头数": paren,
        "括号开头占比": round(paren / len(replies), 3),
        "同族反应数": sum(fams.values()),
        "反应族": dict(fams),
        "最高频开头": top_head, "最高频次数": top_n,
        "不同开头数": len(heads),
    }


def repeated_heads(session_key: str, n: int = 20, min_count: int = 3,
                   head_len: int = 4) -> list:
    """找出**反复使用的起手式**，返回 [(片段, 次数)]。

    两条判据并用，取并集：
      ① 精确开头重复 ≥ min_count 次（如「（脸瞬间」出现 5 次）
      ② 整个开头形态退化：括号开头占比 ≥70% 且同族身体反应 ≥60%
         —— 那种情况即使每个开头字面不同，读起来也是"同一个模子"
    """
    prof = opening_profile(session_key, n)
    if not prof:
        return []
    _sims, replies = session_reply_similarity(session_key, n)
    import collections
    c = collections.Counter(_head_of(r, head_len) for r in replies if _head_of(r, head_len))
    out = [(h, k) for h, k in c.most_common(6) if k >= min_count]
    # 判据②：形态退化——用一个显式条目表达，好让提醒说得具体
    if prof["括号开头占比"] >= 0.7 and prof["同族反应数"] >= max(3, prof["括号开头数"] * 0.6):
        top_fams = sorted(prof["反应族"].items(), key=lambda kv: -kv[1])[:3]
        desc = "、".join(f"{k}×{v}" for k, v in top_fams)
        out.insert(0, (f"（…）身体反应开头（{desc}）", prof["括号开头数"]))
    return out


def _head_of(text: str, head_len: int = 6) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s[:head_len]
    return ""


def update_style_block(session_key: str, path: str = STYLE_FILE) -> str:
    """把她反复用的起手式写进 MEMORY.md 的自动块。返回说明（无改动返回空串）。

    没有 BEGIN/END 标记就什么都不做——需要人工先在那份文件里放好标记块。
    """
    heads = repeated_heads(session_key)
    if not heads:
        return ""
    # ⚠️ 措辞是**实测调出来的**，不是随手写的：
    # 第一版写「别重复这些开头」，A/B 实测**完全无效**——括号开头 100% 没变，
    # 只是换成别的括号动作（模型理解成「换一种动作」，而不是「不要再放开头」）。
    # 第二版直接禁「括号放开头」，并给出可行替代。
    body = ["## 说话方式提醒（引擎自动更新，每条都看一眼）",
            "",
            "**硬要求：不要用「（动作）」开头。** 这是最近最扎眼的问题。",
            ""]
    if heads:
        body.append("最近的开头是这样的（重复得太多）：")
        for h, k in heads[:4]:
            body.append(f"- 用过 {k} 次「{h}」")
        body.append("")
    # ⚠️ 第二版实测把括号开头从 100% 压到 80%，但**有一条回复变成了 NO_REPLY**
    # （OpenClaw 的「无话可说」标记）——因为我写了「大部分回复一个括号都不要有」，
    # 模型把它读成「少说话/别说」。那会让她变哑巴，比模板化严重得多。
    # 第三版只针对**开头**，并明确「该说的一句都不能少」，消除这个副作用。
    body += [
        "**怎么改**（只改开头，话该说多少还说多少）：",
        "- 第一条消息**直接开口说事**，把动作留到后面几条去写：",
        "  「今儿码头上人多得很」比「（抬头看你一眼）今儿码头上人多得很」像真人",
        "- 真想写动作，**放到句子中间或末尾**，别放最前面当引子",
        "- 那种一开口就「（脸瞬间红透）」「（深吸一口气）」「（整个人僵住）」的写法，"
        "这一条不许出现",
        "",
        "> 只说开头怎么写，**不许因此少说或不说**——该讲的事一句都不能省。",
    ]
    new_block = _BEGIN + "\n" + "\n".join(body) + "\n" + _END
    try:
        with open(path, encoding="utf-8") as f:
            cur = f.read()
    except OSError:
        return ""
    if _BEGIN not in cur or _END not in cur:
        return ""                       # 没有标记块就不动这份文件
    i = cur.index(_BEGIN); j = cur.index(_END) + len(_END)
    if cur[i:j] == new_block:
        return ""                       # 内容没变，不写盘
    out = cur[:i] + new_block + cur[j:]
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(out)
        os.replace(tmp, path)
    except OSError:
        return ""
    return f"已更新 {os.path.basename(path)} 的说话方式块（{len(heads)} 个重复起手式）"
