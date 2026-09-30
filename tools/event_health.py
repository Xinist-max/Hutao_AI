#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件系统体检：测"引擎选中但没投递"的白扣率与内容多样性。

引擎是**先 `_fire()` 扣冷却、再交决策层**的。被决策层挡下的那一次，
冷却已经消耗掉了——"没送出去但报废了"。本脚本量化这个损耗：

    白扣率 = 被挡下次数 / 引擎选中次数

跑法（不改任何生产文件）：
    python3 tools/event_health.py            # 默认 7 天 × 3 个种子
    python3 tools/event_health.py --days 14
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import scene.decision as DEC                      # noqa: E402
import scene.engine as E                          # noqa: E402
from scene.decision import DecisionConfig, DecisionMaker   # noqa: E402
from scene.engine import SceneEngine              # noqa: E402
from scene.events import load_templates           # noqa: E402
from scene.filters import FilterConfig, relevance_filter   # noqa: E402
from scene.state import SceneState                # noqa: E402


def run(cfg: dict, tpls: list, ticks: int, seed: int) -> dict:
    tm = cfg["scene"]["tick_minutes"]
    real = cfg["scene"]["tick_interval_seconds"] / 60.0
    fc = FilterConfig(persona_tags=cfg["persona"]["tags"], require_persona_tag=True)

    NOW = [1_800_000_000.0]
    E.time.time = lambda: NOW[0]

    orig_sync = SceneState._sync_to_real_time

    def fake_sync(self):                          # 让场景时钟真的走遍 24 小时
        self.hour += tm / 60.0
        while self.hour >= 24.0:
            self.hour -= 24.0
            self.day += 1

    SceneState._sync_to_real_time = fake_sync
    orig_rand = DEC.random.random
    DEC.random.random = lambda: 0.0               # 关掉概率门，只看规则闸

    st = SceneState(); st.character_state = "idle"; st.sync_to_utc8 = True
    st.last_user_message_ts = NOW[0]
    eng = SceneEngine(
        st, tpls, random.Random(seed),
        anti_silence_minutes=cfg["scene"].get("anti_silence_minutes", 0),
        follow_up_minutes=cfg["scene"].get("follow_up_minutes", 180),
        quiet_buffer_minutes=float((cfg.get("decision") or {}).get("conversation_buffer_minutes", 20.0)),
        news_hold_minutes=float((cfg.get("decision") or {}).get("news_hold_minutes", 60.0)),
    )
    dec = DecisionMaker(DecisionConfig(**cfg.get("decision", {})))

    delivered, blocked = collections.Counter(), collections.Counter()
    for _ in range(ticks):
        NOW[0] += real * 60
        idle = (NOW[0] - st.last_user_message_ts) / 60.0
        for ev in eng.tick(minutes=tm, user_inactive_minutes=idle, real_minutes=real):
            if not relevance_filter(ev, st, fc)[0]:
                continue
            speak, _ = dec.decide(ev, st, NOW[0])
            (delivered if speak else blocked)[ev.template_id] += 1
            if speak:
                st.last_conversation_ts = NOW[0]

    SceneState._sync_to_real_time = orig_sync
    DEC.random.random = orig_rand
    return {"delivered": delivered, "blocked": blocked}


def main() -> None:
    ap = argparse.ArgumentParser(description="事件系统白扣率与多样性体检")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--seeds", type=int, default=3)
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(HERE, "config.sessions.json"), encoding="utf-8"))
    tpls = load_templates(os.path.join(HERE, cfg["scene"]["events_file"]))
    ticks = int(args.days * 24 * 60 / (cfg["scene"]["tick_interval_seconds"] / 60.0))

    tot_d, tot_b = collections.Counter(), collections.Counter()
    for seed in range(args.seeds):
        r = run(cfg, tpls, ticks, seed)
        tot_d += r["delivered"]
        tot_b += r["blocked"]

    nd, nb = sum(tot_d.values()), sum(tot_b.values())
    total = nd + nb
    print(f"时长 {args.days:g} 天 × {args.seeds} 种子　模板总数 {len(tpls)}")
    print(f"  引擎选中（=已扣冷却） {total}")
    print(f"  实际投递             {nd}")
    print(f"  被决策层挡下         {nb}")
    print(f"  **白扣率**           {nb / max(total, 1):.0%}")
    print(f"  投递过的模板种类     {len(tot_d)} / {len(tpls)}")
    if tot_b:
        print("\n被白扣最多的模板（冷却被浪费）：")
        for tid, n in tot_b.most_common(5):
            cd = next((t.cooldown_minutes for t in tpls if t.id == tid), None)
            print(f"    {n:4d} 次  {tid:22s} 冷却={cd} 分钟")


if __name__ == "__main__":
    main()
