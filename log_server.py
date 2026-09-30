#!/usr/bin/env python3
"""胡桃 · 场景运行日志 WebUI（纯 Python 标准库，零依赖）。

用法：
  python3 log_server.py            # 默认 127.0.0.1:8620
  python3 log_server.py --port 9000

页面：http://127.0.0.1:8620/
接口：
  GET /api/log?after=N   增量日志（N 为上次读到的行号）
  GET /api/state         当前场景状态 + 统计
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scene.state import tier_of

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "logs", "harbor.jsonl")
STATE_PATH = os.path.join(HERE, "state.json")
PENDING_PATH = os.path.expanduser("~/.openclaw/workspace/memory/events/pending.json")
REPLIES_PATH = os.path.expanduser("~/.openclaw/workspace/memory/events/replies.jsonl")

PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>璃月港 · 场景运行日志</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --line:#334155; --txt:#e2e8f0; --dim:#94a3b8;
          --amber:#f59e0b; --green:#10b981; --blue:#3b82f6; --gray:#64748b; --purple:#a855f7; --red:#f43f5e; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font:14px/1.6 "PingFang SC","Microsoft YaHei",system-ui,sans-serif; padding:20px; }
  h1 { font-size:20px; margin-bottom:4px; letter-spacing:1px; }
  .sub { color:var(--dim); font-size:12px; margin-bottom:16px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 18px; margin-bottom:16px; }
  .stats { display:flex; flex-wrap:wrap; gap:10px; }
  .stat { background:#0b1220; border:1px solid var(--line); border-radius:8px; padding:8px 14px; min-width:110px; }
  .stat .k { color:var(--dim); font-size:11px; }
  .stat .v { font-size:16px; font-weight:600; margin-top:2px; }
  .feed { display:flex; flex-direction:column; gap:8px; }
  .card { background:var(--panel); border-left:4px solid var(--gray); border-radius:8px; padding:10px 14px; }
  .card .t { font-size:11px; color:var(--dim); display:flex; justify-content:space-between; gap:10px; }
  .card .b { margin-top:4px; }
  .tag { display:inline-block; font-size:11px; padding:1px 8px; border-radius:10px; margin-right:6px; }
  .ev .tag { background:rgba(245,158,11,.15); color:var(--amber); }
  .dv .tag { background:rgba(16,185,129,.15); color:var(--green); }
  .rc .tag { background:rgba(59,130,246,.15); color:var(--blue); }
  .st .tag { background:rgba(100,116,139,.2); color:var(--gray); }
  .us .tag { background:rgba(168,85,247,.15); color:var(--purple); }
  .bt .tag { background:rgba(244,63,94,.15); color:var(--red); }
  .reason { color:var(--dim); font-size:12px; margin-top:2px; }
  .reply { color:#7dd3fc; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:var(--dim); }
  .empty { color:var(--dim); text-align:center; padding:20px; }
</style>
</head>
<body>
  <h1>🌙 胡桃 · 场景运行日志</h1>
  <div class="sub"><a href="/models" style="color:#7dd3fc;text-decoration:none">模型调用情况 →</a></div>
  <div class="sub" id="meta">加载中…</div>

  <div class="panel" id="statePanel">
    <div class="stats" id="stats">读取状态中…</div>
  </div>

  <div class="feed" id="feed"><div class="empty">等待日志…</div></div>

<script>
const TYPE = { event:["事件","ev"], delivered:["投递","dv"], receipt:["回执","rc"],
               state:["状态","st"], user_msg:["用户消息","us"], boot:["启动","bt"] };
let cursor = 0;

function esc(s){ return String(s??"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function renderState(s){
  const loc = ({yard:"往生堂后院",hall:"往生堂",street:"绯云坡",tavern:"三碗不过港",harbor:"璃月码头",terrace:"玉京台"})[s.location]||s.location;
  const w = ({sunny:"晴",cloudy:"多云",drizzle:"小雨",rain:"大雨",snow:"雪",windy:"风"})[s.weather]||s.weather;
  const present = (s.present&&s.present.length)? s.present.join("、") : "—";
  const n = (v,k)=>`<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  document.getElementById("stats").innerHTML =
    n(`第${s.day}天 ${Math.floor(s.hour)}:${String(Math.round((s.hour%1)*60)).padStart(2,"0")}`, "场景时间") +
    n(w, "天气") + n(loc, "地点") + n(present, "在场 NPC") +
    n(s.affinity, "好感度") + n(s.tier, "档位") +
    n(s.pending ?? "-", "待处理事件") + n(s.replies ?? "-", "胡桃已回复");
}

function card(e){
  const [name, cls] = TYPE[e.type] || [e.type, "st"];
  let body = "";
  if (e.type==="event"){
    body = `<b>${esc(e.title)}</b> — ${esc(e.description)}` +
      `<div class="reason">${e.filtered? "被过滤："+esc(e.reason) : (e.speak? "✅ 决策放行（"+esc(e.reason)+"）" : "⏸ 未开口："+esc(e.reason))}</div>`;
  } else if (e.type==="delivered"){
    body = `<b>${esc(e.title)}</b> <span class="mono">${esc(e.event_id)}</span>` +
      `<div class="reason">${(e.channels||[]).map(esc).join("<br>")}</div>`;
  } else if (e.type==="receipt"){
    body = `<span class="mono">${esc(e.event_id)}</span> → ${e.status==="replied"?"已回应":"已处理"}` +
      (e.reply? `<div class="reply">“${esc(e.reply)}”</div>`:"") +
      (e.emotion? `<div class="reason">情绪: ${esc(e.emotion)}</div>`:"");
  } else if (e.type==="state"){
    body = `第${e.day}天 ${e.hour}点 · ${e.weather} · ${e.location} · 在场[${(e.present||[]).join("、")||"—"}] · 好感度 ${e.affinity} · 待处理 ${e.pending}`;
  } else if (e.type==="user_msg"){
    body = `“${esc(e.content)}”`;
  } else if (e.type==="boot"){
    body = `场景引擎启动（配置: ${esc(e.config)}，人设: ${esc(e.persona)}）`;
  }
  return `<div class="card ${cls}"><div class="t"><span><span class="tag">${name}</span>${esc(e.ts||"")}</span></div><div class="b">${body}</div></div>`;
}

async function poll(){
  try{
    const r = await fetch(`/api/log?after=${cursor}`);
    const d = await r.json();
    if (d.entries && d.entries.length){
      const feed = document.getElementById("feed");
      if (feed.firstChild?.className==="empty") feed.innerHTML = "";
      for (const e of d.entries){
        const el = document.createElement("div");
        el.innerHTML = card(e);
        feed.prepend(el);
      }
      cursor = d.next;
    }
    const sr = await fetch("/api/state");
    const s = await sr.json();
    document.getElementById("meta").textContent =
      `实时日志 · 共 ${s.total} 条记录 · 事件 ${s.counts.event} · 投递 ${s.counts.delivered} · 回执 ${s.counts.receipt} · 刷新周期 2s`;
    renderState(s);
  }catch(e){ /* 服务暂不可达，自动重试 */ }
}
setInterval(poll, 2000);
poll();
</script>
</body>
</html>
"""


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
try:
    from model_log import read_engine_log, read_openclaw_calls, is_local
