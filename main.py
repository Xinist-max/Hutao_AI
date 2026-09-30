#!/usr/bin/env python3
"""胡桃 AI 虚拟角色系统 · 场景引擎入口（需求⑥）。

用法：
  python3 main.py --once --ticks 40 --seed 7 --user-idle-min 150   # 快速演示
  python3 main.py --config config.heartbeat.json                  # 常驻运行（写 pending.json 并跟踪回执）
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import threading
import time

from scene.decision import DecisionConfig, DecisionMaker
from scene.delivery import build_stimulus, deliver
from scene.engine import SceneEngine
from scene.events import load_templates
from scene.filters import FilterConfig, relevance_filter
from scene.memory import KeywordMemory
from scene import news as GenshinNews
from scene.receipts import ReceiptTracker
from scene.security import check_weixin_senders
from scene.state import LOCATION_CN, SceneState

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "logs", "harbor.jsonl")

# 跨工作区巡检脚本（与 LocalAI 工作区共用，见 ~/Documents/DSH/_shared/）。
# 它读 OpenClaw 的 model.fallback_step 轨迹事件，判断是否发生了模型降级——
# 云端一抖动，OpenClaw 会静默把胡桃切到本地 9B 兜底（人设还在，但输出形式会退化），
# 更糟是兜底链也断了、那条消息根本没发出去。没有巡检就只能靠人察觉"她今天怎么怪怪的"。
_COMPACT_STATE = os.path.join(HERE, "logs", "compact_state.json")
MODEL_HEALTH_SCRIPT = os.path.expanduser("~/Documents/DSH/_shared/check_model_fallback.py")
_LISTEN_STARTED = False          # 监听线程全局只起一次（见 run_loop 里的说明）
_SECURITY_WARNED = [False]       # "监测未生效"只告警一次，避免每 20 轮刷屏


def write_log(entry: dict) -> None:
    """结构化运行日志（供 log_server.py WebUI 读取）。"""
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_model_health(window_minutes: float) -> str:
    """巡检 OpenClaw 近 window_minutes 内是否发生模型降级 / 兜底链耗尽。

    委托给跨工作区脚本（它读 model.fallback_step 轨迹事件，精确无歧义）。
    返回一行告警文本；一切正常或脚本不可用时返回空串——巡检本身绝不能影响主链路。
    """
    if not os.path.exists(MODEL_HEALTH_SCRIPT):
        return ""
    try:
        proc = subprocess.run(["python3", MODEL_HEALTH_SCRIPT, "--hours", f"{window_minutes / 60:.4f}"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode == 0:
        return ""
    out = proc.stdout or ""
    if "chain_exhausted" in out:
        return "检测到模型兜底链耗尽：有主动消息没发出去，检查云端额度/网络"
    if "fallbackStepFromModel" in out or "次降级" in out:
        return "检测到模型降级：OpenClaw 切到了兜底模型，胡桃的输出形式可能退化"
    return ""


def sync_conversation_activity(cfg: dict, state) -> None:
    """从 OpenClaw 会话同步两个判据：用户最后一次真人发言 + 任一方最后一次开口。

    **必须每个 loop 都调，且与亲密度衰减解耦。** 这段逻辑原本埋在
    `apply_affinity_decay()` 里，而那个函数在 `decay_enabled=false` 时会提前 return——
    衰减一关，两个判据就永远不更新，"不插嘴"和"对话缓冲"全部失效。
    任何异常都吞掉：会话读不到不该影响主链路。
    """
    dc = ((cfg.get("delivery", {}) or {}).get("channels") or [{}])[0].get("config") or {}
    key = dc.get("user_session_key", "")
    if not key:
        return
    try:
        from scene.delivery import last_conversation_activity
        user_ts, any_ts = last_conversation_activity(key)
    except Exception:                       # noqa: BLE001
        return
    if user_ts > state.last_user_message_ts:
        state.last_user_message_ts = user_ts
    if any_ts > float(getattr(state, "last_conversation_ts", 0.0) or 0.0):
        state.last_conversation_ts = any_ts


def apply_affinity_decay(cfg: dict, state) -> str:
    """用户长期不互动 → 亲密度随时间回落，让模型切换成为双向的。

    只在"用户已空闲超过阈值"时才开始衰减，且按**真实流逝时间**扣
    （用 state.last_decay_ts 记账，避免每轮重复扣）。返回说明（有变化时）。
    """
    ac = cfg.get("affinity") or {}
    if not ac.get("decay_enabled", True):
        return ""
    now = time.time()
    # 判据由 sync_conversation_activity() 每 loop 同步，这里直接用。
    if not state.last_decay_ts:
        state.last_decay_ts = now
        return ""
    idle_hours = max(0.0, (now - state.last_user_message_ts) / 3600.0) if state.last_user_message_ts else 0.0
    if idle_hours < float(ac.get("decay_after_idle_hours", 6.0)):
        state.last_decay_ts = now          # 用户活跃期内不衰减，只推进记账点
        return ""
    elapsed_h = max(0.0, (now - state.last_decay_ts) / 3600.0)
    state.last_decay_ts = now
    if elapsed_h <= 0:
        return ""
    delta = state.decay_affinity(elapsed_h,
                                baseline=float(ac.get("baseline", 40.0)),
                                rate_per_hour=float(ac.get("rate_per_hour", 0.002)))
    if abs(delta) < 0.05:
        return ""
    return (f"亲密度衰减 {delta:+.1f} → {state.affinity:.1f}（用户已 {idle_hours:.0f} 小时没来）")


def maybe_compact_session(cfg: dict, state) -> str:
    """会话上下文超阈值就压缩（LLM 摘要），把历史换成"摘要 + 近期尾巴"。

    **为什么需要**：统一会话（`{user_session}`）带来连贯性，代价是历史无限增长，
    每次调用都要重新预填充全部历史——实测单次调用涨到 120 秒以上。
    自动压缩只在接近上下文上限时才触发，所以得主动压。

    用 OpenClaw 官方入口 `openclaw sessions compact <key>`（默认 LLM 摘要，
    比 `--max-lines` 的硬截断更能保住连贯性）。压缩本身失败绝不影响主链路。
    """
    chans = (cfg.get("delivery", {}) or {}).get("channels") or [{}]
    dc = chans[0].get("config") or {}
    if not dc.get("auto_compact", True):
        return ""
    key = dc.get("user_session_key") or ""
    if not key:
        return ""
    limit = float(dc.get("compact_after_tokens", 18000))
    # 用官方 sessions 列表拿当前占用
    try:
        proc = subprocess.run(["openclaw", "sessions", "--json", "--limit", "all"],
                              capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return ""
    items = data if isinstance(data, list) else (data.get("sessions") or data.get("data") or [])
    used = None
    for it in items:
        if (it.get("key") or "") == key:
            used = it.get("totalTokens")
            break
    if not used or float(used) < limit:
        return ""
    try:
        # 用 --max-lines 而不是默认的 LLM 摘要：OpenClaw 的摘要模板是给**编程会话**设计的
        # （Goal / Constraints / Progress / Next），拿去总结角色扮演对话只会产出
        # 一堆 "(none)" 的空摘要——实测如此，等于既没省下上下文、又丢了连贯性。
        # 硬截断保留最近若干行，行为可预测，也保住了近期的真实对话。
        # 同一次量级不重复尝试：OpenClaw 在"无需压缩"时会直接拒绝，
        # 每 20 轮都试一次只会刷日志。判据是 token 又涨了至少 10%。
        prev = 0.0
        try:
            with open(_COMPACT_STATE, encoding="utf-8") as f:
                prev = float(json.load(f).get("last_tokens") or 0)
        except (OSError, json.JSONDecodeError):
            prev = 0.0
        if prev and float(used) < prev * 1.1:
            return ""
        proc = subprocess.run(["openclaw", "sessions", "compact", key, "--agent",
                               dc.get("compact_agent", "main"),
                               "--max-lines", str(dc.get("compact_keep_lines", 80))],
                              capture_output=True, text=True, timeout=300)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        try:
            os.makedirs(os.path.dirname(_COMPACT_STATE), exist_ok=True)
            with open(_COMPACT_STATE, "w", encoding="utf-8") as f:
                json.dump({"last_tokens": float(used), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f)
        except OSError:
            pass
        # 注意大小写："Compacted session ..." 是成功；"No compaction needed" 是 OpenClaw 拒绝
        # （一般是保留行数 ≥ 当前行数，无事可做）。原来只找大写的 "Compact"，
        # 于是"被拒绝"会被当成"没输出"而静默吞掉——这正是阈值设了却不触发的原因之一。
        if "Compacted session" in out:
            line = next((l for l in out.splitlines() if "Compacted session" in l), "")
            return f"会话已压缩（{int(used)} token 超阈值 {int(limit)}）：{line[:110]}"
        if "No compaction needed" in out:
            return (f"会话 {int(used)} token 超阈值 {int(limit)}，但 OpenClaw 判定无需压缩"
                    f"（保留行数 {dc.get('compact_keep_lines', 80)} ≥ 当前行数）；"
                    f"要更狠可调小 compact_keep_lines")
        return ""
    except (OSError, subprocess.SubprocessError):
        return ""


def sync_chat_model(cfg: dict, state) -> str:
    """按亲密度把 OpenClaw 里 agent 的对话模型切到云端/本地。

    只处理**用户直接聊天**那条路（走 OpenClaw 自己的 agent 配置）。引擎发主动消息是在
    `scene/delivery.py` 里用 `--model` 直接指定的，不经过这里。
    返回一行说明（有改动时），无改动返回空串。

    ⚠️ 判据必须和 `scene/delivery.py:_pick_chat_model` **同源**（含滞回），
    否则两条链路各按各的规则切，用户直聊和她的主动消息用的不是同一个模型。
    原来这里只看档位（`tier >= close`，即亲密度 ≥70），完全不读
    `min_affinity`(95) / `drop_affinity`(88)，后果是：
      · 88~95 的滞回区，直聊侧早已切到本地，滞回形同虚设；
      · 一旦切到本地，只有掉到 **70 以下**才回得来——而 47 条事件模板的
        affinity_delta 全是正的、回执还额外加分，"低于 70"几乎不可能发生，
        于是这条路实际是**单向**的（正是 state.py 里记录过的那类 bug）。
    """
    chans = (cfg.get("delivery", {}) or {}).get("channels") or [{}]
    dc = chans[0].get("config") or {}
    local = dc.get("model_local") or {}
    if not (local.get("enabled") and local.get("sync_openclaw", True)):
        return ""
    agent_id = local.get("agent_id", "main")

    # 当前在哪一边：以 OpenClaw 里**实际生效**的模型为准，滞回才有意义
    try:
        from scene.openclaw_sync import current_model
        current = current_model(agent_id) or ""
    except Exception:                                         # noqa: BLE001
        current = ""

    # 复用 delivery 的判定，保证两条链路同一套规则
    try:
        from scene.delivery import _pick_chat_model
        payload = {"state": {"tier": state.tier, "affinity": state.affinity}}
        want, why = _pick_chat_model(payload, dc, current)
    except Exception as e:                                    # noqa: BLE001
        return f"⚠️ 对话模型判定失败（{type(e).__name__}）：{e}"
    if not want:
        return ""
    try:
        from scene.openclaw_sync import sync_agent_model
        changed, note = sync_agent_model(agent_id, want)
    except Exception as e:                                    # noqa: BLE001
        return f"⚠️ 对话模型同步失败（{type(e).__name__}）：{e}"
    return f"{note}　（{why}）" if changed else ""





_STYLE_REWRITE_TS = [0.0]


def apply_style_rewrite(cfg: dict, state) -> str:
    """周期性去掉她历史回复**开头**的括号动作，断开"她模仿自己"的循环。

    ## 为什么需要（实测依据 —— 五轮排查的结论）

    她的回复 **100% 以「（动作）」开头**，而且这些回复全在上下文里，
    模型在延续该模式。实测：

        上下文里她的回复原样   → 新回复括号开头 100%   (n=8)
        上下文里去掉开头括号   → 新回复括号开头  25%   (n=8)

    而**所有其它办法都已被实验排除**：Q2_K 量化（升到 Q3_K_M 无改善，n=8）、
    num_ctx 过大、Modelfile 模板、采样参数（repeat/presence/frequency penalty）、
    系统提示词长度、以及**所有提示词层面的干预**——包括一条措辞强到
    "看到自己前面这么写，你偏不这么写"的指令（12 次生成一次都没被遵守）。

    ## 注意：这不改用户在微信里看到的历史

    改的是 OpenClaw 的**会话转录**（给模型看的上下文），微信消息由微信自己保存。

    ⚠️ 这是**破坏性操作**，所以：工具自带时间戳备份、原子写、写完逐行校验、
    `--restore` 可一键回滚；备份只留最近 3 个。
    """
    dc = ((cfg.get("delivery", {}) or {}).get("channels") or [{}])[0].get("config") or {}
    if not dc.get("style_rewrite", False):
        return ""
    key = dc.get("user_session_key", "")
    if not key:
        return ""
    cooldown = float(dc.get("style_rewrite_cooldown_minutes", 30)) * 60
    now = time.time()
    if now - _STYLE_REWRITE_TS[0] < cooldown:
        return ""
    _STYLE_REWRITE_TS[0] = now
    try:
        import sys as _sys
        _sys.path.insert(0, HERE)
        from tools.style_rewrite import rewrite_session
        from scene.delivery import _session_jsonl_path
        path = _session_jsonl_path(key)
        if not path:
            return ""
        r = rewrite_session(path)
    except Exception as e:                                    # noqa: BLE001
        return f"⚠️ 说话方式改写失败（{type(e).__name__}）：{e}"
    n = r.get("改了几条回复") or 0
    if not n:
        return ""
    note = f"说话方式改写：去掉 {n} 条历史回复开头的括号动作（{r.get('动作','')}）"
    print(f"[style] {note}")
    write_log({"type": "style_rewrite", "level": "info", "message": note,
               "tier": state.tier, "affinity": round(state.affinity, 1)})
    return note


def apply_style_digest(cfg: dict, state) -> str:
    """把"最近重复的起手式"写进 MEMORY.md 的自动块，让她下次开口换一种。

    ## 为什么只有这一条路能作用到直聊

    直聊的回复由 OpenClaw 直接生成发出，引擎完全绕过——拦不到、改不了。
    实测 OpenClaw 注入的是**固定清单**的工作区文件（整行命中率）：
        SOUL.md 168/168、IDENTITY.md 35/35、USER.md 21/21、
        MEMORY.md 40/40、AGENTS.md 70/70、TOOLS.md 11/11
    而 HEARTBEAT.md 是 0/14、自定义新建的 STYLE.md 是 0/3 —— **自定义文件不注入**。
    所以要让模型看见动态提醒，只能写进那六个之一；MEMORY.md 是"她的记忆"，语义最合适。

    ## 实测抓到的形态

    她最近 20 条回复里 **20 条（100%）都以「（动作）」开头**，其中 18 条是同一族
    身体反应（脸红×9、深吸气×4、整个人一顿×3、耳朵×2）。这就是"每段一个模子"。

    ⚠️ 只改 `<!-- AUTO:STYLE BEGIN -->` 与 `END` 之间的内容，且**两个标记必须已存在**
    ——不存在就什么都不做，绝不擅自改用户手写的文件结构。
    """
    dc = ((cfg.get("delivery", {}) or {}).get("channels") or [{}])[0].get("config") or {}
    if not dc.get("style_digest", True):
        return ""
    key = dc.get("user_session_key", "")
    if not key:
        return ""
    _STYLE_GUARD_TS[0]  # noqa: B018 —— 仅表明与守卫共用节流变量命名约定
    try:
        from scene.style import update_style_block
        note = update_style_block(key)
    except Exception:                                        # noqa: BLE001
        return ""
    if note:
        print(f"[style] {note}")
        write_log({"type": "style_digest", "level": "info", "message": note,
                   "tier": state.tier, "affinity": round(state.affinity, 1)})
    return note


_STYLE_GUARD_TS = [0.0]        # 上次触发说话方式守卫的真实时间（节流用）


def apply_style_guard(cfg: dict, state) -> str:
    """说话方式守卫：发现她在复读 → **压缩会话**，清掉被逐字记住的上下文。

    ## 为什么只能这么做

    你直聊时的回复由 **OpenClaw 直接生成并发出**，引擎完全绕过——
    没法在生成前拦、也没法不发。唯一能作用于那条链路的杠杆是**上下文本身**。

    实测证据（她的 48 条真实回复）：有 7 对"提问相似度只有 7~38%、但回复相似度
    81~100%"，其中一条 336 字的回复**隔了 17 分钟逐字再现**（100%）。
    这种"整段吐回来"是上下文里的原文被记住，不是随机生成能出现的。
    把它压掉，循环就断了。

    ⚠️ 关于归因：复读**不是本地模型的问题**。按"有真人对话"的会话做公平对照，
    云端时期 130 对相邻回复里 24% 高度相似，本地时期 47 对里 15%——
    本地反而更低。所以这条路不能靠换模型解决（而且敏感内容也不该出本机）。
    """
    dc = ((cfg.get("delivery", {}) or {}).get("channels") or [{}])[0].get("config") or {}
    # **默认关闭**：见 config 的调参指引——它的收益未被证明（压完复读段仍在上下文里），
    # 代价却是真的改写会话文件。破坏性措施不该默认开。
    if not dc.get("style_guard", False):
        return ""
    key = dc.get("user_session_key", "")
    if not key:
        return ""
    try:
        from scene.style import looks_like_repeating
        repeating, why = looks_like_repeating(
            key, threshold=float(dc.get("style_guard_threshold", 0.85)), n=6)
    except Exception:                                        # noqa: BLE001
        return ""
    if not repeating:
        return ""
    cooldown = float(dc.get("style_guard_cooldown_minutes", 60)) * 60
    now = time.time()
    if now - _STYLE_GUARD_TS[0] < cooldown:
        return ""
    _STYLE_GUARD_TS[0] = now
    # 压得比常规更狠：常规保留 80 行，这里只留 24 行——目标是**把复读的那几段清出去**
    keep = int(dc.get("style_guard_keep_lines", 24))
    try:
        proc = subprocess.run(
            ["openclaw", "sessions", "compact", key,
             "--agent", dc.get("compact_agent", "main"),
             "--max-lines", str(keep)],
            capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"⚠️ 说话方式守卫：压缩失败（{type(exc).__name__}）"
    note = f"说话方式守卫：{why} → 已压缩会话（保留 {keep} 行）以断开上下文复读"
    write_log({"type": "style_guard", "level": "warn", "message": note,
               "tier": state.tier, "affinity": round(state.affinity, 1)})
    print(f"[style] {note}")
    if "needed" in out and "No compaction" in out:
        print(f"[style] （OpenClaw 回：无需压缩——保留行数可能仍 ≥ 当前行数）")
    return note


def _pending_count(cfg: dict) -> int:
    """当前 pending.json 中待处理事件数（供状态快照日志）。"""
    rc = cfg.get("receipts", {})
    if not rc.get("enabled", True):
        return 0
    p = os.path.expanduser(rc.get("pending_file", "~/.openclaw/workspace/memory/events/pending.json"))
    try:
        with open(p, encoding="utf-8") as f:
            return len(json.load(f).get("events", []))
    except (OSError, json.JSONDecodeError):
        return 0


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_cast(cfg: dict):
    path = cfg.get("cast_file", "cast.json")
    full = os.path.join(HERE, path)
    if os.path.exists(full):
        return load_json(full).get("cast", {})
    return {}


def has_channel(cfg: dict, name: str) -> bool:
    return any(ch.get("name") == name for ch in cfg["delivery"]["channels"])


def status_line(state: SceneState) -> str:
    loc = LOCATION_CN.get(state.location, state.location)
    present = "、".join(state.present) if state.present else "（无人）"
    reasons = "；".join(f"{k}因{v}" for k, v in state.present_reason.items())
    return (
        f"第{state.day}天 {state.hour:.1f}点 {state.weather} @ {loc} "
        f"在场[{present}] {reasons} 好感度={state.affinity:.0f} 档位={state.tier}"
    )


NEWS_CACHE = os.path.join(HERE, "logs", "genshin_news.json")


def load_all_templates(cfg: dict) -> list:
    """事件模板 = 静态 events.json ＋ 原神实时资讯翻译出的世界内事件。

    资讯那部分是**时令内容**：版本、卡池里的新面孔、魔神任务进展、当前节庆。
    它不是原样喂给胡桃的——`scene/news.py` 会把元游戏词汇（版本/卡池/祈愿/原石）
    翻译成她世界里说得通的事（港里来了外乡人、远方传回的商队口信、过节）。
    任何失败都只降级为"少几条时令事件"，绝不影响引擎主链路。
    """
    templates = load_templates(os.path.join(HERE, cfg["scene"]["events_file"]))

    ncfg = cfg.get("news") or {}
    if not ncfg.get("enabled", False):
        return templates

    try:
        data = GenshinNews.load(NEWS_CACHE, ncfg)
        got = GenshinNews.world_events(data)
        if not got:
            return templates
        # 兜底去重：同一个 id 只保留一份（避免与 events.json 里手写的撞车）
        have = {t.id for t in templates}
        got = [e for e in got if e.id not in have]
        ver = (data.get("version") or {}).get("no") or "?"
        fest = (data.get("festival") or {}).get("name") or "无节庆"
        stale = "（缓存）" if data.get("stale") else ""
        print(f"[news] 原神资讯 {ver} 版本 · {fest} · 翻译出 {len(got)} 条时令事件{stale}")
        return templates + got
    except Exception as e:
        print(f"[news] 资讯接入失败（不影响主链路）：{type(e).__name__}: {e}")
        return templates


def run_once(cfg: dict, seed: int, ticks: int, user_idle_minutes: float) -> None:
    rng = random.Random(seed)
    state = SceneState(**cfg["scene"]["start"])
    # 演示模式**强制快进**，不跟随真实时钟。
    # 否则 sync_to_utc8=true 时 `_sync_to_real_time()` 会用真机当前时间覆盖 day/hour，
    # `--ticks 40` 一秒都不推进、结果还取决于你几点跑（深夜跑几乎全是"角色在睡觉"），
    # main.py 开头承诺的"快速演示 / 种子可复现"就失效了。
    state.sync_to_utc8 = False
    templates = load_all_templates(cfg)
    cast = load_cast(cfg)
    engine = SceneEngine(
        state, templates, rng, cast,
        anti_silence_minutes=cfg["scene"].get("anti_silence_minutes", 0),
        follow_up_minutes=cfg["scene"].get("follow_up_minutes", 180),
        # 对话缓冲 / 见闻静默：配置写在 decision 块（单一调参入口），
        # 但执法在引擎里——引擎是先扣冷却再交决策层的，在那里挡就等于报废这条事件。
        quiet_buffer_minutes=float((cfg.get("decision") or {}).get("conversation_buffer_minutes", 20.0)),
        news_hold_minutes=float((cfg.get("decision") or {}).get("news_hold_minutes", 60.0)),
        event_rate_multiplier=cfg["scene"].get("event_rate_multiplier", 1.0),
    )
    filter_cfg = FilterConfig(persona_tags=cfg["persona"]["tags"], require_persona_tag=True)
    decision = DecisionMaker(DecisionConfig(**cfg["decision"]))
    channels = cfg["delivery"]["channels"]
    tick_minutes = cfg["scene"]["tick_minutes"]

    now = time.time()
    state.last_user_message_ts = now - user_idle_minutes * 60

    print(f"[demo] seed={seed} 起始: {status_line(state)}")
    for i in range(1, ticks + 1):
        events = engine.tick(minutes=tick_minutes, user_inactive_minutes=user_idle_minutes, real_minutes=0.5)
        for ev in events:
            ok, reason = relevance_filter(ev, state, filter_cfg)
            if not ok:
                engine.rollback_last_fire()      # 被过滤也不该白扣冷却
                print(f"[tick {i}] 事件 {ev.template_id} 被过滤: {reason}")
                continue
            speak, why = decision.decide(ev, state, now)
            print(f"[tick {i}] 事件 {ev.template_id}: {ev.description} | 过滤:{reason} | 决策:{why}")
            if not speak:
                # 决策层挡下 → 把引擎已经扣掉的冷却/好感度退回去。
                # 不退回的话事件"没送出去但冷却报废"，实测白扣率 70%。
                engine.rollback_last_fire()
                continue
            engine.commit_last_fire()
            payload = build_stimulus(ev, state, cast)
            for r in deliver(payload, channels):
                print(f"    -> {r}")
        if i % 10 == 0:
            print(f"[tick {i}] {status_line(state)}")

    # 演示模式跑的是全新初始状态（好感度从 cfg["scene"]["start"] 起算），
    # 绝不能覆盖生产 state.json——否则真实进度会被打回第 1 天、好感度归零重算。
    demo_file = cfg["scene"]["state_file"].replace(".json", ".demo.json")
    state.save(os.path.join(HERE, demo_file))
    print(f"[demo] 结束，场景状态已保存到 {demo_file}（生产 {cfg['scene']['state_file']} 未受影响）")


def run_loop(cfg: dict, config_name: str = "config.json") -> None:
    state = SceneState.load(os.path.join(HERE, cfg["scene"]["state_file"]))
    state.sync_to_utc8 = cfg["scene"].get("sync_to_utc8", False)
    if state.last_user_message_ts == 0:
        state.last_user_message_ts = time.time()
    templates = load_all_templates(cfg)
    cast = load_cast(cfg)
    tpl_info = {t.id: t for t in templates}
    engine = SceneEngine(
        state, templates, random.Random(), cast,
        anti_silence_minutes=cfg["scene"].get("anti_silence_minutes", 0),
        follow_up_minutes=cfg["scene"].get("follow_up_minutes", 180),
        # 对话缓冲 / 见闻静默：配置写在 decision 块（单一调参入口），
        # 但执法在引擎里——引擎是先扣冷却再交决策层的，在那里挡就等于报废这条事件。
        quiet_buffer_minutes=float((cfg.get("decision") or {}).get("conversation_buffer_minutes", 20.0)),
        news_hold_minutes=float((cfg.get("decision") or {}).get("news_hold_minutes", 60.0)),
        event_rate_multiplier=cfg["scene"].get("event_rate_multiplier", 1.0),
    )
    filter_cfg = FilterConfig(persona_tags=cfg["persona"]["tags"], require_persona_tag=True)
    decision = DecisionMaker(DecisionConfig(**cfg["decision"]))
    channels = cfg["delivery"]["channels"]
    tick_minutes = cfg["scene"]["tick_minutes"]

    # 回执回环（配套 openclaw_pending 通道）
    rc = cfg.get("receipts", {})
    tracker = None
    if rc.get("enabled", True) and has_channel(cfg, "openclaw_pending"):
        tracker = ReceiptTracker(
            pending_path=os.path.expanduser(rc.get("pending_file", "~/.openclaw/workspace/memory/events/pending.json")),
            replies_path=os.path.expanduser(rc.get("replies_file", "~/.openclaw/workspace/memory/events/replies.jsonl")),
        )
        print(f"[loop] 回执回环已启用: pending={tracker.pending_path} replies={tracker.replies_path}")

    # 关键词记忆：只存近期话题，投递前注入（替代携带完整会话历史）
    mem = KeywordMemory(os.path.join(HERE, cfg.get("memory_file", "logs/topic_memory.json")),
                        limit=cfg.get("memory_limit", 15))

    def _listen(st) -> None:
        print("[loop] 在这里输入文字视为'用户活跃'（Ctrl-C 退出；主动发言链路运行中）")
        while True:
            try:
                line = input()
            except EOFError:  # 无终端环境（后台运行）时静默退出监听线程
                return
            if line.strip():
                st.last_user_message_ts = time.time()
                write_log({"type": "user_msg", "content": line.strip()[:80]})
                print(f"[loop] 收到用户消息: {line.strip()[:40]}（v1 先只做主动发言，一问一答后续接主对话 LLM）")

    # ⚠️ 监听线程**全局只起一次**。
    # 原来写在这里，而 run_loop 会被监督层在异常后重新调用——每重启一次就多一条
    # 监听线程，旧线程永不退出（`while True: input()` 只在 EOF 时返回），且它的闭包
    # 持有**上一轮已经废弃的 state 对象**：多线程抢同一个 stdin 时，用户敲的一行
    # 可能被旧线程吃掉、写进那个不再被使用的 state。
    global _LISTEN_STARTED
    if not _LISTEN_STARTED:
        _LISTEN_STARTED = True
        threading.Thread(target=_listen, args=(state,), daemon=True).start()
    write_log({"type": "boot", "config": os.path.basename(config_name), "persona": cfg["persona"]["name"]})

    loop_count = 0
    while True:
        loop_count += 1
        if tracker:
            snap = {k: v.get("status") for k, v in state.receipts.items()}
            tracker.scan(state, log=lambda m: print(m))
            for eid, rec in state.receipts.items():
                if snap.get(eid) != rec.get("status"):
                    write_log({
                        "type": "receipt",
                        "event_id": eid,
                        "template_id": rec.get("template_id", ""),
                        "status": rec.get("status"),
                        "reply": rec.get("reply", ""),
                        "emotion": rec.get("emotion", ""),
                    })
                    if rec.get("status") == "replied":
                        # 互动加成：她主动说了话、用户真的回了 → 关系往前走一步。
                        # 光靠事件 delta（全是正的、只增不减）无法体现"用户是否在回应她"，
                        # 加上这一条，亲密度才真正反映互动强度，而不只是时间流逝。
                        ac = cfg.get("affinity") or {}
                        gain = state.add_affinity(float(ac.get("reply_bonus", 1.0)))
                        if abs(gain) >= 0.05:
                            print(f"[affinity] 用户回应了她的分享 → 亲密度 {gain:+.1f} → {state.affinity:.1f}")
                            write_log({"type": "affinity", "level": "info",
                                       "message": f"用户回应了她的分享 → 亲密度 {gain:+.1f}",
                                       "affinity": round(state.affinity, 1), "tier": state.tier})
                        tpl_rec = tpl_info.get(rec.get("template_id", ""))
                        if tpl_rec is not None:
                            mem.add(tpl_rec.title, tpl_rec.characters)
                    # 多轮话题：抛话题型事件被 OpenClaw 回应 → 开启主动会话（引擎随后自动续聊/收尾）
                    if rec.get("status") == "replied":
                        t = tpl_info.get(rec.get("template_id", ""))
                        if t is not None and t.conversational:
                            state.conversation = {
                                "template_id": t.id,
                                "topic": t.title,
                                "minute": state.scene_minutes(),
                                "rounds": 0,
                            }
                            write_log({"type": "topic", "topic": t.title, "rounds": 0})
            state.save(os.path.join(HERE, cfg["scene"]["state_file"]))

        # 每 loop 先同步对话判据，再交给决策层——决策层靠它判断"对话是否还热着"，
        # 热着就不许插话（长对话中途不冒见闻）。
        sync_conversation_activity(cfg, state)
        user_inactive_min = max(0.0, (time.time() - state.last_user_message_ts) / 60.0)
        events = engine.tick(minutes=tick_minutes, user_inactive_minutes=user_inactive_min,
                             real_minutes=cfg["scene"]["tick_interval_seconds"] / 60.0)
        for ev in events:
            ok, reason = relevance_filter(ev, state, filter_cfg)
            if not ok:
                # 被相关性过滤器挡下的事件同样不该白扣冷却——引擎开火时已经扣了，
                # 这里必须一起退回（否则每次过滤都报废一条事件）。
                engine.rollback_last_fire()
                write_log({"type": "event", "template_id": ev.template_id, "title": ev.title,
                           "description": ev.description, "filtered": True, "reason": reason})
                print(f"[loop] 事件 {ev.template_id} 被过滤: {reason}")
                continue
            speak, why = decision.decide(ev, state, time.time())
            write_log({"type": "event", "template_id": ev.template_id, "title": ev.title,
                       "description": ev.description, "filtered": False, "speak": speak, "reason": why})
            print(f"[loop] 事件 {ev.template_id}: {ev.description} | 决策: {why}")
            if speak:
                engine.commit_last_fire()      # 真要投递了，冷却/好感度副作用才算数
                # **立刻落盘**：投递段（build_stimulus / mem.add / deliver）任何一步抛异常
                # 都会冒泡到监督层 → 重启后从 state.json 续上。若不先落盘，这一轮已扣的
                # 冷却就丢了，同一条模板会立刻再次通过筛选 → 用户收到重复消息
                # （历史症状"welcome_back 投递 187 次"就是这一类）。
                try:
                    state.save(os.path.join(HERE, cfg["scene"]["state_file"]))
                except OSError as exc:
                    print(f"[loop] ⚠️ 冷却落盘失败（{exc}），本条仍会尝试投递")
                payload = build_stimulus(ev, state, cast, memory=mem.summary())
                mem.add(ev.title, ev.characters)
                results = deliver(payload, channels)
                for r in results:
                    print(f"    -> {r}")
                write_log({"type": "delivered", "event_id": payload["event"]["id"],
                           "template_id": ev.template_id, "title": ev.title,
                           "channels": results})
                # 只有 openclaw_pending 真正写入成功才登记回执（积压跳过时不登记，避免误判"已处理"）
                if tracker and any(r.startswith("openclaw_pending ->") for r in results):
                    tracker.register_written(state, payload["event"]["id"], payload["event"]["template_id"])
                state.save(os.path.join(HERE, cfg["scene"]["state_file"]))
        # 安全监测：每 20 轮检查微信渠道是否出现"已知发送者之外"的访问（不拦截，仅告警）
        if loop_count % 20 == 0:
            owners = (cfg.get("security", {}) or {}).get("weixin_owner_ids", [])
            known, unknown = check_weixin_senders(owners)
            if unknown:
                msg = f"⚠️ 微信渠道出现未知发送者: {unknown}（若非本人操作请立即检查）"
                print(f"[security] {msg}")
                write_log({"type": "security", "level": "warn", "message": msg,
                           "known": known, "unknown": unknown})
            # "没有未知发送者"和"根本没在监测"是两件事。原来只看 unknown 是否为空，
            # 于是一旦 bot 账号改名/目录被清、名单读不到，监测会永久静默失效而没人知道。
            from scene.security import monitoring_status
            offline = monitoring_status(owners)
            if offline and not _SECURITY_WARNED[0]:
                _SECURITY_WARNED[0] = True          # 只告警一次，别每 20 轮刷一遍
                print(f"[security] ⚠️ {offline}")
                write_log({"type": "security", "level": "warn",
                           "message": f"发送者监测未生效：{offline}"})

            # 说话方式守卫（默认关，破坏性）：发现复读就压会话
            style_note = apply_style_guard(cfg, state)
            # 说话方式摘要（默认关，实测无效）：把重复起手式写进 MEMORY.md
            apply_style_digest(cfg, state)
            # 说话方式改写（唯一被实测证明有效的办法）：去掉历史回复开头的括号动作
            apply_style_rewrite(cfg, state)

            # 亲密度衰减：用户长期不来，关系会冷——这是"本地→云端"切回去的唯一通路
            decay_note = apply_affinity_decay(cfg, state)
            if decay_note:
                print(f"[affinity] {decay_note}")
                write_log({"type": "affinity", "level": "info", "message": decay_note,
                           "affinity": round(state.affinity, 1), "tier": state.tier})

            # 会话上下文压缩：历史涨过头就压成"摘要 + 近期尾巴"，
            # 否则每次调用都要重新预填充全部历史（实测会到 120 秒以上）
            compact_note = maybe_compact_session(cfg, state)
            if compact_note:
                print(f"[session] {compact_note}")
                write_log({"type": "session_compact", "level": "info", "message": compact_note})

            # 模型健康：窗口取 20 轮对应的真实时长，避免重复报同一次降级
            window_min = cfg["scene"]["tick_interval_seconds"] * 20 / 60.0
            model_warn = check_model_health(window_min)
            if model_warn:
                print(f"[model] ⚠️ {model_warn}")
                write_log({"type": "model_health", "level": "warn", "message": model_warn,
                           "window_minutes": round(window_min, 1)})

            # 亲密度 → 直接聊天那条路的模型同步。
            # 引擎发主动消息可以 --model 直接指定，但**用户直接跟胡桃聊天**走的是 OpenClaw
            # 自己的 agent 配置，引擎插不上手，只能改配置（键级合并，见 scene/openclaw_sync.py）。
            sync_warn = sync_chat_model(cfg, state)
            if sync_warn:
                print(f"[model] {sync_warn}")
                write_log({"type": "model_switch", "level": "info", "message": sync_warn,
                           "tier": state.tier})

        if loop_count % 10 == 0:
            write_log({"type": "state", "day": state.day, "hour": round(state.hour, 1),
                       "weather": state.weather, "location": state.location,
                       "present": state.present, "affinity": round(state.affinity, 1),
                       "tier": state.tier, "pending": _pending_count(cfg)})
            print(f"[loop] {status_line(state)}")
            state.save(os.path.join(HERE, cfg["scene"]["state_file"]))  # 定期持久化（同步模式夜间无事件也落盘）
        time.sleep(cfg["scene"]["tick_interval_seconds"])


def main() -> None:
    ap = argparse.ArgumentParser(description="胡桃 AI 虚拟角色系统 · 场景引擎（需求⑥）")
    ap.add_argument("--config", default=None,
                    help="配置文件。生产用 config.sessions.json（走微信）；"
                         "不传则用 config.json，只写本地 outbox 不发微信")
    ap.add_argument("--once", action="store_true", help="跑固定次数后退出（演示）")
    ap.add_argument("--ticks", type=int, default=60, help="--once 模式下的 tick 次数")
    ap.add_argument("--seed", type=int, default=7, help="随机种子（可复现）")
    ap.add_argument("--user-idle-min", type=float, default=10.0, help="演示时用户已空闲分钟数")
    args = ap.parse_args()

    explicit_config = args.config is not None
    if not explicit_config:
        args.config = os.path.join(HERE, "config.json")

    cfg = load_json(args.config)
    if args.once:
        run_once(cfg, args.seed, args.ticks, args.user_idle_min)
    else:
        if not explicit_config:
            # 这是最容易踩的坑：不带 --config 直接跑，看着像在生产，
            # 实际用的是演示配置——事件只写 outbox，一条微信都不会发。
            print("=" * 66)
            print("⚠️  未指定 --config，正在使用 config.json（演示配置）")
            print("    事件只会写进 outbox，不会发到微信。")
            print("    要跑生产主链路请用：")
            print("      python3 main.py --config config.sessions.json")
            print("=" * 66, flush=True)
        # ── 监督层：常驻循环里任何未预料的异常都不该让进程死掉 ──
        # 本机没有 LaunchAgent 拉起，进程一死角色就**永久沉默**，只能靠人发现。
        # 所以这里兜住异常、睡一会儿再把循环重新拉起来（状态从 state.json 续上），
        # 而不是让异常冲出 main() 结束进程。实测踩过一次：续聊两轮后
        # `del state.conversation` 删掉 dataclass 实例属性，下一个 tick 就
        # AttributeError，整个引擎直接死掉。
        backoff = 5.0
        while True:
            try:
                run_loop(cfg, args.config)
                break                       # 正常退出（Ctrl-C 之类）
            except KeyboardInterrupt:
                print("\n[loop] 收到中断，退出。")
                break
            except Exception as exc:        # noqa: BLE001
                import traceback
                tb = traceback.format_exc()
                print(f"[loop] ⚠️ 主循环抛异常，{backoff:.0f} 秒后自动重启："
                      f"{type(exc).__name__}: {exc}", flush=True)
                print(tb, flush=True)
                try:
                    cfg = load_json(args.config)   # 配置可能被改过，重读
                except Exception:                  # noqa: BLE001
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 300.0)  # 反复崩就退避，别刷屏


if __name__ == "__main__":
    main()
