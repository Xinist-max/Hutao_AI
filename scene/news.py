#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原神官方资讯 → 璃月港世界内事件。

## 这个模块解决的根本矛盾

官方资讯是**元游戏**的：版本号、卡池、祈愿、原石、UP——这些全是玩家视角的
系统词汇。而胡桃是**活在璃月港里的人**，她一提"卡池"就把第四面墙砸了
（见 SOUL.md「没有什么隔着我们」那一节费了多大劲才把墙砌好）。

所以这里**不做"把资讯喂给她"，而是做一层世界内翻译**：

  | 现实资讯                     | 翻译成她能经历的                       |
  |------------------------------|----------------------------------------|
  | 7.1「往冥府的安魂歌」         | 往生堂行业风向、港里传的歌谣           |
  | 卡池 UP 薇斯纳(风)            | 跑商的回来说港里来了个使风的外乡人     |
  | 魔神任务「无神怜爱的雪国」     | 远方（雪国）出了大事的风闻             |
  | 六周年 / 溯月系列             | 璃月港的节庆气氛、堂里接的活计变多     |
  | 逐月节（农历八月十五前后）     | 挂灯、供月、香菱试新菜                 |

**所有生成的事件都走"传闻"框架**（跑商的说的 / 听人讲 / 港里都在传）——
这样她复述一个只知姓名和元素的外乡人时**天然不需要编造性格**，
既不胡吣，也不出戏。

## 数据源

米游社官方公告 API（`hk4e-api.mihoyo.com`，CN 服，**无需鉴权**）：
`getAnnContent` 一次返回约 36 条公告正文。实测可用，不需要 key。
曾经考虑过的备选：`api.ennead.cc`、`api.hakush.in` 在本机**连不通**
（HTTP 000），`gi.yatta.moe` 通但没有版本/卡池层信息。

## 缓存与降级

公告更新很慢（几天一次），没必要每次 tick 都拉。
默认缓存 6 小时（`news.cache_hours`），**离线时回退到缓存**并标记 `stale`，
网络彻底不可用且无缓存时**返回空列表**——引擎照常跑，只是少了时令事件。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from .events import EventTemplate

CN_TZ = timezone(timedelta(hours=8))

