#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""胡桃功能体检：逐项确认云端 / 本地两条路是否正常、能否配合。

    python3 tools/verify_all.py              # 离线检查（不花钱、不发消息）
    python3 tools/verify_all.py --online     # 附带真实调用（会花生图额度、会发微信）

每一项都打印证据，不打印"应该没问题"这种空话。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

OK, WARN, BAD = "✅", "⚠️", "❌"
results: list = []


def rec(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  {status} {name}" + (f"\n        {detail}" if detail else ""))


def _run(cmd, timeout=30, shell=False):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=shell)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                   # noqa: BLE001
        return 99, f"{type(e).__name__}: {e}"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(1.5)
        return s.connect_ex((host, port)) == 0


# ── 1. 配置与数据 ─────────────────────────────────────────────────────────


def _load_json(path: str):
    """安全读 JSON：坏文件返回 (None, 原因)。体检最该覆盖的场景之一就是
    `~/.openclaw/openclaw.json` 被写坏——那时绝不能让整份体检崩掉。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), ""
    except json.JSONDecodeError as e:
        return None, f"JSON 损坏: {e}"
    except OSError as e:
        return None, f"读不到: {e}"

def _is_real_image(path: str) -> bool:
    """是不是**真正的完整图片**（不是"文件存在就行"）。"""
    try:
        sys.path.insert(0, HERE)
        from scene.imagery import is_valid_image
        return is_valid_image(path)
    except Exception:                                         # noqa: BLE001
        return os.path.exists(path) and os.path.getsize(path) > 10000


def _img_note(path: str) -> str:
    try:
        sys.path.insert(0, HERE)
        from scene.imagery import describe
        return describe(path)
    except Exception:                                         # noqa: BLE001
        return f"{os.path.getsize(path)//1024}KB"

def check_configs() -> None:
    print("\n[1] 配置与数据")
    for f in ("config.json", "config.sessions.json", "cast.json", "events.json"):
        p = os.path.join(HERE, f)
        if not os.path.exists(p):
            rec(f"{f} 存在", BAD, "文件缺失")
            continue
        try:
            d, _err = _load_json(p)
            if d is None:
                continue
            if isinstance(d, list):                        # events.json 顶层就是数组
                n = len(d)
            else:
                n = len(d.get("cast") or d.get("events")
                        or (d.get("delivery", {}) or {}).get("channels") or [])
            rec(f"{f} 可解析", OK, f"{os.path.getsize(p)} 字节" + (f"，{n} 条目" if n else ""))
        except json.JSONDecodeError as e:
            rec(f"{f} 可解析", BAD, str(e))

    # 人格文件：仓库快照 vs 线上工作区必须一致，否则"改了半天没生效"
    import filecmp
    ws = os.path.expanduser("~/.openclaw/workspace")
    for name in ("SOUL.md", "IDENTITY.md", "USER.md"):
        a, b = os.path.join(HERE, "persona", name), os.path.join(ws, name)
        if not os.path.exists(b):
            rec(f"线上 {name}", WARN, "工作区里没有这个文件")
        elif filecmp.cmp(a, b, shallow=False):
            rec(f"线上 {name} 与仓库快照一致", OK, f"{os.path.getsize(b)} 字节")
        else:
            # 指出哪边更新，方向别猜错：**线上是事实源**（用户在那里手改），
            # 仓库快照是镜像。不一致时应当把线上收进仓库，而不是拿仓库覆盖线上。
            ta, tb = os.path.getmtime(a), os.path.getmtime(b)
            newer = "线上工作区" if tb > ta else "仓库快照"
            rec(f"线上 {name} 与仓库快照一致", WARN,
                f"两边不一致，更新的是「{newer}」——"
                f"若线上更新（用户手改），应 cp 线上→仓库 收进快照，别反过来覆盖")


# ── 2. 场景引擎 ───────────────────────────────────────────────────────────
def check_engine() -> None:
    print("\n[2] 场景引擎")
    try:
        import random
        from scene.engine import SceneEngine
        from scene.events import load_templates
        from scene.state import SceneState, tier_of

        cfg, _e1 = _load_json(os.path.join(HERE, "config.sessions.json"))
        if cfg is None:
            rec("配置文件可读", BAD, _e1)
            return
        st = SceneState(**{k: v for k, v in cfg["scene"]["start"].items() if k != "start"})
        st.sync_to_utc8 = True
        st.tick_minutes(0, random.Random(1), real_minutes=0)
        rec("时间同步到 UTC+8", OK, f"第{st.day}天 {st.hour:.2f}点（真实北京时间）")

        _c, _e2 = _load_json(os.path.join(HERE, "cast.json"))
        cast = (_c or {}).get("cast", {})
        tpls = load_templates(os.path.join(HERE, cfg["scene"]["events_file"]))
        eng = SceneEngine(st, tpls, random.Random(7), cast=cast)
        evs = eng.tick(minutes=cfg["scene"]["tick_minutes"],
                       user_inactive_minutes=120, real_minutes=0.5)
        rec("引擎 tick 产出事件", OK if evs else WARN,
            f"{len(evs)} 条：" + "、".join(e.template_id for e in evs[:3]) if evs else "本轮无事件（正常）")
        rec("阵容载入", OK, f"{len(cast)} 位角色")
        rec("事件模板载入", OK, f"{len(tpls)} 条模板")

        tiers = "、".join(f"{t}({lo}-{hi})" for t, lo, hi in
                          __import__("scene.state", fromlist=["AFFINITY_TIERS"]).AFFINITY_TIERS)
        rec("亲密度档位", OK, tiers + f"　当前 affinity=100 → {tier_of(100.0)}")
    except Exception as e:                                   # noqa: BLE001
        rec("引擎构造与 tick", BAD, f"{type(e).__name__}: {e}")


# ── 3. 云端 LLM ───────────────────────────────────────────────────────────
def check_cloud_llm(online: bool) -> None:
    print("\n[3] 对话模型 · 云端")
    rc, out = _run(["openclaw", "agents", "list"], timeout=45)
    if rc == 0:
        line = next((l.strip() for l in out.splitlines() if "main" in l), "")
        rec("OpenClaw agent main 存在", OK, line[:100] or "（无描述）")
    else:
        rec("OpenClaw agent main 存在", BAD, out.strip()[:150])

    cfg = os.path.expanduser("~/.openclaw/openclaw.json")
    if os.path.exists(cfg):
        d, jerr = _load_json(cfg)
        if d is None:
            rec("OpenClaw 配置可读", BAD, jerr)
            return
        m = (d.get("agents", {}).get("defaults", {}) or {}).get("model") or {}
        rec("云端主模型已配置", OK if m.get("primary") else BAD,
            f"primary={m.get('primary')}　fallbacks={m.get('fallbacks')}")
        provs = list((d.get("models", {}).get("providers") or {}).keys())
        rec("模型供应商", OK, "、".join(provs))

    if online:
        rc, out = _run(["openclaw", "agent", "--agent", "main",
                        "--session-key", "agent:main:verify",
                        "-m", "[体检] 回一句：本地体检链路正常。"], timeout=180)
        rec("云端对话实测", OK if rc == 0 else BAD, out.strip().splitlines()[-1][:120] if out.strip() else "")


# ── 4. 本地 LLM ───────────────────────────────────────────────────────────
def check_local_llm(online: bool) -> None:
    print("\n[4] 对话模型 · 本地（Ollama）")
    rc, out = _run(["ollama", "list"], timeout=30)
    if rc != 0:
        rec("Ollama 可用", BAD, out.strip()[:150])
        return
    models = [l.split()[0] for l in out.splitlines()[1:] if l.strip()]
    rec("Ollama 可用", OK, "已装模型：" + "、".join(models))
    for m in ("local", "xiaoman"):
        rec(f"本地模型 {m} 已装", OK if any(x.startswith(m) for x in models) else BAD)

    if online:
        rc, out = _run(["openclaw", "agent", "--agent", "main", "--model", "ollama/local",
                        "--session-key", "agent:main:verify-local",
                        "-m", "[体检] 回一句：本地模型体检正常。"], timeout=240)
        rec("本地对话实测（胡桃跑在本地 9B）", OK if rc == 0 else BAD,
            out.strip().splitlines()[-1][:120] if out.strip() else "")


# ── 5. 生图：云端 + 本地 ──────────────────────────────────────────────────
def check_image_cloud(online: bool) -> None:
    print("\n[5] 生图 · 云端（阿里通义万相）")
    script = os.path.join(HERE, "tools", "gen_image.py")
    cfgp = os.path.expanduser("~/.openclaw/workspace/config/aliyun-image.json")
    if not os.path.exists(cfgp):
        rec("阿里生图配置", BAD, f"缺 {cfgp}")
        return
    c, _e3 = _load_json(cfgp)
    if c is None:
        rec("OpenClaw 模型配置", BAD, _e3)
        return
    rec("阿里生图配置", OK, f"model={c.get('model', 'wanx2.1-t2i-turbo')}（0.14 元/张）")
    rec("生图脚本存在", OK if os.path.exists(script) else BAD)
    anchors = os.path.join(HERE, "tools", "image_anchors.json")
    if os.path.exists(anchors):
        a, _ea = _load_json(anchors)
        if a is None:
            a = {}
        rec("专有名词锚定词典", OK,
            f"{len(a.get('characters', {}))} 角色 / {len(a.get('locations', {}))} 场景")
    if online:
        out = os.path.join("/tmp", "verify_cloud_img.png")
        # 跑前先删：失败时脚本不会写 --out，旧图会一直留着冒充"本次实测通过"
        try:
            os.remove(out)
        except OSError:
            pass
        rc, log = _run(["python3", script, "--raw", "一只猫", "--out", out], timeout=300)
        ok = _is_real_image(out)
        rec("云端生图实测", OK if ok else BAD,
            _img_note(out) if ok else log.strip()[-150:])


def check_image_local(online: bool) -> None:
    print("\n[6] 生图 · 本地（ComfyUI，绕开云端审核）")
    comfy = os.path.expanduser("~/ComfyUI-Installs/ComfyUI")
    main_py = os.path.join(comfy, "ComfyUI", "main.py")
    rec("ComfyUI 已安装", OK if os.path.exists(main_py) else BAD, comfy)
    ckpt_dir = os.path.expanduser("~/ComfyUI-Shared/models/checkpoints")
    ckpts = [f for f in glob.glob(os.path.join(ckpt_dir, "*.safetensors")) if os.path.getsize(f) > 1e9]
    partial = [f for f in glob.glob(os.path.join(ckpt_dir, "*.segs", "seg_*"))]
    if ckpts:
        rec("本地底模已就位", OK,
            "、".join(f"{os.path.basename(f)}（{os.path.getsize(f)/2**30:.1f}GB）" for f in ckpts))
    elif partial:
        rec("本地底模已就位", WARN, f"正在下载（{len(partial)} 个分段文件）——完成后需重启本项检查")
    else:
        rec("本地底模已就位", BAD, f"{ckpt_dir} 里没有 >1GB 的 .safetensors，本地生图跑不起来")

    api_up = _port_open(8188)
    rec("ComfyUI 服务在跑", OK if api_up else WARN,
        "127.0.0.1:8188 已监听" if api_up else "未启动（需要时再拉起，仅本地生图依赖它）")

    script = os.path.join(HERE, "tools", "gen_image_local.py")
    rec("本地生图脚本存在", OK if os.path.exists(script) else BAD, os.path.basename(script))
    anchors = os.path.join(HERE, "tools", "image_anchors_local.json")
    if os.path.exists(anchors):
        a, _ea = _load_json(anchors)
        if a is None:
            a = {}
        rec("本地标签词典（danbooru）", OK,
            f"{len(a.get('characters', {}))} 角色 / {len(a.get('locations', {}))} 场景")

    # 亲密度路由：到阈值必须切本地，否则敏感内容仍会送给云端审核
    try:
        import sys as _sys
        _sys.path.insert(0, HERE)
        from scene.delivery import _pick_image_backend
        cfg, _e4 = _load_json(os.path.join(HERE, "config.sessions.json"))
        if cfg is None:
            rec("亲密度→本地生图路由", BAD, _e4)
            return
        cfg = (cfg.get("delivery") or {}).get("channels", [{}])[0].get("config") or {}
        if not (cfg.get("image_local") or {}).get("enabled"):
            rec("亲密度→本地生图路由", WARN, "image_local 未启用，始终走云端")
        else:
            r = {t: _pick_image_backend({"state": {"tier": t}}, cfg)[0]
                 for t in ("stranger", "familiar", "close")}
            good = r["stranger"] == "cloud" and r["familiar"] == "cloud" and r["close"] == "local"
            rec("亲密度→本地生图路由", OK if good else BAD,
                f"stranger/familiar→cloud、close→local：{r}")

        # 对话模型路由：与生图同源，但阈值可以不同（close 是最高档，想更严就得用数值阈值）
        from scene.delivery import _pick_chat_model
        ml = cfg.get("model_local") or {}
        if not ml.get("enabled"):
            rec("亲密度→本地对话模型路由", WARN, "model_local 未启用，始终走云端")
        else:
            aff = ml.get("min_affinity")
            probe = [60.0, 80.0, 96.0] if aff is not None else [20.0, 50.0, 100.0]
            got = {}
            for a in probe:
                # 从 scene.state.AFFINITY_TIERS 读，别硬编码 30/70——
                # 档位定义一改，这里会测错档位却照样给 ✅/❌（同文件别处已经这么做了）
                from scene.state import tier_of
                tier = tier_of(a)
                got[a] = _pick_chat_model({"state": {"tier": tier, "affinity": a}}, cfg)[0]
            local_at = [a for a, m in got.items() if m and "ollama" in m]
            rec("亲密度→本地对话模型路由", OK if local_at else BAD,
                f"min_tier={ml.get('min_tier')} min_affinity={aff}；"
                f"本地在 {local_at or '无'} 生效：{got}")

        # 直接聊天那条路走 OpenClaw 配置，检查引擎能否键级同步
        from scene.openclaw_sync import current_model
        cur = current_model(ml.get("agent_id", "main"))
        rec("OpenClaw 侧对话模型可读", OK if cur else BAD, f"当前 {cur or '(读不到)'}")
    except Exception as e:                                   # noqa: BLE001
        rec("亲密度路由", BAD, f"{type(e).__name__}: {e}")

    if online and ckpts and api_up:
        lout = "/tmp/verify_local_img.png"
        try:
            os.remove(lout)
        except OSError:
            pass
        rc, log = _run(["python3", script, "1girl, solo, smile", "--out", lout], timeout=600)
        ok = _is_real_image(lout)
        rec("本地生图实测", OK if ok else BAD,
            _img_note(lout) if ok else (log.strip().splitlines()[-1][:150] if log.strip() else "无输出"))


# ── 7. 语音（本地 TTS）────────────────────────────────────────────────────
def check_tts(online: bool) -> None:
    print("\n[7] 语音 · 本地（Thunder-TTS 胡桃音色）")
    s = os.path.expanduser("~/Thunder-TTS/scripts/hutao_tts.sh")
    rec("TTS 脚本存在", OK if os.path.exists(s) else BAD, s)
    ref = os.path.expanduser("~/Documents/Hutao Voice/references/LQ/vo_HTLQ001_11_hutao_02.wav")
    rec("参考音频存在", OK if os.path.exists(ref) else WARN, os.path.basename(ref))
    if online and os.path.exists(s):
        rc, log = _run([s, "/tmp/verify_tts.wav", "本堂主体检中。"], timeout=300)
        tts_out = "/tmp/verify_tts.wav"
        try:
            os.remove(tts_out)
        except OSError:
            pass
        ok = os.path.exists(tts_out) and os.path.getsize(tts_out) > 10000
        rec("TTS 合成实测", OK if ok else BAD,
            f"{os.path.getsize('/tmp/verify_tts.wav')//1024}KB" if ok else log.strip()[-120:])
    # 这条原来是**假检查**：items 拿到后从不参与判断，状态与说明全硬编码，
    # 把 voice_enabled 改回 true（语音重新产出音频）时它照样报 ✅。
    from scene.delivery import _parse_media_markers
    vtext, items = _parse_media_markers("[[voice: 测试]]", {"voice_enabled": False})
    voice_items = [it for it in items if it.get("kind") == "voice"]
    rec("语音输出已按微信限制禁用",
        OK if (not voice_items and "测试" in vtext) else BAD,
        f"items={items}、文本保留={'测试' in vtext}")


# ── 8. 图库 ───────────────────────────────────────────────────────────────
def check_gallery() -> None:
    print("\n[8] 图库（官方图复用）")
    g = os.path.join(HERE, "gallery")
    # 按扩展名过滤：原来把 README.md / tags.json 也数成"图片"，
    # 于是全新 clone（图片被 .gitignore 排除）也会报"✅ 2 张"。
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    n = sum(len([f for f in fs if f.lower().endswith(exts) and not f.startswith(".")])
            for _, _, fs in os.walk(g))
    chars = [d for d in os.listdir(g) if os.path.isdir(os.path.join(g, d))]
    # 角色目录存在但一张图都没有 → 取图必然 MISS，不能算 OK
    rec("图库已入库", OK if (n and chars) else BAD,
        f"{n} 张图，覆盖 {len(chars)} 个角色")
    rc, out = _run(["python3", os.path.join(HERE, "tools", "gallery.py"), "pick", "胡桃 立绘"], timeout=20)
    rec("关键词取图", OK if "GALLERY_OK" in out else BAD,
        out.strip().splitlines()[0].replace(HERE + "/", "") if out.strip() else "")
    # 用图库里**没有**的角色测：必须 MISS，而不是拿"立绘"这半边匹配、发回一张胡桃
    rc, out = _run(["python3", os.path.join(HERE, "tools", "gallery.py"), "pick", "芙宁娜 立绘"], timeout=20)
    rec("主题词缺失时正确 MISS", OK if "GALLERY_MISS" in out else BAD, "不会发错成别的角色")


# ── 9. 投递通道 ───────────────────────────────────────────────────────────
def check_channels() -> None:
    print("\n[9] 投递通道（可达性，不实发）")
    p = os.path.join(HERE, "config.webhook.json")
    if os.path.exists(p):
        d, _e5 = _load_json(p)
        if d is None:
            rec("渠道③ webhook 配置", BAD, _e5)
            d = {}
        rec("渠道③ webhook 配置", OK, f"endpoint {d.get('delivery', {}).get('channels', [{}])[0].get('config', {}).get('url', '?')[:60]}")
    else:
        rec("渠道③ webhook 配置", WARN, "config.webhook.json 不存在（该路线未启用）")
    pend = os.path.expanduser("~/.openclaw/workspace/memory/events/pending.json")
    rec("渠道② pending 文件", OK if os.path.exists(pend) else WARN,
        f"{len(json.load(open(pend, encoding='utf-8')).get('events', []))} 条待处理" if os.path.exists(pend) else "未创建")
    rc, out = _run(["openclaw", "message", "--help"], timeout=30)
    rec("渠道① openclaw message send 可用", OK if rc == 0 else BAD)


# ── 10. 辅助系统 ──────────────────────────────────────────────────────────
def check_aux() -> None:
    print("\n[10] 辅助系统")
    for name, path in (("关键词记忆", "logs/topic_memory.json"),
                       ("运行日志", "logs/harbor.jsonl"),
                       ("场景状态", "state.json")):
        p = os.path.join(HERE, path)
        rec(name, OK if os.path.exists(p) else WARN,
            f"{os.path.getsize(p)//1024}KB" if os.path.exists(p) else "未生成")

    for name, port in (("日志 WebUI", 8620), ("Live2D 桥", 8630), ("VTube Studio", 8001)):
        up = _port_open(port)
        rec(f"{name}（{port}）", OK if up else WARN, "在跑" if up else "未启动")
    rec("桌面控制工具", OK if os.path.exists(os.path.join(HERE, "tools", "desktop.py")) else WARN,
        "cliclick: " + ("已装" if shutil.which("cliclick") else "缺失"))

    # 原神实时资讯 → 世界内事件（离线：只读缓存，不联网）
    try:
        sys.path.insert(0, HERE)
        from scene import news as GN
        nj = GN.load(os.path.join(HERE, "logs", "genshin_news.json"), {"cache_hours": 6})
        nevs = GN.world_events(nj)
        ver = (nj.get("version") or {}).get("no") or "?"
        fest = (nj.get("festival") or {}).get("name") or "无节庆"
        src = {"api": "官方API", "cache": "缓存", "none": "无数据"}.get(nj.get("source"), "?")
        rec("原神实时资讯", OK if nevs else WARN,
            f"{ver} 版本 · {fest} · {len(nevs)} 条时令事件 · {src}")
    except Exception as e:
        rec("原神实时资讯", WARN, f"{type(e).__name__}: {e}")

    shared = os.path.expanduser("~/Documents/DSH/_shared")
    if os.path.isdir(shared):
        rc, out = _run(["python3", os.path.join(shared, "check_openclaw.py")], timeout=30)
        rec("跨工作区配置校验", OK if rc == 0 else BAD, out.strip().splitlines()[-1] if out.strip() else "")
        rc, out = _run(["python3", os.path.join(shared, "check_model_fallback.py")], timeout=40)
        rec("模型降级巡检", OK if rc == 0 else WARN, out.strip().splitlines()[0] if out.strip() else "")
    else:
        rec("跨工作区共享目录", WARN, f"{shared} 不存在")


def main() -> int:
    ap = argparse.ArgumentParser(description="胡桃功能体检")
    ap.add_argument("--online", action="store_true", help="附带真实调用（花额度/发消息）")
    args = ap.parse_args()

    print("胡桃功能体检" + ("（含在线实测）" if args.online else "（离线）"))
    check_configs()
    check_engine()
    check_cloud_llm(args.online)
    check_local_llm(args.online)
    check_image_cloud(args.online)
    check_image_local(args.online)
    check_tts(args.online)
    check_gallery()
    check_channels()
    check_aux()

    bad = [r for r in results if r[1] == BAD]
    warn = [r for r in results if r[1] == WARN]
    print(f"\n{'=' * 60}\n通过 {len(results) - len(bad) - len(warn)}　"
          f"警告 {len(warn)}　失败 {len(bad)}")
    for n, s, d in results:
        if s != OK:
            print(f"  {s} {n}" + (f" — {d}" if d else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
