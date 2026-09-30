"""事件投递通道：把 stimulus（场景刺激）投递给本地 OpenClaw。

已实现三种 OpenClaw 对接方式（可插拔，config 里配置执行顺序，失败自动落到下一通道）：

  1. openclaw_pending —— 路子一 Heartbeat（定时巡检）：
     写入 ~/.openclaw/workspace/memory/events/pending.json，由 OpenClaw 心跳任务定时读取处理
  2. openclaw_sessions —— 路子二 直接发消息：
     CLI 模式执行 `openclaw sessions send --session <key> --message <msg>`；
     HTTP 模式 POST 到 OpenClaw 的 /api/sessions/send
  3. openclaw_webhook —— 路子三 Webhook 接收：
     POST 到 OpenClaw Gateway 配置的接收端点（Bearer Token 鉴权）

另有通用通道：
  json_file —— 本地 outbox（调试/审计）
  console   —— 打印到控制台（开发演示，无需 OpenClaw）
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import subprocess
import time
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from .engine import SceneEvent
from .state import SceneState


def _now_iso() -> str:
    """形如 2026-08-31T16:07:43+08:00（与 OpenClaw pending.json 规范一致）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------- 统一 stimulus 构造 ----------

# 输出形式约束：胡桃向用户"转述/分享"场景事件（间接引语），而不是把对 NPC 的话发给用户。
# 该约束对所有事件生效，避免用户因缺少上下文而觉得对话割裂。
# 每次注入给模型的输出形式约束。
#
# **为什么这么短**：这里的内容会**写进会话历史并永久保留**，而统一会话下引擎会反复注入，
# 于是同一份规则在历史里重复 N 次、每次都占着上下文。原来 366 字符 × N 条，
# 实测 8 条注入就占 3,610 字符（历史的 22%）。
# 完整规则已经全在 `SOUL.md`（系统提示词，只出现一次、永远都在）——实测 9 条约束逐项覆盖：
# 空行分条/上限 5 条、长短交错、emoji ≤1、不写 Markdown、至少一句冲他说、
# 括号动作 2~8 字、别老用同一称呼、绝不发语音、以及"什么时候会想画"。
# 所以这里只留一行指针 + 配图这个**最需要即时提醒**的点（实测不提醒时她不会自发配图）。
OUTPUT_FORM = (
    "【本次怎么发】按 SOUL.md 的输出规范来（空行分条、可带括号动作）；"
    "说出口的话里**不许出现**微信/手机/屏幕/消息/线上这类词，也别拿它们做动作——"
    "你在璃月港，他在你旁边；"
    "这一幕值得他亲眼看见就另起一行写 `[[img_gen: 画面描述]]`，不确定就别写。"
)


def _enrich_instruction(event: SceneEvent, cast: Optional[Dict]) -> str:
    """给事件指令追加：认知反差提示（cast.json 的 drama_hint）+ 输出形式约束。

    核心戏剧机制：魈知道钟离/温迪是七执政且自知地位低于二者，胡桃却不知情
    （她眼中钟离只是客卿、温迪只是吟游诗人、魈才是地位最高者）。
    提示只供 OpenClaw 内部理解，输出给用户时保持胡桃的"不知情"视角。
    """
    parts = [event.instruction]
    hints = []
    for c in event.characters:
        member = cast.get(c) if cast else None
        if member and member.get("drama_hint"):
            hints.append(f"【{c}】{member['drama_hint']}")
    if hints:
        parts.append(
            "【认知反差提示·仅供 OpenClaw 内部理解，绝不可让胡桃在对话中说破真相】\n"
            + "\n".join(hints)
        )
    # 动作连贯性：告诉模型刚用过哪些动作、当前身体状态是什么，
    # 免得出现"上一段刚把帽子摘下来、下一段又摘一次"或"没戴却说扶正帽檐"。
    try:
        from scene.actions import tracker
        ah = tracker().hint()
        if ah:
            parts.append("【动作连贯】" + ah)
    except Exception:                       # noqa: BLE001 —— 提示失败绝不影响投递
        pass
    # 说话方式去模板化：实测真实语料里"出现≥12 次的起手式"吃掉了 40% 的段落。
    # 只在她最近确实扎堆时才提醒（见 scene/style.py），否则提醒本身会变成新模板。
    try:
        from scene.style import tracker as style_tracker
        sh = style_tracker().hint()
        if sh:
            parts.append(sh)
    except Exception:                       # noqa: BLE001
        pass
    parts.append(OUTPUT_FORM)
    return "\n\n".join(parts)


def build_stimulus(
    event: SceneEvent, state: SceneState, cast: Optional[Dict] = None, memory: str = ""
) -> dict:
    """构造喂给 OpenClaw 的刺激消息（统一格式，各通道共用）。

    memory：关键词记忆摘要（近期话题），用于在**不携带完整会话历史**的前提下保持连贯性。
    """
    event_id = f"evt_{int(time.time() * 1000)}{random.randint(100, 999)}"
    return {
        "role": "scene_event",
        "memory": memory,
        "event": {
            "id": event_id,
            "template_id": event.template_id,
            "type": event.type,
            "title": event.title,
            "description": event.description,
            "priority": event.priority,
            "tags": event.tags,
            "characters": event.characters,
            # 透传模板想要的画面——`_fallback_image` 就是读这个键来兜底配图的。
            # 原来不透传，于是那条兜底逻辑读到的永远是 None，从未生效过。
            "image": getattr(event, "image", None),
        },
        "state": {
            "day": state.day,
            "hour": round(state.hour, 1),
            "weather": state.weather,
            "location": state.location,
            "character_state": state.character_state,
            "affinity": round(state.affinity, 1),
            "tier": state.tier,
            "present": state.present,
            "present_reason": state.present_reason,
        },
        "instruction": _enrich_instruction(event, cast),
        "created_at": _now_iso(),
    }


# ---------- 事件类型/优先级 → OpenClaw 规范 ----------

def _event_type_cn(event_type: str) -> str:
    return {
        "environment": "环境",
        "time": "日常",
        "relationship": "关系",
        "surprise": "惊喜",
        "user_state": "日常",
    }.get(event_type, "事件")


def _priority_cn(priority: str) -> str:
    return {"high": "high", "medium": "normal", "low": "low"}.get(priority, "normal")


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _event_content(payload: dict) -> str:
    """事件正文（带角色指示），供 Heartbeat / Webhook 通道使用。"""
    return f"{payload['event']['description']}（角色指示：{payload['instruction']}）"


# ---------- 路子一：Heartbeat（写入 pending.json，OpenClaw 定时读） ----------

