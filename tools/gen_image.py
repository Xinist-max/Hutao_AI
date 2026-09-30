#!/usr/bin/env python3
"""生图工具（阿里云 DashScope 通义万相，异步模式）—— 供引擎与 agent 调用。

用法:
  python3 tools/gen_image.py "一只戴着乾坤泰卦帽的橘猫，璃月港背景" [--out /tmp/x.png] [--size 1024*1024]

成功时输出: IMAGE_OK <本地路径>
失败时输出: IMAGE_FAIL <原因>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

CONFIG = os.path.expanduser("~/.openclaw/workspace/config/aliyun-image.json")
ANCHORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_anchors.json")
DEFAULT_OUT = "/tmp/hutao_img.png"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scene.imagery import describe, is_valid_image          # noqa: E402


def enrich_prompt(prompt: str) -> tuple:
    """专有名词锚定：把『胡桃』『璃月港』等 IP 名词补充为精确视觉特征。

    生图模型不认识 IP 名词，只会按字面拼通用元素（如"赤墙黄瓦的海边建筑""红发少女"）。
    这里检测关键词并追加锚定描述，让画面真正指向目标角色/场景。

    返回 (增强后 prompt, 命中的锚点列表)
    """
    if not os.path.exists(ANCHORS):
        return prompt, []
    try:
        cfg = json.load(open(ANCHORS, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prompt, []

    parts = [prompt.rstrip("，,。 ")]
    hits = []

    def scan(group: str, label: str):
        for name, item in (cfg.get(group) or {}).items():
            if name.startswith("_"):
                continue
            for alias in item.get("aliases", []):
                if alias in prompt:
                    parts.append(item["descriptors"])
                    hits.append(f"{label}:{name}")
                    break

    scan("characters", "角色")
    scan("locations", "场景")
    scan("elements", "元素")

    style = cfg.get("style_suffix")
    if style:
        parts.append(style)

    merged = "，".join(p for p in parts if p)
    limit = cfg.get("max_prompt_chars", 480)
    if len(merged) > limit:
        merged = merged[:limit]
    return merged, hits


def _curl_json(url: str, headers: dict, body: dict | None = None, timeout: int = 30) -> dict:
    """用 curl 发请求（走系统证书链，避免 python.org 版 Python 的 CA 缺失问题）。"""
    cmd = ["curl", "-s", "--max-time", str(timeout), url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        cmd += ["-X", "POST", "-d", json.dumps(body, ensure_ascii=False)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out[:300]}


def _curl_download(url: str, path: str, timeout: int = 60) -> bool:
    """下载图片并**校验内容**。

    坑：curl 不加 `-f` 时 4xx/5xx 的退出码仍是 0，所以"rc==0 且文件够大"
    完全挡不住 HTTP 错误页。实测拉一个不存在的 wiki 页面会拿到 139KB 的 HTML，
    旧判定返回 True → 打印 IMAGE_OK → 那段 HTML 被当成图片发到微信，
    而云端已按张计费。所以这里三重把关：`-f` 让 curl 自己失败、
    下载到 `.part` 再原子替换、最后用 `is_valid_image()` 校验魔数与结束标记
    （只查魔数会漏掉"下载被截断"——历史上真出过）。
    """
    part = path + ".part"
    rc = subprocess.run(
        ["curl", "-fsSL", "--max-time", str(timeout), "-o", part, url]
    ).returncode
    if rc != 0 or not os.path.exists(part):
        _cleanup(part)
        print(f"[gen_image] 下载失败（curl rc={rc}）: {url[:100]}")
        return False
    if not is_valid_image(part):
        print(f"[gen_image] 下载到的不是完整图片：{describe(part)}")
        _cleanup(part)
        return False
    os.replace(part, path)          # 原子替换：失败时不会留下半个文件
    return True


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="通义万相生图")
    ap.add_argument("prompt")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--size", default="1024*1024")
    ap.add_argument("--timeout", type=int, default=180, help="轮询总超时（秒）")
    ap.add_argument("--raw", action="store_true", help="不做专有名词锚定（直接用原始 prompt）")
    args = ap.parse_args()

    if not os.path.exists(CONFIG):
        print(f"IMAGE_FAIL 缺少配置 {CONFIG}")
        return 2
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    api_key = cfg.get("api_key") or os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("IMAGE_FAIL 未找到 api_key")
        return 2

    final_prompt, hits = (args.prompt, []) if args.raw else enrich_prompt(args.prompt)
    if hits:
        print(f"PROMPT_ENRICHED {','.join(hits)}")

    endpoint = cfg["endpoint"]
    model = cfg.get("model", "wanx2.1-t2i-turbo")
    headers = {"Authorization": f"Bearer {api_key}", "X-DashScope-Async": "enable"}

    try:
        sub = _curl_json(endpoint, headers,
                         {"model": model, "input": {"prompt": final_prompt},
                          "parameters": {"size": args.size, "n": 1}})
    except Exception as e:  # noqa: BLE001
        print(f"IMAGE_FAIL 提交失败: {e}")
        return 3

    task_id = (sub.get("output") or {}).get("task_id")
    if not task_id:
        print(f"IMAGE_FAIL 未取得 task_id: {json.dumps(sub, ensure_ascii=False)[:200]}")
        return 3

    # 轮询任务
    task_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
    poll_headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(4)
        try:
            st = _curl_json(task_url, poll_headers)
        except Exception as e:  # noqa: BLE001
            print(f"IMAGE_FAIL 轮询失败: {e}")
            return 4
        out = st.get("output") or {}
        status = out.get("task_status")
        if status == "SUCCEEDED":
            results = out.get("results") or []
            url = results[0].get("url") if results else None
            if not url:
                print("IMAGE_FAIL 成功但无图片 URL")
                return 4
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            if not _curl_download(url, args.out):
                print("IMAGE_FAIL 图片下载失败")
                return 4
            print(f"IMAGE_OK {args.out}")
            return 0
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            print(f"IMAGE_FAIL 任务{status}: {json.dumps(out, ensure_ascii=False)[:200]}")
            return 4
    print("IMAGE_FAIL 轮询超时")
    return 4


if __name__ == "__main__":
    sys.exit(main())
