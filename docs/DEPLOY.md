# OpenClaw 侧部署清单（实测配置）

本文件记录把引擎接入本地 OpenClaw 的**全部 OpenClaw 侧配置**（均在真实环境验证）。

## 1. 心跳任务（HEARTBEAT.md）

文件位置：`~/.openclaw/workspace/HEARTBEAT.md`

**⚠️ 关键**：文件**必须以 `<!-- Heartbeat template; ... -->` 注释开头**——这是 OpenClaw 识别心跳文件的标记；
丢失后心跳会被跳过（`reason=empty-heartbeat-file`）。完整模板见 [`HEARTBEAT.example.md`](HEARTBEAT.example.md)。

## 2. 心跳投递配置（openclaw.json）

```json5
{
  agents: {
    defaults: {
      heartbeat: {
        every: "10m",
        target: "openclaw-weixin",                    // 固定投递渠道
        to: "<用户微信ID>@im.wechat",                 // 必须用微信 ID，不能用会话名（如 YOUR_ACCOUNT）
        accountId: "<bot账号ID>",                     // 微信 bot 账号（如 YOUR_BOT_ACCOUNT）
        directPolicy: "allow",
        lightContext: true,                           // 心跳只注入 HEARTBEAT.md（省 token）
        isolatedSession: true,                        // 全新会话（~100K → 2~5K tokens）
      },
    },
    list: [
      { id: "main", heartbeat: { /* 同上 */ } },      // 显式启用
      { id: "venti", heartbeat: { every: "0m" } },    // 不用的 agent 禁用心跳（防无关消息）
    ],
  },
}
```

> 说明：`agents.list[]` 中只要定义了 `heartbeat`，**只有这些 agent 会跑心跳**，
> 所以 main 必须显式配置，venti 等不用的 agent 用 `every: "0m"` 禁用。

## 3. Gateway Hooks（路子三 Webhook）

```json5
{
  hooks: {
    enabled: true,
    token: "<随机生成的secret>",
    path: "/hooks",
    allowedAgentIds: ["main"],
  },
}
```

网关默认监听 `127.0.0.1:18789`。引擎 POST：

```bash
curl -X POST http://127.0.0.1:18789/hooks/agent \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"message":"[璃月港事件] ...（角色指示：...）","name":"璃月见闻","deliver":true,"channel":"openclaw-weixin","to":"<用户微信ID>@im.wechat"}'
```

## 4. 微信渠道约束（平台限制，务必知悉）

- 微信 bot 只能向**最近约 30 分钟内互动过**的用户主动发送（接收方上下文 token 过期即失败，
  `sendMessage ret=-2 prepare failed`）。OpenClaw 自身的 cron/心跳投递同样受限。
- 恢复方式：用户任意发一条消息刷新 token。
- 若需要"完全无人互动也能持续发送"，请使用无此限制的渠道（如 Telegram）。

## 5. 手动触发心跳（测试/调试）

```bash
# 触发立即心跳 —— 文本必须唯一（相同文本会被去重，投递被抑制）
openclaw system event --mode now --text "璃月港巡检-$(date +%H%M%S)"

# 查看最近心跳结果
openclaw system heartbeat last --json
```

## 6. 验证清单

- [ ] `openclaw system heartbeat last` 状态为 `ok`/`sent`（而非 `skipped/empty-heartbeat-file`/`no-target`）
- [ ] 用户微信能收到胡桃的转述消息（无说明性文字、间接引语、邀请回应）
- [ ] `replies.jsonl` 有回复回写（路子一）或会话转录可见（路子二）
- [ ] 不用的 agent（如 venti）不再向用户发消息