except ImportError:                        # 工具缺失时页面降级，不影响主日志页
    read_engine_log = read_openclaw_calls = None
    def is_local(m):
        return "ollama" in (m or "")


_MODEL_CACHE = {"t": 0.0, "hours": 0.0, "data": None}
MODEL_CACHE_TTL = 10.0                     # 秒：页面 15 秒刷一次，缓存 10 秒足够


def model_summary(hours: float) -> dict:
    """汇总模型调用情况。扫描 OpenClaw 轨迹较慢，加 TTL 缓存。"""
    now = time.time()
    c = _MODEL_CACHE
    if c["data"] is not None and c["hours"] == hours and now - c["t"] < MODEL_CACHE_TTL:
        return c["data"]
    if read_engine_log is None:
        return {"error": "tools/model_log.py 不可用", "calls": [], "by_model": []}

    engine = read_engine_log(hours)
    oc = read_openclaw_calls(hours)
    calls = sorted(engine + oc, key=lambda x: x.get("ts") or 0)

    agg = {}
    for x in calls:
        m = x.get("model") or "(未知)"
        a = agg.setdefault(m, {"model": m, "n": 0, "sec": [], "fail": 0, "local": is_local(m)})
        a["n"] += 1
        if x.get("seconds") is not None:
            a["sec"].append(float(x["seconds"]))
        if not x.get("ok", True):
            a["fail"] += 1
    by_model = []
    for a in agg.values():
        a["avg"] = round(sum(a["sec"]) / len(a["sec"]), 1) if a["sec"] else None
        a.pop("sec")
        by_model.append(a)
    by_model.sort(key=lambda a: -a["n"])

    def _side(want_local):
        xs = [x for x in calls if is_local(x.get("model", "")) == want_local]
        ss = [float(x["seconds"]) for x in xs if x.get("seconds") is not None]
        return {"n": len(xs), "avg": round(sum(ss) / len(ss), 1) if ss else None}

    data = {
        "hours": hours,
        "engine_n": len(engine), "oc_n": len(oc), "total": len(calls),
        "by_model": by_model,
        "local": _side(True), "cloud": _side(False),
        "calls": list(reversed(calls[-120:])),      # 最近的在最前，页面直接渲染
    }
    c.update({"t": now, "hours": hours, "data": data})
    return data


