#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看本地大模型调用情况。

## 两个数据源，缺一不可

| 来源 | 覆盖什么 | 在哪 |
|---|---|---|
| 引擎日志 | **主动发言**时那次调用（引擎自己发起的） | `logs/model_usage.jsonl`（本项目） |
| OpenClaw 轨迹 | **所有**调用，含用户直聊、心跳、子 agent | `~/.openclaw/agents/*/sessions/*.trajectory.jsonl` |

用户**直接跟胡桃聊天**走的是 OpenClaw 自己的链路，引擎完全看不到，只有轨迹文件里有。
所以只看一边都会漏——这个工具两边都读。

轨迹里的 `model.completed` 事件带完整用量（input/output/cacheRead/reasoningTokens）
以及 aborted/timedOut 标记；与它前面那条 `prompt.submitted` 配对即可算出**单次延迟**。

    python3 tools/model_log.py                  # 近 24 小时总览
    python3 tools/model_log.py --hours 168      # 近一周
    python3 tools/model_log.py --local-only     # 只看本地模型的调用
    python3 tools/model_log.py --calls 40       # 列出最近 40 次调用明细
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_LOG = os.path.join(HERE, "logs", "model_usage.jsonl")
AGENTS_DIR = os.path.expanduser("~/.openclaw/agents")

OK, WARN, BAD = "✅", "⚠️", "❌"


