"""胡桃 AI 虚拟角色系统 · 场景引擎（Hutao AI Companion · Scene Engine）。

纯逻辑"虚拟场景"：无图形、无 3D 建模，用 Python 实现场景状态机 + 随机事件生成，
把"对角色有影响"的事件按条件反馈给本地 OpenClaw，驱动 AI 虚拟角色主动向用户分享见闻。

包结构：
  state.py    场景状态（时间/天气/地点/好感度/在场 NPC/回执）
  events.py   事件模板加载（JSON/YAML）
  engine.py   主循环：状态演化、NPC 在场、随机事件 + 防沉默
  filters.py  相关性过滤
  decision.py 主动发言决策器
  delivery.py 投递通道（openclaw_pending / sessions / webhook / json_file / console）
  receipts.py 回执回环
"""

__version__ = "1.1.1"
