#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地生图：调用 ComfyUI 的 HTTP API（默认 127.0.0.1:8188）。

**为什么需要它**：阿里通义万相有内容审核，亲密向/敏感度高一点的画面会被拒。
ComfyUI 跑在本地，没有服务端审核，是「亲密度到阈值后改走本地」那条路的后端。
另外它不按张计费，画多少都不花钱，只是慢一些。

和 `gen_image.py` 的接口保持一致：成功打印 `IMAGE_OK <路径>`，失败打印 `IMAGE_FAIL <原因>`，
产出文件落在 `--out`，所以 `scene/delivery.py` 两个后端都能直接调。

**提示词体系不同**：万相吃中文描述，本地 SDXL（animagine 系）吃 **danbooru 英文标签**。
`tools/image_anchors_local.json` 负责把「胡桃」「璃月港」这类词补成标签，可用 `--raw` 关掉。

    python3 tools/gen_image_local.py "hu tao (genshin impact), 1girl, smile" --out /tmp/a.png
    python3 tools/gen_image_local.py --list-ckpt          # 看有哪些底模
    python3 tools/gen_image_local.py --check              # 看 ComfyUI 是否在跑
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ANCHORS = os.path.join(HERE, "image_anchors_local.json")

# animagine 系推荐的负面词
DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, missing finger, extra digits, "
    "fewer digits, cropped, worst quality, low quality, low score, bad score, "
    "average score, signature, watermark, username, blurry, jpeg artifacts"
)
DEFAULT_QUALITY = "masterpiece, best quality, very aesthetic, absurdres"


def _unload_llm() -> None:
    """生图前卸掉 Ollama 里常驻的大模型，腾内存给 ComfyUI。失败不影响生图。"""
    try:
        proc = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=15)
        loaded = [ln.split()[0] for ln in (proc.stdout or "").splitlines()[1:] if ln.strip()]
        for name in loaded:
            subprocess.run(["ollama", "stop", name], capture_output=True, timeout=30)
            print(f"UNLOADED {name}（腾出内存给 ComfyUI）")
    except (OSError, subprocess.SubprocessError):
        pass


def _api(host: str, path: str, payload: dict = None, timeout: int = 30):
    url = f"http://{host}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"_raw": body[:200]}


def _strip_cjk(text: str) -> str:
    """去掉中日韩字符——本地模型不认识它们，留着只是噪音（标签已经把意思带上了）。"""
    out = "".join(ch for ch in text if not ("\u4e00" <= ch <= "\u9fff"
                                            or "\u3040" <= ch <= "\u30ff"))
    out = re.sub(r"\s*,\s*(?=,)", "", out)      # 清掉剥空后留下的连续逗号
    return re.sub(r",\s*,+", ",", out).strip(" ,")


def enrich(prompt: str, raw: bool = False) -> tuple:
    """中文/角色名 → danbooru 标签（本地模型只认标签，不认中文描述）。"""
    if raw or not os.path.exists(ANCHORS):
        return prompt, []
    try:
        cfg = json.load(open(ANCHORS, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prompt, []
    hits, extra = [], []
    for group, label in (("characters", "角色"), ("locations", "场景"), ("elements", "元素")):
        for name, item in (cfg.get(group) or {}).items():
            if name.startswith("_"):
                continue
            if any(a in prompt for a in item.get("aliases", [])):
                extra.append(item["tags"])
                hits.append(f"{label}:{name}")
    quality = cfg.get("quality_prefix", DEFAULT_QUALITY)
    body = _strip_cjk(prompt) if hits else prompt.strip()
    parts = [quality, body.rstrip(" ,")] + extra
    return ", ".join(p for p in parts if p), hits


def build_workflow(ckpt: str, positive: str, negative: str,
                   width: int, height: int, steps: int, cfg: float, seed: int,
                   lora: str = "", lora_strength: float = 0.8) -> dict:
    """ComfyUI API 格式的最小 txt2img 工作流。给了 lora 就插一个 LoraLoader 节点。

    LoraLoader 串在 Checkpoint → KSampler 之间：模型权重被"打上角色补丁"，
    **CLIP 也要一起换**（LoRA 通常同时微调了文本编码器，只接模型那一半效果会打折）。
    """
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
    }
    model_src, clip_src = ["4", 0], ["4", 1]
    if lora:
        wf["10"] = {"class_type": "LoraLoader",
                    "inputs": {"lora_name": lora,
                               "strength_model": lora_strength,
                               "strength_clip": lora_strength,
                               "model": ["4", 0], "clip": ["4", 1]}}
        model_src, clip_src = ["10", 0], ["10", 1]
    wf["6"] = {"class_type": "CLIPTextEncode",
               "inputs": {"text": positive, "clip": clip_src}}
    wf["7"] = {"class_type": "CLIPTextEncode",
               "inputs": {"text": negative, "clip": clip_src}}
    wf["3"] = {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                          "sampler_name": "euler_ancestral", "scheduler": "normal",
                          "denoise": 1.0, "model": model_src,
                          "positive": ["6", 0], "negative": ["7", 0],
                          "latent_image": ["5", 0]}}
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}}
    wf["9"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "hutao_local", "images": ["8", 0]}}
    return wf


