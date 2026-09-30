# 胡桃 · AI 虚拟角色系统

> 一个会**主动说话**的 AI 虚拟角色「胡桃」：她有自己的日子、情绪和身体感，会讲璃月港的见闻，
> 还会画画、露脸、上 Live2D、帮你操作电脑。整套系统由六项需求拼成——
> 其中「数字化场景」只是**需求⑥**：一个纯逻辑的场景模拟器（无图形、无 3D 建模），
> 负责生成对她有影响的随机事件，喂给她，让她**主动**跟你分享见闻。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Deps](https://img.shields.io/badge/deps-stdlib%20only-orange)](#安装)

---

## 六项需求一览

| # | 需求 | 落点 |
|---|---|---|
| ① | 心理 / 情感表达 | `persona/`（SOUL/IDENTITY/USER）+ 动作记忆 + 说话去模板化 |
| ② | 用训练过的大模型说话 | OpenClaw agent `main` + 云↔本地双向切换 |
| ③ | 显示图片 | `[[img_gen:…]]` 生图 / `[[img:…]]` 官方图库 |
| ④ | Live2D / 真实感视觉 | `live2d/` 桥接 → VTube Studio |
| ⑤ | 调用本地训练的 OpenClaw | 本地 Ollama（Qwen MoE）+ 键级合并同步 |
| ⑥ | 纯逻辑数字场景 + 随机事件 | `scene/` 引擎（本文的主体内容） |

> 系统全貌、模块职责、数据流、模型路由详见 [`docs/系统总览.md`](docs/系统总览.md)。

---

## 简介

本仓库是「胡桃」这套系统的**代码与配置**。其中需求⑥的场景模拟器是它的心脏：一个**纯逻辑的数字化场景**——
场景状态机（时间/天气/地点/好感度）+ 随机事件生成器，把"对角色有影响"的事件通过三条可插拔投递路线
反馈给本地 [OpenClaw](https://github.com/rubencou/claw)，驱动胡桃**主动**向用户转述璃月港的见闻——
不是一问一答，而是"角色有感知、会自己说话"。

核心特色：

- ⏰ **UTC+8 时间同步**：场景时钟锚定真实北京时间。胡桃作息（07 晨起/08-12 上午工作/12-13 午饭/13-17 下午工作/17-18 晚饭/**18-22 晚间出殡**/22 收尾/深夜休息）、饭点事件（早市/午饭/晚饭）、NPC 在场规律全部与真实时间统一。
- 🎭 **认知反差戏剧机制**：魈知道钟离是岩之七执政、温迪是风神，但胡桃不知道——在她眼里钟离只是"总忘带钱的客卿"、温迪只是"欠酒钱的吟游诗人"，魈才是地位最高者。指令自动注入反差提示。
- 💬 **间接引语转述**：角色以"转述/分享"口吻向用户叙述场景（含邀请回应），而非把对 NPC 说的话原样转给用户。
- 🚀 **三条投递路线（均实测打通）**：① Heartbeat 心跳（pending 文件）；② 会话注入（`openclaw agent --deliver`，**生产主链路**）；③ Webhook（Gateway Hooks）。
- 🔁 **回执回环**：追踪 OpenClaw 处理结果，回执反哺决策器（同类事件冷却期不重复触发）。
- 📊 **运行日志 WebUI**：纯标准库实时面板（事件/投递/回执/场景状态）。
- 💰 **成本优化**：心跳 `lightContext + isolatedSession`（~100K→2~5K tokens）、空文件短路、批量攒批、稳定节流。

> ⚠️ 本项目为《原神》同人学习项目，角色与世界观版权归 [miHoYo/米哈游](https://www.mihoyo.com/) 所有，仅供学习交流。

---

## 架构总览

```
┌──────── 场景引擎（scene/，纯逻辑，需求⑥） ──────┐      ┌──────── OpenClaw（角色脑） ────────┐
│ 场景状态机(UTC+8时间/天气/地点/好感度/在场NPC)   │      │  胡桃生成转述口吻的见闻分享         │
│ 胡桃作息：工作/午饭/晚饭/晚间出殡/深夜休息       │      │                                    │
│   │ tick() 随机事件生成（47+ 模板，饭点/出殡对齐）│      │                                    │
│   ▼                                            │      │                                    │
│ 相关性过滤 → 决策器(勿扰/最小间隔20min/概率门)     │ ───► │  路子一 心跳读 pending.json         │
│   │                                            │      │  路子二 agent --deliver 会话注入     │
│ 投递通道: sessions(主)/pending/webhook/json     │ ───► │  路子三 Gateway Hooks /hooks/agent   │
└───────────────────────────────────────────────┘      └────────────────────────────────────┘
         │
         ▼
  运行日志 logs/harbor.jsonl ──► 日志 WebUI（log_server.py，127.0.0.1:8620）
```

**主动性的主体划分**：*时机判断*（什么时候该说）由引擎决策器负责（勿扰时段/最小间隔/主动度）；
*内容生成*（说什么、什么语气）由 OpenClaw 角色脑负责。

---

## 文档导航

| 想了解… | 读… |
|---|---|
| 系统全貌、六需求映射、数据流、模块/工具/文档索引 | [`docs/系统总览.md`](docs/系统总览.md) |
| 从零部署到跑通（含换机路径调整、常见问题） | [`docs/部署手册.md`](docs/部署手册.md) |
| OpenClaw 侧配置清单（心跳/Gateway/微信约束） | [`docs/DEPLOY.md`](docs/DEPLOY.md) |
| 版本与能力演进、已知取舍 | [`CHANGELOG.md`](CHANGELOG.md) |

---

## 安装

```bash
# 核心功能零第三方依赖（Python 3.10+ 标准库即可）
git clone <repo-url> hutao
cd hutao

# 可选：事件模板 YAML 支持
pip install -r requirements.txt        # 仅 PyYAML
```

---

## 快速开始

```bash
# 1) 演示（UTC+8 同步，跑 60 tick，用户空闲 150 分钟）
python3 main.py --once --ticks 60 --seed 7 --user-idle-min 150

# 2) 常驻运行（默认：json_file 日志 + 控制台，不连 OpenClaw）
python3 main.py

# 3) 对接 OpenClaw —— 三条路线任选
python3 main.py --config config.sessions.json    # 路子二（生产主链路，推荐）
python3 main.py --config config.heartbeat.json   # 路子一（心跳 pending）
python3 main.py --config config.webhook.example.json  # 路子三（Gateway Hooks；先填 token/微信 ID）

# 4) 运行日志 WebUI
python3 log_server.py          # http://127.0.0.1:8620/    模型调用情况: /models
```

---

## 与 OpenClaw 对接（三条路线，全部真实环境实测）

| 路线 | 机制 | 配置 | 说明 |
|---|---|---|---|
| **路子二（主）** | `openclaw agent --agent main --session-key <会话> --message <事件> --deliver` | `config.sessions.json` | 事件驱动实时投递，回复写入会话转录可验证；**推荐生产** |
| **路子一** | 引擎写 `pending.json` → 心跳读取 → 胡桃回复 | `config.heartbeat.json` | 批量攒批，10 分钟周期；需 OpenClaw 心跳配置正确 |
| **路子三** | `POST http://127.0.0.1:18789/hooks/agent`（Bearer 鉴权） | `config.webhook.json`（本库为 `config.webhook.example.json`，含本地 token 不入库） | Gateway Hooks，HTTP 触发 |

**回执回环协议**（配套路子一）：事件带唯一 `id` 写入 pending.json → OpenClaw 处理完清空/标记 →
回复追加到 `replies.jsonl` → 引擎扫描捕获。OpenClaw 侧集成模板见
[`docs/HEARTBEAT.example.md`](docs/HEARTBEAT.example.md)，完整部署清单见
[`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## UTC+8 时间同步（作息表）

| 时段 | 胡桃状态 | 典型事件 |
|---|---|---|
| 00-07 | 睡觉 | （安静，不打扰） |
| 07-08 | 晨起梳洗 | 晨光问候、晨间早市（7-9 点） |
| 08-12 | 上午工作（往生堂营业） | 客卿讲古/赊账、玉衡巡查、群玉阁议事等 |
| 12-13 | 午饭 | 午饭时分（香菱在场、万民堂排队） |
| 13-17 | 下午工作 | 各 NPC 事件（按在场规则） |
| 17-18 | 晚饭 | 晚饭时分（饭香、收工） |
| 18-22 | **晚间工作（含出殡）** | 晚间出殡（街头/码头路线）、月夜、怪谈 |
| 22-24 | 收尾休息 | 夜深道别、夜观星象 |

地点按时段加权：工作→往生堂/街市；午饭→万民堂；晚饭→酒馆；**19-22→街头/码头（出殡路线）**；深夜→院内。

---

## 配置说明

### config（引擎配置）

| 节 | 关键项 | 说明 |
|---|---|---|
| `scene` | `sync_to_utc8` | `true`：场景时间锚定 UTC+8 真实时钟（默认开启） |
| `scene` | `tick_minutes` / `tick_interval_seconds` | 每 tick 场景分钟数 / 真实间隔秒数 |
| `scene` | `anti_silence_minutes` | 防沉默：N 真实分钟无事件 → 强制日常闲聊（同步模式下为真实分钟） |
| `scene` | `follow_up_minutes` | 话题续聊间隔（真实分钟） |
| `decision` | `proactiveness` | `reserved`/`balanced`/`lively`（主动度上限） |
| `decision` | `min_interval_minutes` | **最小发言间隔**（稳定节流，默认 20 分钟，防突发+静默） |
| `decision` | `dnd_hours` / `suppress_after_user_minutes` | 勿扰时段 / 用户刚发消息不插嘴窗口 |
| `delivery` | `channels` | 投递通道顺序（失败自动降级到下一通道） |

### cast.json（角色库，18 名）

每个角色定义：`title_public`（胡桃眼中的身份）、`identity_secret`（真实身份）、
`presence`（在场规则：standard/conditional/rare）、`drama_hint`（认知反差提示，可选）。

### events.json（事件模板，47 个）

触发条件（时间段/天气/地点/好感度档位/在场角色/周期日）+ 概率 + 标题/描述/指令 + 影响与冷却。
带 `"idle": true` 为日常闲聊池（防沉默触发）；带 `"conversational": true` 为抛话题事件（回执后开启多轮续聊）。

---

## 目录结构

```
├── main.py                  # 引擎入口（run_once 演示 / run_loop 常驻 + 自愈守护）
├── log_server.py            # 运行日志 WebUI（纯标准库）
├── test_chain.py            # OpenClaw 真实链路测试工具
├── test_engine_invariants.py / test_delivery_parsing.py / test_output_form.py  # 单元测试
├── config.json              # 默认配置（本地演示）
├── config.sessions.json     # 路子二（生产主链路）
├── config.heartbeat.json    # 路子一（心跳 pending）
├── config.live2d.json       # 微信 + Live2D 双通道
├── config.webhook.example.json  # 路子三（Gateway Hooks，占位符 token）
│   └─ config.webhook.json   # 本地敏感配置（含真实 token，不入库，用 example 自行生成）
├── cast.json                # 角色库（18 名，身份认知反差 + 在场规则）
├── events.json              # 事件模板库（47 个，含作息/饭点/出殡）
├── scene/                   # 核心包（15 个模块）
│   ├── state.py             # 场景状态（UTC+8 同步 / 天气 / 好感度 / 在场 / 回执）
│   ├── engine.py            # 主循环：时间同步、地点加权、NPC 在场、事件生成 + 防沉默
│   ├── events.py            # 事件模板加载（JSON/YAML）
│   ├── filters.py           # 相关性过滤（睡觉不打扰等）
│   ├── decision.py          # 主动发言决策器（勿扰/最小间隔/频率上限/概率门）
│   ├── delivery.py          # 投递通道（sessions/pending/webhook/json_file/console）
│   ├── receipts.py          # 回执回环
│   ├── actions.py           # 动作记忆（身体动作跨回复连贯）
│   ├── memory.py            # 关键词记忆（投递前注入，省上下文）
│   ├── style.py             # 说话风格去模板化（形状记忆）
│   ├── news.py              # 原神官方资讯 → 世界内事件
│   ├── imagery.py           # 图片文件完整性校验
│   ├── security.py          # 意外发送者监测告警（不拦截）
│   └── openclaw_sync.py     # 亲密度 → 云/本模型切换（键级合并）
├── persona/                 # 人设快照（SOUL/IDENTITY/USER/SKILL/MEMORY.example）
├── tools/                   # 20+ 运维/功能脚本（生图、图库、资讯、体检、风格改写…）
├── live2d/                  # Live2D 桥接（表情同步，见 docs/Live2D集成方案.md）
│   ├── bridge.py            # HTTP → 情绪 → VTube Studio
│   ├── emotions.py          # 情绪识别 + 双层情绪模型
│   └── mapping.json         # 情绪 → 表情/参数/动作 映射（换模型改这里）
├── docs/                    # 系统总览 / 部署手册 / 设计方案 / DEPLOY / 专题文档…
├── CHANGELOG.md             # 版本与能力演进
├── SHA256SUMS / MANIFEST.txt # 存档完整性校验
├── logs/                    # 运行日志（自动生成，git 忽略）
├── outbox/                  # json_file 通道落盘（自动生成，git 忽略）
└── state.json               # 场景状态持久化（自动生成，git 忽略）
```

---

## 扩展指南（保持可拓展性）

**加角色**：在 `cast.json` 加成员（presence 规则 + 可选 drama_hint），引擎数据驱动，不改代码。

**加事件**：在 `events.json` 加模板（条件/概率/描述/指令/角色/标签）；
带 `"idle": true` 进日常闲聊池；带 `"conversational": true` 进多轮话题；时段写真实小时即可自动与 UTC+8 对齐。

**加投递通道**：在 `scene/delivery.py` 实现 `def xxx_channel(payload, cfg) -> str`，注册进 `CHANNELS`。

**调节奏**：`decision.min_interval_minutes`（发言间隔）、`scene.tick_interval_seconds`、`scene.anti_silence_minutes`。

**省成本**：投递通道配置里设 `model`（廉价模型）、`session_key` 用 `{user_session}`（与直聊共用一条会话线）、
以及 `memory_limit`（关键词记忆条数）；要极致省 token 可退回 `agent:main:liyue-scene-{date}` 按天轮换（丢连贯性）。

---

## 成本优化（实测：¥3/上午 → ¥0.1~0.2/上午）

1. **共用 / 隔离会话二选一**：当前生产用 `{user_session}` 与直聊共用一条会话线（保连贯性）；
   极致省 token 可退回 `agent:main:liyue-scene-{date}` 按天轮换（约 40K tokens → 1~3K tokens/次，但丢连贯性）；
2. **关键词记忆**：`scene/memory.py` 只保留近期话题关键词（默认 15 条，如「钟离·客卿赊账、香菱·万民堂新菜」），
   每次投递前注入摘要，在不携带完整历史的前提下保持连贯性；
3. **廉价模型**：投递可指定 `--model`（如 `deepseek/deepseek-v4-flash`）；
4. **稳定节流**：`min_interval_minutes`（默认 20 分钟）+ 主动度上限；
5. **心跳降本**：`lightContext + isolatedSession`、空文件短路、批量攒批；不用心跳时可 `every: "0m"` 完全禁用。

---

## 生产环境注意事项

1. **微信渠道约束（重要）**：微信投递依赖用户与机器人**最近有互动**（接收方上下文 token 约 30 分钟过期）。
   超时后微信拒绝主动发送（`sendMessage ret=-2 prepare failed`），恢复方式：用户任意发一条消息刷新。
   这是微信 bot 平台限制（OpenClaw 自身 cron/心跳投递同样受限）；其他渠道（如 Telegram）无此限制。
2. **心跳配置**（openclaw.json）：`agents.defaults.heartbeat` 需设置 `target`/`to`（用户 `@im.wechat` ID）/`accountId`；
   详见 `docs/DEPLOY.md`。
3. **Venti agent**：若存在不用的 agent，请设 `heartbeat: {"every": "0m"}` 禁用，避免它继承全局心跳发无关消息。
4. **手动触发注意**：`openclaw system event --mode now` 使用**唯一文本**触发，相同文本会被去重抑制投递。
5. **敏感配置**：`config.webhook.json` 含本地 Gateway Hooks token，已被 git 忽略；发布请使用 `config.webhook.example.json`。

---

## Live2D 表情同步（可选）

把胡桃接入 Live2D 模型，让见闻分享**上屏并同步表情**（微信投递与 Live2D 并存）：

```bash
pip3 install websockets                 # 依赖
python3 live2d/bridge.py                # 启动桥接（VTube Studio 需开启 Plugin API）
python3 main.py --config config.live2d.json   # 引擎：微信 + Live2D 双通道
```

完整设计（选型、情绪映射表、模型清单、分期实施）见 [`docs/Live2D集成方案.md`](docs/Live2D集成方案.md)，
使用说明见 [`live2d/README.md`](live2d/README.md)。

## 桌面控制（可选）

让角色 agent 能"看见并操作"这台 Mac（截屏 / 应用切换 / 点击 / 输入 / 菜单）：

```bash
python3 tools/desktop.py info          # 能力自检（先跑这个）
python3 tools/desktop.py screenshot    # 截屏
python3 tools/desktop.py apps          # 看当前开着哪些程序
```

配套技能：`~/.openclaw/workspace/skills/desktop-control/SKILL.md`（教 agent 何时用、怎么用、缺权限怎么报告）。
完整研究（官方 3 条路径对比、权限矩阵、坐标换算、安全须知）见 [`docs/桌面控制.md`](docs/桌面控制.md)。

## 角色人设与输出自然化

角色的声音由 OpenClaw 工作区的人设文件承载（仓库 `persona/` 为快照）：

| 文件 | 作用 |
|---|---|
| `SOUL.md` | 说话方式 + **微信发消息规范**（分次讲述、空行分段、长短交错、括号动作神态、emoji 节制、禁 Markdown、禁语音）+ **AI 腔对照表** + 底线 |
| `IDENTITY.md` | 身份、来历、边界（"我不是什么/我是什么"） |
| `USER.md` | 关于旅行者的档案（称呼、时区、偏好、相处方式） |

**多模态输出**：胡桃可在回复里写 `[[img_gen: 画面描述]]`（通义万相生图）或
`[[img: 角色 关键词]]`（本地官方图库取图，100% 保真），每次至多 1 个媒体。
`[[voice: ...]]` **已禁用**——微信收不了音频文件，发出去会报错；万一她写了，
引擎会把那段文字转成普通气泡发出，内容不丢。

**输出形式**：一整段为主，正文超过 `split_if_longer_than`（默认 160 字）
才按**空行**拆成多条（上限 5），**长短交错**地分次发送，
每条之间的间隔按"思考时间 + 字数 ÷ 打字速度 + 随机分心"算，所以**每条都不一样**；
拆分只改形式，`"".join(气泡)` 与原文逐字相同（由 `test_output_form.py` 守）。
`|||` 已彻底取消——SOUL.md 里明确让她别写，引擎侧 `_normalize_separators()` 再兜一层。
完整优化记录见 [`docs/输出自然化.md`](docs/输出自然化.md)。

## 原神实时资讯（版本 / 卡池 / 剧情 / 节日）

她会知道"现在提瓦特发生了什么"：引擎每次启动拉一次官方公告，
把**元游戏资讯翻译成她世界里说得通的事**，并入事件池。

| 现实资讯 | 翻译成她能经历的 |
|---|---|
| 7.1「往冥府的安魂歌」 | 港里、行商之间在传的一件和这四个字有关的怪事 |
| 卡池 UP 薇斯纳(风) | 跑商的回来说港里来了个使风的外乡人 |
| 魔神任务「无神怜爱的雪国」 | 走远路的商队从雪国回来，说那边出了大事 |
| 逐月节（农历八月十五前后） | 璃月自己的节：张灯结彩，堂里的活计换了路数 |

**她绝不会说"版本""卡池""祈愿""原石"**——所有事件都走「传闻」框架
（跑商的说的 / 港里都在传），所以她复述一个只知道姓名和元素的外乡人时
天然不编造性格。数据源是米游社官方公告 API（CN 服，无需鉴权），
缓存 6 小时，断网自动降级回缓存、无缓存就少几条时令事件，**绝不阻断主链路**。

```bash
python3 tools/genshin_news.py --events -v    # 看翻译出的世界内事件与完整指令
```

实测：接入后事件种类 20 → 27，资讯事件占全部事件的 **3.9%**；
三条指令真实投喂，**零元游戏词汇**。完整设计与**测量陷阱**见
[`docs/原神资讯接入.md`](docs/原神资讯接入.md)。

## 对话缓冲（见闻不许打断对话）

长对话中途她不会冷不丁冒一句见闻。两道时间闸，配置都在 `decision` 块：

| 配置 | 默认 | 作用 |
|---|---|---|
| `conversation_buffer_minutes` | 20 | 双方**任一方**最近开口后 N 分钟内，**一条主动事件都不产生**（不看 priority，高优先级同样压） |
| `news_hold_minutes` | 60 | 带 `news` 标签的时令事件，在对话结束后再压这么久 |

判据是**任一方**最后开口的时间——用户说完、她回完，对话仍然是热的。
只看用户发言的话，用户沉默几分钟就被误判成"人走了"，这正是原来会冒见闻的根源。

**闸实现在引擎里而不是决策层**：原链路是"引擎先扣冷却、再交决策层"，
被决策层挡下的事件冷却已经消耗掉了；见闻的冷却是 3~7 天，挡一次就等于
**永久丢掉那条见闻**。所以硬闸必须在开火之前拦。

冷下来后开关开/关结果完全一致——这个功能只影响对话热的时候，其余情况分毫不动。
设计与验证数据见 [`docs/原神资讯接入.md`](docs/原神资讯接入.md) 第七节。

## 许可

[MIT](LICENSE)。角色与世界观版权归 miHoYo/米哈游所有，本项目仅作学习交流用途。
