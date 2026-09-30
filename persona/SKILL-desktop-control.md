---
name: desktop-control
description: 控制 macOS 桌面（截屏、看应用、切前台、点击、输入、按键、点菜单）。用户要求"帮我操作电脑/打开某个 App/点一下/输一段字"时使用。
---

# 桌面控制（macOS）

工具集位置：`<HUTAO>/tools/desktop.py`

## 何时使用

用户要求**操作这台 Mac** 时（不是问知识、不是聊天）：
- "看一下我屏幕上有什么" / "截个图"
- "帮我打开/切到某个 App"
- "点一下某个位置" / "帮我输入这段话" / "按一下回车"
- "帮我点某个菜单"

## 使用前必做：能力自检

```bash
python3 <HUTAO>/tools/desktop.py info
```

它会报告四项能力状态（截屏 / 应用枚举 / 辅助功能 / cliclick）。
**若某项不可用，直接如实告诉用户缺什么权限，不要反复重试。**

## 可用命令

```bash
python3 .../tools/desktop.py screenshot [--path /tmp/shot.png]   # 截屏（默认 /tmp/dsh_shot.png）
python3 .../tools/desktop.py apps                                # 可见应用列表
python3 .../tools/desktop.py activate "WeChat"                   # 切到前台
python3 .../tools/desktop.py open "/Users/xxx/文件.pdf"           # 用默认程序打开
python3 .../tools/desktop.py windows "WeChat"                    # 窗口列表
python3 .../tools/desktop.py click 640 400                       # 坐标点击
python3 .../tools/desktop.py type "你好"                          # 输入文本
python3 .../tools/desktop.py key 36                              # 回车（keycode 36）
python3 .../tools/desktop.py key 36 --cmd                        # ⌘+回车
python3 .../tools/desktop.py menubar "Google Chrome" "文件" "新建标签页"
```

常用 keycode：`36` 回车、`53` Esc、`48` Tab、`51` Delete、`123/124/125/126` 方向键。

## 看懂屏幕内容（视觉读屏）

截图只是文件；**要理解屏幕内容，必须再用 `image` 工具分析它**（当前主模型 kimi-k2.6 支持图像输入）：

```
1) exec:  python3 <HUTAO>/tools/desktop.py screenshot
2) image: image=/tmp/dsh_shot.png，prompt="描述这张屏幕上显示的内容"
   （若是针对具体问题看屏，prompt 写具体些，如"这个报错是什么原因"）
```

用途举例：用户说"看看我这个报错什么意思""帮我看看现在什么情况""屏幕上那个窗口写了啥"。
读完后用**自己的口吻**转述关键信息，别念技术流水账。

## 工作方式（重要）

1. **先看再动**：需要操作界面时，先 `activate` 目标应用，再 `screenshot` 看清当前状态；
2. **坐标要基于截图**：截图是 Retina 全分辨率（如 3840×2486），而点击坐标使用**逻辑点**
   （通常为分辨率的一半，如 1920×1243）——换算：`点 = 像素 ÷ 缩放比`；
3. **一步一步来**：每个动作后确认结果（再次截图或 `windows` 查状态），不要连发一串盲操作；
4. **危险操作先问**：删除文件、发送消息、支付、关机等**必须先向用户确认**，得到同意再执行；
5. **用完归还**：操作完成后把前台切回用户原本的应用（可用 `apps` 回忆）。

## 权限对照（缺失时这样告诉用户）

| 能力 | 需要的权限 | 在哪开 |
|---|---|---|
| 截屏 / 看屏幕 | 屏幕录制 | 系统设置 → 隐私与安全性 → 屏幕录制 |
| 点击 / 输入 / 按键 / 窗口 / 菜单 | 辅助功能 | 系统设置 → 隐私与安全性 → 辅助功能 |
| 控制其他 App（AppleScript） | 自动化 | 首次调用会弹窗，点"允许" |
| 精确坐标点击 | 安装 `cliclick` | `brew install cliclick`（可只给该程序授权，权限面更窄） |

## 汇报风格

用户是让你**办事**时，用简短口语汇报结果（如"打开啦，Chrome 已经切到前台"），
不要长篇解释技术细节；只在**失败或缺权限**时说明原因和下一步。
