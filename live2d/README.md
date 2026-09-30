# Live2D 桥接（OpenClaw 胡桃 × VTube Studio）

把「胡桃说了什么、什么情绪」翻译成 Live2D 的表情、动作与口型。
完整设计见 [`../docs/Live2D集成方案.md`](../docs/Live2D集成方案.md)。

## 组成

| 文件 | 作用 |
|---|---|
| `bridge.py` | 桥接服务：HTTP 接收文本 → 情绪识别 → 驱动 VTube Studio（WebSocket） |
| `emotions.py` | 情绪识别（本地关键词，零 API 成本）+ 双层情绪模型（瞬时反应 + 持续心情） |
| `mapping.json` | **情绪 → 表情/参数/动作 映射**（换模型主要改这里） |
| `.vts_token.json` | VTS 授权 token（首次授权后自动生成，勿提交） |

## 快速开始

```bash
# 1) 依赖（仅此一个）
pip3 install websockets

# 2) 打开 VTube Studio → 设置 → API → 勾选 Allow Plugin API（默认端口 8001）
#    导入你的 Live2D 模型（Cubism 3/4 的 .moc3 模型）

# 3) 启动桥接（首次会弹授权窗，点"允许"）
python3 live2d/bridge.py
#   → http://127.0.0.1:8630/

# 4) 测试：说一句话，看表情
curl -X POST localhost:8630/say -H 'Content-Type: application/json' \
     -d '{"text":"哎呀旅行者，你终于来啦！本堂主等你好久了～"}'

# 5) 只看情绪不上屏（不连 VTS 联调）
python3 live2d/bridge.py --no-vts
```

## 与场景引擎联动（微信 + Live2D 共存）

```bash
python3 main.py --config config.live2d.json     # 同一条回复：既发微信，也上屏 Live2D
```

链路：`引擎事件 → OpenClaw(胡桃回复) → 微信投递 + POST /say → 表情/动作同步`。
Live2D 未启动时只影响上屏，**不影响微信投递**（失败自动跳过并记录）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/say` | `{"text": "...", "emotion": "joy"(可选), "intensity": 0.8(可选)}` |
| GET | `/state` | 当前情绪、心情分布、VTS 连接状态 |

## 情绪能力

- **14 类情绪**：joy / amused / smug / shy / surprise / annoyed / sad / worried / curious / thinking / sleepy / serious / excited / neutral
- **双层情绪**：瞬时情绪（本条消息，立即反应）+ 持续心情（累积衰减，形成"底色表情"）
- **三层驱动**：表情文件（`.exp3.json`）→ 参数注入（眉/眼/嘴/头/身，**最丰富**）→ 动作热键
- **自动行为**：眨眼、呼吸、待机摇摆、说话时嘴型起伏

## 换模型时改什么

1. 把模型放进 VTS 的模型目录并在 VTS 里加载；
2. 若模型自带表情文件：在 `mapping.json` 的 `expressions` 里填文件名（如 `exp_joy.exp3.json`）；
   没有表情文件也可以——**参数注入层可独立工作**；
3. 若参数 ID 非 Cubism 标准命名：改 `mapping.json` 的 `emotion_params` 键名；
4. 想要身体动作：在 VTS 里给动作绑热键，把热键 ID 填进 `motions`。

> 模型参数清单可在 VTS 里查看，或参考模型的 `.cdi3.json`；映射中不存在的参数会被自动跳过，不会报错。