def _parse_ts(value) -> float:
    """把各种时间写法统一成秒级时间戳。"""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def read_engine_log(hours: float) -> list:
    """读引擎自己写的调用记录。"""
    if not os.path.exists(ENGINE_LOG):
        return []
    cutoff = dt.datetime.now().timestamp() - hours * 3600
    out = []
    with open(ENGINE_LOG, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(d.get("ts"))
            if ts >= cutoff:
                # 注意顺序：解析好的数值 ts 必须放在 **d 之后，
                # 否则会被 d 里那个 ISO 字符串覆盖，排序时报 float<str。
                out.append({**d, "src": "引擎", "ts": ts})
    return out


def read_openclaw_calls(hours: float, agent_filter: str = "") -> list:
    """读 OpenClaw 轨迹，配对 prompt.submitted → model.completed 得到延迟与用量。"""
    cutoff = dt.datetime.now().timestamp() - hours * 3600
    calls = []
    for path in glob.glob(os.path.join(AGENTS_DIR, "*", "sessions", "*.trajectory.jsonl")):
        agent = path.split(os.sep + "agents" + os.sep)[1].split(os.sep)[0]
        if agent_filter and agent != agent_filter:
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        # 按 traceId 分组，内部按 seq 排序，才能把 submitted 与 completed 配上
        by_trace: dict = collections.defaultdict(list)
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"prompt.submitted"' not in line and '"model.completed"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = d.get("type")
                    if t in ("prompt.submitted", "model.completed"):
                        by_trace[d.get("traceId") or path].append(d)
        except OSError:
            continue

        for _tid, evs in by_trace.items():
            # **按时间戳排序，不能按 seq**：有些事件是事后补录的，seq 反而更小
            # （例如 model.fallback_step 的 seq=1 却发生在最后），按 seq 配会把延迟算成负数。
            evs.sort(key=lambda x: _parse_ts(x.get("ts")))
            pending = None
            for e in evs:
                if e.get("type") == "prompt.submitted":
                    pending = e
                    continue
                ts = _parse_ts(e.get("ts"))
                start = _parse_ts(pending.get("ts")) if pending else 0.0
                if not pending or ts < start:   # 没配上 / 时间倒挂 → 跳过这一笔
                    pending = None
                    continue
                pending = None
                if ts < cutoff:
                    continue
                data = e.get("data") or {}
                usage = data.get("usage") or {}
                calls.append({
                    "src": f"oc:{agent}", "ts": ts,
                    "model": f"{e.get('provider') or '?'}/{e.get('modelId') or '?'}",
                    "session_key": e.get("sessionKey") or "",
                    "seconds": round(ts - start, 1) if start else None,
                    "input": usage.get("input"), "output": usage.get("output"),
                    "cacheRead": usage.get("cacheRead"),
                    "reasoning": usage.get("reasoningTokens"),
                    "total": usage.get("total"),
                    "timedOut": bool(data.get("timedOut") or data.get("idleTimedOut")),
                    "aborted": bool(data.get("aborted")),
                    "ok": not (data.get("aborted") or data.get("timedOut")),
                })
            # 只有 submitted 没有 completed 的（比如超时中断）单独记一笔
    return calls


def is_local(model: str) -> bool:
    return "ollama" in (model or "") or (model or "").startswith("local")


def fmt(v, dash="—"):
    return dash if v in (None, "") else str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="查看本地大模型调用情况")
    ap.add_argument("--hours", type=float, default=24.0, help="回溯小时数（默认 24）")
    ap.add_argument("--calls", type=int, default=0, help="列出最近 N 次调用明细")
    ap.add_argument("--local-only", action="store_true", help="只看本地模型")
    ap.add_argument("--agent", default="", help="只统计某个 agent")
    args = ap.parse_args()

    engine = read_engine_log(args.hours)
    oc = read_openclaw_calls(args.hours, args.agent)
    calls = engine + oc
    if args.local_only:
        calls = [c for c in calls if is_local(c.get("model", ""))]
    calls.sort(key=lambda c: c.get("ts") or 0)

    print(f"本地大模型调用情况　近 {args.hours:g} 小时")
    print(f"  数据源：引擎日志 {len(engine)} 条（主动发言）"
          f"　OpenClaw 轨迹 {len(oc)} 条（含直聊/心跳）")
    print(f"  合计 {len(calls)} 次调用"
          + ("（已过滤：只看本地）" if args.local_only else ""))

    if not calls:
        print("\n  这段时间没有调用记录。")
        print(f"  引擎日志：{ENGINE_LOG}")
        print(f"  轨迹目录：{AGENTS_DIR}/*/sessions/*.trajectory.jsonl")
        return 0

    # ── 按模型汇总 ───────────────────────────────────────────────
    print(f"\n{'模型':<26}{'次数':>5}{'本地':>5}{'平均延迟':>10}{'超时':>5}{'中止':>5}")
    print("-" * 60)
    agg: dict = collections.defaultdict(lambda: {"n": 0, "sec": [], "to": 0, "ab": 0})
    for c in calls:
        m = c.get("model") or "(未知)"
        a = agg[m]
        a["n"] += 1
        if c.get("seconds"):
            a["sec"].append(float(c["seconds"]))
        a["to"] += 1 if c.get("timedOut") else 0
        a["ab"] += 1 if c.get("aborted") else 0
    for m, a in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        avg = f"{sum(a['sec']) / len(a['sec']):.1f}s" if a["sec"] else "—"
        print(f"{m:<26}{a['n']:>5}{'是' if is_local(m) else '否':>5}{avg:>10}"
              f"{a['to']:>5}{a['ab']:>5}")

    # ── 本地 vs 云端 ─────────────────────────────────────────────
    loc = [c for c in calls if is_local(c.get("model", ""))]
    cloud = [c for c in calls if not is_local(c.get("model", ""))]
    def _avg(xs):
        s = [float(c["seconds"]) for c in xs if c.get("seconds")]
        return f"{sum(s) / len(s):.1f}s" if s else "—"
    print(f"\n本地：{len(loc)} 次，平均 {_avg(loc)}　|　云端：{len(cloud)} 次，平均 {_avg(cloud)}")
    if loc:
        slow = max(loc, key=lambda c: c.get("seconds") or 0)
        print(f"最慢的本地调用：{fmt(slow.get('seconds'))}s"
              f"　{dt.datetime.fromtimestamp(slow['ts']):%m-%d %H:%M}"
              f"　模型 {slow.get('model')}")
    tok = [c for c in calls if c.get("total")]
    if tok:
        ti = sum(c.get("input") or 0 for c in tok)
        to = sum(c.get("output") or 0 for c in tok)
        print(f"轨迹用量合计：输入 {ti} tok、输出 {to} tok（{len(tok)} 次带用量）")

    bad = [c for c in calls if not c.get("ok", True)]
    if bad:
        print(f"\n{BAD} 失败/超时 {len(bad)} 次：")
        for c in bad[-5:]:
            print(f"   {dt.datetime.fromtimestamp(c['ts']):%m-%d %H:%M}  {c.get('model')}"
                  f"  {'超时' if c.get('timedOut') else ''}{'中止' if c.get('aborted') else ''}")

    # ── 调用明细 ─────────────────────────────────────────────────
    n = args.calls or 0
    if n:
        print(f"\n最近 {min(n, len(calls))} 次调用明细：")
        print(f"{'时间':<14}{'来源':<14}{'模型':<24}{'耗时':>7}{'出字':>6}　事件/会话")
        print("-" * 90)
        for c in calls[-max(0, n):] if n else calls:
            when = dt.datetime.fromtimestamp(c["ts"]).strftime("%m-%d %H:%M:%S")
            sec = f"{c['seconds']}s" if c.get("seconds") is not None else "—"
            out = c.get("chars_out") if c.get("chars_out") is not None else c.get("output")
            tag = c.get("event") or (c.get("session_key") or "")[-28:]
            mark = "" if c.get("ok", True) else " ❌"
            print(f"{when:<14}{c.get('src', ''):<14}{(c.get('model') or '')[:23]:<24}"
                  f"{sec:>7}{fmt(out):>6}　{tag}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
