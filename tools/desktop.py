#!/usr/bin/env python3
"""桌面控制工具集（macOS）——供 OpenClaw agent 通过 exec 调用。

设计目标：把零散的 AppleScript / CLI 封装成**稳定、可发现、可自检**的接口，
让角色 agent 能"看屏幕 + 操作桌面"，并在权限不足时**明确报告缺哪一项**而不是静默失败。

用法：
  python3 tools/desktop.py info                          # 能力自检（推荐先跑这个）
  python3 tools/desktop.py screenshot [--path P]         # 截屏（默认 /tmp/dsh_shot.png）
  python3 tools/desktop.py apps                          # 可见应用列表
  python3 tools/desktop.py activate "<App>"              # 激活（切到前台）
  python3 tools/desktop.py open <路径|URL>                # 用默认程序打开
  python3 tools/desktop.py windows ["<App>"]             # 窗口列表（需辅助功能）
  python3 tools/desktop.py click <x> <y>                 # 坐标点击（需辅助功能）
  python3 tools/desktop.py type "<文本>"                  # 输入文本（需辅助功能）
  python3 tools/desktop.py key <keycode> [--cmd|--shift] # 按键（需辅助功能）
  python3 tools/desktop.py menubar "<App>" "<菜单>" ["<项>"]  # 点菜单（需辅助功能）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

SHOT_DEFAULT = "/tmp/dsh_shot.png"


def run(cmd: list, timeout: int = 20) -> tuple:
    """返回 (returncode, stdout+stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"



def _as_str(s: str) -> str:
    """把任意文本转成**安全的 AppleScript 字符串字面量内容**（含引号，不含外层引号）。

    ⚠️ 原来只有 `type` 做了转义，而且只转了引号、**没转反斜杠**：
      · `a\b`、`C:\\Users\\x` 这类文本 → SYNTAX ERROR(-2741)，直接失败；
      · 而 app 名 / menu 名是**直接插值**的，从屏幕读来的窗口标题可能带引号，
        构造串甚至能拼出可编译的任意 AppleScript（同进程可 `do shell script`）。
    AppleScript 里反斜杠本身是转义符，所以必须**先转反斜杠、再转引号**（顺序不能反）。
    """
    return str(s).replace("\\", "\\\\").replace('"', '\\"')

def osa(script: str, timeout: int = 20) -> tuple:
    return run(["osascript", "-e", script], timeout=timeout)


def _ax_hint(err: str) -> str:
    if "-1728" in err or "辅助访问" in err or "not allowed assistive" in err.lower():
        return "（缺『辅助功能』权限：系统设置 → 隐私与安全性 → 辅助功能，勾选运行本命令的程序）"
    return ""


# ---------------- 能力自检 ----------------

def cmd_info(_args) -> int:
    print("=== 桌面控制能力自检 (macOS) ===")

    # 1) 截屏
    tmp = "/tmp/_dsh_probe.png"
    rc, out = run(["screencapture", "-x", tmp])
    shot_ok = rc == 0 and os.path.exists(tmp)
    size = ""
    if shot_ok:
        import struct
        try:
            head = open(tmp, "rb").read(33)
            w, h = struct.unpack(">II", head[16:24])
            size = f"{w}x{h}"
            if w < 200 or h < 200:
                shot_ok = False
                size += "（疑似被 TCC 拦截）"
        except Exception:
            pass
        os.remove(tmp)

    # 2) AppleScript 读（枚举可见应用）
    rc2, out2 = osa('tell application "System Events" to get name of every process whose visible is true')
    apps_ok = rc2 == 0 and bool(out2)

    # 3) 辅助功能（窗口/点击/键盘）
    rc3, out3 = osa('tell application "System Events" to get name of every window of (first process whose frontmost is true)')
    ax_ok = rc3 == 0 and "-1728" not in out3

    # 4) cliclick（不仅检测存在，还功能验证权限）
    cc_path = subprocess.run(["which", "cliclick"], capture_output=True, text=True).stdout.strip()
    cc_ok = False
    if cc_path:
        rc_cc, out_cc = run([cc_path, "p"])      # 读鼠标位置：只读、无副作用
        cc_ok = rc_cc == 0 and "," in out_cc

    print(f"  {'✅' if shot_ok else '❌'} 截屏（看屏幕）        {size}")
    print(f"  {'✅' if apps_ok else '❌'} 枚举可见应用          {out2[:60] if apps_ok else out2[:60]}")
    print(f"  {'✅' if ax_ok else '❌'} 窗口/点击/键盘（辅助功能）{'' if ax_ok else _ax_hint(out3)}")
    if cc_ok:
        print(f"  ✅ 精确坐标点击(cliclick) {cc_path}")
    elif cc_path:
        print(f"  ⚠️  cliclick 已装但无权限（辅助功能里勾选 cliclick）: {out_cc[:50]}")
    else:
        print("  ⚠️  精确坐标点击 未安装：brew install cliclick（可只给该二进制授权）")
    print()
    if not ax_ok:
        print("  说明：缺辅助功能权限时，click/type/key/windows/menubar 不可用；")
        print("       截屏与应用激活不受影响。授权后无需重启命令，立即生效。")
    return 0


# ---------------- 各能力 ----------------

def cmd_screenshot(args) -> int:
    path = args.path or SHOT_DEFAULT
    rc, out = run(["screencapture", "-x", path])
    if rc != 0 or not os.path.exists(path):
        print(f"❌ 截屏失败: {out}")
        print(_ax_hint(out))
        return 1
    kb = os.path.getsize(path) // 1024
    print(f"✅ 截图已保存: {path}（{kb}KB）")
    return 0


def cmd_apps(_args) -> int:
    rc, out = osa('tell application "System Events" to get name of every process whose visible is true')
    print(out if rc == 0 else f"❌ {out}{_ax_hint(out)}")
    return 0 if rc == 0 else 1


def cmd_activate(args) -> int:
    app = args.app
    rc, out = osa(f'tell application "{_as_str(app)}" to activate')
    if rc != 0:
        print(f"❌ 激活失败: {out}")
        return 1
    time.sleep(args.wait)
    print(f"✅ 已激活: {app}")
    return 0


def cmd_open(args) -> int:
    rc, out = run(["open", args.target])
    print(f"✅ 已打开: {args.target}" if rc == 0 else f"❌ 打开失败: {out}")
    return 0 if rc == 0 else 1


def cmd_windows(args) -> int:
    target = f'process "{args.app}"' if args.app else "(first process whose frontmost is true)"
    rc, out = osa(f'tell application "System Events" to get name of every window of {target}')
    if rc != 0:
        print(f"❌ {out}{_ax_hint(out)}")
        return 1
    print(out)
    return 0


def cmd_click(args) -> int:
    # 优先 cliclick（可只给该二进制授予辅助功能，权限面更窄）
    cc = subprocess.run(["which", "cliclick"], capture_output=True, text=True).stdout.strip()
    if cc:
        rc, out = run([cc, f"c:{args.x},{args.y}"])
    else:
        rc, out = osa(f'tell application "System Events" to click at {{{args.x}, {args.y}}}')
    if rc != 0:
        print(f"❌ 点击失败: {out}{_ax_hint(out)}")
        return 1
    print(f"✅ 已点击 ({args.x}, {args.y})")
    return 0


def cmd_type(args) -> int:
    text = _as_str(args.text)
    rc, out = osa(f'tell application "System Events" to keystroke "{text}"')
    if rc != 0:
        print(f"❌ 输入失败: {out}{_ax_hint(out)}")
        return 1
    print("✅ 已输入文本")
    return 0


def cmd_key(args) -> int:
    mods = []
    if args.cmd:
        mods.append("command down")
    if args.shift:
        mods.append("shift down")
    if args.opt:
        mods.append("option down")
    using = f" using {{{', '.join(mods)}}}" if mods else ""
    rc, out = osa(f'tell application "System Events" to key code {args.keycode}{using}')
    if rc != 0:
        print(f"❌ 按键失败: {out}{_ax_hint(out)}")
        return 1
    print(f"✅ 已发送按键 {args.keycode}{' + 修饰键' if mods else ''}")
    return 0


def cmd_menubar(args) -> int:
    path = [args.menu] + ([args.item] if args.item else [])
    clicks = "".join(f'\n  click menu item "{p}" of menu 1 of ' if i == 0 else "" for i, p in enumerate([]))
    # 逐级点击：menu bar item → menu item
    script = f'tell application "System Events" to tell process "{args.app}"\n'
    script += f'  click menu bar item "{args.menu}" of menu bar 1\n'
    if args.item:
        script += f'  click menu item "{args.item}" of menu 1 of menu bar item "{args.menu}" of menu bar 1\n'
    script += "end tell"
    rc, out = osa(script)
    if rc != 0:
        print(f"❌ 菜单操作失败: {out}{_ax_hint(out)}")
        return 1
    print(f"✅ 已点击菜单: {args.app} → {' → '.join(path)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="桌面控制工具集（macOS）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(func=cmd_info)

    p = sub.add_parser("screenshot"); p.add_argument("--path"); p.set_defaults(func=cmd_screenshot)
    sub.add_parser("apps").set_defaults(func=cmd_apps)

    p = sub.add_parser("activate"); p.add_argument("app"); p.add_argument("--wait", type=float, default=0.8); p.set_defaults(func=cmd_activate)
    p = sub.add_parser("open"); p.add_argument("target"); p.set_defaults(func=cmd_open)
    p = sub.add_parser("windows"); p.add_argument("app", nargs="?"); p.set_defaults(func=cmd_windows)

    p = sub.add_parser("click"); p.add_argument("x", type=int); p.add_argument("y", type=int); p.set_defaults(func=cmd_click)
    p = sub.add_parser("type"); p.add_argument("text"); p.set_defaults(func=cmd_type)
    p = sub.add_parser("key"); p.add_argument("keycode", type=int)
    p.add_argument("--cmd", action="store_true"); p.add_argument("--shift", action="store_true")
    p.add_argument("--opt", action="store_true"); p.set_defaults(func=cmd_key)

    p = sub.add_parser("menubar"); p.add_argument("app"); p.add_argument("menu"); p.add_argument("item", nargs="?"); p.set_defaults(func=cmd_menubar)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
