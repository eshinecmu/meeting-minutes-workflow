"""T2-01 验收脚本（人可读版）：独立验证施工卡 §4 的十项标准。

标准 10 只把全知假模型的管道召回计入通过数；真模型指标单列为不支持。
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import shutil
import socket
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.adapters.llm.fake_client import FakeLlmClient, build_oracle_minutes
from mmwf.domain.extraction.chunker import chunk_segments
from mmwf.domain.extraction.extractor import extract_minutes
from mmwf.domain.extraction.merge import merge_chunk_results, similarity
from mmwf.domain.extraction.schemas import MinutesSchema
from mmwf.domain.extraction.segmentize import Segment, segmentize
from mmwf.settings import load_settings

PASS = "✓"
FAIL = "✗"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "transcripts"


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _assert(cond: bool, label: str, detail: str = "") -> bool:
    line = f"  {PASS if cond else FAIL} {label}"
    if detail:
        line += f"（{detail}）"
    print(line)
    return bool(cond)


def _empty_minutes() -> dict:
    """提供六维信息全空的合法响应，不填造任何事实。"""
    return {"title": "", "meeting_time": "", "location": "", "host": "",
            "attendees": [], "recorder": "", "topics": []}


def _long_text() -> str:
    return (FIXTURES / "long_20000.txt").read_text(encoding="utf-8")


def check_standard_1(tmp: Path) -> bool:
    banner("标准 1：分块正确（说话人 / 转折词 / 长静音）")
    ok = True
    for kind, label in (("speaker", "说话人切换"), ("turn", "转折词"), ("silence", "长静音")):
        segments = [Segment(i, str(i) * 300, speaker="张三") for i in range(1, 8)]
        if kind == "speaker":
            segments[4] = Segment(5, "五" * 300, speaker="李四")
        elif kind == "turn":
            segments[4] = Segment(5, "下面" + "五" * 298, speaker="张三")
        else:
            segments[4] = Segment(5, "五" * 300, speaker="张三", gap_after_ms=1501)
        chunks = chunk_segments(segments, overlap_chars=0)
        ok &= _assert(len(chunks) == 2 and chunks[0].segments == tuple(segments[:5]),
                      f"{label}：攒够 1500 字后在边界切开")
    return ok


def check_standard_2(tmp: Path) -> bool:
    banner("标准 2：块大小约束与单段超长强制切分")
    segments, _ = segmentize(_long_text())
    chunks = chunk_segments(segments)
    ok = _assert(bool(chunks) and all(1000 <= c.char_count <= 2000 for c in chunks[:-1])
                 and chunks[-1].char_count <= 2000,
                 "两万字夹具：非末块 1000～2000 字，末块不超上限", f"{len(chunks)} 块")
    for text, first_size in (("甲" * 4500, 2000), ("甲" * 1899 + "。" + "乙" * 2600, 1900)):
        forced = chunk_segments([Segment(1, text)], overlap_chars=0)
        ok &= _assert(len(forced) == 3 and len(forced[0].text) == first_size
                      and all(c.forced_split and c.char_count <= 2000 for c in forced)
                      and "".join(s.text for c in forced for s in c.segments) == text,
                      "超长段无损切分，优先句末标点", f"首块 {first_size} 字")
    return ok


def check_standard_3(tmp: Path) -> bool:
    banner("标准 3：重叠约 200 字并按完整段对齐")
    segments = [Segment(i, str(i) * 120, speaker="张三" if i % 2 else "李四")
                for i in range(1, 10)]
    chunks = chunk_segments(segments, target_chars=600)
    ok = _assert(len(chunks) > 1, "实际产生相邻块", f"{len(chunks)} 块")
    for left, right in zip(chunks, chunks[1:]):
        count = right.overlap_lead
        size = sum(s.char_count for s in right.segments[:count])
        ok &= _assert(count > 0 and right.segments[:count] == left.segments[-count:]
                      and 200 <= size < 320 and bool(right.segments[count:]),
                      f"块 {left.index} → {right.index}：完整尾段重叠且有新内容", f"{size} 字")
    return ok


def check_standard_4(tmp: Path) -> bool:
    banner("标准 4：跨块议题依据取并集")
    a, b, c = [{"segment_id": f"seg-{i}"} for i in range(1, 4)]
    chunks = [{"topics": [{"title": "预算", "discussion": "讨论预算", "refs": refs}]}
              for refs in ([a, b], [b, c])]
    result = merge_chunk_results(chunks)
    return _assert(len(result.topics) == 1 and result.topics[0]["refs"] == [a, b, c],
                   "同题合并，重叠依据去重，其余依据全保留")


def check_standard_5(tmp: Path) -> bool:
    banner("标准 5：相似议定仅在依据区间重叠时去重")
    text = "由办公室负责组织预算方案的审核落实并在本月底前提交全部工作成果"
    ok = _assert(similarity(text, text + "。") > 0.9, "探针文本相似度大于 0.9")
    for overlap, expected in ((True, 1), (False, 2)):
        a = {"text": text, "owner": "暂无", "refs": [
            {"segment_id": "seg-1", "start_ms": 0, "end_ms": 1000}]}
        b = {"text": text + "。", "owner": "张三", "deadline": "月底", "refs": [
            {"segment_id": "seg-2", "start_ms": 500 if overlap else 2000, "end_ms": 3000}]}
        result = merge_chunk_results([{"resolutions": [a]}, {"resolutions": [b]}])
        ok &= _assert(len(result.resolutions) == expected,
                      "区间重叠合一、不重叠保留两条", f"重叠={overlap}，{len(result.resolutions)} 条")
        if overlap:
            r = result.resolutions[0]
            ok &= _assert(r["owner"] == "张三" and r["deadline"] == "月底"
                          and r["refs"] == a["refs"] + b["refs"], "保留更完整事项与依据并集")
    return ok


def check_standard_6(tmp: Path) -> bool:
    banner("标准 6：修改 extraction.yaml 后归并行为变化")
    import yaml

    cfg_dir = tmp / "standard_6" / "config"
    shutil.copytree(PROJECT_ROOT / "config", cfg_dir)
    cfg_path = cfg_dir / "extraction.yaml"
    doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    names = ("预算方案审核落实", "预算方案审核落实工作")
    text = "[00:00] 张三：开始讨论。\n[00:01] 李四：批准预算方案。\n[00:02] 王五：继续讨论。"
    counts = []
    ok = True
    for threshold in (0.82, 0.2):
        doc["extraction"]["merge"]["topic_similarity_threshold"] = threshold
        cfg_path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        settings = load_settings(cfg_dir)
        script = []
        for name in names:
            data = _empty_minutes()
            data["topics"] = [{"name": name, "discussion": ""}]
            script.append(json.dumps(data, ensure_ascii=False))
        llm = FakeLlmClient(script=script)
        result = extract_minutes(text, llm=llm, settings=settings, target_chars=8,
                                 overlap_chars=6, max_workers=1)
        counts.append(len(result.topics))
        ok &= _assert(result.stats["chunks_ok"] == 2 and len(llm.calls) == 2,
                      f"阈值 {threshold}：实际抽取两块", f"归并后 {len(result.topics)} 个议题")
    return _assert(counts == [2, 1], "YAML 阈值从 0.82 改为 0.2，议题由两条归为一条") and ok


def check_standard_7(tmp: Path) -> bool:
    banner("标准 7：纯寒暄六维全部为空，不编造")
    llm = FakeLlmClient(fn=lambda prompt: json.dumps(build_oracle_minutes(prompt, [])))
    result = extract_minutes((FIXTURES / "chitchat.txt").read_text(encoding="utf-8"), llm=llm)
    MinutesSchema.model_validate(result.minutes)
    return _assert(len(llm.calls) == result.stats["chunks"] > 0
                   and result.minutes["attendees"] == [] and result.minutes["topics"] == []
                   and result.to_graph_dict() == {"topics": [], "resolutions": [], "action_items": []},
                   "寒暄实际经过抽取、归并与补全，六维均无事实条目")


def check_standard_8(tmp: Path) -> bool:
    banner("标准 8：空输入返回空结果且不调用模型")
    ok = True
    for text in ("", " \n", "# 空记录\n"):
        llm = FakeLlmClient(script=["不得调用"])
        result = extract_minutes(text, llm=llm)
        ok &= _assert(not llm.calls and result.minutes == {}
                      and result.to_graph_dict() == {"topics": [], "resolutions": [], "action_items": []},
                      "空记录不崩溃、不调用模型", repr(text))
    return ok


def check_standard_9(tmp: Path) -> bool:
    banner("标准 9：Schema 合规、失败最多重试两次并落 issue")
    items = json.loads((FIXTURES / "long_20000_expected.json").read_text(encoding="utf-8"))
    result = extract_minutes(_long_text(), llm=FakeLlmClient(oracle_items=items), max_workers=1)
    MinutesSchema.model_validate(result.minutes)
    ok = _assert(result.stats["chunks_ok"] == result.stats["chunks"] > 0,
                 "两万字所有块成功，最终输出通过 MinutesSchema.model_validate")
    good = build_oracle_minutes(items[0]["quote"], items[:1])
    del good["topics"][0]["resolutions"][0]["owner"]
    text = "[00:00] 张三：" + items[0]["quote"] + "\n[00:01] 李四：收到。"
    bad = '{"topics":"错误"}'
    llm = FakeLlmClient(script=[bad, json.dumps(good)])
    recovered = extract_minutes(text, llm=llm)
    MinutesSchema.model_validate(recovered.minutes)
    ok &= _assert(len(llm.calls) == 2 and all(c["has_schema"] for c in llm.calls)
                  and "【校验报错】" in llm.calls[1]["prompt"]
                  and len(recovered.resolutions) == 1 and recovered.resolutions[0]["owner"] == "暂无",
                  "错误回注后恢复合规，责任人缺失填「暂无」")
    for bad_response, rule in ((bad, "chunk_schema_failed"), ("不是 JSON", "chunk_bad_json")):
        llm = FakeLlmClient(script=[bad_response])
        failed = extract_minutes(text, llm=llm)
        MinutesSchema.model_validate(failed.minutes)
        ok &= _assert(len(llm.calls) == 3 and all(c["has_schema"] for c in llm.calls)
                      and all("【校验报错】" in c["prompt"] for c in llm.calls[1:])
                      and any(i["rule"] == rule for i in failed.issues),
                      "初次加两次重试仍失败，落 issue", rule)
    return ok


def probe_local_llm() -> str:
    """运行时取证；仅探测回环端口，不发送模型请求。"""
    executable = shutil.which("ollama")
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
            port = "可达（未运行模型质量验收）"
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno == errno.EPERM:
            port = "探测被沙箱拦截（不能作为无模型证据）"
        else:
            port = f"unreachable（{type(exc).__name__}: {exc}）"
    return f"which ollama：{executable or '未找到'}；127.0.0.1:11434：{port}"


def check_standard_10(tmp: Path) -> bool:
    banner("标准 10：全知 fake 验证两万字管道无损")
    items = json.loads((FIXTURES / "long_20000_expected.json").read_text(encoding="utf-8"))
    llm = FakeLlmClient(oracle_items=items)
    result = extract_minutes(_long_text(), llm=llm, max_workers=1)
    expected = {i["content"] for i in items}
    actual = {r["text"] for r in result.resolutions}
    recalled = expected & actual
    rate = len(recalled) / len(expected) if expected else 0
    ok = _assert(len(items) == len(expected) == 25 and actual == expected
                 and len(result.resolutions) == 25 and all(r["refs"] for r in result.resolutions)
                 and result.stats["chunks_ok"] == result.stats["chunks"] == len(llm.calls) > 1,
                 f"管道无损 {rate:.0%}（fake）", f"{len(recalled)}/{len(expected)} 个标注议定事项，{len(llm.calls)} 块")
    print(f"  真模型召回率：UNSUPPORTED（不计入通过数；{probe_local_llm()}）")
    if expected - actual:
        print("  漏召回：" + "、".join(i["id"] for i in items if i["content"] not in actual))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="T2-01 分块分层抽取验收")
    ap.add_argument("--only", type=int, choices=range(1, 11), help="只跑某一个标准（1-10）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = ap.parse_args()
    logging.disable(logging.CRITICAL)
    tmp = Path(tempfile.mkdtemp(prefix="t201_"))
    print(f"临时目录：{tmp}")
    checks = [
        (1, "分块正确", check_standard_1), (2, "块大小约束", check_standard_2),
        (3, "重叠区正确", check_standard_3), (4, "归并取并集", check_standard_4),
        (5, "议定去重", check_standard_5), (6, "阈值可配", check_standard_6),
        (7, "不编造", check_standard_7), (8, "空输入", check_standard_8),
        (9, "Schema 100% 合规", check_standard_9), (10, "管道无损（fake）", check_standard_10),
    ]
    results = []
    try:
        for num, name, fn in checks:
            if args.only and num != args.only:
                continue
            try:
                ok = fn(tmp)
            except Exception as exc:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                ok = _assert(False, f"标准 {num} 执行期异常", f"{type(exc).__name__}: {exc}")
            results.append((num, name, ok))
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    banner("T2-01 验收结果")
    for num, name, ok in results:
        print(f"  {PASS if ok else FAIL} 标准 {num}  {name}")
    passed = sum(1 for _, _, ok in results if ok)
    print(f"\n  共 {len(results)} 项，通过 {passed} 项")
    if passed == len(results):
        print(f"\n{PASS} T2-01 验收通过（{passed}/{len(results)}）")
        return 0
    print(f"\n{FAIL} T2-01 验收未通过（{passed}/{len(results)}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
