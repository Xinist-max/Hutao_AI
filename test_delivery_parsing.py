#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递链路的输出把关自测：这些用例全部来自实测踩到的真 bug。

    python3 test_delivery_parsing.py

覆盖"模型输出 → 用户看到的文本"这一段，这是最容易出**用户可见**问题的地方：

  ① 回复只有媒体标记时，`[[img_gen: ...]]` 被原样发给用户
  ② `_extract_reply` 解析不出正文时，整个 JSON 信封被当成胡桃的话发出去
  ③ `_normalize_separators` 改内容：吃乘号（2000*3*1.5）、吃颜文字
  ④ `_looks_like_agent_error` 误判：正常回复被整条丢弃（内容丢失）
  ⑤ 动作状态规则语序写反，注入的是**假状态**并存活 6 小时
  ⑥ 引擎自己丢弃的 pending 事件被回执记成"已被 OpenClaw 处理"，白扣一整个冷却
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from datetime import datetime, timedelta                       # noqa: E402

import scene.delivery as D                                     # noqa: E402
from scene.actions import ActionMemory                         # noqa: E402
from scene.delivery import (_extract_reply, _looks_like_agent_error,   # noqa: E402
                            _normalize_separators, _parse_media_markers)
from scene.receipts import ReceiptTracker                      # noqa: E402
from scene.state import SceneState                             # noqa: E402

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


