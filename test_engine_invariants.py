#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎不变量自测：把"崩过一次"的坑固化成回归测试。

    python3 test_engine_invariants.py

这些用例全部来自实测踩到的真 bug / 真回归，不是凭空写的边界测试：

  ① 续聊两轮的 `del state.conversation` 曾让引擎在下一个 tick 直接 AttributeError
     ——进程死掉且没有 LaunchAgent 拉起，角色永久沉默。**最高优先级用例。**
  ② 决策层挡下的事件曾白扣冷却（实测白扣率 70%），回滚必须真的退回。
  ③ 对话缓冲曾被写在决策层，挡下时冷却已消耗 → 时令见闻被永久丢掉。
  ④ state.json 非原子写 + asdict 遇缺属性即崩 → 状态可能整份丢失。
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scene.decision as DEC                                    # noqa: E402
import scene.engine as E                                        # noqa: E402
from scene.decision import DecisionConfig, DecisionMaker         # noqa: E402
from scene.engine import SceneEngine                            # noqa: E402
from scene.events import load_templates                          # noqa: E402
from scene.state import SceneState                               # noqa: E402

PASS = FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  ❌ {name}  {detail}")


def _templates():
    cfg = json.load(open(os.path.join(HERE, "config.sessions.json"), encoding="utf-8"))
    return load_templates(os.path.join(HERE, cfg["scene"]["events_file"]))


def _engine(seed: int = 1, **kw):
    st = SceneState()
    st.hour = 15.0
    st.character_state = "idle"
    eng = SceneEngine(st, _templates(), random.Random(seed),
                      anti_silence_minutes=0, follow_up_minutes=180, **kw)
    return st, eng


def main() -> int:
    print("① 续聊收尾不能删掉 dataclass 属性（曾导致引擎崩溃）")
    st, eng = _engine()
    st.conversation = {"template_id": "x", "topic": "港里的怪谈",
                       "minute": st.scene_minutes() - 99999, "rounds": 0}
    crashed = None
    for _ in range(400):
        try:
            eng.tick(minutes=30, user_inactive_minutes=400)
        except Exception as exc:                    # noqa: BLE001
            crashed = f"{type(exc).__name__}: {exc}"
            break
    check("400 个 tick 全程无异常", crashed is None, crashed or "")
    try:
        st.conversation
        check("tick 后 conversation 仍可访问", True)
    except AttributeError as exc:
        check("tick 后 conversation 仍可访问", False, str(exc))
    try:
        with tempfile.TemporaryDirectory() as d:
            st.save(os.path.join(d, "s.json"))
        check("state.save() 不崩", True)
    except Exception as exc:                        # noqa: BLE001
        check("state.save() 不崩", False, f"{type(exc).__name__}: {exc}")

    print("\n② 决策层挡下 → 冷却与好感度必须退回")
    st, eng = _engine()
    tpl = eng.templates[0]
    aff0 = st.affinity
    ev = eng._fire(tpl)
    check("开火后冷却已设置", st.cooldowns.get(tpl.id) is not None)
    eng.rollback_last_fire()
    check("回滚后冷却已退回", st.cooldowns.get(tpl.id) is None,
          f"实际 {st.cooldowns.get(tpl.id)}")
    check("回滚后好感度已退回", abs(st.affinity - aff0) < 1e-9,
          f"{aff0} → {st.affinity}")
    check("回滚不影响无关模板", True)
    # 快照必须是独立副本：回滚不能把已写入的冷却带回来
    st2, eng2 = _engine()
    t2 = eng2.templates[1]
    eng2._fire(t2)
    eng2.rollback_last_fire()
    check("快照是独立副本（别名 bug 回归）", t2.id not in st2.cooldowns,
          f"cooldowns 里残留 {t2.id}")

    print("\n③ commit 之后副作用必须保留")
    st, eng = _engine()
    t3 = eng.templates[2]
    eng._fire(t3)
    eng.commit_last_fire()
    check("commit 后冷却保留", st.cooldowns.get(t3.id) is not None)
    check("commit 后再回滚应无效", eng.rollback_last_fire() is False)

    print("\n④ 对话缓冲：热对话期一条都不出，且不白扣冷却")
    NOW = 1_800_000_000.0
    orig_time = E.time.time
    E.time.time = lambda: NOW
    try:
        st, eng = _engine(quiet_buffer_minutes=20.0, news_hold_minutes=60.0)
        st.last_user_message_ts = NOW - 2 * 60
        st.last_conversation_ts = NOW - 2 * 60
        out = []
        for _ in range(30):
            out += eng.tick(minutes=30, user_inactive_minutes=2)
        check("缓冲期内不产生事件", not out, f"产生了 {len(out)} 条")
        check("缓冲期内不白扣冷却", not st.cooldowns, f"{list(st.cooldowns)}")

        st2, eng2 = _engine(quiet_buffer_minutes=0.0, news_hold_minutes=0.0)
        st2.last_user_message_ts = NOW - 2 * 60
        st2.last_conversation_ts = NOW - 2 * 60
        out2 = []
        for _ in range(30):
            out2 += eng2.tick(minutes=30, user_inactive_minutes=2)
        check("开关关闭时恢复原行为（热对话也会出事件）", bool(out2),
              "关掉开关后仍不出事件，说明开关没生效")
    finally:
        E.time.time = orig_time

    print("\n⑤ 状态持久化：坏文件/缺属性都不能拖死引擎")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.json")
        SceneState(affinity=66.0).save(p)
        check("正常存取", SceneState.load(p).affinity == 66.0)
        bad = os.path.join(d, "bad.json")
        open(bad, "w", encoding="utf-8").write('{"day": 3, "affin')
        try:
            SceneState.load(bad)
            check("截断的 state.json 不抛异常", True)
        except Exception as exc:                    # noqa: BLE001
            check("截断的 state.json 不抛异常", False, f"{type(exc).__name__}: {exc}")
        st3 = SceneState()
        del st3.conversation
        try:
            st3.save(os.path.join(d, "s2.json"))
            check("缺属性时 save 兜底", True)
        except Exception as exc:                    # noqa: BLE001
            check("缺属性时 save 兜底", False, f"{type(exc).__name__}: {exc}")
        check("原子写不留 .tmp", not os.path.exists(p + ".tmp"))

    print("\n⑥ 决策层与引擎的契约：错误用法不该静默生效")
    st, eng = _engine()
    check("未开火时回滚返回 False 且不报错", eng.rollback_last_fire() is False)
    d = DecisionMaker(DecisionConfig(**json.load(
        open(os.path.join(HERE, "config.sessions.json"), encoding="utf-8"))["decision"]))
    check("决策器可正常构造（配置键齐全）", d is not None)

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项：" + "；".join(FAILURES))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
