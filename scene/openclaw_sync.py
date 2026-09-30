#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按亲密度同步 OpenClaw agent 的对话模型（键级合并，绝不整数组重写）。

## 为什么要改配置文件

引擎发主动消息时可以 `openclaw agent --model X` 直接指定模型，但**用户直接跟胡桃聊天**
走的是 OpenClaw 自己的 agent 默认链——引擎插不上手，只能改配置。

## 为什么必须键级合并

`~/.openclaw/openclaw.json` 的 `agents.list` 是**数组**。另一个工作区（LocalAI）的补丁
是整体重写这个数组的，两边一起改就会互相静默抹掉对方的键。
归属与规则见 `~/Documents/DSH/_shared/OPENCLAW-OWNERSHIP.md`。
所以这里只做一件事：**读现有数组 → 按 id 找到目标项 → 只改 model 这一个键 → 写回**，
其余 agent、其余键原样保留。

写回用「临时文件 + 原子替换」，并先留一份时间戳备份。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

DEFAULT_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")


def _dig_agent(cfg: dict, agent_id: str) -> dict:
    for a in (cfg.get("agents", {}).get("list") or []):
        if a.get("id") == agent_id:
            return a
    return {}


def current_model(agent_id: str = "main", config_path: str = DEFAULT_CONFIG) -> str:
    """读 agent **实际生效**的 primary 模型。

    注意要回落到 `agents.defaults.model.primary`：agent 上的 model 为空时它继承全局默认。
    只看字面值会误判成"(空)"，于是每次巡检都白写一次配置，还把继承关系钉成显式覆盖。
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    own = ((_dig_agent(cfg, agent_id).get("model") or {}).get("primary")) or ""
    if own:
        return own
    default = ((cfg.get("agents", {}).get("defaults", {}) or {}).get("model") or {})
    return default.get("primary") or ""


def sync_agent_model(agent_id: str, primary: str,
                     config_path: str = DEFAULT_CONFIG) -> tuple:
    """把 `agents.list[<id>].model.primary` 设成 primary。

    返回 (是否改动, 说明)。**生效值**已经是目标值就不写盘——避免每次巡检都动配置文件。
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"读配置失败：{type(e).__name__}"

    agents = (cfg.get("agents", {}) or {}).get("list")
    if not isinstance(agents, list):
        return False, "配置里没有 agents.list，放弃改动"
    target = next((a for a in agents if a.get("id") == agent_id), None)
    if target is None:
        return False, f"agents.list 里没有 {agent_id}，放弃改动"

    default = ((cfg.get("agents", {}).get("defaults", {}) or {}).get("model") or {})
    own = ((target.get("model") or {}).get("primary")) or ""
    effective = own or (default.get("primary") or "")
    if effective == primary:
        return False, f"生效值已是 {primary}" + ("（继承默认）" if not own else "")

    # 只动这一个键：其余 agent、其余字段原样保留
    target.setdefault("model", {})
    if not isinstance(target["model"], dict):
        return False, f"{agent_id}.model 不是对象，放弃改动"
    target["model"]["primary"] = primary

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{config_path}.pre-dsh-{stamp}"
    n = 1
    while os.path.exists(backup):        # 同一秒内多次改动不能互相覆盖备份
        backup = f"{config_path}.pre-dsh-{stamp}-{n}"
        n += 1
    try:
        shutil.copy2(config_path, backup)
        d = os.path.dirname(config_path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".openclaw-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, config_path)          # 原子替换，避免写一半被读到
    except OSError as e:
        return False, f"写配置失败：{type(e).__name__}"
    return True, f"{agent_id}.model.primary: {effective or "（空）"} → {primary}（备份 {os.path.basename(backup)}）"
