#!/usr/bin/env python3
"""真实链路测试：向本地 OpenClaw 写入一条测试事件，并跟踪回执回环。

用法：
  python3 test_chain.py --wait 300          # 写 1 条专用链路测试事件，轮询回执最多 300 秒
  python3 test_chain.py --event welcome_back # 用事件模板生成真实事件（替代专用测试事件）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from scene.delivery import build_stimulus, openclaw_pending_channel
from scene.engine import SceneEvent
from scene.receipts import ReceiptTracker
from scene.state import SceneState

HERE = os.path.dirname(os.path.abspath(__file__))

TEST_EVENT = SceneEvent(
    template_id="chain_test",
    type="time",
    title="【链路测试】久违的问候",
    description="这是一条系统链路测试事件：场景模拟器与 OpenClaw 正在握手验证。",
    instruction="这是一次系统链路测试。请以胡桃的身份，向用户主动发送一句简短、活泼、符合人设的问候（一句话即可）。不要等待用户回复，也无需提及这是测试。",
    priority="high",
    tags=["greeting"],
    affinity_delta=0.0,
    characters=[],
)


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_test_event(event_id: str) -> SceneEvent:
    data = load_json(os.path.join(HERE, "events.json"))
    tpl = next((t for t in data if t["id"] == event_id), None)
    if tpl is None:
        sys.exit(f"未找到事件模板: {event_id}")
    # 直接构造时补全占位符（引擎正常路径会做同样的事）
    fmt = {"location": "绯云坡", "weather": "晴空"}
    return SceneEvent(
        template_id=tpl["id"],
        type=tpl["type"],
        title=tpl.get("title", tpl["id"]),
        description=tpl["description"].format(**fmt),
        instruction=tpl["instruction"].format(**fmt),
        priority=tpl["priority"],
        tags=list(tpl.get("tags", [])),
        affinity_delta=tpl.get("affinity_delta", 0.0),
        characters=list(tpl.get("characters", [])),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenClaw 真实链路测试")
    ap.add_argument("--config", default=os.path.join(HERE, "config.heartbeat.json"))
    ap.add_argument("--event", default=None, help="用 events.json 中的模板事件替代专用测试事件")
    ap.add_argument("--wait", type=int, default=300, help="回执轮询时长上限（秒）")
    args = ap.parse_args()

    cfg = load_json(args.config)
    rc = cfg.get("receipts", {})
    pending_path = os.path.expanduser(rc.get("pending_file", "~/.openclaw/workspace/memory/events/pending.json"))
    replies_path = os.path.expanduser(rc.get("replies_file", "~/.openclaw/workspace/memory/events/replies.jsonl"))

    # ---- 参数自检（非测试环境） ----
    print("==== 参数自检 ====")
    ch_names = [c.get("name") for c in cfg["delivery"]["channels"]]
    print(f"  通道: {ch_names}")
    print(f"  pending 路径: {pending_path}  存在目录: {os.path.isdir(os.path.dirname(pending_path))}")
    print(f"  replies 路径: {replies_path}")
    print(f"  角色人设: {cfg['persona']['name']}  | 事件模板: {cfg['scene']['events_file']}")
    if "openclaw_pending" not in ch_names:
        sys.exit("config 里没有 openclaw_pending 通道，无法测试路子一")
    assert os.path.isdir(os.path.dirname(pending_path)), "pending.json 所在目录不存在，请先确认 OpenClaw workspace 路径"

    # ---- 构造事件 ----
    event = build_test_event(args.event) if args.event else TEST_EVENT
    cast = load_json(os.path.join(HERE, "cast.json")).get("cast", {})
    state = SceneState(**cfg["scene"]["start"])
    payload = build_stimulus(event, state, cast)

    print("\n==== 写入 pending.json ====")
    result = openclaw_pending_channel(payload, {"path": pending_path})
    print(f"  -> {result}")
    print("  事件内容:", json.dumps(payload["event"], ensure_ascii=False))
    print(f"  当前文件: {pending_path}")
    with open(pending_path, encoding="utf-8") as f:
        print("  ", json.dumps(json.load(f), ensure_ascii=False, indent=2))

    # ---- 回执轮询 ----
    tracker = ReceiptTracker(pending_path, replies_path)
    tracker.register_written(state, payload["event"]["id"])
    print(f"\n==== 等待 OpenClaw 处理（最多 {args.wait}s，事件 id: {payload['event']['id']}）====")
    deadline = time.time() + args.wait
    while time.time() < deadline:
        tracker.scan(state, log=lambda m: print("  ", m))
        status = state.receipts.get(payload["event"]["id"], {}).get("status", "written")
        if status in ("processed", "replied"):
            break
        time.sleep(10)

    print("\n==== 最终回执 ====")
    print(json.dumps(state.receipts, ensure_ascii=False, indent=2))
    if state.receipts.get(payload["event"]["id"], {}).get("status") == "written":
        print("（事件仍在 pending.json 中，OpenClaw 下一次心跳会处理；可用 --wait 加大等待时长）")


if __name__ == "__main__":
    main()