MODELS_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>璃月港 · 大模型调用情况</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --line:#334155; --txt:#e2e8f0; --dim:#94a3b8;
          --amber:#f59e0b; --green:#10b981; --blue:#3b82f6; --gray:#64748b; --red:#f43f5e; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font:14px/1.6 "PingFang SC","Microsoft YaHei",system-ui,sans-serif; padding:20px; }
  h1 { font-size:20px; margin-bottom:4px; letter-spacing:1px; }
  a { color:#7dd3fc; text-decoration:none; }
  .sub { color:var(--dim); font-size:12px; margin-bottom:16px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 18px; margin-bottom:16px; }
  .stats { display:flex; flex-wrap:wrap; gap:10px; }
  .stat { background:#0b1220; border:1px solid var(--line); border-radius:8px; padding:8px 14px; min-width:120px; }
  .stat .k { color:var(--dim); font-size:11px; }
  .stat .v { font-size:18px; font-weight:600; margin-top:2px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { padding:6px 8px; text-align:left; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:500; font-size:11px; }
  td.num, th.num { text-align:right; }
  .local { color:var(--amber); font-weight:600; }
  .cloud { color:var(--blue); }
  .fail { color:var(--red); }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .hint { color:var(--dim); font-size:12px; margin-top:8px; }
</style>
</head>
<body>
  <h1>🤖 大模型调用情况</h1>
  <div class="sub"><a href="/">← 返回场景日志</a>　·　每 15 秒自动刷新</div>
  <div class="panel"><div class="stats" id="stats">加载中…</div></div>
  <div class="panel">
    <table><thead><tr><th>模型</th><th class="num">次数</th><th>类型</th>
      <th class="num">平均延迟</th><th class="num">失败</th></tr></thead>
      <tbody id="byModel"><tr><td colspan="5">—</td></tr></tbody></table>
    <div class="hint">「本地」= 跑在 Ollama（不出本机、不计费，但慢）；「云端」= moonshot / deepseek。</div>
  </div>
  <div class="panel">
    <table><thead><tr><th>时间</th><th>来源</th><th>模型</th><th class="num">耗时</th>
      <th class="num">出字</th><th>事件 / 会话</th></tr></thead>
      <tbody id="calls"><tr><td colspan="6">—</td></tr></tbody></table>
    <div class="hint">来源「引擎」= 她主动发消息那次调用；「oc:agent」= OpenClaw 记录，
      含你直接跟她聊天、心跳、子 agent —— 直聊这条路引擎看不到，只有这里能查到。</div>
  </div>
<script>
function esc(s){ return String(s??"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function fmtTime(ts){ const d=new Date(ts*1000); const p=n=>String(n).padStart(2,"0");
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; }

async function tick(){
  let d;
  try { d = await (await fetch("/api/models?hours=24")).json(); }
  catch(e){ document.getElementById("stats").textContent = "接口不可用"; return; }
  if (d.error){ document.getElementById("stats").textContent = d.error; return; }

  document.getElementById("stats").innerHTML = [
    ["总调用", d.total],
    ["本地", `${d.local.n} 次 · 均 ${d.local.avg??"—"}s`],
    ["云端", `${d.cloud.n} 次 · 均 ${d.cloud.avg??"—"}s`],
    ["引擎主动发言", d.engine_n],
    ["OpenClaw 记录", d.oc_n],
    ["统计窗口", d.hours + " 小时"],
  ].map(([k,v])=>`<div class="stat"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join("");

  document.getElementById("byModel").innerHTML = d.by_model.map(m=>`<tr>
    <td class="mono">${esc(m.model)}</td>
    <td class="num">${m.n}</td>
    <td class="${m.local?"local":"cloud"}">${m.local?"本地":"云端"}</td>
    <td class="num">${m.avg??"—"}${m.avg!=null?"s":""}</td>
    <td class="num ${m.fail?"fail":""}">${m.fail||""}</td></tr>`).join("")
    || '<tr><td colspan="5">暂无调用</td></tr>';

  document.getElementById("calls").innerHTML = d.calls.map(c=>`<tr>
    <td class="mono">${fmtTime(c.ts)}</td>
    <td>${esc(c.src||"")}</td>
    <td class="mono ${(c.model||"").includes("ollama")?"local":"cloud"}">${esc(c.model||"")}</td>
    <td class="num">${c.seconds!=null?c.seconds+"s":"—"}</td>
    <td class="num">${c.chars_out??c.output??"—"}</td>
    <td class="mono">${esc(c.event||c.session_key||"")}</td></tr>`).join("")
    || '<tr><td colspan="6">暂无调用</td></tr>';
}
tick(); setInterval(tick, 15000);
</script>
</body>
</html>
"""


def read_log_after(after: int) -> tuple:
    entries, i = [], 0
    if not os.path.exists(LOG_PATH):
        return entries, 0
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < after:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries, i + 1


def state_summary() -> dict:
    state = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            state = {}
    pending = 0
    if os.path.exists(PENDING_PATH):
        try:
            with open(PENDING_PATH, encoding="utf-8") as f:
                pending = len(json.load(f).get("events", []))
        except (OSError, json.JSONDecodeError):
            pending = 0
    replies = 0
    if os.path.exists(REPLIES_PATH):
        try:
            with open(REPLIES_PATH, encoding="utf-8", errors="replace") as f:
                replies = sum(1 for _ in f)
        except OSError:
            replies = 0
    total, counts = 0, {}
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                for line in f:
                    total += 1
                    try:
                        t = json.loads(line).get("type")
                        counts[t] = counts.get(t, 0) + 1
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
    return {
        "day": state.get("day", 1), "hour": state.get("hour", 0.0),
        "weather": state.get("weather", "?"), "location": state.get("location", "?"),
        "present": state.get("present", []), "affinity": state.get("affinity", 0),
        "tier": tier_of(state.get("affinity", 0)), "pending": pending, "replies": replies,
        "total": total, "counts": counts,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html")
        elif path == "/api/log":
            after = 0
            if "?" in self.path:
                q = self.path.split("?", 1)[1]
                for kv in q.split("&"):
                    k, _, v = kv.partition("=")
                    if k == "after":
                        try:
                            after = int(v)
                        except ValueError:
                            after = 0
            entries, nxt = read_log_after(after)
            self._send(200, json.dumps({"next": nxt, "entries": entries}, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/state":
            self._send(200, json.dumps(state_summary(), ensure_ascii=False).encode("utf-8"))
        elif path == "/models":
            self._send(200, MODELS_PAGE.encode("utf-8"), "text/html")
        elif path == "/api/models":
            hours = 24.0
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    if k == "hours":
                        try:
                            hours = max(0.5, min(720.0, float(v)))
                        except ValueError:
                            pass
            self._send(200, json.dumps(model_summary(hours), ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *args):  # 静默访问日志
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="璃月港运行日志 WebUI")
    ap.add_argument("--port", type=int, default=8620)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"[logui] http://{args.host}:{args.port}/  模型调用: /models  (日志文件: {LOG_PATH})")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
