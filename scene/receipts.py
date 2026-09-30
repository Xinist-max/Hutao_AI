"""回执回环：感知 OpenClaw 对事件的"处理结果"。

协议（与 OpenClaw 侧 HEARTBEAT.md 任务约定，配套 openclaw_pending 通道使用）：
  1. 引擎把事件写入 pending.json（事件 id 形如 evt_xxx）；
  2. OpenClaw 心跳任务读取并处理事件，处理完从 pending.json 删除（或标记 processed: true）；
  3. OpenClaw 处理时可选地把"角色回应"追加到 replies.jsonl（每行一个 JSON）：
     {"event_id": "evt_xxx", "reply": "角色的回应文本", "emotion": "joy", "timestamp": "..."}
  4. 本模块轮询 pending.json 与 replies.jsonl，把结果记入 state.receipts（随 state.json 持久化）：
     - 我们写入的 id 从 pending.json 消失/被标记 → status = processed
     - replies.jsonl 出现已知 id 的新条目 → status = replied，并捕获角色回应

回执数据可供决策器使用（如"同类事件已由角色回应过，短期内不再触发"）。
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .state import SceneState


class ReceiptTracker:
    def __init__(self, pending_path: str, replies_path: str):
        self.pending_path = pending_path
        self.replies_path = replies_path
        self._seen_reply_lines: set = set()

    # ---- 写入侧：引擎每投递一个 pending 事件后调用 ----

    def register_written(self, state: SceneState, event_id: str, template_id: str = "") -> None:
        state.receipts.setdefault(
            event_id,
            {"status": "written", "template_id": template_id, "written_at": _now_iso()},
        )

    # ---- 读取侧：主循环每轮调用 ----

    def scan(self, state: SceneState, log: Optional[Callable[[str], None]] = None) -> None:
        log = log or (lambda m: None)

        # 1) pending.json 现存事件（id -> 条目）
        current: dict = {}
        if os.path.exists(self.pending_path):
            try:
                with open(self.pending_path, encoding="utf-8") as f:
                    current = {e.get("id"): e for e in json.load(f).get("events", [])}
            except (json.JSONDecodeError, OSError):
                current = {}  # 文件暂缺/损坏不阻塞

        # 引擎自己按过期丢弃的事件 id（见 delivery.py 的 _record_expired）
        expired_ids = set()
        try:
            from .delivery import expired_ids_path
            ep = expired_ids_path(self.pending_path)
            if os.path.exists(ep):
                with open(ep, encoding="utf-8") as f:
                    expired_ids = {r.get("id") for r in (json.load(f) or [])}
        except (OSError, json.JSONDecodeError, ImportError):
            expired_ids = set()

        # 2) 已写入但已从 pending 消失（或标记 processed/handled）→ 判定已处理
        #
        # ⚠️ 但"从 pending 消失"有**两个来源**，必须分开：
        #   a) OpenClaw 消费掉后删的                          → 真的已处理 ✅
        #   b) 引擎自己的过期清理删的（见 delivery.py）        → **从未投递** ❌
        # 原来不加区分，把 (b) 也判成 processed，于是：
        #   · harbor.jsonl 里出现「事件 evt_x 已被 OpenClaw 处理」，把投递中断伪装成正常；
        #   · 还写 receipt_meta，让该模板的**整个冷却**被白扣
        #     （welcome_back / good_night 是 12 小时）——恰好是在链路坏掉的时候。
        for eid, info in list(state.receipts.items()):
            if info.get("status") != "written":
                continue
            if eid in expired_ids:
                info["status"] = "expired"
                info["expired_at"] = _now_iso()
                log(f"[回执] 事件 {eid} 被引擎按过期清理丢弃（**未曾投递**，不计入回执反哺）")
                continue
            entry = current.get(eid)
            if entry is None or entry.get("processed") is True or entry.get("handled") is True:
                info["status"] = "processed"
                info["processed_at"] = _now_iso()
                # 回执反哺：记录该事件模板最近一次"已处理"的场景时刻
                if info.get("template_id"):
                    state.receipt_meta[info["template_id"]] = state.scene_minutes()
                log(f"[回执] 事件 {eid} 已被 OpenClaw 处理（已从 pending.json 移除/标记）")

        # 3) 读取 OpenClaw 追加的角色回应
        if not os.path.exists(self.replies_path):
            return
        with open(self.replies_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line in self._seen_reply_lines:
                    continue
                self._seen_reply_lines.add(line)
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = r.get("event_id")
                if not eid or eid not in state.receipts:
                    continue
                state.receipts[eid].update(
                    {
                        "status": "replied",
                        "reply": r.get("reply", ""),
                        "emotion": r.get("emotion", ""),
                        "reply_at": r.get("timestamp", _now_iso()),
                    }
                )
                # 回执反哺：记录该事件模板最近一次"已回应"的场景时刻
                if state.receipts[eid].get("template_id"):
                    state.receipt_meta[state.receipts[eid]["template_id"]] = state.scene_minutes()
                log(f"[回执] 事件 {eid} 角色回应: {r.get('reply', '')[:60]}")


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")