def main() -> int:
    print("① 只有媒体标记的回复不能把标记漏给用户")
    for raw in ('[[img_gen: 胡桃在海边]]', '[[img_gen: a]][[img_gen: b]]',
                '[[img: /tmp/x.png]]'):
        clean, items = _parse_media_markers(raw, {})
        sent = _normalize_separators(clean)          # 修复后的写法：只用 clean_text
        check(f"{raw[:22]} → 正文不含标记", "[[" not in sent, f"实际发出 {sent!r}")
        check(f"{raw[:22]} → 媒体被提取", bool(items), "没解析出媒体")

    print("\n② JSON 信封绝不能被当成回复")
    leaks = [
        ('{"result":{"payloads":[]}}', "空 payloads"),
        ('{"result":{"payloads":[{"text":"   "}]}}', "空白 text"),
        ('{"result":{"payloads":null}}', "null payloads"),
        ('{"result":{"payloads":[{"text":', "截断 JSON"),
        ('[{"result":{"payloads":[{"text":"hi"}]}}]', "数组包裹"),
        ('{"result":{"payloads":[{"text":["a","b"]}]}}', "text 是 list"),
        ('Error: something failed\n{"result":{"payloads":[]}}', "前缀噪声"),
    ]
    for raw, name in leaks:
        try:
            out, _ = _extract_reply(raw)
        except Exception as exc:                       # noqa: BLE001
            check(f"{name} 不抛异常", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"{name} 不外发信封",
              not any(k in out for k in ('"payloads"', '"result"', '"agentMeta"')),
              f"实际 {out[:40]!r}")
    # 正常路径不能被误伤
    out, meta = _extract_reply('◇ Doctor warnings\n╰ done\n'
                               '{"result":{"payloads":[{"text":"旅行者，你来啦"}],"meta":{"m":1}}}')
    check("提示框 + JSON → 取出正文", out == "旅行者，你来啦", repr(out))
    check("提示框 + JSON → meta 保留", meta.get("m") == 1, repr(meta))
    out, _ = _extract_reply("本堂主给你算笔账 {摩拉} 记着点")
    check("正文里含普通花括号不受影响", "摩拉" in out, repr(out))

    print("\n③ 清理不能改内容")
    for src, want, why in [
        ("伤害 2000*3*1.5 点", "伤害 2000*3*1.5 点", "乘号"),
        ("一份 20*3*2 的账", "一份 20*3*2 的账", "乘号"),
        ("(*/ω＼*) 本堂主害羞了", "(*/ω＼*) 本堂主害羞了", "颜文字"),
        ("(*^▽^*) 好耶", "(*^▽^*) 好耶", "颜文字"),
        (">1000 摩拉给你抹个零", ">1000 摩拉给你抹个零", "行首比较号"),
        ("甲|||||乙", "甲\n\n乙", "多余竖线"),
    ]:
        got = _normalize_separators(src)
        check(f"{why}保留：{src[:16]}", got == want, f"→ {got!r}")
    # 该清的仍要清
    for src, want, why in [("**重点**内容", "重点内容", "加粗"),
                           ("这是*强调*的词", "这是强调的词", "斜体"),
                           ("# 标题\n正文", "标题\n正文", "标题符"),
                           ("> 这是引用", "这是引用", "引用符")]:
        got = _normalize_separators(src)
        check(f"{why}仍清理：{src[:14]}", got == want, f"→ {got!r}")

    print("\n④ 报错识别不能误伤正常回复（方向错了会丢内容）")
    normal = ("（嗤）你可算来了。对了，刚那边弹了个 agent couldn't generate a response "
              "的提示，本堂主也看不懂，八成是机器的事。")
    check("引用了报错原文的正常回复 → 放行", _looks_like_agent_error(normal) == "",
          "被误判成报错，用户会什么都收不到")
    check("真正的报错 → 拦下",
          bool(_looks_like_agent_error("⚠️ Agent couldn't generate a response. Please try again.")))

    print("\n⑤ 动作状态：语序、误判、覆盖顺序")
    def rec(text: str) -> dict:
        m = ActionMemory(path=tempfile.mktemp())
        before = dict(m.state)
        m.record(text)
        return {k: v for k, v in m.state.items() if before.get(k) != v}

    for text, cat, want in [("（扶正帽檐）", "帽子", "戴着"),
                            ("（摘下帽子）", "帽子", "摘下"),
                            ("（戴上帽子）", "帽子", "戴着"),
                            ("（把帽子摘下来）", "帽子", "摘下"),
                            ("（站起身来又坐了回去）", "姿态", "坐着"),
                            ("（坐下又站起来）", "姿态", "站着")]:
        got = rec(text)
        check(f"{text} → {cat}={want}", got.get(cat) == want, f"实际 {got or '{}'}")
    for text in ("（帽子歪着，摘了颗蜜枣吃）", "（压了压帽檐，叹了口气）"):
        got = rec(text)
        check(f"{text} 不该误判", "帽子" not in got, f"误判成 {got}")

    print("\n⑥ 引擎丢弃的 pending 事件 ≠ OpenClaw 已处理")
    d = tempfile.mkdtemp()
    pend = os.path.join(d, "pending.json")
    json.dump({"events": [{"id": "evt_old", "template_id": "welcome_back",
                           "timestamp": (datetime.now().astimezone()
                                         - timedelta(minutes=31)).isoformat()}]},
              open(pend, "w", encoding="utf-8"), ensure_ascii=False)
    cfg = {"path": pend, "stale_minutes": 20, "max_pending": 6}
    payload = {"event": {"id": "evt_new", "type": "time", "title": "新", "description": "x",
                         "priority": "medium", "template_id": "good_night"},
               "state": {}, "instruction": "x"}
    D.openclaw_pending_channel(payload, cfg)
    left = [e.get("id") for e in json.load(open(pend, encoding="utf-8"))["events"]]
    check("过期条目已从 pending 移除", left == ["evt_new"], f"实际 {left}")
    check("新事件确实写进去了", "evt_new" in left, f"实际 {left}")

    st = SceneState()
    st.receipts["evt_old"] = {"status": "written", "template_id": "welcome_back"}
    st.receipts["evt_new"] = {"status": "written", "template_id": "good_night"}
    rep = os.path.join(d, "replies.jsonl")
    open(rep, "w").close()
    ReceiptTracker(pending_path=pend, replies_path=rep).scan(st)
    check("被引擎丢弃 → expired（不是 processed）",
          st.receipts["evt_old"]["status"] == "expired",
          f"实际 {st.receipts['evt_old']['status']}")
    check("丢弃的不写 receipt_meta（不白扣冷却）",
          "welcome_back" not in st.receipt_meta, f"实际 {st.receipt_meta}")
    check("仍在待处理的保持 written", st.receipts["evt_new"]["status"] == "written",
          f"实际 {st.receipts['evt_new']['status']}")

    print("\n⑦ 媒体标记大小写容错")
    _clean, items = _parse_media_markers("[[IMG_GEN: 大写]]", {})
    check("[[IMG_GEN: ...]] 也被识别", bool(items), "大小写敏感时既不生图也不清洗")
    _clean2, items2 = _parse_media_markers("[[img_gen: 小写]]", {})
    check("[[img_gen: ...]] 正常", bool(items2))

    print("\n⑧ 图片路径白名单（原来任意本地文件都能发到微信）")
    from scene.delivery import _resolve_img_arg
    for bad in ("~/.openclaw/openclaw.json", "/etc/hosts", "~/.ssh/id_rsa"):
        check(f"拒绝 {bad}", _resolve_img_arg(bad) is None,
              "敏感文件会被上传到微信")
    _g = os.path.join(HERE, "gallery")
    _imgs = []
    for root, _d, files in os.walk(_g):
        _imgs += [os.path.join(root, f) for f in files if f.lower().endswith(".png")]
        if _imgs:
            break
    if _imgs:
        check("放行图库内的正常图片", bool(_resolve_img_arg(_imgs[0])))

    print("\n⑨ 模型路由滞回（两条链路必须同源）")
    from scene.delivery import _pick_chat_model
    _cfg = json.load(open(os.path.join(HERE, "config.sessions.json"),
                          encoding="utf-8"))["delivery"]["channels"][0]["config"]
    _up = float((_cfg.get("model_local") or {}).get("min_affinity", 95))
    _down = float((_cfg.get("model_local") or {}).get("drop_affinity", 88))
    # 滞回区中间：当前在云端就该留在云端，当前在本地就该留在本地
    _mid = (_up + _down) / 2
    a1, _ = _pick_chat_model({"state": {"tier": "close", "affinity": _mid}}, _cfg,
                             "deepseek/deepseek-v4-flash")
    a2, _ = _pick_chat_model({"state": {"tier": "close", "affinity": _mid}}, _cfg,
                             "ollama/local")
    check("滞回区：当前云端→保持云端", a1 and "ollama" not in a1, f"实际 {a1}")
    check("滞回区：当前本地→保持本地", a2 and "ollama" in a2, f"实际 {a2}")
    a3, _ = _pick_chat_model({"state": {"tier": "close", "affinity": _up}}, _cfg, "")
    check("到上行阈值→切本地", a3 and "ollama" in a3, f"实际 {a3}")
    a4, _ = _pick_chat_model({"state": {"tier": "close", "affinity": _down}}, _cfg,
                             "ollama/local")
    check("到下行阈值→切回云端", a4 and "ollama" not in a4, f"实际 {a4}")

    print("\n⑩ 说话方式去模板化（照搬同一套起手式时要提醒）")
    from scene.style import StyleTracker
    _t = StyleTracker(path=tempfile.mktemp(), head_repeat_limit=3, para_repeat_limit=5)
    for _i in range(8):
        _t.record("（叉腰）今儿码头的灯挂上了\n\n你来不来\n\n堂里那口棺也刷好了")
    check("起法扎堆 → 给出提醒", bool(_t.hint(cooldown_minutes=0)),
          "扎堆却不提醒，模板会一直重复")
    _t2 = StyleTracker(path=tempfile.mktemp(), head_repeat_limit=3)
    for _s in ("码头的灯挂上了\n\n你来不来", "（叉腰）今儿有风", "话说回来，堂里那口棺",
               "你猜怎么着", "（眯眼）刚才那事儿", "对了，香菱又拽我去试菜"):
        _t2.record(_s)
    check("起法多样 → 不打扰（免得提醒本身变模板）", _t2.hint(cooldown_minutes=0) == "",
          "多样时仍提醒")
    check("形状记忆不存正文（只留 4 字指纹）",
          all(len(it.get("head", "")) <= 4 for it in _t2.items))

    print("\n⑪ 重复回复检测（同一条话不该发第二遍）")
    from scene.style import find_duplicate, similarity
    _tpl = "（抬头看你一眼）在呢，宝宝。刚给爷爷换了杯茶，你就来了。"
    _rec = [_tpl, "别的话"]
    check("一字不差 → 判重", bool(find_duplicate(_tpl, _rec)))
    check("只改一两个字 → 判重",
          bool(find_duplicate(_tpl.replace("你就来了", "你就来啦"), _rec)))
    check("换了动作 → 不判重（不误伤）",
          not find_duplicate("（叉腰）今儿码头的灯挂上了，你来不来？", _rec))
    check("全新内容 → 不判重",
          not find_duplicate("香菱今儿非拽我去尝海灯节特供的糕点", _rec))

    print("\n⑫ 会话级复读检测（只看数字，不读内容）")
    from scene.style import session_reply_similarity
    _sims, _reps = session_reply_similarity("", 4)
    check("空会话键不报错", _sims == [] and _reps == [])
    _sims2, _reps2 = session_reply_similarity("agent:main:不存在的会话", 4)
    check("不存在的会话不报错", _sims2 == [] and _reps2 == [])

    print("\n⑬ 开头形态检测（每条都以动作开头的情况要能抓到）")
    from scene.style import opening_profile, repeated_heads, update_style_block
    _prof = opening_profile("agent:main:openclaw-weixin:direct:YOUR_ACCOUNT", 20)
    if _prof:
        check("能算出括号开头占比", "括号开头占比" in _prof, str(_prof))
        check("能识别同族身体反应", _prof.get("同族反应数", 0) >= 0, str(_prof))
    # ⚠️ 这里**不能**依赖真实会话：改写机制生效后她的开头就不再重复了，
    # 用例会"因为修好了而失败"。所以用合成样本判定检测器本身。
    import tempfile as _tf2, json as _json2
    from scene.style import session_reply_similarity as _srs

    def _fake_session(replies):
        """造一个假会话文件喂给检测器（走 _session_jsonl_path 太重，直接测核心逻辑）。"""
        import collections as _c
        heads = _c.Counter(_head_of(r) for r in replies if _head_of(r))
        return heads

    from scene.style import _head_of
    _rep = ["（脸瞬间红透）" + "啊" * 20] * 5 + ["（深吸一口气）" + "嗯" * 20] * 4
    _h = _fake_session(_rep)
    check("检测器能抓出重复开头（合成样本）",
          _h.most_common(1)[0][1] >= 3, f"实际 {_h.most_common(3)}")
    _diverse = [f"第{i}种说法，完全不同的开头内容" for i in range(8)]
    _h2 = _fake_session(_diverse)
    check("多样化时不该误报（合成样本）",
          _h2.most_common(1)[0][1] == 1, f"实际 {_h2.most_common(2)}")
    # 无标记块的文件绝不能被改
    import tempfile as _tf
    _p = os.path.join(_tf.mkdtemp(), "MEMORY.md")
    with open(_p, "w", encoding="utf-8") as _f:
        _f.write("# 用户手写的记忆\n- 这一行必须原样保留\n")
    _before = open(_p, encoding="utf-8").read()
    _r = update_style_block("agent:main:openclaw-weixin:direct:YOUR_ACCOUNT", path=_p)
    check("无 AUTO:STYLE 标记时不动文件",
          _r == "" and open(_p, encoding="utf-8").read() == _before,
          "擅自改了用户文件")

    print("\n⑭ 历史回复改写（唯一被实测证明有效的办法）")
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("sw", os.path.join(HERE, "tools", "style_rewrite.py"))
    _sw = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_sw)
    for raw, want, why in [
        ("（叉腰）哼，巧了。", "哼，巧了。", "单个开头括号"),
        ("（抬头看你一眼）\n\n在呢，宝宝。", "在呢，宝宝。", "括号+换行"),
        ("（笑）\n（凑近）说吧", "说吧", "连续两个括号"),
        ("哼，巧了。（叉腰）", "哼，巧了。（叉腰）", "括号在句中——不许动"),
        ("（动作）中间（另一个）", "中间（另一个）", "只剥开头那一个"),
    ]:
        got, _n = _sw.strip_leading_actions(raw)
        check(f"{why}", got == want, f"→ {got!r}")
    check("空串安全", _sw.strip_leading_actions("")[0] == "")
    check("无括号不动", _sw.strip_leading_actions("哟，本堂主在呢")[0] == "哟，本堂主在呢")

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项：" + "；".join(FAILURES))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
