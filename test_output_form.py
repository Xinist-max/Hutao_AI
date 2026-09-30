#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输出形式的不变量测试（纯函数，不联网、不发消息）。

    python3 test_output_form.py

守的是两条底线：
  1. **内容不变**：分气泡只是形式优化，`"".join(bubbles)` 必须与原文逐字相同
     （去掉 `|||` 分隔符与首尾空白）——一个字都不能丢、不能改。
  2. **形式像人**：气泡长短交错、间隔有快有慢、不发「多条中等长度且整齐」的 AI 形态；
     语音标记默认被禁用且内容转成文字。
"""

from __future__ import annotations

import random
import re
import sys

from scene import delivery as dv

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{('  — ' + detail) if detail else ''}")


def _skeleton(text: str) -> str:
    """去掉 `|||` 分隔符与所有空白后的骨架，用于比对内容是否原样保留。"""
    return re.sub(r"\s+", "", text.replace("|||", ""))


# ── 语料：覆盖真人会出现的各种形态 ─────────────────────────────────────────
CORPUS = [
    "雨停了。",
    "今天码头挂灯啦，我想去看看。你要不要一起？",
    "诶你猜怎么着|||今天往生堂来了个老先生，一进门就说要给自己订口好棺木，"
    "还认真挑了半个时辰木料，最后问我能不能分期|||你说他是不是把本堂主当钱庄了😂",
    # 没分段的长叙事（最考验"能不能自己讲成几条"）
    "我跟你说啊，今天往生堂来了个老先生，一进门就要给自己订口好棺木，"
    "蹲在木料堆里挑了半个时辰，敲敲打打比本堂主还认真。结果末了问我能不能分期付款，"
    "本堂主差点没绷住，这年头连身后事都流行分期付款啦？你说他是不是把往生堂当钱庄了😂",
    # 多条中等长度且整齐（AI 形态，应被合并）
    "甲" * 30 + "|||" + "乙" * 30 + "|||" + "丙" * 30,
    # 短促连发（真人常态，应保持）
    "hhhh|||绝了|||你说呢",
    # 长短交错（应原样保留条数）
    "诶你猜怎么着|||钟离先生端着茶从旁边一过，魈大圣竟然低了头|||稀奇不稀奇😏",
    # 无标点超长文（硬切也不能丢字）
    "甲" * 300,
    # 只有空白
    "   ",
    # emoji 与半角标点混排
    "嘿嘿,今天赚啦!|||三笔生意呢~|||够本堂主吃一个月糖葫芦了🍡",
]

# 随机语料：随机拼接句子 + 随机插入分隔符，确保不变量在任意输入下都成立
RANDOM_SENTENCES = [
    "诶你猜怎么着", "钟离先生又忘带钱啦", "今天来了个老先生", "他挑了半个时辰木料",
    "本堂主差点没绷住", "你说他是不是把往生堂当钱庄了", "雨停啦", "海灯节快到了",
    "香菱又拉我去试菜", "爷爷以前常坐那个位子", "这伤得挺艺术", "要不要顺便订个灵位",
]


def test_content_invariant() -> None:
    print("\n[1] 内容不变量：分气泡后逐字相同")
    cases = list(CORPUS)
    rng = random.Random(20260925)
    for _ in range(200):
        n = rng.randint(1, 7)
        parts = [rng.choice(RANDOM_SENTENCES) for _ in range(n)]
        sep = rng.choice(["|||", "，", "。", "", "|||"])
        cases.append(sep.join(parts))

    bad = []
    for text in cases:
        bubbles = dv._split_bubbles(text, 5)
        if _skeleton("".join(bubbles)) != _skeleton(text):
            bad.append(text[:40])
    check(f"{len(cases)} 条语料内容逐字不变", not bad,
          f"{len(bad)} 条不一致：{bad[:3]}" if bad else "")


def test_bubble_count_and_cap() -> None:
    print("\n[2] 条数上限与尾部合并")
    long_text = "。".join(f"第{i}句话，讲的是往生堂里的一桩小事" for i in range(1, 21))
    for cap in (1, 2, 3, 5):
        bubbles = dv._split_bubbles(long_text, cap)
        ok = len(bubbles) <= cap and _skeleton("".join(bubbles)) == _skeleton(long_text)
        check(f"上限 {cap} 条：实际 {len(bubbles)} 条且不丢内容", ok)
    check("空白输入返回空列表", dv._split_bubbles("   ", 5) == [])


def test_narrative_variety() -> None:
    print("\n[3] 叙事形态：长短交错、拒绝整齐的中等长度")
    # 三条 30 字且整齐 → 应制造落差（不再是 3 条一样长）
    uniform = "甲" * 30 + "|||" + "乙" * 30 + "|||" + "丙" * 30
    b = dv._split_bubbles(uniform, 5)
    lens = [len(x) for x in b]
    check("3 条整齐中等长度被拉开落差", max(lens) - min(lens) > 6, f"长度 {lens}")
    check("合并后内容仍完整", _skeleton("".join(b)) == _skeleton(uniform))

    # 短促连发不该被合并
    short = "hhhh|||绝了|||你说呢"
    check("短促连发保持 3 条", len(dv._split_bubbles(short, 5)) == 3)

    # 未分段的长叙事应自动讲成 2 条以上
    story = CORPUS[3]
    check("未分段长叙事自动分条", len(dv._split_bubbles(story, 5)) >= 2,
          f"{len(dv._split_bubbles(story, 5))} 条")


def test_delay_variance() -> None:
    print("\n[4] 发送间隔：有快有慢、在范围内")
    cfg = {"think_time_range": [0.4, 2.0], "typing_chars_per_sec": 11.0,
           "long_pause_chance": 0.12, "long_pause_range": [2.5, 6.0],
           "bubble_delay_range": [0.4, 8.0]}
    rng = random.Random(7)
    random.seed(7)
    samples = [dv._bubble_delay("刚才那句", t, cfg) for t in
               ["绝了！", "嗯嗯", "往生堂今天来了一位老先生，挑木料挑了整整半个时辰，最后问本堂主能不能分期"] * 40]
    check("间隔都在 [0.4, 8.0] 内", all(0.4 <= d <= 8.0 for d in samples),
          f"min {min(samples):.2f} / max {max(samples):.2f}")
    check("间隔确实有差异（标准差 > 0.5s）",
          (sum((d - sum(samples) / len(samples)) ** 2 for d in samples) / len(samples)) ** 0.5 > 0.5)
    short = [dv._bubble_delay("x", "绝了", cfg) for _ in range(50)]
    long_ = [dv._bubble_delay("x", "往生堂今天来了一位老先生，挑木料挑了整整半个时辰，最后问本堂主" * 1, cfg)
             for _ in range(50)]
    check("短句比长句等得少（长短反应不同）",
          sum(short) / len(short) < sum(long_) / len(long_),
          f"短 {sum(short)/len(short):.2f}s < 长 {sum(long_)/len(long_):.2f}s")


def test_voice_disabled() -> None:
    print("\n[5] 语音已禁用（微信不支持音频）")
    text = "本堂主给你留了句话。[[voice: 旅行者，别忘了吃饭呀]]"
    clean, items = dv._parse_media_markers(text, {})
    check("默认不产出任何媒体项", items == [], f"items={items}")
    check("语音文本转成文字气泡、内容不丢",
          "别忘了吃饭呀" in clean and "[[voice" not in clean, repr(clean))
    check("转出的文字会被分泡发送", len(dv._split_bubbles(clean, 5)) >= 2)

    _, items_on = dv._parse_media_markers(text, {"voice_enabled": True})
    check("显式开启时才产生语音项", [i["kind"] for i in items_on] == ["voice"])

    clean_img, items_img = dv._parse_media_markers("看这个。[[img: 胡桃 立绘]]", {})
    check("图片标记不受影响", [i["kind"] for i in items_img] == ["img"]
          and "[[img" not in clean_img)


def test_parenthesis_safety() -> None:
    print("\n[6] 括号动作（（叉腰）/（眯着眼笑））不会被切两半")
    cases = {
        "切点正好落在括号内部": "甲" * 30 + "（" + "乙" * 40 + "）" + "丙" * 30,
        "括号里有逗号": "甲" * 40 + "（笑眯眯地，把茶推过去）" + "丁" * 20,
        "括号跨越切分阈值": "开头一段话" * 8 + "（这里有个挺长的动作描述，还带逗号，故意放长一点）" + "结尾又来一句" * 5,
        "括号里带句号": "（他叹了口气。然后又笑了）" + "老先生说他改主意了，不留了，要回家陪孙子去" * 3,
    }
    for name, text in cases.items():
        pieces = dv._split_long(text, 45)
        balanced = all(p.count("（") == p.count("）") and p.count("(") == p.count(")")
                       for p in pieces)
        lossless = "".join(pieces) == text
        check(f"{name}：括号配平且逐字不变", balanced and lossless,
              f"{len(pieces)} 片" if balanced and lossless else f"配平={balanced} 逐字={lossless}")

    # 端到端：分气泡后每个气泡的括号仍然是配平的
    text = ("（叉腰）本堂主说话算话|||（眯着眼笑）你信不信|||"
            + "今天来了个老先生" * 6 + "（蹲在木料堆里挑了半个时辰，敲敲打打比本堂主还认真）")
    bubbles = dv._split_bubbles(text, 5)
    check("分气泡后每条括号配平",
          all(b.count("（") == b.count("）") for b in bubbles), f"{len(bubbles)} 条")
    check("气泡内容逐字不变", _skeleton("".join(bubbles)) == _skeleton(text))


def main() -> int:
    print("输出形式不变量测试（不联网、不发消息）")
    test_content_invariant()
    test_bubble_count_and_cap()
    test_narrative_variety()
    test_delay_variance()
    test_voice_disabled()
    test_parenthesis_safety()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
