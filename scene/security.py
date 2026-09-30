"""安全监测：发现意外发送者时告警（不拦截，仅记录）。

设计取向：用户希望保留完整能力（不做硬性限权），因此采用"监测 + 告警"而非"拦截"：
  · 微信渠道出现**已知发送者之外**的新发送者 → 判定为异常访问，写入运行日志（WebUI 可见）并打印告警；
  · 任何新发送者都会在微信 context-tokens 与会话存储中留下痕迹，因此该监测是可靠的。

配置（config.*.json）：
  "security": { "weixin_owner_ids": ["YOUR_WECHAT_OPENID@im.wechat"] }
"""

from __future__ import annotations

import glob
import json
import os
from typing import List, Tuple

# ⚠️ bot 账号 id 会变（重新登录、换账号、目录被清）。原来硬编码一个路径，
# 于是"账号一变监测就永远静默失效、而且没有任何提示"——`unknown` 恒为空，
# 调用方的 `if unknown:` 永不成立，看起来一切正常。所以：
#   1) 按模式找账号文件（取最新改动的那个），找不到再退回已知路径；
#   2) 提供 monitoring_status()，让调用方能区分"没有异常"和"根本没在监测"。
_ACCOUNT_GLOB = os.path.expanduser(
    "~/.openclaw/openclaw-weixin/accounts/*-im-bot.context-tokens.json"
)
WEIXIN_TOKEN_FILE = os.path.expanduser(
    "~/.openclaw/openclaw-weixin/accounts/YOUR_BOT_ACCOUNT.context-tokens.json"
)


def token_file() -> str:
    """当前应读的 token 文件：优先按模式匹配（取最新改动的），否则退回已知路径。"""
    hits = glob.glob(_ACCOUNT_GLOB)
    if hits:
        try:
            return max(hits, key=os.path.getmtime)
        except OSError:
            return hits[0]
    return WEIXIN_TOKEN_FILE


def monitoring_status(owner_ids: List[str]) -> str:
    """监测状态说明：**空串 = 监测正常**；非空 = 实际没在监测（原因）。"""
    if not owner_ids:
        return "白名单为空（配置里没有 weixin_owner_ids）→ 监测未生效"
    path = token_file()
    if not os.path.exists(path):
        return f"token 文件不存在（{path}）→ 监测未生效：读不到发送者名单"
    if not weixin_senders():
        return f"token 文件里没有任何发送者（{os.path.basename(path)}）→ 监测未生效"
    return ""


def weixin_senders() -> List[str]:
    """返回曾给微信 bot 发过消息的发送者 id 列表（context token 的键）。"""
    path = token_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return list(data.keys()) if isinstance(data, dict) else []


def check_weixin_senders(owner_ids: List[str]) -> Tuple[List[str], List[str]]:
    """返回 (已知发送者, 未知/异常发送者)。owner_ids 为空时视为"未配置白名单"，不做判定。

    调用方请配合 `monitoring_status()`：`unknown` 为空**不代表**监测正常，
    也可能是根本没读到名单。
    """
    senders = weixin_senders()
    if not owner_ids:
        return senders, []
    unknown = [s for s in senders if s not in owner_ids]
    return [s for s in senders if s in owner_ids], unknown
