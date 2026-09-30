#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看亲密度与"云端/本地"模型切换状态。

为什么需要它：模型按亲密度在云端与本地之间切换，但这件事**没有可见性**——
只能翻日志猜。这个工具一次说清：现在多少分、在哪个模型上、为什么、
离切换还差多少、以及最近的趋势。

    python3 tools/affinity_status.py            # 当前状态
    python3 tools/affinity_status.py --trend     # 附最近变化趋势
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from scene.state import AFFINITY_TIERS, tier_of          # noqa: E402
from scene.openclaw_sync import current_model            # noqa: E402


def load() -> tuple:
    state = json.load(open(os.path.join(HERE, "state.json"), encoding="utf-8"))
    cfg = json.load(open(os.path.join(HERE, "config.sessions.json"), encoding="utf-8"))
    dc = cfg["delivery"]["channels"][0]["config"]
    return state, cfg, dc


def decide(aff: float, tier: str, local: dict, current: str, dc: dict = None) -> tuple:
    """返回 (目标模型, 说明)。

    ⚠️ 原来这里是**复刻**了一份判据，而它与引擎并不一致：
      · 云端侧用的是 `local["cloud_model"]`，而引擎 `_pick_chat_model` 返回的是
        `cfg["model"]`——当前配置里这两个值不同（moonshot/kimi-k2.6 vs
        deepseek/deepseek-v4-flash），于是亲密度掉进下行区时这个工具显示的目标模型是错的；
      · 云端/本地侧还漏了 `min_tier` 档位门槛以外的分支细节。
    这个工具存在的唯一目的就是让"云端↔本地切换"可见，判据必须与引擎同源。
    改成直接调用引擎函数；取不到时才退回本地近似实现（并在说明里标明）。
    """
    # 优先直接用引擎的判定（同源=不会说谎）。
    # 注意传给 _pick_chat_model 的必须是**渠道配置**（内含 model_local 子键），
    # 不是 model_local 本身——传错会静默走成"云端"分支。
    if dc:
        try:
            import os as _os
            import sys as _sys
            _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
            from scene.delivery import _pick_chat_model
            want, why = _pick_chat_model(
                {"state": {"tier": tier, "affinity": aff}}, dc, current)
            if want:
                return want, why
        except Exception:                   # noqa: BLE001 —— 取不到才退回近似
            pass
    # 退回近似实现（说明里标明，别让人误以为和引擎一致）
    up = float(local.get("min_affinity", 95))
    down = float(local.get("drop_affinity", up - 7))
    lm = local.get("model", "ollama/local")
    cloud = local.get("cloud_model") or local.get("model")
    if not local.get("enabled"):
        return cloud, "本地路由未启用"
    if tier != "close":
        return cloud, f"档位 {tier} 未到 close"
    if aff >= up:
        return lm, f"亲密度 {aff:.1f} ≥ {up:.0f}"
    if aff <= down:
        return cloud, f"亲密度 {aff:.1f} ≤ {down:.0f}"
    return ((lm if "ollama" in (current or "") else cloud),
            f"亲密度 {aff:.1f} 在滞回区 {down:.0f}~{up:.0f}，保持现状（近似判据）")


def main() -> int:
    ap = argparse.ArgumentParser(description="亲密度与模型切换状态")
    ap.add_argument("--trend", action="store_true", help="附最近变化趋势")
    args = ap.parse_args()

    state, cfg, dc = load()
    aff = float(state.get("affinity") or 0)
    tier = tier_of(aff)
    local = dc.get("model_local") or {}
    cur = current_model(local.get("agent_id", "main")) or "(读不到)"
    target, why = decide(aff, tier, local, cur, dc)
    up = float(local.get("min_affinity", 95))
    down = float(local.get("drop_affinity", up - 7))

    ac = cfg.get("affinity") or {}
    # 自己从 OpenClaw 会话算真实空闲时长，别读 state.json 里的中间值——
    # 那个字段由引擎每 10 分钟同步一次，直接读会显示成几十天前的陈旧值。
    last_ts = 0.0
    try:
        from scene.delivery import last_user_message_ts
        last_ts = last_user_message_ts(dc.get("user_session_key", ""))
    except Exception:                                        # noqa: BLE001
        pass
    if not last_ts:
        last_ts = float(state.get("last_user_message_ts") or 0)
    idle_h = max(0.0, (dt.datetime.now().timestamp() - last_ts) / 3600) if last_ts else 0.0
    if last_ts:
        print(f"  用户最后发言 {dt.datetime.fromtimestamp(last_ts):%m-%d %H:%M}")

    print("亲密度与模型切换")
    print(f"  亲密度   {aff:.1f} / 100　档位 {tier}")
    print(f"  当前模型 {cur}　→　按规则应为 {target}")
    print(f"  判据     {why}")
    print()
    print(f"  上行阈值 {up:.0f}（亲密度到此切本地）")
    print(f"  下行阈值 {down:.0f}（掉到此切回云端）")
    print(f"  用户已空闲 {idle_h:.1f} 小时"
          + (f"（超过 {ac.get('decay_after_idle_hours', 6):.0f} 小时开始衰减）" if idle_h else ""))

    if aff >= up:
        need = 0
    elif aff > down:
        need = aff - down
    else:
        need = aff - down
    if aff < up:
        print(f"  → 还要涨 {up - aff:.1f} 分才会切到本地")
    if aff > down:
        print(f"  → 掉 {aff - down:.1f} 分才会切回云端"
              f"（约 {(aff - down) / max(0.001, (aff - ac.get('baseline', 40)) * ac.get('rate_per_hour', 0.002) * 24):.1f} 天不互动）")
    else:
        print("  → 已在云端侧")

    print()
    print("  档位定义：" + "、".join(f"{n}({lo}-{hi-1})" for n, lo, hi in AFFINITY_TIERS))

    if args.trend:
        rows = []
        path = os.path.join(HERE, "logs", "harbor.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"type": "state"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("affinity") is not None:
                        rows.append((d.get("ts", "")[:16], d["affinity"]))
        print(f"\n  趋势（共 {len(rows)} 条状态记录，每 10 轮记一次）：")
        for ts, a in rows[-12:]:
            bar = "█" * int(a / 4)
            print(f"    {ts}  {a:5.1f}  {bar}")
        if rows:
            print(f"    最早 {rows[0][0]} {rows[0][1]:.1f} → 现在 {rows[-1][1]:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
