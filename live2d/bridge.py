#!/usr/bin/env python3
"""Live2D 桥接服务：把「胡桃说了什么、什么情绪」翻译成 VTube Studio 的表情与动作。

架构：
    引擎/OpenClaw 回复 ──HTTP POST /say──► 本服务 ──WebSocket──► VTube Studio（Live2D 渲染）

能力：
    · 情绪识别（本地关键词，零 API 成本）→ 表情文件 + 参数注入
    · 双层情绪（瞬时反应 + 持续心情底色）
    · 自动行为：眨眼 / 呼吸 / 待机摇摆 / 说话时嘴型起伏
    · VTS 未启动时自动重连，不影响主链路（微信投递等）

用法：
    python3 live2d/bridge.py                       # 默认 127.0.0.1:8630，连 ws://127.0.0.1:8001
    python3 live2d/bridge.py --fps 24 --vts ws://127.0.0.1:8001

接口：
    POST /say     {"text": "...", "emotion": "joy"(可选), "intensity": 0.8(可选)}
    GET  /state   当前情绪 / 心情分布 / VTS 连接状态
    GET  /       简易状态页
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from emotions import MoodModel, infer

HERE = os.path.dirname(os.path.abspath(__file__))
MAPPING_PATH = os.path.join(HERE, "mapping.json")
TOKEN_PATH = os.path.join(HERE, ".vts_token.json")


def load_mapping() -> dict:
    with open(MAPPING_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- VTS 客户端

class VTSClient:
    """VTube Studio Public API 的最小客户端（同步版，websockets>=12）。"""

    def __init__(self, url: str, plugin_name: str = "hutao", developer: str = "hutao"):
        self.url = url
        self.plugin_name = plugin_name
        self.developer = developer
        self.ws = None
        self.connected = False
        self._req_id = 0
        self._lock = threading.Lock()

    # ---- 连接与鉴权 ----
    def connect(self) -> bool:
        from websockets.sync.client import connect as ws_connect
        try:
            self.ws = ws_connect(self.url, open_timeout=5, close_timeout=2, legacy=True)
        except Exception as e:
            print(f"[vts] 连接失败: {e}")
            return False

        token = self._load_token()

        # ① 已有 token → 直接鉴权
        if token:
            try:
                resp = self._request("AuthenticationRequest", {
                "pluginName": self.plugin_name,
                "pluginDeveloper": self.developer,
                    "authenticationToken": token,
                }, timeout=20)
            except Exception as e:
                print(f"[vts] 鉴权请求异常: {type(e).__name__}")
                return False
            if self._is_authed(resp):
                self.connected = True
                print("[vts] 已连接并鉴权（复用已保存的 token）")
                return True
            print("[vts] 旧 token 失效，将重新申请授权")

        # ② 无 token（或已失效）→ 先申请 token（VTS 会弹窗，需点"允许"）
        print("[vts] 正在申请授权：请在 VTube Studio 弹窗中点『允许』（等待最多 120 秒）")
        try:
            resp = self._request("AuthenticationTokenRequest", {
                "pluginName": self.plugin_name,
                "pluginDeveloper": self.developer,
            }, timeout=120)
        except Exception as e:
            print(f"[vts] 未获得授权（{type(e).__name__}）——请确认 VTS 窗口可见并点击『允许』")
            return False
        new_token = (resp.get("data") or {}).get("authenticationToken")
        if not new_token:
            err = resp.get("data", {})
            print(f"[vts] 申请 token 失败: {resp.get('messageType')} {err.get('message') or err.get('errorID')}")
            return False
        self._save_token(new_token)
        print("[vts] 已获得授权 token，开始鉴权…")

        # ③ 用新 token 鉴权
        try:
            resp = self._request("AuthenticationRequest", {
                "pluginName": self.plugin_name,
                "pluginDeveloper": self.developer,
                "authenticationToken": new_token,
            }, timeout=20)
        except Exception as e:
            print(f"[vts] 鉴权异常: {type(e).__name__}")
            return False
        if not self._is_authed(resp):
            print(f"[vts] 鉴权失败: {(resp.get('data') or {}).get('message') or resp.get('messageType')}")
            return False
        self.connected = True
        print("[vts] ✅ 已连接并鉴权")
        return True

    # ---- token 存取与判定 ----
    def _load_token(self):
        if not os.path.exists(TOKEN_PATH):
            return None
        try:
            return json.load(open(TOKEN_PATH, encoding="utf-8")).get("token")
        except Exception:
            return None

    def _save_token(self, token: str) -> None:
        try:
            json.dump({"token": token}, open(TOKEN_PATH, "w", encoding="utf-8"))
        except OSError:
            pass

    @staticmethod
    def _is_authed(resp: dict) -> bool:
        return bool((resp.get("data") or {}).get("authenticated"))

    def _request(self, message_type: str, data: dict, timeout: float = 5) -> dict:
        with self._lock:
            self._req_id += 1
            payload = {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": f"liyue-{self._req_id}",
                "messageType": message_type,
                "data": data,
            }
            self.ws.send(json.dumps(payload))
            raw = self.ws.recv(timeout=timeout)
            return json.loads(raw)

    def close(self) -> None:
        self.connected = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ---- 具体能力 ----
    def inject_params(self, params: Dict[str, float]) -> None:
        if not self.connected or not params:
            return
        values = [{"id": k, "value": float(v), "weight": 1.0} for k, v in params.items()]
        try:
            self._request("InjectParameterDataRequest", {
                "faceFound": False, "mode": "set", "parameterValues": values,
            })
        except Exception as e:
            print(f"[vts] 参数注入失败（将重连）: {e}")
            self.close()

    def activate_expression(self, expression_file: str) -> None:
        if not self.connected or not expression_file:
            return
        try:
            self._request("ExpressionActivationRequest", {"expressionFile": expression_file, "active": True})
        except Exception as e:
            print(f"[vts] 表情切换失败: {e}")

    def deactivate_expression(self, expression_file: str) -> None:
        if not self.connected or not expression_file:
            return
        try:
            self._request("ExpressionActivationRequest", {"expressionFile": expression_file, "active": False})
        except Exception as e:
            print(f"[vts] 表情关闭失败: {e}")

    def trigger_hotkey(self, hotkey_id: str) -> None:
        if not self.connected or not hotkey_id:
            return
        try:
            self._request("HotkeyTriggerRequest", {"hotkeyID": hotkey_id})
        except Exception as e:
            print(f"[vts] 热键触发失败: {e}")


# ---------------------------------------------------------------- 桥接状态

class BridgeState:
    """共享状态：情绪、说话窗口、参数映射。"""

    def __init__(self, mapping: dict):
        self.mapping = mapping
        b = mapping.get("behavior", {})
        self.mood = MoodModel(decay=b.get("mood_decay", 0.85), mood_weight=b.get("mood_weight", 0.35))
        self.emotion_params: Dict[str, Dict[str, float]] = mapping.get("emotion_params", {})
        self.limits: Dict[str, list] = mapping.get("param_limits", {})
        self.ease_alpha = b.get("ease_alpha", 0.18)
        self.speaking_until = 0.0
        self.current = {pid: 0.0 for pid in self.limits}
        self.last_emotion = "neutral"
        self.last_text = ""
        self.lock = threading.Lock()

    def on_say(self, text: str, emotion: Optional[str] = None, intensity: Optional[float] = None) -> dict:
        with self.lock:
            if emotion:
                emo, inten, clean = emotion, (intensity if intensity is not None else 0.8), text
            else:
                emo, inten, clean = infer(text)
            self.mood.update(emo, inten)
            # 说话窗口：按字数估时长（中文 ~4.5 字/秒），至少 2 秒
            dur = max(2.0, min(20.0, len(clean) / 4.5))
            self.speaking_until = time.time() + dur
            self.last_emotion, self.last_text = emo, clean
            return {"emotion": emo, "intensity": inten, "speaking_seconds": round(dur, 1), "text": clean}

    def speaking(self) -> bool:
        return time.time() < self.speaking_until


# ---------------------------------------------------------------- 动画循环

def animator(state: BridgeState, vts: VTSClient, fps: int, stop: threading.Event) -> None:
    """按 fps 计算参数并注入 VTS：情绪混合 + 眨眼 + 呼吸 + 摇摆 + 说话嘴型。"""
    interval = 1.0 / max(1, fps)
    mapping = state.mapping
    b = mapping.get("behavior", {})
    blink_range = b.get("blink_interval_sec", [2.5, 6.0])
    blink_dur = b.get("blink_duration_sec", 0.12)
    breath_period = b.get("breath_period_sec", 3.5)
    sway_period = b.get("sway_period_sec", 6.0)
    sway_amp = b.get("sway_amplitude", 2.5)
    talk_mouth = mapping.get("talk_mouth", True)
    expressions = mapping.get("expressions", {})

    t0 = time.time()
    last_tick = t0
    next_blink = t0 + random.uniform(*blink_range)
    blink_until = 0.0
    last_expr_emotion = None

    while not stop.is_set():
        now = time.time()
        dt = now - last_tick
        last_tick = now
        with state.lock:
            state.mood.decay_step(dt)
            target = state.mood.blend(state.emotion_params)
            speaking = state.speaking()
            dominant = state.mood.dominant()
            emotion_now = state.last_emotion if speaking else dominant

        # 表情文件（若模型提供了对应表情）：切换时先关闭上一个，避免叠加/残留
        #
        # ⚠️ 原来切换条件里带了 `and speaking`，而"关闭"也写在同一个分支里——
        # 于是说话一结束就再没有机会进入这段代码，`deactivate_expression` **全程 0 次调用**，
        # 表情文件永久生效：一次"害羞"之后她的脸会一直红着。
        # 现在把"说话中就跟着情绪换"和"说完了就收掉"分成两件事。
        if speaking:
            if state.last_emotion != last_expr_emotion:
                prev = expressions.get(last_expr_emotion) if last_expr_emotion else None
                if prev:
                    vts.deactivate_expression(prev)
                expr = expressions.get(state.last_emotion)
                if expr:
                    vts.activate_expression(expr)
                last_expr_emotion = state.last_emotion
        elif last_expr_emotion:
            prev = expressions.get(last_expr_emotion)
            if prev:
                vts.deactivate_expression(prev)
            last_expr_emotion = None

        # 自动行为叠加
        #
        # ⚠️ 参数名必须用 **mapping.json 的 VTS 命名**（EyeOpenLeft / MouthOpen /
        # FaceAngleZ…）。原来这里写的是 Live2D 原生名（ParamEyeLOpen / ParamMouthOpenY /
        # ParamBreath / ParamBodyAngleZ），与 mapping 里那套**完全对不上**：
        #   · param_limits 里没有这些键 → 限幅走默认 [-1e9, 1e9]，等于不限幅；
        #   · 更糟的是下面那句守卫写的是 `"ParamEyeLOpen" not in target`，
        #     按错名字判断 → 情绪给的眼部参数（EyeOpenLeft）**每帧被覆盖成 0.85**，
        #     shy(0.45)、sleepy(0.15) 这类全失效。
        #   · VTS 收到模型里不存在的参数名，轻则忽略，重则整条注入请求失败。
        breath = 0.5 + 0.5 * math.sin(2 * math.pi * (now - t0) / breath_period)
        sway = sway_amp * math.sin(2 * math.pi * (now - t0) / sway_period)
        # 表情参数用映射名；模型如果没有呼吸/身体摆动参数，就用有映射的那些代替
        target["FaceAngleX"] = target.get("FaceAngleX", 0.0) * 0.7 + sway * 0.3
        target["FaceAngleZ"] = target.get("FaceAngleZ", 0.0) + sway * 0.6
        target["FacePositionY"] = target.get("FacePositionY", 0.0) + (breath - 0.5) * 1.2

        # 眨眼（用映射名，别覆盖情绪给的眼睛开合）
        if now >= next_blink:
            blink_until = now + blink_dur
            next_blink = now + random.uniform(*blink_range)
        if now < blink_until:
            target["EyeOpenLeft"] = 0.0
            target["EyeOpenRight"] = 0.0
        else:
            # 情绪已经给过眼睛开合就不要动它（shy/sleepy 就是靠这个表达的）
            if "EyeOpenLeft" not in target:
                target["EyeOpenLeft"] = 0.85
            if "EyeOpenRight" not in target:
                target["EyeOpenRight"] = 0.85

        # 说话时嘴型（无语音时的"静音说话"动画）——同样用映射名 MouthOpen
        if talk_mouth and speaking:
            talk = 0.35 + 0.35 * abs(math.sin(2 * math.pi * now * 3.2))
            target["MouthOpen"] = max(target.get("MouthOpen", 0.1), talk)
        elif "MouthOpen" in target:
            target["MouthOpen"] = target.get("MouthOpen", 0.08) * 0.6

        # 缓动 + 限幅
        #
        # ⚠️ 原来只遍历本轮 target：情绪已经在 emotions.py 里被跳过（权重≤0.001 的
        # 参数既不进 target、也不会被拉回），于是那些参数**永久冻结在最后一次的值**上——
        # 心情早就归零了，注入内容却和归零前逐字节相同（实测）。
        # 这里对"上一轮有、这一轮没有"的参数补一个静息目标，让它们缓动回去。
        with state.lock:
            for pid, val in target.items():
                lo, hi = state.limits.get(pid, [-1e9, 1e9])
                want = max(lo, min(hi, val))
                cur = state.current.get(pid, want)
                state.current[pid] = cur + (want - cur) * state.ease_alpha
            rest = [pid for pid in state.current if pid not in target]
            for pid in rest:
                lo, hi = state.limits.get(pid, [-1e9, 1e9])
                rest_v = max(lo, min(hi, 0.0))            # 静息值：0（表情参数的中性位）
                cur = state.current[pid]
                nxt = cur + (rest_v - cur) * state.ease_alpha
                # 已经贴近静息值就从表里摘掉，别让 state.current 无限膨胀
                if abs(nxt - rest_v) < 1e-3:
                    state.current.pop(pid, None)
                else:
                    state.current[pid] = nxt
            out = dict(state.current)

        if vts.connected:
            vts.inject_params(out)
        elif now % 10 < interval:      # 每 10 秒尝试重连一次
            try:
                vts.connect()
            except Exception as e:
                print(f"[vts] 重连异常: {type(e).__name__}")
        stop.wait(interval)


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    state: BridgeState = None
    vts: VTSClient = None

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/state"):
            with self.state.lock:
                self._json(200, {
                    "vts_connected": self.vts.connected,
                    "emotion": self.state.last_emotion,
                    "speaking": self.state.speaking(),
                    "mood": {k: round(v, 3) for k, v in self.state.mood.mood.items() if v > 0.01},
                    "last_text": self.state.last_text[:80],
                })
        elif self.path == "/":
            html = ("<html><meta charset='utf-8'><body style='font-family:system-ui;background:#0f172a;color:#e2e8f0;padding:24px'>"
                    "<h2>🌙 璃月港 · Live2D 桥接</h2>"
                    f"<p>VTS 连接：{'✅ 已连接' if self.vts.connected else '❌ 未连接'}</p>"
                    f"<p>当前情绪：{self.state.last_emotion}</p>"
                    f"<p>最近文本：{self.state.last_text[:120]}</p>"
                    "<p>POST <code>/say</code> {\"text\":\"...\"} 即可驱动表情</p></body></html>")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/say"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        result = self.state.on_say(
            payload.get("text", ""), payload.get("emotion"), payload.get("intensity")
        )
        print(f"[say] ({result['emotion']}) {result['text'][:60]}")
        # 可选：热键动作
        motion = self.state.mapping.get("motions", {}).get(result["emotion"])
        if motion:
            self.vts.trigger_hotkey(motion)
        self._json(200, {"ok": True, **result})

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- 入口

def main() -> None:
    ap = argparse.ArgumentParser(description="Live2D 桥接（VTube Studio）")
    ap.add_argument("--vts", default="ws://127.0.0.1:8001", help="VTube Studio API 地址")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8630)
    ap.add_argument("--fps", type=int, default=None, help="参数刷新帧率（默认取 mapping.json）")
    ap.add_argument("--no-vts", action="store_true", help="只跑 HTTP（不连 VTS，用于联调）")
    args = ap.parse_args()

    mapping = load_mapping()
    fps = args.fps or mapping.get("fps", 20)
    state = BridgeState(mapping)
    vts = VTSClient(args.vts)

    if not args.no_vts:
        vts.connect()

    Handler.state, Handler.vts = state, vts
    stop = threading.Event()
    threading.Thread(target=animator, args=(state, vts, fps, stop), daemon=True).start()

    print(f"[bridge] HTTP: http://{args.host}:{args.port}/  | VTS: {args.vts} | fps={fps}")
    print(f"[bridge] 测试: curl -X POST localhost:{args.port}/say -d '{{\"text\":\"哎呀旅行者，你来啦！\"}}'")
    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        stop.set()
        vts.close()


if __name__ == "__main__":
    main()