def openclaw_pending_channel(payload: dict, cfg: dict) -> str:
    path = _expand(cfg["path"])  # 默认 ~/.openclaw/workspace/memory/events/pending.json
    # 读-改-写：保留 OpenClaw 尚未处理/未删除的事件，只追加新事件
    data: dict = {"events": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {"events": []}  # 文件损坏时重建，不阻塞链路
        # 合法但**不是对象**的 JSON（`[]`、`null`、`"x"`）原来会让
        # `data.setdefault` 抛 AttributeError 逃出函数，而且不像坏 JSON 那样被重建
        # ——一次外来写入就能让这条通道永久失败。统一类型校验。
        if not isinstance(data, dict):
            print(f"[pending] ⚠️ {path} 不是对象（{type(data).__name__}），按空重建")
            data = {"events": []}
        if not isinstance(data.get("events"), list):
            data["events"] = []
    events = data.setdefault("events", [])
    # 过期清理：OpenClaw 可能已处理（发送）但忘记清理文件 → 超过该时长的条目视为已消费，防重复发送。
    #
    # ⚠️ 但"按年龄删"和"真的被消费了"是两件事。删掉的条目**可能从未被投递**
    # （心跳停摆时就是这样），所以必须留下痕迹：给被丢弃的条目打
    # `dropped_by_engine`，回执模块据此判 expired 而不是 processed，
    # 否则会把"没送出去"记成"已被 OpenClaw 处理"，还白扣该模板一整个冷却。
    stale_minutes = cfg.get("stale_minutes", 30)
    if stale_minutes > 0:
        kept, dropped = [], []
        for e in events:
            ts = str(e.get("timestamp", ""))
            try:
                from datetime import datetime
                age_min = (datetime.now().astimezone() - datetime.fromisoformat(ts)).total_seconds() / 60.0
            except (ValueError, TypeError):
                # 解析不出时间戳按**最老**处理（而不是最年轻）。原来写 age_min=0.0
                # （= 最新鲜）会让坏条目永远清不掉，攒到 max_pending 后这条通道就废了。
                age_min = float("inf")
            if age_min < stale_minutes:
                kept.append(e)
            else:
                dropped.append(e)
        if dropped:
            print(f"[pending] ⚠️ 按过期丢弃 {len(dropped)} 条未消费事件（>{stale_minutes} 分钟）："
                  f"{[d.get('id') for d in dropped][:3]}　"
                  f"——说明 OpenClaw 心跳可能没在消费，检查 heartbeat.every")
            # 丢弃的条目**不能留在 pending.json 里**（否则 OpenClaw 仍可能把它们发出去，
            # 那正是清理要防的）。但也不能一删了之：回执模块需要知道"这条是引擎丢的、
            # 从未投递"，否则会把没送出去的事件记成"已被 OpenClaw 处理"，还白扣一整个冷却。
            # 所以记到旁路文件里，让 receipts 能区分。
            _record_expired(path, dropped)
        events = kept                      # 注意：events 必须仍是 data["events"] 用的那个列表
    data["events"] = events
    # 积压保护：OpenClaw 未及时清理时防止事件无限堆积、心跳重复轰炸
    max_pending = cfg.get("max_pending", 3)
    if len(events) >= max_pending:
        raise RuntimeError(
            f"pending 已积压 {len(events)} 条（上限 {max_pending}），本轮跳过写入（等待 OpenClaw 清理）"
        )
    entry = {
        "id": payload["event"].get("id") or f"evt_{int(time.time() * 1000)}-{len(events)}",
        "type": _event_type_cn(payload["event"]["type"]),
        "title": payload["event"].get("title", ""),
        "content": _event_content(payload),
        "timestamp": _now_iso(),
        "priority": _priority_cn(payload["event"]["priority"]),
    }
    events.append(entry)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 原子写：这个文件与 OpenClaw 心跳**是两个进程共用**的，直接 open(w) 会先截断，
    # 实测 4 个并发读者下有 50% 的读落在"已截断、还没写入"的空窗口里。
    _atomic_write_json(path, data)
    return f"openclaw_pending -> {path}（待处理 {len(events)} 条）"


def expired_ids_path(pending_path: str) -> str:
    """被引擎按过期丢弃的事件 id 记在哪（与 pending.json 同目录的旁路文件）。"""
    return pending_path + ".expired.json"


def _record_expired(pending_path: str, dropped: list) -> None:
    """记下"这些事件是引擎自己丢弃的、从未投递"，供回执模块区分。"""
    path = expired_ids_path(pending_path)
    try:
        prev = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                prev = json.load(f) or []
            if not isinstance(prev, list):
                prev = []
        prev += [{"id": d.get("id"), "at": _now_iso(), "template_id": d.get("template_id")}
                 for d in dropped if d.get("id")]
        _atomic_write_json(path, prev[-200:])       # 只留最近 200 条
    except (OSError, json.JSONDecodeError):
        pass


def _atomic_write_json(path: str, data) -> None:
    """临时文件 + os.replace 原子替换，避免读者看到半截内容。"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------- 路子二：sessions_send 直接发消息 ----------

def _session_message(payload: dict) -> str:
    return (
        f"[场景事件] {payload['event'].get('title', '')}：{payload['event']['description']}"
        f"（{payload['instruction']}）"
    )


# ---------- 多模态输出（图片 / 语音） ----------

# re.I：模型偶尔写大写（[[IMG_GEN: ...]]）。大小写敏感时它既不生图、也不被清洗，
# 结果是协议标记原样漏到微信里。
_MEDIA_RE = re.compile(r"\[\[\s*(voice|img|img_gen)\s*:\s*(.+?)\s*\]\]", re.S | re.I)

# 括号配对：切分气泡时要避开括号内部（`（叉腰）` 不能被切成两半）
_OPEN_BRACKETS = "（(「【《"
_CLOSE_BRACKETS = "）)」】》"

TTS_SCRIPT = os.path.expanduser("~/Thunder-TTS/scripts/hutao_tts.sh")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_IMAGE_SCRIPT = os.path.join(_PROJECT_ROOT, "tools", "gen_image.py")
# 本地生图（ComfyUI）。亲密度到阈值后改走这条，绕开云端内容审核，见 _pick_image_backend()
LOCAL_IMAGE_SCRIPT = os.path.join(_PROJECT_ROOT, "tools", "gen_image_local.py")
GALLERY_SCRIPT = os.path.join(_PROJECT_ROOT, "tools", "gallery.py")


def _pick_from_gallery(query: str) -> Optional[str]:
    """图库取图：`[[img: 胡桃 打招呼]]` 这种不是路径的参数，当关键词去图库找一张。

    官方图 100% 保真且免费，适合「画面主体就是胡桃本人」的场合；
    找不到就返回 None，由调用方记一条失败说明（她自己会在下轮改用生图或换词）。
    """
    try:
        proc = subprocess.run(["python3", GALLERY_SCRIPT, "pick", query],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("GALLERY_OK "):
            path = line[len("GALLERY_OK "):].strip()
            return path if os.path.exists(path) else None
    return None


def _parse_media_markers(text: str, cfg: Optional[dict] = None) -> tuple:
    """解析回复里的多模态标记，返回 (清洗后文本, [媒体请求...])。

    支持的标记（由 SOUL.md 教给胡桃）：
      [[img_gen: 画面描述]]    → 生成图片并发送
      [[img: /本地/路径.png]]  → 发送已有图片（不是路径则当图库关键词）
      [[voice: 要说的话]]      → 合成语音并发送。**默认禁用**：微信收音频会报错，
                                禁用时把这段文字原样转成普通文字气泡，内容一个字不丢。

    cfg.voice_enabled 显式设为 true 才会真的发语音（渠道支持音频时才开）。
    """
    voice_on = bool((cfg or {}).get("voice_enabled", False))
    items, kept_voice = [], []
    for m in _MEDIA_RE.finditer(text or ""):
        kind, arg = m.group(1), m.group(2).strip()
        if kind == "voice":
            if voice_on:
                items.append({"kind": "voice", "arg": arg})
            else:
                kept_voice.append(arg)          # 禁用音频：原话转文字气泡，绝不丢内容
            continue
        items.append({"kind": "img_gen" if kind == "img_gen" else "img", "arg": arg})
    clean = _MEDIA_RE.sub("", text or "").strip()
    if kept_voice:
        print(f"[delivery] 语音已禁用（微信不支持音频），{len(kept_voice)} 段语音内容转文字气泡")
        clean = "|||".join([p for p in [clean] + kept_voice if p])
    return clean, items


def _send_wechat_media(path: str, caption: str, cfg: dict) -> bool:
    """用 openclaw message send --media 发送图片/语音。"""
    if not (path and os.path.exists(path)):
        return False
    channel, target = cfg.get("reply_channel"), cfg.get("reply_to")
    if not (channel and target):
        return False
    parts = ["openclaw message send", f"--channel {shlex.quote(channel)}"]
    if cfg.get("reply_account"):
        parts.append(f"--account {shlex.quote(cfg['reply_account'])}")
    parts.append(f"--target {shlex.quote(target)}")
    if caption:
        parts.append(f"--message {shlex.quote(caption[:200])}")
    parts.append(f"--media {shlex.quote(path)}")
    try:
        proc = subprocess.run(" ".join(parts), shell=True, capture_output=True,
                              text=True, timeout=cfg.get("media_timeout", 120))
        out = (proc.stdout or "") + (proc.stderr or "")
        return "✅" in out or "Sent via" in out
    except Exception:  # noqa: BLE001
        return False


# `[[img: /路径]]` 允许发送的目录白名单 + 扩展名白名单。
#
# 为什么必须收口：SOUL.md 教了胡桃"写指定路径的图"，而原来的实现是
# `path = expanduser(arg); if os.path.exists(path): return path`——**零校验**。
# 也就是说 `[[img: ~/.openclaw/openclaw.json]]`（里面是各家 API key）、
# `[[img: /etc/hosts]]`、会话转录……只要路径存在，就会被 `openclaw message send --media`
# **上传到微信（腾讯服务器）**。而她的回复由模型生成、又和用户消息同处一条会话上下文，
# 存在被话术引导写出该标记的路径。
_IMG_ALLOW_DIRS = (
    os.path.expanduser("~/Documents/DSH/hutao/gallery"),
    os.path.expanduser("~/ComfyUI-Installs"),
    "/tmp",
    "/private/tmp",
    "/var/folders",                     # macOS 的 tempfile 默认落在这里
)
_IMG_ALLOW_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _is_allowed_image(path: str) -> bool:
    """路径是否在允许目录内、且扩展名是图片。"""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    if not os.path.isfile(real):
        return False
    if os.path.splitext(real)[1].lower() not in _IMG_ALLOW_EXT:
        return False
    for d in _IMG_ALLOW_DIRS:
        try:
            rd = os.path.realpath(d)
        except OSError:
            continue
        if real == rd or real.startswith(rd.rstrip("/") + "/"):
            return True
    return False


def _resolve_img_arg(arg: str) -> Optional[str]:
    """把 `[[img: ...]]` 的参数解析成文件路径：先当路径试，不是路径就查图库关键词。

    ⚠️ 路径必须过 `_is_allowed_image`：只允许图库/生图输出/temp 下的图片文件。
    不在白名单里就**当作图库关键词**处理（而不是报错）——这样她写错了也只是取不到图，
    不会把配置或密钥发出去。
    """
    path = os.path.expanduser(arg)
    if os.path.exists(path):
        if _is_allowed_image(path):
            return path
        print(f"[delivery] ⚠️ 拒绝发送不在白名单内的路径（{path[:60]}）——"
              f"只允许图库/生图输出/temp 下的图片")
        return None
    return _pick_from_gallery(arg)


def _tier_reached(payload: Optional[dict], min_tier: str,
                  min_affinity: Optional[float] = None) -> tuple:
    """亲密度门槛是否达到。返回 (是否达到, 说明)。

    两个条件可同时给（都要满足）：
      · `min_tier`：档位门槛（stranger < familiar < close）
      · `min_affinity`：数值门槛。**档位只有三档，"close" 是最高档**——
        想表达"很亲密才切"（比如 95 以上）就得用数值，否则一旦进 close 档就全切了。
    """
    state = ((payload or {}).get("state") or {})
    tier = state.get("tier") or "stranger"
    aff = state.get("affinity")
    order = ["stranger", "familiar", "close"]
    try:
        ok = order.index(tier) >= order.index(min_tier)
    except ValueError:
        ok = False
    why = f"亲密度={tier}"
    if min_affinity is not None and aff is not None:
        try:
            ok = ok and float(aff) >= float(min_affinity)
            why += f"/{float(aff):.0f}（阈值 {float(min_affinity):.0f}）"
        except (TypeError, ValueError):
            ok = False
    return ok, why


MODEL_LOG = os.path.join(_PROJECT_ROOT, "logs", "model_usage.jsonl")


def _normalize_separators(text: str) -> str:
    """发出前的兜底清理：分隔符与 markdown 残留。

    **为什么必须做**：单条模式下文本是整段发出的，不经过分条器，模型写出什么就发什么。
    实测漏过两种东西：
      · `|||`（旧约定的残留）—— 换成空行，等价于"这里该断一段"
      · 行首的 `>`（模型模仿 SOUL.md 示例里的 markdown 引用符号）—— 微信不渲染，看着像乱码
    另外清掉行首的 `#` 标题符与 `**加粗**`（人设明令禁止，但模型偶尔仍会写）。
    """
    t = text or ""
    if "|" in t:
        # 连续 3 个以上竖线一律折成空行。原来只匹配恰好 `|||`，模型写 4~5 个时
        # 会残留 `||`，到 _split_bubbles 那里再被切一次，用户看到以 `||` 开头的气泡。
        t = re.sub(r"(?:[ \t]*\|){3,}[ \t]*", "\n\n", t)
    # 行首 markdown 标记：> 引用、# 标题、- 列表
    #
    # `>` 的匹配原来写成 `^[ \t]*>[ \t]?`——那个"可选空格"太贪，会把
    # **行首比较号**也吃掉：「>1000 摩拉本堂主给你抹个零」→「1000 摩拉…」，
    # 语义直接反了（打折变全价）。要求 `>` 后面必须有空白，才是引用语法。
    t = re.sub(r"(?m)^[ \t]*>[ \t]+", "", t)
    t = re.sub(r"(?m)^[ \t]*>[ \t]*$", "", t)          # 单独一行的 `>` 也算引用符
    t = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", t)
    t = re.sub(r"(?m)^[ \t]*[-*+][ \t]+", "", t)
    # 行内加粗（`**x**`）——这个安全，微信不渲染，只会露出星号
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    # ⚠️ 单个 `*` 的斜体清理**必须看内侧内容**，不能只看星号本身。
    # 实测损坏（原来的 `([^*\n]+?)` 会全吃）：
    #   「伤害 2000*3*1.5 点」→「伤害 200031.5 点」
    #   「一份 20*3*2 的账」  →「一份 2032 的账」
    #   「(*/ω＼*) 本堂主害羞了」→「(/ω＼) 本堂主害羞了」
    # 这类角色写颜文字很常见、算式也真实存在，**改内容比留星号严重得多**。
    # 判据：星号内侧必须首尾都是字母或汉字 —— 颜文字（`/ω＼`、`^▽^`）和
    # 纯数字算式（`3`、`1.5`）都不满足，因此不会被误吃。
    t = re.sub(r"(?<!\*)\*([A-Za-z\u4e00-\u9fff](?:[^*\n]*[A-Za-z\u4e00-\u9fff])?)\*(?!\*)",
               r"\1", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _should_split(text: str, cfg: dict) -> bool:
    """整段输出为主，**太长了才分条**。

    阈值按"正文字数"算（不含空白与换行），避免把一串短句误判成长文。
    `split_if_longer_than` 设为 0 表示永不分条。
    """
    limit = cfg.get("split_if_longer_than", 0)
    if not limit:
        return False
    return len(re.sub(r"\s+", "", text or "")) > float(limit)


def _extract_reply(raw: str) -> tuple:
    """从 openclaw agent 的输出里取出**回复正文**，返回 (正文, meta 或 {})。

    **为什么必须做这一步**：`openclaw agent` 会先在 stdout 打一段 box-drawing 提示框
    （例如 `◇ Doctor warnings …`），再输出回复。整段当回复会**把警告发到微信里**——
    实测踩过：用户收到一条 "Failed detecting Workboard .28 plugin-state KV" 的报错框。

    主路径是 `--json`：结构化输出，正文在 `result.payloads[].text`，还附带
    `meta.agentMeta.model`（实际用的模型）、`durationMs`（真实耗时）、`usage`（token）。
    解析不了才退回文本模式，并按 box 边框剥掉提示框。
    """
    raw = (raw or "").strip()

    # **先剥提示框，再判 JSON。** 顺序反了就会漏：`openclaw agent` 是先打
    # `◇ Doctor warnings …` 这段 box-drawing 再输出 JSON 的，raw 因此不以 `{`
    # 开头、JSON 分支根本不进，最后整段 JSON 信封被当成胡桃的话发出去。
    candidate = raw
    if "◇" in raw:
        lines = raw.splitlines()
        cut = max((i for i, ln in enumerate(lines)
                   if ln.strip().startswith(("╰", "├", "└", "╯"))), default=-1)
        if cut >= 0 and cut + 1 < len(lines):
            candidate = "\n".join(lines[cut + 1:]).strip()

    # 前缀噪声（例如 "Error: something failed" 后面才跟信封）→ 从第一个花括号重试
    body = candidate
    if not body.startswith(("{", "[")):
        i = body.find("{")
        if i > 0:
            body = body[i:]

    # "看着就是信封"的判据：出现这些键名，说明这是 openclaw 的结构化输出，
    # 一旦解不出来（被截断、半截 JSON）就**只能当空回复**，绝不能原样外发。
    envelope = ('"payloads"' in body or '"agentMeta"' in body or '"result"' in body)

    if body.startswith(("{", "[")) or envelope:
        try:
            d = json.loads(body)
        except (json.JSONDecodeError, ValueError, TypeError):
            d = None
        if d is not None:
            if isinstance(d, list):                  # 某些版本会包成数组
                d = d[0] if d and isinstance(d[0], dict) else {}
            result = (d.get("result") or {}) if isinstance(d, dict) else {}
            payloads = result.get("payloads") or []
            # text 未必是字符串（实测出现过 list）。不判类型会在 join 里抛 TypeError，
            # 而那个异常比发送点还早，整条投递会静默丢失——所以显式只取 str。
            text = "\n".join(
                p.get("text") for p in payloads
                if isinstance(p, dict) and isinstance(p.get("text"), str) and p.get("text")
            )
            # 解析成功就**到此为止**：正文为空就返回空正文，让上层按"模型没生成"处理。
            return text.strip(), (result.get("meta") or {})
        if envelope:
            return "", {}                            # 信封但解不出（截断等）→ 不外发

    # 非 JSON：退回文本模式（candidate 已经剥过提示框）
    return candidate, {}


# OpenClaw 在模型失败/超时/上下文溢出时会把自己的提示语当成回复吐出来。
# 这些绝不能被当成胡桃说的话发给用户。
#
# ⚠️ 刻意**不放** "please try again" 这类通用短语：它太泛，正常回复里引用一句
# 报错原文就会命中，而命中方向是"整条回复被丢弃"——内容丢失比漏一条报错严重。
_AGENT_ERROR_PATTERNS = (
    "agent couldn't generate a response",
    "couldn't generate a response",
    "model did not produce",
    "context length exceeded",
    "context_length_exceeded",
    "doctor warnings",
)


def _looks_like_agent_error(text: str) -> str:
    """判断这段"回复"其实是 OpenClaw 的报错。返回命中的片段（空串=正常回复）。

    ⚠️ 判据是"**整条回复本身就是报错**"，不是"里面出现过报错字样"。
    原来的实现是 `pat in low` 加一条"400 字以内"的闸门，方向正好是错的：
    实测一条 71 字的**正常回复**（内容里引用了报错原文）被判成报错 →
    调用侧直接 return，用户什么也收不到。真正的报错都是一整条短消息、
    以报错语开头，所以这里改成"去掉装饰符号后以报错语开头，且整体很短"。
    """
    low = (text or "").strip().lower()
    if not low:
        return ""
    head = low.lstrip("⚠️❗❕⁉ \t\n·-—*_")
    if len(head) > 200:                    # 报错都很短；长文是正常回复
        return ""
    for pat in _AGENT_ERROR_PATTERNS:
        if head.startswith(pat):
            return pat
    return ""


def _session_jsonl_path(session_key: str) -> str:
    """会话键 → 实际转录文件路径（读不到返回空串）。"""
    if not session_key:
        return ""
    store = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
    try:
        with open(store, encoding="utf-8") as f:
            sid = (json.load(f).get(session_key) or {}).get("sessionId")
    except (OSError, json.JSONDecodeError):
        return ""
    if not sid:
        return ""
    path = os.path.expanduser(f"~/.openclaw/agents/main/sessions/{sid}.jsonl")
    return path if os.path.exists(path) else ""


def _scan_session(path: str) -> tuple:
    """扫一遍转录，返回 (最后一条真人发言 ts, 最后一条任一方发言 ts)。

    - 真人发言：`role == "user"` 且**不含我们注入的 `[璃月港事件]`**
      （注入的事件是引擎自己发的，算成"用户活跃"会造成自激）。
    - 任一方：user 或 assistant。**assistant 也算**，因为用户说完、她回完，
      对话仍然是热的——只看用户发言的话，用户沉默几分钟就被误判成"人走了"，
      于是在长对话中途冷不丁插一句见闻（这正是要修的问题）。
    """
    newest_user = 0.0
    newest_any = 0.0
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"role": "user"' not in line and '"role":"user"' not in line \
                        and '"role": "assistant"' not in line and '"role":"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(str(c.get("text", "")) for c in content
                                       if isinstance(c, dict))
                text = str(content or "")
                if role == "user" and "[璃月港事件]" in text:
                    continue                      # 我们注入的事件不算用户发言
                ts = rec.get("timestamp")
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                if not isinstance(ts, (int, float)):
                    continue
                ts = ts / 1000 if ts > 1e12 else float(ts)
                newest_any = max(newest_any, ts)
                if role == "user":
                    newest_user = max(newest_user, ts)
    except OSError:
        return 0.0, 0.0
    return newest_user, newest_any


# 按 (路径, mtime, 大小) 缓存扫描结果：转录文件会一直变大，
# 每 loop（30 秒）无脑重扫一遍是白烧 CPU；文件没变就直接用上次结果。
_SESSION_SCAN_CACHE: dict = {}


def last_conversation_activity(session_key: str) -> tuple:
    """返回 (最后真人发言 ts, 最后任一方发言 ts)，单位秒；读不到都是 0。

    供"对话缓冲"用：只要任一方最近说过话，就算对话还热着。
    """
    path = _session_jsonl_path(session_key)
    if not path:
        return 0.0, 0.0
    try:
        st = os.stat(path)
        sig = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return 0.0, 0.0
    hit = _SESSION_SCAN_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1], hit[2]
    user_ts, any_ts = _scan_session(path)
    _SESSION_SCAN_CACHE[path] = (sig, user_ts, any_ts)
    return user_ts, any_ts


def last_user_message_ts(session_key: str) -> float:
    """读用户在微信里**最后一次真人发言**的真实时间戳（0 = 读不到）。

    为什么需要：引擎里的 `state.last_user_message_ts` 原本只由控制台输入更新，
    **不追踪真实的微信消息**——实测它停在 26 天前，于是"用户已空闲 627 小时"，
    基于它做的亲密度衰减、不插嘴规则全都不准。
    这里从 OpenClaw 会话里取真实时间，供 main.py 同步。
    """
    return last_conversation_activity(session_key)[0]



def log_model_call(record: dict) -> None:
    """把一次大模型调用记进 logs/model_usage.jsonl（每行一条 JSON，便于工具统计）。

    只记我们主动发起的那次调用（引擎侧）。用户**直接聊天**走的是 OpenClaw 自己的链路，
    这里看不到——那部分由 tools/model_log.py 从 OpenClaw 的轨迹文件里读，两者合起来才是全貌。

    日志本身绝不能影响主链路：任何写入异常都吞掉。
    """
    try:
        os.makedirs(os.path.dirname(MODEL_LOG), exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        with open(MODEL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _fallback_image(payload: dict, reply_text: str) -> tuple:
    """模型没写 `[[img_gen: ...]]` 时，由引擎按事件要求补一个。返回 (描述, 来源) 或 ("", "")。

    **为什么需要**：本地 9B（亲密度到阈值后会切过去）在扮演角色、遵守输出形式约束时，
    会**稳定地丢掉 `[[img_gen: ...]]` 标记**——实测直接让它"原样输出这一行"能写对，
    但一旦加上人设与输出形式要求就漏。结果是本地模式下她永远发不出图，
    "亲密度到了走本地生图"这条路根本触发不到。提示词加强也救不了，只能引擎兜底。

    优先级：事件自带的 `image` 字段 > 指令文本里写明的 `[[img_gen: ...]]`。
    只在模型没产出图片时才补，且只在引擎自己要求过配图时才补——不会替她擅自加图。
    """
    ev = (payload or {}).get("event") or {}
    want = (ev.get("image") or "").strip()
    if want:
        return want, "事件 image 字段"
    instr = (payload or {}).get("instruction") or ev.get("instruction") or ""
    # **必须切掉 OUTPUT_FORM 那一段再找**：OUTPUT_FORM 里含有 `[[img_gen: 画面描述]]`
    # 这个示例，不切掉就会被当成"引擎要求配图"，于是拿字面量"画面描述"去生图（实测踩过）。
    # 按标记切一刀 + 下面的占位符判断，两重保险（标记名改过，两处都要认）。
    head = instr
    for mark in ("【本次怎么发】", "【输出形式】"):
        head = head.split(mark, 1)[0]
    m = re.search(r"\[\[\s*img_gen\s*:\s*(.+?)\s*\]\]", head, re.S)
    if m:
        arg = m.group(1).strip()
        # 占位符/示例文本不算真实要求
        if arg in ("画面描述", "描述", "...", "…") or arg.startswith("<"):
            return "", ""
        return arg, "指令里的 img_gen"
    return "", ""


def _pick_chat_model(payload: Optional[dict], cfg: dict, current: str = "") -> tuple:
    """按亲密度选**对话模型**，**双向**且带滞回。返回 (模型名 或 None, 说明)。

    上行（云→本）：亲密度 ≥ `min_affinity`（默认 95）且 tier 到档 → 走本地，
      本地不过云端、不计费，代价是比云端模型笨。
    下行（本→云）：亲密度 ≤ `drop_affinity`（默认比上行低 7）→ 切回云端。
      亲密度靠"用户长期不互动"的衰减掉下来（见 SceneState.decay_affinity）。

    **滞回**：两个阈值之间保持现状。否则亲密度在阈值附近抖动时会来回切换模型，
    而每次切换都打断上下文节奏；留出 7 分的安全带。
    `current` 传当前生效的模型（从 OpenClaw 配置读），用于判断"现在在哪一边"。
    """
    local = cfg.get("model_local") or {}
    if not local.get("enabled"):
        return cfg.get("model"), ""
    state = ((payload or {}).get("state") or {})
    aff = state.get("affinity")
    up = float(local.get("min_affinity", 95.0))
    down = float(local.get("drop_affinity", up - 7.0))
    local_model = local.get("model", "ollama/local")
    on_local = "ollama" in (current or "") or local_model in (current or "")

    if aff is None:                       # 拿不到亲密度就按 tier 判，保持旧行为
        reached, why = _tier_reached(payload, local.get("min_tier", "close"), None)
        return (local_model, f"模型走本地（{why}）") if reached else (cfg.get("model"), f"模型走云端（{why}）")

    tier = state.get("tier") or "stranger"
    if tier != "close":
        return cfg.get("model"), f"模型走云端（档位={tier}，未到 close）"
    if aff >= up:
        return local_model, f"模型走本地（亲密度 {aff:.0f} ≥ {up:.0f}）"
    if aff <= down:
        return cfg.get("model"), f"模型走云端（亲密度 {aff:.0f} ≤ {down:.0f}）"
    # 滞回区：保持现状
    if on_local:
        return local_model, f"保持本地（亲密度 {aff:.0f} 在滞回区 {down:.0f}~{up:.0f}）"
    return cfg.get("model"), f"保持云端（亲密度 {aff:.0f} 在滞回区 {down:.0f}~{up:.0f}）"


def _pick_image_backend(payload: Optional[dict], cfg: dict) -> tuple:
    """按亲密度选生图后端：到阈值就走本地 ComfyUI，绕开云端内容审核。

    为什么需要这个：阿里通义万相有服务端审核，亲密向/敏感度高的画面会被拒或改写。
    本地 ComfyUI 没有审核层（也不需要联网），代价是慢一些、且要自己准备底模。

    返回 (backend, 说明)。backend ∈ {"cloud", "local"}。
    未开启 image_local 时永远是 cloud——行为与之前完全一致，不影响已有链路。
    """
    local = cfg.get("image_local") or {}
    if not local.get("enabled"):
        return "cloud", ""
    reached, why = _tier_reached(payload, local.get("min_tier", "close"),
                                 local.get("min_affinity"))
    return ("local" if reached else "cloud"), why


def _deliver_media(items: list, cfg: dict, payload: Optional[dict] = None) -> list:
    """按序处理媒体请求：合成/生成后发送。返回结果说明列表。"""
    notes = []
    for idx, it in enumerate(items[: cfg.get("max_media", 1)]):   # 默认每次最多 1 个媒体
        kind, arg = it["kind"], it["arg"]
        path, caption = None, ""
        try:
            if kind == "voice":
                path = f"/tmp/hutao_voice_{int(time.time())}_{idx}.wav"
                subprocess.run([TTS_SCRIPT, path, arg], capture_output=True,
                               timeout=cfg.get("tts_timeout", 180))
                caption = ""
            elif kind == "img_gen":
                backend, why = _pick_image_backend(payload, cfg)
                script = LOCAL_IMAGE_SCRIPT if backend == "local" else GEN_IMAGE_SCRIPT
                path = f"/tmp/hutao_img_{int(time.time())}_{idx}.png"
                extra_args: List[str] = []
                if backend == "local":             # 本地参数从 config 透传（步数/尺寸/地址/底模）
                    lc = cfg.get("image_local") or {}
                    for key, flag in (("steps", "--steps"), ("size", "--size"),
                                      ("host", "--host"), ("ckpt", "--ckpt"),
                                      ("lora", "--lora"),
                                      ("lora_strength", "--lora-strength")):
                        if lc.get(key) not in (None, ""):
                            extra_args += [flag, str(lc[key])]
                proc = subprocess.run(["python3", script, arg, "--out", path] + extra_args,
                                      capture_output=True, text=True,
                                      timeout=(cfg.get("image_local_timeout", 900)
                                               if backend == "local"
                                               else cfg.get("image_timeout", 240)))
                caption = arg if cfg.get("image_caption", True) else ""
                tag = f"{backend}"
                if backend == "local":
                    # 本地失败**不回退云端**（默认）：可能正是云端会拒的内容，
                    # 回退只会把敏感提示词送出去，还多半失败。要回退就显式开 fallback_to_cloud。
                    if not (path and os.path.exists(path)) and \
                            (cfg.get("image_local") or {}).get("fallback_to_cloud"):
                        subprocess.run(["python3", GEN_IMAGE_SCRIPT, arg, "--out", path],
                                       capture_output=True, text=True,
                                       timeout=cfg.get("image_timeout", 240))
                        tag = "local失败→cloud"
                notes.append(f"生图后端 {tag}（{why}）" if why else f"生图后端 {tag}")
            else:                                  # 已有图片；不是路径就当作图库关键词
                path = _resolve_img_arg(arg)
                caption = ""                   # 图库图不配文，正文自己会说话
        except Exception as e:  # noqa: BLE001
            notes.append(f"{kind} 失败({type(e).__name__})")
            continue
        if path and os.path.exists(path):
            ok = _send_wechat_media(path, caption, cfg)
            notes.append(f"{kind} {'✅' if ok else '❌'}")
        else:
            notes.append(f"{kind} 未产出文件")
    return notes


# ---------- 自然化输出：多气泡拆分与分条发送 ----------


def _user_spoke_since(session_key: str, since_ts: float) -> bool:
    """检查用户是否在 `since_ts` **之后**在微信里说过话（打断处理）。

    必须传入"本条回复开始生成的时间"，而不是用固定回看窗口：
    生成一条回复要 18~110 秒（本地模型 + 本地生图更久），用户在这之前发的消息
    **不是打断**。用固定窗口（如"最近 90 秒"）会把投递开始前的消息也算进来，
    于是她只发一条就"被自己吓停"了——实测踩过：用户 00:49:04 发消息刷新 token，
    投递 00:49:38 才开始，却被判成打断、3 条只发出 1 条。

    读取 OpenClaw 会话存储 → 定位用户微信会话文件 → 找最后一条"真人 user 消息"。
    我们注入的事件也是 user 角色，用 `[璃月港事件]` 标记排除。
    """
    if not session_key or not since_ts:
        return False
    store = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
    try:
        with open(store, encoding="utf-8") as f:
            store_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    sid = (store_data.get(session_key) or {}).get("sessionId")
    if not sid:
        return False
    path = os.path.expanduser(f"~/.openclaw/agents/main/sessions/{sid}.jsonl")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            tail = f.readlines()[-40:]
    except OSError:
        return False
    now_ms = time.time() * 1000
    for line in reversed(tail):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        if "[璃月港事件]" in str(content or ""):
            continue                      # 我们自己注入的事件，不算用户发言
        ts = msg.get("timestamp") or rec.get("timestamp")
        t_ms = None
        if isinstance(ts, (int, float)):
            t_ms = ts if ts > 1e12 else ts * 1000
        elif isinstance(ts, str):
            try:
                from datetime import datetime
                t_ms = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                t_ms = None
        if t_ms is None:
            continue
        # 只有**晚于本条回复开始生成**的用户消息才算打断
        return t_ms > since_ts * 1000
    return False


def _bubble_delay(prev_text: str, next_text: str, cfg: dict) -> float:
    """两条气泡之间的间隔：模仿真人打字 = 思考时间 + 按字数敲键盘 + 随机分心。

    关键是**间隔要不一样**。真人连发时快慢差别很大：短促反应几乎立刻跟上，
    长段先"想一会儿"，偶尔还会走神停顿好几秒。所以这里除了按字数算打字时间，
    还有一定概率插一次明显的长停顿。

    可调项（delivery 配置）：think_time_range / typing_chars_per_sec /
    long_pause_chance / long_pause_range / bubble_delay_range
    """
    think_lo, think_hi = cfg.get("think_time_range", [0.4, 2.0])
    cps = max(2.0, float(cfg.get("typing_chars_per_sec", 11.0)))    # 每秒敲几个字
    d = random.uniform(think_lo, think_hi) + len(next_text) / cps

    if len(next_text) <= 8:                       # 短促反应：像"补一句"，几乎不隔时间
        d *= random.uniform(0.35, 0.7)
    if random.random() < cfg.get("long_pause_chance", 0.12):
        d += random.uniform(*cfg.get("long_pause_range", [2.5, 6.0]))

    lo, hi = cfg.get("bubble_delay_range", [0.4, 8.0])
    return max(lo, min(hi, d))


def _depths(text: str) -> List[int]:
    """每个字符**之前**的括号深度（out[i] 表示切在 i 处是否在括号内部）。

    输出里允许 `（叉腰）`、`（把帽子扶正）` 这类动作神态，所以切分必须避开括号内部——
    切在括号里会变成半截「（」和半截「）」，比不切还难看。
    """
    depth = 0
    out = []
    for ch in text:
        out.append(depth)
        if ch in _OPEN_BRACKETS:
            depth += 1
        elif ch in _CLOSE_BRACKETS and depth > 0:
            depth -= 1
    return out


def _split_sentences(segment: str) -> List[str]:
    """按句末标点在**括号外**断句（括号里的句号不算句末）。"""
    depths = _depths(segment)
    out, start = [], 0
    for i, ch in enumerate(segment):
        if ch in "。！？!?～~…" and depths[i] == 0:
            out.append(segment[start:i + 1])
            start = i + 1
    if start < len(segment):
        out.append(segment[start:])
    return [x for x in out if x]


def _safe_cut(segment: str, max_len: int) -> int:
    """在 ≤max_len 处挑一个安全的切点：优先标点，且**绝不落在括号内部**。"""
    depths = _depths(segment)
    limit = min(max_len, len(segment) - 1)
    # 优先：括号外的逗号/分号/冒号（标点跟着前一句）
    for i in range(limit - 1, -1, -1):
        if segment[i] in "，,、；;：:" and depths[i + 1] == 0 and i + 1 > max_len // 3:
            return i + 1
    # 退而求其次：≤max_len 里最后一个括号外的位置
    for i in range(limit - 1, -1, -1):
        if depths[i + 1] == 0:
            return i + 1
    return max(1, limit)                         # 整段都在括号里（不该发生）


def _split_long(segment: str, max_len: int) -> List[str]:
    """把一条超长文本切成 ≤max_len 的片段：优先句末标点，其次逗号，最后硬切。

    只做切分，不增删任何字符（拼接回去必须等于原文）；
    句末标点与逗号都按「括号外」处理，所以 `（笑眯眯地，把茶推过去）` 不会被切两半。
    """
    out: List[str] = []
    for sent in _split_sentences(segment):
        while len(sent) > max_len:
            cut = _safe_cut(sent, max_len)
            out.append(sent[:cut])
            sent = sent[cut:]
        if sent:
            out.append(sent)
    return out


def _split_bubbles(text: str, max_bubbles: int = 5) -> List[str]:
    """把回复拆成 1~max_bubbles 条"微信气泡"，模拟真人的**分次讲述**。

    目标不是"把一段话切碎"，而是"一件事分几条说、每条都有完整意思"：
      1. 先按**空行**或 `|||` 分段（空行是主用约定；`|||` 仅作兼容保留）；
      2. 每段过长 → 按句末标点切，再按**长短交错**的目标长度归组——
         真人讲事的节奏是「短开场 → 细节 → 短反应」，而不是每条一样长；
      3. 超过 max_bubbles → 尾部内容合并进最后一条（绝不小截断）；
      4. 长度过于整齐（连发 3 条几乎一样长）是最典型的 AI 形态，
         把中间几条并成一条，制造「短—长—短」的自然落差。

    **不变量**：`"".join(bubbles)` 与原文（去掉分隔符与首尾空白）逐字相同——
    只优化形式，内容一个字不改。
    """
    t = (text or "").strip()
    if not t:
        return []
    parts = [p.strip() for p in re.split(r"\|\|\||\n{2,}", t) if p.strip()]

    # 长短交错的目标长度：短开场、长细节、中段、短收尾，循环使用
    targets = [16, 46, 30, 24, 40]

    expanded: List[str] = []
    for p in parts:
        if len(p) <= 90:
            expanded.append(p)
            continue
        # 先按句末标点/逗号切成 ≤45 字的碎片，再用下面的长短目标重新归组——
        # 不切碎就归不出「短—长—短」的节奏（单句常有 40~50 字，直接就顶满一条）
        sents = _split_long(p, 45)
        buf, ti = "", 0
        for sent in sents:
            target = targets[ti % len(targets)]
            if buf and len(buf) + len(sent) > target:
                expanded.append(buf)
                buf, ti = sent, ti + 1
            else:
                buf += sent
        if buf:
            expanded.append(buf)

    if not expanded:
        return [t]

    # 反 AI 形态（代码级保证，模型不听话也不会发出这种形态）：
    # 「连发 3 条**中等长度**且长度整齐」的消息最像 AI。
    # 注意只在中等长度（20~80 字）时才合并——连发几条短句（"hhh" / "绝了" / "你说呢"）
    # 本来就是真人常态，合并反而就不像了。
    medium_lo, medium_hi = 20, 80
    if len(expanded) >= 3:
        lens = [len(b) for b in expanded]
        if (max(lens) - min(lens) <= 6
                and all(medium_lo <= x <= medium_hi for x in lens)):
            if len(expanded) == 3:
                # 3 条时中间只有一条，取 [1:-1] 等于没并——必须并掉后两条
                expanded = [expanded[0], "".join(expanded[1:])]
            else:
                expanded = [expanded[0], "".join(expanded[1:-1]), expanded[-1]]

    if len(expanded) > max_bubbles:
        head = expanded[: max_bubbles - 1]
        head.append("".join(expanded[max_bubbles - 1:]))   # 尾部合并，不丢内容
        expanded = head
    return [b for b in expanded if b]


def _send_wechat_bubble(text: str, cfg: dict) -> tuple:
    """用 openclaw message send 发一条微信气泡，返回 (是否成功, 失败原因)。

    以前这里把失败原因吞掉了，日志只报"成功 N"，分次讲述时叙事断在半路却不知道为什么。
    微信最常见的失败是 token 过期（`sendMessage ret=-2 errmsg=prepare failed`）——
    用户先给 bot 发一条消息就会恢复。
    """
    channel, target = cfg.get("reply_channel"), cfg.get("reply_to")
    if not (channel and target and text.strip()):
        return False, "缺少渠道/目标或内容为空"
    parts = ["openclaw message send", f"--channel {shlex.quote(channel)}"]
    if cfg.get("reply_account"):
        parts.append(f"--account {shlex.quote(cfg['reply_account'])}")
    parts.append(f"--target {shlex.quote(target)}")
    parts.append(f"--message {shlex.quote(text)}")
    try:
        proc = subprocess.run(" ".join(parts), shell=True, capture_output=True,
                              text=True, timeout=cfg.get("bubble_timeout", 30))
    except subprocess.SubprocessError as e:
        return False, f"{type(e).__name__}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "✅" in out or "Sent via" in out:
        return True, ""
    first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "无输出")
    if "ret=-2" in out or "prepare failed" in out:
        first += "（微信 token 过期：让用户先给 bot 发一条消息即可恢复）"
    return False, first[:160]


def openclaw_sessions_channel(payload: dict, cfg: dict) -> str:
    msg = _session_message(payload)
    mode = cfg.get("mode", "cli")
    if mode == "http":
        url = cfg.get("url") or "http://localhost:8080/api/sessions/send"
        body = {"session_key": cfg.get("session_key", ""), "message": msg}
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 5)) as resp:
            return f"openclaw sessions_send(http) -> {url} [{resp.status}]"
    # CLI 模式：openclaw agent 注入事件 → 胡桃回复 → 投递给用户
    # 两种投递方式：
    #   ① 单条：--deliver（OpenClaw 直接投递整段回复）
    #   ② 多气泡（multi_bubble=true，推荐）：先取回文本，再按 ||| 拆成 1~3 条短消息分次发送，
    #      并加"打字间隔"，模拟真人连发节奏——这是自然化的关键
    agent = cfg.get("agent", "main")
    session_key = cfg.get("session_key", "")
    if "{date}" in session_key:                       # 会话按天轮换：上下文自动封顶，无需人工清理
        session_key = session_key.replace("{date}", time.strftime("%Y%m%d"))
    # {user_session}：把主动发言并进**用户直聊那条会话线**。
    # 这是上下文连贯性的关键：分开两条会话时，"她主动说的话"不在"你回她时的上下文"里，
    # 于是你问"你忘了昨晚咱们做什么了吗"，她只能编（实测发生过）。
    # 合并之后她主动说的话成为直聊历史的一部分，你回她时她看得见，双向都对得上。
    if "{user_session}" in session_key:
        session_key = session_key.replace("{user_session}", cfg.get("user_session_key", ""))
    mem = payload.get("memory") or ""
    mem_line = f"【近期话题·仅作连贯性参考，不必逐条展开】{mem}\n\n" if mem else ""
    msg = (
        f"{mem_line}"
        f"[璃月港事件] {payload['event'].get('title', '')}：{payload['event']['description']}\n"
        f"（角色指示：{payload['instruction']}）"
    )
    agent_parts = ["openclaw agent", f"--agent {shlex.quote(agent)}"]
    if session_key:
        agent_parts.append(f"--session-key {shlex.quote(session_key)}")
    # 对话模型按亲密度选：到阈值走本地 LLM（不出本机、不计费）
    try:                                    # 当前生效的模型：滞回判据需要知道"现在在哪边"
        from scene.openclaw_sync import current_model as _cur
        _now_model = _cur(cfg.get("model_local", {}).get("agent_id", "main"))
    except Exception:                       # noqa: BLE001
        _now_model = ""
    chat_model, model_why = _pick_chat_model(payload, cfg, _now_model)
    if chat_model:
        agent_parts.append(f"--model {shlex.quote(chat_model)}")
        if model_why and "本地" in model_why:
            print(f"[delivery] {model_why}: {chat_model}")
    agent_parts.append("--json")          # 结构化输出：拿到干净正文 + 真实耗时/模型/用量
    agent_parts.append(f"--message {shlex.quote(msg)}")

    # 统一先取回文本（**不用 --deliver**）：
    # 这样多模态标记在两种发送模式下都能被解析——否则关掉分条发送时，
    # [[img_gen: ...]] 会以纯文本形式漏给用户，图片也不会生成。
    # 记下"开始生成本条回复"的时刻：只有此后的用户发言才算打断（见 _user_spoke_since）。
    gen_started = time.time()
    # 这里必须接住 TimeoutExpired：它是本函数里**唯一**没包 try 的子进程调用
    # （bubble / media 都有）。不接的话异常直接逃到 deliver()，整条回复静默丢失，
    # 连 model_usage.jsonl 都不会记一行——读那个文件的巡检就看不到这次调用。
    try:
        proc = subprocess.run(" ".join(agent_parts), shell=True, capture_output=True,
                              text=True, timeout=cfg.get("timeout", 180))
    except subprocess.TimeoutExpired:
        # ⚠️ 超时**不等于**没发出去：真正的 openclaw 是 node shim，会再 spawn 孙进程，
        # 实测杀掉外层后孙进程仍把活干完了（3.5 秒后落盘）。所以这里**不重试**——
        # 重试就是重复投递。按"已经可能发出去了"对待，只记失败让人看见。
        note = f"⚠️ agent 调用超时（{cfg.get('timeout', 180)}s），本条按未确认处理、不重试"
        print(f"[delivery] {note}")
        log_model_call({"source": "engine", "kind": "proactive",
                        "model": chat_model or "", "ok": False, "error": "TimeoutExpired",
                        "event": (payload.get("event") or {}).get("template_id", ""),
                        "session_key": session_key, "seconds": cfg.get("timeout", 180)})
        return note
    except subprocess.SubprocessError as exc:
        note = f"⚠️ agent 调用失败（{type(exc).__name__}），本条未发送"
        print(f"[delivery] {note}")
        log_model_call({"source": "engine", "kind": "proactive",
                        "model": chat_model or "", "ok": False, "error": type(exc).__name__,
                        "event": (payload.get("event") or {}).get("template_id", ""),
                        "session_key": session_key})
        return note
    raw_out = (proc.stdout or "").strip()
    out, meta = _extract_reply(raw_out)
    # OpenClaw 失败时会把自己的报错当成"回复"输出（实测发过
    # "⚠️ Agent couldn't generate a response. Please try again."），
    # 直接发出去等于以胡桃的名义甩一句系统报错。识别出来当失败处理。
    agent_error = _looks_like_agent_error(out)
    if agent_error:
        print(f"[delivery] ⚠️ 模型没能生成回复（{agent_error}），本条不发送")
    agent_meta = (meta.get("agentMeta") or {})
    usage = (agent_meta.get("usage") or {})
    real_model = (f"{agent_meta.get('provider')}/{agent_meta.get('model')}"
                  if agent_meta.get("provider") and agent_meta.get("model") else "")
    log_model_call({
        "source": "engine", "kind": "proactive",
        "model": real_model or chat_model or "",
        "backend": "local" if "ollama" in (real_model or chat_model or "") else "cloud",
        "reason": model_why, "tier": ((payload.get("state") or {}).get("tier") or ""),
        "affinity": ((payload.get("state") or {}).get("affinity")),
        "event": (payload.get("event") or {}).get("template_id", ""),
        "session_key": session_key,
        # 优先用 OpenClaw 报的真实耗时；拿不到才退回子进程墙钟（含启动开销，偏大）
        "seconds": round(agent_meta.get("durationMs", 0) / 1000, 1) if agent_meta.get("durationMs")
                   else round(time.time() - gen_started, 1),
        "seconds_wall": round(time.time() - gen_started, 1),
        "input": usage.get("input"), "output": usage.get("output"),
        "chars_out": len(out),
        "ok": proc.returncode == 0 and bool(out) and not agent_error,
        **({"error": agent_error} if agent_error else {}),
    })
    if agent_error:
        # 直接把 OpenClaw 的报错当回复发出去，等于以胡桃的名义甩一句系统提示——
        # 实测发生过（用户收到 "⚠️ Agent couldn't generate a response."）。
        # 这里直接放弃本条，不发送任何内容，让上层日志与巡检看得见。
        return (f"⚠️ 模型没能生成回复（{agent_error}），本条未发送"
                f"｜{real_model or chat_model}　耗时 {round(time.time() - gen_started)}s")

    # 记账：把她这条回复里的动作记进动作记忆（供下一条参考，避免重复/矛盾）
    try:
        from scene.actions import tracker
        acts = tracker().record(out)
        if acts:
            print(f"[action] 记下动作 {len(acts)} 处：{'、'.join(a[:14] for a in acts[:4])}")
    except Exception:                       # noqa: BLE001
        pass

    # 记账：记下这条回复的**形状**（起手式/段数/结尾标点），供下一条判断是否扎堆。
    # 只存 4 字指纹，不存正文——去模板化不需要正文，也不该把正文再存一份。
    try:
        from scene.style import tracker as style_tracker
        style_tracker().record(out)
    except Exception:                       # noqa: BLE001
        pass

    # 剥离多模态标记（图片；语音默认禁用，其文本会转成文字气泡）
    clean_text, media_items = _parse_media_markers(out, cfg)
    # ⚠️ 这里**只能**用 clean_text，不能写 `clean_text or out`。
    # 当回复"只有标记、没有正文"时 clean_text 是空串（falsy），`or` 会把**没清洗过的
    # 原始文本**请回来 —— 于是 `[[img_gen: 胡桃在海边]]` 被原样发给用户。
    # 实测确认。空正文就老老实实当空正文，后面按"只发媒体、不发文字气泡"处理。
    out = _normalize_separators(clean_text)
    img_fallback = ""
    if not any(it.get("kind") == "img_gen" for it in media_items):
        fb, src = _fallback_image(payload, out)
        if fb:
            media_items.append({"kind": "img_gen", "arg": fb})
            img_fallback = f"（模型未写图标记，引擎按{src}补上：{fb[:24]}）"
            print(f"[delivery] {img_fallback}")

    ok, interrupted, aborted, errors = 0, False, "", []

    # 正文为空时不能走"发一个空气泡"这条路：
    #   · 有媒体（她只写了 [[img_gen: ...]]）→ 跳过文字，只发图，这是正常情况；
    #   · 没媒体 → 说明模型什么都没生成，属于失败，要让日志和巡检看得见，
    #     而不是记成"分 0 条连发（成功 0）"这种既没 ⚠️ 也不进 errors 的假成功。
    if not out:
        if media_items:
            send_note = "无正文（只发媒体）"
            media_notes = _deliver_media(media_items, cfg, payload)
            return f"{send_note}｜媒体: " + ", ".join(media_notes)
        return (f"⚠️ 模型返回空回复，本条未发送"
                f"｜{real_model or chat_model}　耗时 {round(time.time() - gen_started)}s")

    if cfg.get("multi_bubble") or _should_split(out, cfg):
        # ② 分条发送：按空行拆成 1~5 条，逐条发、条间隔按字数与随机分心算
        #    触发条件：multi_bubble=true，或正文超过 split_if_longer_than（整段太长就拆开）
        bubbles = _split_bubbles(out, cfg.get("max_bubbles", 5))
        prev_text = ""
        for i, b in enumerate(bubbles):
            if i > 0:
                # 打断处理：本条回复**开始生成之后**旅行者才说过话 → 立刻收尾，不再继续发
                if cfg.get("interrupt_check", True) and _user_spoke_since(
                        cfg.get("user_session_key", ""), gen_started):
                    interrupted = True
                    break
                time.sleep(_bubble_delay(prev_text, b, cfg))
            good, err = _send_wechat_bubble(b, cfg)
            if not good and cfg.get("bubble_retry", 1):        # 先重试一次再判定失败
                time.sleep(cfg.get("bubble_retry_delay", 2.0))
                good, err = _send_wechat_bubble(b, cfg)
            if good:
                ok += 1
            else:
                errors.append(err)
                # 第一条就发不出去 → 整段没送达，后面的也没必要发（叙事不能只剩半截）
                if i == 0 and cfg.get("abort_on_first_failure", True):
                    aborted = err
                    break
            prev_text = b
        send_note = f"分 {len(bubbles)} 条连发（成功 {ok}）"
    else:
        # ① 单条发送：整段一次性发出（默认形态；内容不长时不拆）
        good, err = _send_wechat_bubble(out, cfg)
        if not good and cfg.get("bubble_retry", 1):
            time.sleep(cfg.get("bubble_retry_delay", 2.0))
            good, err = _send_wechat_bubble(out, cfg)
        ok = 1 if good else 0
        if not good:
            errors.append(err)
        send_note = f"单条发送（成功 {ok}）"

    if aborted:
        send_note += f"｜⚠️ 首条即失败，整段未送达：{aborted}"
    elif errors:
        send_note += f"｜⚠️ {len(errors)} 条失败：{errors[0]}"
    # 首条就失败（aborted）时不能发媒体：正文一句都没送到，图却到了，
    # 用户收到一张没头没尾的裸图；若是 [[img_gen]] 还会白烧一次本地生图（超时 900s）。
    if media_items and not aborted:
        media_notes = _deliver_media(media_items, cfg, payload)
        send_note += "｜媒体: " + ", ".join(media_notes)
    if img_fallback:
        send_note += "｜" + img_fallback
    if interrupted:
        send_note += "｜被旅行者打断，提前收尾"

    # 共存：同一条回复同时上屏 Live2D（表情同步），失败不影响微信投递
    display_note = ""
    if cfg.get("display_url") and out:
        try:
            body = json.dumps({"text": out}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                cfg["display_url"], data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=cfg.get("display_timeout", 6)) as r:
                display_note = f" | live2d[{r.status}]"
        except Exception as e:  # noqa: BLE001
            display_note = f" | live2d 未连接({str(e)[:40]})"

    return f"{send_note} | {out[:140]}{display_note}"


# ---------- 路子三：Webhook 接收 ----------

def openclaw_webhook_channel(payload: dict, cfg: dict) -> str:
    """路子三：POST 到 OpenClaw Gateway Hooks（/hooks/agent，胡桃回复后投递到指定渠道，已实测）。"""
    url = cfg["url"]  # 如 http://127.0.0.1:18789/hooks/agent
    mem = payload.get("memory") or ""
    mem_line = f"【近期话题·仅作连贯性参考，不必逐条展开】{mem}\n\n" if mem else ""
    data = {
        "message": (
            f"{mem_line}"
            f"[璃月港事件] {payload['event'].get('title', '')}：{payload['event']['description']}\n"
            f"（角色指示：{payload['instruction']}）"
        ),
        "name": f"璃月见闻-{payload['event'].get('template_id', '')}",
        "deliver": True,
        "channel": cfg.get("channel", "openclaw-weixin"),
        "to": cfg.get("to", ""),
    }
    headers = {"Content-Type": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 30)) as resp:
        body = resp.read().decode("utf-8", "replace")[:120]
        return f"openclaw webhook -> {url} [{resp.status}] {body}"


# ---------- 通用通道 ----------

def json_file_channel(payload: dict, cfg: dict) -> str:
    path = _expand(cfg["path"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return f"json_file -> {path}"


def console_channel(payload: dict, cfg: dict) -> str:
    print("-" * 60)
    print(f"[主动发言] {payload['event'].get('title', '')}：{payload['event']['description']}")
    print(f"  情绪/表情: 由事件类型驱动（v1 前端映射占位）")
    print(f"  注入 OpenClaw stimulus:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("-" * 60)
    return "console"


CHANNELS = {
    "openclaw_pending": openclaw_pending_channel,
    "openclaw_sessions": openclaw_sessions_channel,
    "openclaw_webhook": openclaw_webhook_channel,
    "json_file": json_file_channel,
    "console": console_channel,
}


def deliver(payload: dict, channels: List[Dict]) -> List[str]:
    """按 config 顺序投递；某通道失败不阻断后续通道（console 常作兜底）。"""
    results = []
    for ch in channels:
        name = ch.get("name")
        fn = CHANNELS.get(name)
        if fn is None:
            results.append(f"未知通道: {name}")
            continue
        try:
            results.append(fn(payload, ch.get("config", {})))
        except Exception as e:  # noqa: BLE001 —— 通道失败只记录，不影响主链路
            results.append(f"{name} 失败: {e}")
    return results