ANN_URL = (
    "https://hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/getAnnContent"
    "?game=hk4e&game_biz=hk4e_cn&lang=zh-cn&bundle_id=hk4e_cn"
    "&platform=pc&region=cn_gf01&level=60"
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---------------------------------------------------------------- 解析

RE_VERSION = re.compile(r"「(.+?)」\s*(\d+\.\d+)\s*版本更新说明")
RE_BANNER = re.compile(r"「(.+?)」祈愿：「(.+?)」概率UP")
# 「雪宴之锋·薇斯纳(风)」→ 称号、名字、元素
RE_CHAR = re.compile(r"^(.+?)·(.+?)[（(]([^）)]+)[）)]$")
RE_STORY = re.compile(r"魔神任务「(.+?)」")
RE_WORLDQ = re.compile(r"「(.+?)」世界任务")
RE_EVENT = re.compile(r"「(.+?)」活动[：:]")
RE_REGION = re.compile(r"「(.+?)」时限内完成探索任务")

# 元素 → 世界内的说法（她不该说"风元素"这种系统词，但知道"使风的好手"）
ELEMENT_WORLD = {
    "风": "使风的好手", "水": "玩水的一把好手", "火": "玩火的高手",
    "雷": "带电的狠角色", "冰": "一手寒气", "岩": "石头里刨出来的硬功夫",
    "草": "跟草木亲近", "物理": "一身蛮力",
}


def _ssl_context() -> ssl.SSLContext:
    """找一个能用的 CA 包。

    本机是 python.org 版 Python 3.14，**默认信任库是空的**——
    直接 urlopen 会 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`，
    而同一条 URL 用 curl 却正常（curl 读系统钥匙串）。实测可用顺序：
    certifi → /etc/ssl/cert.pem（macOS 系统）。
    """
    for path in (_certifi_path(), "/etc/ssl/cert.pem"):
        if path and os.path.exists(path):
            try:
                return ssl.create_default_context(cafile=path)
            except Exception:
                continue
    return ssl.create_default_context()


def _certifi_path() -> Optional[str]:
    try:
        import certifi  # type: ignore
        return certifi.where()
    except Exception:
        return None


def _is_announcement_meta(title: str) -> bool:
    """过滤玩家社区/运营类公告——这些和她的世界毫无关系。"""
    junk = ("米游社", "签到", "调研", "首充", "返利", "公平运营", "防沉迷",
            "社区", "FAQ", "周边", "声明", "玩家社区", "礼包", "内容展示页")
    return any(j in title for j in junk)


def parse_announcements(items: List[dict]) -> dict:
    """把公告标题列表解析成结构化资讯。只读 title，不碰几百 KB 的 content 正文。"""
    titles = [a.get("title", "") for a in items if a.get("title")]

    out: Dict[str, object] = {
        "version": None, "banners": [], "weapon_banners": [],
        "story": None, "world_quests": [], "events": [], "regions": [],
        "anniversary": None,
    }

    for t in titles:
        if _is_announcement_meta(t):
            continue

        m = RE_VERSION.search(t)
        if m and not out["version"]:
            out["version"] = {"name": m.group(1), "no": m.group(2)}

        m = RE_BANNER.search(t)
        if m:
            pool, up_raw = m.group(1), m.group(2)
            # 武器池一张公告会并列多把：「单手剑·蝶变」「法器·漩流颂歌」
            ups = [u for u in up_raw.split("」「") if u]
            is_weapon = pool == "神铸赋形" or any(
                k in u for u in ups
                for k in ("单手剑·", "法器·", "长柄武器·", "弓·", "双手剑·")
            )
            key = "weapon_banners" if is_weapon else "banners"
            target = next((e for e in out[key] if e["pool"] == pool), None)
            if target is None:
                target = {"pool": pool, "up": [], "characters": []}
                out[key].append(target)
            for up in ups:
                if up not in target["up"]:
                    target["up"].append(up)
                # 角色池：拆出名字 + 元素（武器池不拆）
                cm = None if is_weapon else RE_CHAR.match(up)
                if cm:
                    target["characters"].append(
                        {"name": cm.group(2), "element": cm.group(3), "title": cm.group(1)}
                    )

        m = RE_STORY.search(t)
        if m and not out["story"]:
            out["story"] = {"kind": "魔神任务", "chapter": m.group(1)}

        m = RE_WORLDQ.search(t)
        if m and m.group(1) not in out["world_quests"]:
            out["world_quests"].append(m.group(1))

        m = RE_REGION.search(t)
        if m and m.group(1) not in out["regions"]:
            out["regions"].append(m.group(1))

        m = RE_EVENT.search(t)
        if m and m.group(1) not in out["events"]:
            out["events"].append(m.group(1))

        for word, label in (("周年", None), ("海灯节", "海灯节"), ("逐月节", "逐月节"),
                            ("风花节", "风花节"), ("羽球节", "羽球节")):
            if word in t:
                if word == "周年":
                    am = re.search(r"([一二三四五六七八九十]+)周年", t)
                    if am and not out["anniversary"]:
                        out["anniversary"] = am.group(1)
                elif label and label not in out["events"]:
                    out["events"].append(label)

    return out


# ---------------------------------------------------------------- 节日（按日期推导）

# 璃月的两大节庆。公告标题不一定会带上节名，所以**用现实日期窗口兜底推导**。
# 农历换算在标准库里做不了，这里用公历近似窗口（标注为 approx，宁可粗略也别乱认）。
FESTIVALS = [
    # (节日名, 起月, 起日, 止月, 止日, 属于璃月?)
    ("海灯节", 1, 20, 2, 20, True),    # 农历正月初一前后（春节）
    ("逐月节", 9, 10, 10, 10, True),   # 农历八月十五前后（中秋）
    ("风花节", 3, 1, 3, 31, False),    # 蒙德
    ("羽球节", 10, 1, 10, 31, False),  # 蒙德
]


def current_festival(now: Optional[datetime] = None) -> Optional[dict]:
    now = now or datetime.now(CN_TZ)
    y, mo, d = now.year, now.month, now.day
    for name, m1, d1, m2, d2, is_liyue in FESTIVALS:
        start = datetime(y, m1, d1, tzinfo=CN_TZ)
        end = datetime(y, m2, d2, 23, 59, tzinfo=CN_TZ)
        if start <= now <= end:
            return {"name": name, "liyue": is_liyue, "approx": True}
    return None


# ---------------------------------------------------------------- 抓取 + 缓存


def fetch(cache_path: str, cache_hours: float = 6.0, timeout: int = 20,
          force: bool = False) -> dict:
    """拉公告并解析。失败回退缓存。返回 dict（含 source/stale/fetched_at）。"""
    cached = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = None

    fresh_enough = (
        cached
        and not force
        and (time.time() - float(cached.get("fetched_ts") or 0)) < cache_hours * 3600
    )
    if fresh_enough:
        cached["source"] = "cache"
        cached["stale"] = False
        return cached

    try:
        req = urllib.request.Request(ANN_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        if raw.get("retcode") != 0:
            raise RuntimeError(f"retcode={raw.get('retcode')} {raw.get('message')}")
        items = (raw.get("data") or {}).get("list") or []
        parsed = parse_announcements(items)
        parsed["fetched_ts"] = time.time()
        parsed["fetched_at"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        parsed["ann_count"] = len(items)
        parsed["source"] = "api"
        parsed["stale"] = False
        parsed["festival"] = current_festival()
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cache_path)
        return parsed
    except Exception as e:  # 网络/解析任一出错都不该让引擎挂掉
        if cached:
            cached["source"] = "cache"
            cached["stale"] = True
            cached["error"] = f"{type(e).__name__}: {e}"
            return cached
        return {"version": None, "banners": [], "weapon_banners": [], "story": None,
                "world_quests": [], "events": [], "regions": [], "anniversary": None,
                "festival": current_festival(), "source": "none", "stale": True,
                "error": f"{type(e).__name__}: {e}", "fetched_ts": 0}


# ---------------------------------------------------------------- 世界内翻译

# 这些词**绝不能出现在她说出口的话里**——全是元游戏词汇。
META_WORDS = ["版本", "卡池", "祈愿", "概率UP", "UP", "原石", "纠缠之缘",
              "抽卡", "保底", "十连", "官方", "公告", "更新", "活动奖励",
              "限定", "复刻", "命座", "圣遗物", "深渊"]


def _rumor_instruction(topic: str) -> str:
    """统一的"传闻"框架——她只转述听到的，不编造细节。"""
    return (
        f"{topic}\n\n"
        "请以胡桃身份，把这个当成**街头听来的新鲜事**讲给他听——你是往生堂堂主，"
        "消息灵通，但**只说你确实听到的那点**，别人长什么样、脾气如何一律不知道就别说，"
        "更不要编。想知道他的反应就问一句。\n\n"
        "⚠️ 只说你世界里能知道的事：跑商的说的、港里都在传、同行捎来的口信。"
        "**绝对不许出现**" + "、".join(META_WORDS) + "这类词——你根本不知道这些是什么。"
    )


def world_events(news: dict, state=None) -> List[EventTemplate]:
    """把资讯翻译成**世界内事件模板**，可直接并进引擎的事件池。

    生成的事件 priority=medium、cooldown 很长（时令话题不该反复讲），
    tag 统一带 `news`，方便过滤和统计。
    """
    evs: List[EventTemplate] = []
    if not news:
        return evs

    ver = news.get("version") or {}
    banners = news.get("banners") or []
    story = news.get("story") or {}
    festival = news.get("festival") or {}
    anniv = news.get("anniversary")

    # ---- ① 卡池角色 → 港里来了外乡人（传闻框架，不必编性格）
    for b in banners:
        for c in (b.get("characters") or [])[:2]:
            name, elem = c.get("name"), c.get("element")
            skill = ELEMENT_WORLD.get(elem, "有点本事")
            evs.append(EventTemplate(
                id=f"news_visitor_{_slug(name)}",
                type="surprise",
                title=f"外乡人来港：{name}",
                description=f"跑商的回来说，港里最近来了个外乡人，叫{name}，{skill}。",
                instruction=_rumor_instruction(
                    f"你听说港里最近来了个外乡人，叫「{name}」，据说{skill}。"
                    f"你还没见着本人，只是听跑商的说的。"
                ),
                probability=0.75,
                priority="medium",
                hours_min=9, hours_max=22,
                locations=["street", "harbor", "market", "terrace", "teahouse"],
                cooldown_minutes=4320,   # 3 天：时令话题不反复讲
                affinity_delta=0.5,
                tags=["news", "visitor", "drama"],
                idle=False,
            ))

    # ---- ② 版本名 → 港里的新话题（版本名往往是意象化的，正好当歌谣/传言）
    if ver.get("name"):
        vname = ver["name"]
        evs.append(EventTemplate(
            id="news_version_topic",
            type="surprise",
            title=f"港里都在传：{vname}",
            description=f"最近港里、行商之间都在传一件和「{vname}」有关的事。",
            instruction=_rumor_instruction(
                f"最近码头上、茶馆里都在传一件和「{vname}」这四个字有关的怪事/歌谣。"
                f"（这四个字的来路你也不清楚，只是听来的名头。）"
            ),
            probability=0.7,
            priority="medium",
            hours_min=10, hours_max=23,
            locations=["street", "harbor", "teahouse", "market"],
            cooldown_minutes=10080,  # 7 天
            affinity_delta=0.5,
            tags=["news", "version", "ghost_story"],
        ))

    # ---- ③ 魔神任务 → 远方的大事
    if story.get("chapter"):
        ch = story["chapter"]
        # 章节名常是「无神怜爱的雪国」这种修饰+地名的写法，取最后一个「的」之后当地名
        # （取第一个「的」之前会得到「无神怜爱」这种不成话的碎片）。
        place = ch.rsplit("的", 1)[-1] if "的" in ch else "远方"
        evs.append(EventTemplate(
            id="news_story_rumor",
            type="surprise",
            title=f"远方传闻：{ch}",
            description=f"有商队从{place}那边回来，说那边出了大事——「{ch}」。",
            instruction=_rumor_instruction(
                f"有走远路的商队回港，说远方出了大事，他们把那事叫作「{ch}」。"
                f"细节你也只是道听途说，讲个大概就行。"
            ),
            probability=0.7,
            priority="medium",
            hours_min=10, hours_max=23,
            locations=["harbor", "street", "teahouse", "terrace"],
            cooldown_minutes=10080,
            affinity_delta=0.5,
            tags=["news", "story", "drama"],
        ))

    # ---- ④ 节日（璃月的最上心，外地的当谈资）
    if festival.get("name"):
        fname = festival["name"]
        if festival.get("liyue"):
            evs.append(EventTemplate(
                id=f"news_festival_{_slug(fname)}",
                type="time",
                title=f"{fname}到了",
                description=f"{fname}到了，港里张灯结彩，往生堂这几天的活计也换了路数。",
                instruction=(
                    f"「{fname}」到了。你是往生堂堂主，这是璃月自己的节——"
                    f"港里张灯结彩，堂里的活计也跟着变了（该办的仪程、该备的物件、"
                    f"街坊的请托都比平时多）。\n\n"
                    f"挑一件你这两天**实际在忙的**事说给他听，顺口邀他一起过节。"
                    f"别把它说成「节庆活动」这种词——就是过节。"
                ),
                probability=0.85,
                priority="high",
                hours_min=8, hours_max=23,
                cooldown_minutes=1440,
                affinity_delta=1.0,
                tags=["news", "festival"],
            ))
        else:
            evs.append(EventTemplate(
                id=f"news_festival_{_slug(fname)}",
                type="surprise",
                title=f"外地过节：{fname}",
                description=f"听说邻邦在过{fname}，港里有人跟着凑热闹。",
                instruction=_rumor_instruction(
                    f"你听说邻邦在过「{fname}」——不是璃月的节，你是从港里那些"
                    f"爱凑热闹的人嘴里听来的。"
                ),
                probability=0.65,
                priority="medium",
                hours_min=10, hours_max=22,
                locations=["street", "market", "teahouse"],
                cooldown_minutes=10080,
                affinity_delta=0.5,
                tags=["news", "festival"],
            ))

    # ---- ⑤ 周年 → 港里的热闹（不说"周年"，说"大日子"）
    if anniv:
        evs.append(EventTemplate(
            id="news_anniversary",
            type="surprise",
            title="港里的大日子",
            description="这几天璃月港格外热闹，到处都在张罗一场大庆祝。",
            instruction=(
                "这几天璃月港格外热闹，到处都在张罗一场大庆祝（你只知道是件"
                "难得的大喜事，具体缘由各有各的说法）。\n\n"
                "以胡桃身份跟他说说港里这股热闹劲，挑你看到的场面讲（灯笼、"
                "新搭的台子、铺子里的新货、街上的人比平时多）。"
            ),
            probability=0.8,
            priority="medium",
            hours_min=9, hours_max=23,
            locations=["street", "market", "harbor", "terrace"],
            cooldown_minutes=4320,
            affinity_delta=0.5,
            tags=["news", "festival"],
        ))

    # ---- ⑥ 世界任务 → 港里的小道消息（取一条，避免堆叠）
    for wq in (news.get("world_quests") or [])[:1]:
        evs.append(EventTemplate(
            id=f"news_worldq_{_slug(wq)[:24]}",
            type="surprise",
            title=f"街谈：{wq}",
            description=f"最近港里有人在说一件怪事，管它叫「{wq}」。",
            instruction=_rumor_instruction(
                f"最近港里有人在传一件怪事，管它叫「{wq}」，说得有鼻子有眼。"
                f"你也是听来的。"
            ),
            probability=0.6,
            priority="low",
            hours_min=11, hours_max=22,
            locations=["street", "teahouse", "yard"],
            cooldown_minutes=10080,
            affinity_delta=0.5,
            tags=["news", "rumor", "ghost_story"],
        ))

    # 按 id 去重（同一批资讯可能命中多条规则）
    seen, uniq = set(), []
    for e in evs:
        if e.id in seen:
            continue
        seen.add(e.id)
        uniq.append(e)
    return uniq


def _slug(s: str) -> str:
    """把中文名转成可用的 id 片段（保留中文，去掉标点）。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "") or "x"


def load(cache_path: str, cfg: Optional[dict] = None,
         force: bool = False) -> dict:
    """引擎侧入口：读配置 → 拉取 → 返回资讯。任何异常都吞掉，绝不阻断引擎。"""
    cfg = cfg or {}
    hours = float(cfg.get("cache_hours", 6))
    timeout = int(cfg.get("timeout", 20))
    try:
        return fetch(cache_path, cache_hours=hours, timeout=timeout, force=force)
    except Exception:
        return {}
