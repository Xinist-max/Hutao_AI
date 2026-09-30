# HEARTBEAT.md 集成模板（OpenClaw 侧）

> ⚠️ **关键**：文件必须以 `<!-- Heartbeat template; ... -->` 注释开头——这是 OpenClaw 识别心跳文件的标记。
> 丢失该标记后心跳会被跳过（`reason=empty-heartbeat-file`）。

将以下内容写入 OpenClaw 工作区 `HEARTBEAT.md`（通常位于 `~/.openclaw/workspace/HEARTBEAT.md`）：

```markdown
<!-- Heartbeat template; comments-only content prevents scheduled heartbeat API calls. -->

# 璃月港 · 胡桃巡逻与主动会话任务

1. 检查 `memory/events/pending.json` 的 `events` 数组。
   **如果数组为空：回复 HEARTBEAT_OK 并立即结束，不要发送任何消息、不要执行任何其他操作。**（空闲心跳零开销）
2. 如果有事件：以胡桃的身份**主动发起会话**——用"分享 + 邀请回应"的口吻向用户转述璃月港见闻，
   例如：「刚才钟离先生又在绯云坡赊账啦，你猜他这次打算怎么抵账？」，
   让用户愿意接话，而不是单方面播报。直接发送消息本身，不要附加任何说明性文字
   （如「检测到新事件」「系统通知」之类）。
3. 发送后【必须】完成以下两步，否则事件会被重复处理、用户会收到重复消息：

   a. 把 `memory/events/pending.json` 整个文件内容替换为：

   ```json
   {"events": []}
   ```

   b. 把胡桃的回复追加到 `memory/events/replies.jsonl`（文件不存在就创建），每行一个 JSON：

   ```json
   {"event_id": "刚刚处理的事件的id", "reply": "胡桃发出的消息", "emotion": "joy", "timestamp": "2026-08-31T16:30:00+08:00"}
   ```

   `event_id` 必须是所处理事件的 `id` 字段值（形如 evt_1788165173868587）。
```

## 文件协议（引擎 ⇄ OpenClaw）

| 文件 | 写入方 | 说明 |
|---|---|---|
| `memory/events/pending.json` | 引擎 | `{"events": [{id, type, title, content, timestamp, priority}]}`，心跳读取后须清空或标记 `handled: true` |
| `memory/events/replies.jsonl` | OpenClaw | 每行一个 JSON：`{"event_id", "reply", "emotion", "timestamp"}`，引擎回执回环据此捕获角色回应 |

## 成本建议

- 心跳周期建议 10~30 分钟：事件在 pending 攒批，一次心跳批量处理多条；
- 空文件短路（第 1 条）保证空闲心跳不做任何事；
- openclaw.json 中开启 `lightContext + isolatedSession`，单次心跳 token 从 ~100K 降到 2~5K；
- 引擎侧另有最小发言间隔（`min_interval_minutes`）与主动度上限限流。

完整 OpenClaw 侧部署见 [`DEPLOY.md`](DEPLOY.md)。