def _node_options(host: str, node: str, field: str) -> list:
    info = _api(host, f"/object_info/{node}", timeout=8)
    return (((info.get(node) or {}).get("input") or {}).get("required") or {}).get(field, [[]])[0] or []


def main() -> int:
    ap = argparse.ArgumentParser(description="本地生图（ComfyUI）")
    ap.add_argument("prompt", nargs="?", default="", help="英文 danbooru 标签，或含中文关键词")
    ap.add_argument("--out", default="/tmp/hutao_local.png")
    ap.add_argument("--host", default="127.0.0.1:8188")
    ap.add_argument("--ckpt", default=None, help="底模文件名（默认自动选第一个）")
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--cfg", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--negative", default=DEFAULT_NEGATIVE)
    ap.add_argument("--timeout", type=int, default=900, help="轮询总超时（秒）")
    ap.add_argument("--raw", action="store_true", help="不做标签增强")
    ap.add_argument("--lora", default=None, help="LoRA 文件名（模糊匹配），不传则用配置里的默认值")
    ap.add_argument("--no-lora", action="store_true", help="这次不用 LoRA")
    ap.add_argument("--lora-strength", type=float, default=None)
    ap.add_argument("--list-ckpt", action="store_true")
    ap.add_argument("--list-lora", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check or args.list_ckpt or args.list_lora:
        try:
            ck = _node_options(args.host, "CheckpointLoaderSimple", "ckpt_name")
            lo = _node_options(args.host, "LoraLoader", "lora_name")
            print(f"COMFY_OK {args.host}")
            print(f"底模 {len(ck)} 个：")
            for o in ck:
                print(f"   {o}")
            print(f"LoRA {len(lo)} 个：")
            for o in lo:
                print(f"   {o}")
            return 0 if ck else 1
        except Exception as e:                                # noqa: BLE001
            print(f"COMFY_DOWN {args.host} 不可达：{type(e).__name__}: {e}")
            return 2

    if not args.prompt:
        print("IMAGE_FAIL 没有提示词")
        return 2

    # 先卸掉本地大模型，把内存让给 ComfyUI。
    # 这台机器 24GB：SDXL 生图要约 8GB，本地模型常驻 12GB，两者叠加必然换页。
    # 而这两件事本来就是**先后发生**的（先让模型写出回复、再画图），没有并存的必要。
    # 卸掉只花一次 6.5s 重载（下次对话时），换来生图不 swap。
    _unload_llm()

    try:
        ckpts = _node_options(args.host, "CheckpointLoaderSimple", "ckpt_name")
        loras = _node_options(args.host, "LoraLoader", "lora_name")
    except Exception as e:                                    # noqa: BLE001
        print(f"IMAGE_FAIL ComfyUI 不可达（{args.host}）：{type(e).__name__}")
        return 2
    if not ckpts:
        print("IMAGE_FAIL ComfyUI 里没有可用底模（检查 extra_model_paths.yaml 与 checkpoints 目录）")
        return 2
    # 底模优先选 Illustrious（胡桃 LoRA 是 SDXL 系，配动漫向 SDXL 底座效果最好）
    ckpt = args.ckpt or next((c for c in ckpts if "illustrious" in c.lower()),
                             next((c for c in ckpts if "animagine" in c.lower()), ckpts[0]))

    # LoRA：--no-lora 关掉；--lora 模糊匹配；否则用配置默认（image_anchors_local.json 的 default_lora）
    lora = ""
    strength = args.lora_strength
    if not args.no_lora:
        want = args.lora
        if want is None:
            try:
                cfg = json.load(open(ANCHORS, encoding="utf-8"))
                want = cfg.get("default_lora")
                if strength is None:
                    strength = cfg.get("default_lora_strength", 0.8)
            except (OSError, json.JSONDecodeError):
                want = None
        if want:
            hit = next((l for l in loras if want.lower() in l.lower()), None)
            if hit:
                lora = hit
            elif loras:
                print(f"LR_WARN 找不到匹配 '{want}' 的 LoRA（现有：{'、'.join(loras)}）")
    if strength is None:
        strength = 0.8

    positive, hits = enrich(args.prompt, args.raw)
    if hits:
        print(f"PROMPT_ENRICHED {','.join(hits)}")
    if lora:
        print(f"LORA {lora} (strength={strength})")
    try:
        w, h = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        w = h = 1024
    seed = args.seed if args.seed is not None else random.randint(1, 2**31)

    try:
        res = _api(args.host, "/prompt",
                   {"prompt": build_workflow(ckpt, positive, args.negative, w, h,
                                             args.steps, args.cfg, seed,
                                             lora, strength)}, timeout=30)
    except Exception as e:                                    # noqa: BLE001
        print(f"IMAGE_FAIL 提交任务失败：{type(e).__name__}: {e}")
        return 2
    pid = res.get("prompt_id")
    if not pid:
        print(f"IMAGE_FAIL 提交被拒：{json.dumps(res, ensure_ascii=False)[:200]}")
        return 2

    t0 = time.time()
    while time.time() - t0 < args.timeout:
        time.sleep(2)
        try:
            hist = _api(args.host, f"/history/{pid}", timeout=20)
        except Exception:                                     # noqa: BLE001
            continue
        entry = hist.get(pid)
        if not entry:
            continue
        status = (entry.get("status") or {}).get("status_str")
        if status == "error":
            msgs = (entry.get("status") or {}).get("messages") or []
            print(f"IMAGE_FAIL ComfyUI 报错：{json.dumps(msgs, ensure_ascii=False)[:300]}")
            return 3
        for node_out in (entry.get("outputs") or {}).values():
            for img in node_out.get("images", []):
                q = urllib.parse.urlencode({"filename": img.get("filename", ""),
                                            "subfolder": img.get("subfolder", ""),
                                            "type": img.get("type", "output")})
                try:
                    with urllib.request.urlopen(f"http://{args.host}/view?{q}", timeout=60) as r:
                        data = r.read()
                except Exception as e:                        # noqa: BLE001
                    print(f"IMAGE_FAIL 取图失败：{type(e).__name__}")
                    return 3
                os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
                # 落盘前校验**完整性**：只判异常不够——服务端走 chunked、不给
                # Content-Length 时 urllib 不会抛 IncompleteRead，截断的图会静默落盘
                # 并打印 IMAGE_OK（历史上真出过"截断了却不报错"）。校验魔数+尾部结束
                # 标记，再原子替换，避免半个文件被下游当成品图发出去。
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from scene.imagery import describe, is_valid_image
                part = args.out + ".part"
                with open(part, "wb") as f:
                    f.write(data)
                if not is_valid_image(part):
                    print(f"IMAGE_FAIL 取到的不是完整图片：{describe(part)}")
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    return 3
                os.replace(part, args.out)
                print(f"IMAGE_OK {args.out}  ({len(data)//1024}KB, {time.time()-t0:.0f}s, "
                      f"{w}x{h}, steps={args.steps}, seed={seed}, ckpt={ckpt})")
                return 0
        print("IMAGE_FAIL 任务完成但没有图片输出")
        return 3

    print(f"IMAGE_FAIL 超时（{args.timeout}s）")
    return 4


if __name__ == "__main__":
    sys.exit(main())
