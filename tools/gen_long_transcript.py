"""生成 T2-01 的 2 万字记录稿夹具 + 25 项人工标注（可复跑）。

为什么用脚本生成而不是手写
--------------------------
施工卡 §4.2 明写"2 万字夹具**不要手写**"。三个理由：

1. **手写保证不了字数**。差几百字就会让分块数变化，
   于是"块数落在 10—20"这条判据变成看运气。
2. **手写保证不了"该有的特征都在"**：说话人切换、转折词、
   长静音（`gap_after_ms > 1500`）—— 这三条是 U-EX-01/02/03 的判据，
   夹具里必须**确定性地**出现，否则那三条测试是在测空气。
3. **25 个议定事项必须位置分散**（跨越多个块），手写很容易全挤在开头，
   那样"块间归并"根本不会被触发 —— 而它恰恰是 T2-01 最要命的一环。

★ 确定性
--------
用固定随机种子。同样的代码跑两次必须得到**逐字节相同**的夹具 ——
否则召回率的分母（25 项）会漂，验收结果不可复现。

用法：
    python tools/gen_long_transcript.py            # 写入 tests/fixtures/transcripts/
    python tools/gen_long_transcript.py --check     # 只校验现有夹具是否仍匹配
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "transcripts"

#: 目标正文长度（**只算 segment 正文，不含时间码**）。
TARGET_CHARS = 20000

#: 固定种子 —— 见模块 docstring 的"确定性"。
SEED = 20260930

SPEAKERS = ("主持人", "张三", "李四", "王五", "赵六")

#: 转折词（必须与 `chunker.TURN_WORDS` 一致）。
TURN_WORDS = ("那么", "接下来", "第二个议题", "下面", "另外", "关于")

#: 议定事项模板。`{i}` 会被替换成序号，让每条 quote 唯一可辨 ——
#: 这是召回率能算出来的前提（quote 重复就分不清是哪一条被召回了）。
RESOLUTION_TPL = "会议决定，第{i}项工作由{topic}组牵头推进，于本月底前完成。"

#: 背景陈述模板（凑字数 + 制造"非议定"的干扰内容）。
FILLER_TPL = (
    "从目前情况看，{topic}方面的整体进展比较平稳，各项指标都在预期区间内。",
    "相关同志汇报了{topic}的推进情况，也提出了一些需要协调的问题。",
    "会议认为，{topic}这项工作还需要各部门继续配合，不能有松劲的想法。",
    "关于{topic}，前一段时间做了不少基础性工作，底数是清楚的。",
)

TOPICS = (
    "预算", "安全生产", "人事", "信息化", "后勤保障", "宣传",
    "信访", "督查", "培训", "档案",
)


def _timecode(seconds: float) -> str:
    seconds = round(seconds)
    return f"[{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}]"


def build() -> tuple[str, list[dict]]:
    """生成 `(记录稿 Markdown, 25 项标注)`。"""
    rng = random.Random(SEED)
    timing_rng = random.Random(SEED + 1)

    def advance(text: str) -> float:
        # 保留内容随机序列；时间另用固定种子，约 4 字/秒加短停顿。
        rng.randrange(20, 45)
        pause = 3.0 if len(lines) % 47 == 0 else timing_rng.uniform(0.3, 1.2)
        return len(text) / 4.0 + pause

    lines: list[str] = ["# 会议记录稿", ""]
    expected: list[dict] = []

    seconds = 0
    body_chars = 0
    res_index = 0
    topic_i = 0

    #: 25 个议定事项分散在整个文稿里 —— 每 ~800 字放一条，
    #: 保证它们跨越多个块（块目标 1500 字），从而触发块间归并。
    total_resolutions = 25
    chars_per_resolution = TARGET_CHARS // total_resolutions   # ≈800

    since_last_res = 0
    while body_chars < TARGET_CHARS:
        topic = TOPICS[topic_i % len(TOPICS)]
        speaker = SPEAKERS[rng.randrange(len(SPEAKERS))]

        # 兼容原内容随机序列；静音由 advance 单独生成。
        rng.random()
        # ② 转折词：每 5 轮一次（U-EX-02 的判据）
        use_turn = (topic_i % 5 == 0)
        # ③ 说话人切换：由上面随机挑人天然产生（U-EX-01 的判据）

        prefix = TURN_WORDS[topic_i % len(TURN_WORDS)] if use_turn else ""
        text = prefix + FILLER_TPL[topic_i % len(FILLER_TPL)].format(topic=topic)
        lines.append(f"{_timecode(seconds)} {speaker}：{text}")
        body_chars += len(text)
        seconds += advance(text)

        # ---- 该放一条议定事项了 ----
        if since_last_res >= chars_per_resolution and res_index < total_resolutions:
            since_last_res = 0
            res_index += 1
            owner = SPEAKERS[1 + (res_index % (len(SPEAKERS) - 1))]
            quote = RESOLUTION_TPL.format(i=res_index, topic=topic)
            lines.append(f"{_timecode(seconds)} {owner}：{quote}")
            body_chars += len(quote)
            quote_start_ms = round(seconds) * 1000
            seconds += advance(quote)
            expected.append({
                "id": f"R{res_index:02d}",
                "quote": quote,
                "kind": "resolution",
                "topic": f"{topic}工作",
                "content": quote,
                "owner": owner,
                "start_ms": quote_start_ms,
            })
        else:
            since_last_res += len(text)
        topic_i += 1

    # 补足剩余的议定事项（循环可能提前退出）
    while res_index < total_resolutions:
        res_index += 1
        topic = TOPICS[res_index % len(TOPICS)]
        owner = SPEAKERS[1 + (res_index % (len(SPEAKERS) - 1))]
        quote = RESOLUTION_TPL.format(i=res_index, topic=topic)
        lines.append(f"{_timecode(seconds)} {owner}：{quote}")
        body_chars += len(quote)
        quote_start_ms = round(seconds) * 1000
        seconds += advance(quote)
        expected.append({
            "id": f"R{res_index:02d}",
            "quote": quote,
            "kind": "resolution",
            "topic": f"{topic}工作",
            "content": quote,
            "owner": owner,
            "start_ms": quote_start_ms,
        })

    return "\n".join(lines) + "\n", expected


#: 纯寒暄块（U-EX-09：无信息块**不得编造**，六维应全部为空数组）。
CHITCHAT = """# 会议记录稿

[00:00:00] 主持人：大家上午好。
[00:00:03] 张三：早上好。
[00:00:05] 李四：今天路上有点堵，来晚了。
[00:00:09] 主持人：没关系，先喝口水。
[00:00:14] 王五：这茶叶不错。
[00:00:18] 主持人：喜欢就多喝点。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 T2-01 的 2 万字夹具")
    ap.add_argument("--check", action="store_true",
                    help="只校验现有夹具与生成结果是否一致")
    args = ap.parse_args()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    md, expected = build()
    body_len = sum(len(l.split("：", 1)[-1]) for l in md.splitlines()
                   if "：" in l)

    txt_path = FIXTURE_DIR / "long_20000.txt"
    exp_path = FIXTURE_DIR / "long_20000_expected.json"
    chi_path = FIXTURE_DIR / "chitchat.txt"

    if args.check:
        ok = True
        if not txt_path.exists() or txt_path.read_text(encoding="utf-8") != md:
            print("✗ long_20000.txt 与生成结果不一致")
            ok = False
        if (not exp_path.exists()
                or json.loads(exp_path.read_text(encoding="utf-8")) != expected):
            print("✗ long_20000_expected.json 与生成结果不一致")
            ok = False
        print("✓ 夹具一致" if ok else "✗ 夹具不一致，请重新生成")
        return 0 if ok else 1

    txt_path.write_text(md, encoding="utf-8")
    exp_path.write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    chi_path.write_text(CHITCHAT, encoding="utf-8")

    print(f"✓ {txt_path.relative_to(PROJECT_ROOT)}")
    print(f"    正文 {body_len} 字 / 总长 {len(md)} 字符 / "
          f"{len(md.splitlines())} 行")
    print(f"✓ {exp_path.relative_to(PROJECT_ROOT)}（{len(expected)} 项标注）")
    print(f"✓ {chi_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
