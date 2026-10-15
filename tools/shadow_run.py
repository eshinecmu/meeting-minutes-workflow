"""T2-05 E3：从已核对记录稿开始的影子作业，绝不创建业务纪要版本。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mmwf.domain.shadow import (  # noqa: E402
    RECORD_FIELDS, SYNTHETIC, WARNING, parse_manifest, record_template, score_template, thaw,
)
from mmwf.domain.minutes.granularity import normalize_granularity  # noqa: E402
from mmwf.domain.minutes.issues import aggregate_issues, canonical_json  # noqa: E402
from mmwf.domain.minutes.post_validation import (  # noqa: E402
    business_entries, post_validate, source_segments, validate_hard, verify_references,
)
from mmwf.domain.minutes.state_compat import authoritative_segments, upgrade_legacy_state  # noqa: E402
from mmwf.domain.minutes.template_application import apply_template, render_template_markdown  # noqa: E402
from mmwf.domain.template import parse_template  # noqa: E402
from mmwf.graph.build import route_after_validate  # noqa: E402
from mmwf.graph.nodes.extraction import make_llm_extractor  # noqa: E402


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def input_path(base, value):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("输入路径不得穿越父目录")
    return (base / path).resolve()


def overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def prepare_batch(manifest_path, output):
    """先校验所有路径；内容错误在逐会议阶段保留，不删计划行。"""
    manifest_path = Path(manifest_path).resolve(strict=True)
    batch = parse_manifest(read_json(manifest_path))
    output = Path(output)
    if ".." in output.parts or output.is_symlink():
        raise ValueError("输出路径穿越或符号链接")
    output = output.resolve()
    if output.exists():
        raise ValueError("输出必须是新批次目录，禁止覆盖")
    base = manifest_path.parent
    protected = [ROOT / "data", Path.cwd() / "data"]
    directories = batch.metadata.get("production_data_dirs", ())
    if not isinstance(directories, tuple) or any(not isinstance(p, str) or not p for p in directories):
        raise ValueError("production_data_dirs 必须为路径数组")
    protected.extend(input_path(base, p) for p in directories)
    if batch.dataset_kind == "real" and not directories:
        raise ValueError("real 必须显式列出 production_data_dirs，防止写入自定义正式数据目录")
    paths = {"manifest": manifest_path}
    for meeting in batch.meetings:
        for field in ("record_path", "segments_path", "template_snapshot_path", "fake_response_path"):
            value = getattr(meeting, field)
            if value:
                paths[f"{meeting.meeting_id}/{field}"] = input_path(base, value)
    # 文件所在目录视为输入目录；解析符号链接后再做重叠判断。
    protected.extend(p.parent for p in paths.values())
    if any(overlaps(output, p.resolve()) for p in protected):
        raise ValueError("输出与输入目录或正式数据目录重叠")
    return batch, output, paths


def make_client(batch, meeting, paths):
    """Fake 只在 synthetic；real 仅允许显式的既有本机适配器。"""
    from mmwf.adapters.llm import FakeLlmClient, make_llm_client
    from mmwf.settings import ExtractionConfig, LlmConfig

    model = thaw(batch.metadata.get("model_config", {}))
    if not isinstance(model, dict):
        raise ValueError("model_config 必须为对象")
    extraction = ExtractionConfig.model_validate(model.get("extraction", {"engine": "llm"}))
    if extraction.engine != "llm":
        raise ValueError("影子链必须显式使用 llm 抽取，不得 naive 回退")
    if batch.dataset_kind == "synthetic":
        response = read_json(paths[f"{meeting.meeting_id}/fake_response_path"])
        if not isinstance(response, dict) or not isinstance(response.get("script"), list) or not response["script"] or any(not isinstance(s, str) for s in response["script"]):
            raise ValueError("synthetic Fake 响应必须提供非空字符串 script 数组")
        client = FakeLlmClient(script=response["script"])
        settings = SimpleNamespace(extraction=extraction)
        info = {"provider": "fake", "model": "SYNTHETIC", "model_version": "SYNTHETIC",
                "extraction": extraction.model_dump(mode="json")}
    else:
        # 不隐式读取配置或探测生产 DB；使用单位提供同版本的配置快照。
        llm_raw = model.get("llm")
        if not isinstance(llm_raw, dict) or not {"provider", "base_url", "model"} <= llm_raw.keys():
            raise ValueError("real 缺显式 llm 配置快照")
        url = urlsplit(llm_raw["base_url"])
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1", "localhost") or url.username or url.password:
            raise ValueError("仅允许受控本机 HTTP 模型服务")
        llm = LlmConfig.model_validate(llm_raw)
        if llm.provider != "ollama_local":
            raise ValueError("real 只使用既有本地适配器，禁止 Fake")
        settings = SimpleNamespace(llm=llm, extraction=extraction)
        client = make_llm_client(settings)
        info = {**llm.model_dump(mode="json"), "model_version": batch.metadata.get("model_version", "unknown"),
                "extraction": extraction.model_dump(mode="json")}
        if not client.available():
            raise ValueError("本地模型不可用；停止，不回退")
    return client, settings, info


def load_inputs(batch, meeting, paths):
    record = paths[f"{meeting.meeting_id}/record_path"].read_text(encoding="utf-8")
    segments = read_json(paths[f"{meeting.meeting_id}/segments_path"])
    if not record.strip() or not isinstance(segments, list) or not segments:
        raise ValueError("必须提供非空核对记录稿及权威分段")
    seen = set()
    for segment in segments:
        if not isinstance(segment, dict) or type(segment.get("seq")) is not int or segment["seq"] < 1 or segment["seq"] in seen:
            raise ValueError("权威分段 seq 必须为唯一正整数")
        seen.add(segment["seq"])
        if not isinstance(segment.get("text"), str) or not segment["text"].strip():
            raise ValueError("权威分段 text 不能为空")
        for key in ("start_ms", "end_ms"):
            if segment.get(key) is not None and (type(segment[key]) is not int or segment[key] < 0):
                raise ValueError("分段时间须为非负整数或 null")
        if segment.get("start_ms") is not None and segment.get("end_ms") is not None and segment["end_ms"] < segment["start_ms"]:
            raise ValueError("分段结束早于开始")
    raw_template = read_json(paths[f"{meeting.meeting_id}/template_snapshot_path"])
    template = parse_template({**raw_template, "version": raw_template.get("version_no", raw_template.get("version"))})
    if template.meeting_kind != meeting.meeting_kind:
        raise ValueError("meeting_kind 与模板快照不符")
    snapshot = template.to_dict()
    context = {"meeting_meta": thaw(meeting.meeting_meta), "template_id": template.template_id,
               "template_version": template.version_no, "template_snapshot": snapshot,
               "template_fingerprint": fingerprint(snapshot), "source_version": meeting.source_version,
               "source_fingerprint": fingerprint(record)}
    return {"record_markdown": record, "transcript": segments}, context, template


def generate_candidate(state, context, template, extractor, *, steps):
    """与生产 extract/generate/validate 同序；跳过带业务写入的节点包装器。"""
    extracted = extractor(state["record_markdown"])
    steps.append({"step": "extract"})
    state = upgrade_legacy_state({**state, **extracted, "minutes_structured": {}, "gen_retry": 0}, context)
    failures = {"CHUNK_LLM_FAILED", "CHUNK_BAD_JSON", "CHUNK_SCHEMA_FAILED", "CHUNK_TRUNCATED", "NO_CHUNK_SUCCEEDED"}
    if any(i["code"] in failures for i in state["issues"]):
        return state, "failed", "抽取未完整完成，保留问题清单"
    while True:
        adapted = upgrade_legacy_state(state, context)
        normalized = normalize_granularity(adapted["minutes"])
        facts = [i for i in adapted["issues"] if i["source"] not in {"template", "grounding", "consistency"}]
        state.update(minutes=normalized.minutes, minutes_structured=apply_template(normalized.minutes, template),
                     gen_retry=state["gen_retry"] + 1, issues=aggregate_issues(facts, normalized.issues))
        steps.append({"step": "generate", "gen_retry": state["gen_retry"]})
        adapted = upgrade_legacy_state(state, context)
        structured = adapted["minutes_structured"]
        facts = [i for i in adapted["issues"] if i["source"] not in {"template", "grounding", "consistency"}]
        issues = post_validate(structured, segments=authoritative_segments(state),
                               record_markdown=state["record_markdown"], template=template, extraction_issues=facts)
        segments = source_segments(authoritative_segments(state), state["record_markdown"])
        for path, entry in business_entries(structured):
            if "." not in path and not entry.get("discussion", "").strip():
                continue
            refs, diagnostics = verify_references(entry, segments, path)
            if not any(i["blocking"] for i in diagnostics):
                entry["source_refs"] = refs
        state.update(minutes_structured=apply_template(structured, template), minutes=adapted["minutes"],
                     issues=issues, validation_issues=[i for i in issues if i["source"] in {"template", "grounding", "consistency"}])
        route = route_after_validate(state)
        steps.append({"step": "validate", "route": route})
        if route == "blocked":
            return state, "blocked", "无依据硬阻断，已停止成文"
        if route != "retry":
            break
    hard = validate_hard(state["minutes_structured"], authoritative_segments(state), record_markdown=state["record_markdown"])
    steps.append({"step": "validate_hard"})
    if any(i["blocking"] for i in hard):
        state["issues"] = aggregate_issues(state["issues"], hard)
        return state, "blocked", "成文前复检硬阻断"
    state["minutes_markdown"] = render_template_markdown(apply_template(state["minutes_structured"], template), state["issues"])
    steps.append({"step": "render_template_markdown"})
    return state, "generated", ""



class TracedClient:
    """记录实际抽取调用及模式重试；不保存提示词正文或模型原始回答。"""
    def __init__(self, client, steps):
        self.client, self.steps = client, steps
        self.lock = Lock()
        self.call_count = 0

    def chat(self, prompt, **kwargs):
        from mmwf.domain.extraction.prompts import RETRY_PREFIX
        with self.lock:
            self.call_count += 1
            event = {"step": "extract_chat", "call": self.call_count,
                     "schema_retry": prompt.startswith(RETRY_PREFIX.split("{errors}")[0])}
            self.steps.append(event)
        started = time.monotonic()
        try:
            result = self.client.chat(prompt, **kwargs)
            event["outcome"] = "returned"
            return result
        except Exception:
            event["outcome"] = "failed"
            raise
        finally:
            event["elapsed_seconds"] = time.monotonic() - started


def run_meeting(batch, meeting, paths, output):
    started = time.monotonic()
    folder = output / meeting.meeting_id
    folder.mkdir()
    steps, state, context, model = [], {}, {}, {}
    status, reason = "failed", ""
    file_hashes = {}
    try:
        for key, path in paths.items():
            if key.startswith(meeting.meeting_id + "/"):
                file_hashes[key.split("/", 1)[1]] = hashlib.sha256(path.read_bytes()).hexdigest()
        state, context, template = load_inputs(batch, meeting, paths)
        client, settings, model = make_client(batch, meeting, paths)
        # 工厂与生产使用相同配置；不经过正式 runner 或 extract 的写状态包装器。
        traced = TracedClient(client, steps)
        state, status, reason = generate_candidate(state, context, template,
            make_llm_extractor(traced, settings), steps=steps)
        model["calls"] = traced.call_count
        context = (state.get("minutes") or {}).get("_context", context)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
    labels = [WARNING] + ([SYNTHETIC] if batch.dataset_kind == "synthetic" else [])
    common = {"format_version": 1, "batch_id": batch.batch_id, "dataset_kind": batch.dataset_kind,
              "meeting_id": meeting.meeting_id, "labels": labels}
    candidate = {**common, "status": status, "failure_reason": reason,
                 "minutes": state.get("minutes_structured") or state.get("minutes") or {},
                 "context": context, "source_fingerprint": context.get("source_fingerprint", "unknown"),
                 "template_fingerprint": context.get("template_fingerprint", "unknown"),
                 "source_file_fingerprints": file_hashes, "source_version": meeting.source_version,
                 "model": model, "steps": steps, "elapsed_seconds": time.monotonic() - started}
    candidate["candidate_fingerprint"] = fingerprint(candidate["minutes"])
    write_json(folder / "candidate.json", candidate)
    issues = []
    for issue in aggregate_issues(state.get("issues", [])):
        identity = [issue[k] for k in ("source", "code", "target", "evidence")]
        issues.append({**issue, "issue_id": fingerprint(identity)})
    write_json(folder / "issues.json", {**common, "issues": issues, "complete": status != "failed"})
    if status == "generated":
        (folder / "candidate.md").write_text("\n\n".join(labels) + "\n\n" + state["minutes_markdown"], encoding="utf-8")
    score = score_template()
    score.update(meeting_id=meeting.meeting_id, candidate_fingerprint=candidate["candidate_fingerprint"],
                 source_fingerprint=candidate["source_fingerprint"], template_fingerprint=candidate["template_fingerprint"])
    write_json(folder / "score.template.json", {**common, "scores": [score]})
    return candidate


def run_batch(manifest_path, output):
    started = time.monotonic()
    batch, output, paths = prepare_batch(manifest_path, output)
    # mkdir 的排他语义拒绝并发复用输出目录；不在生产目录登记 batch_id。
    output.mkdir(parents=True, exist_ok=False)
    candidates = [run_meeting(batch, meeting, paths, output) for meeting in batch.meetings]
    labels = [WARNING] + ([SYNTHETIC] if batch.dataset_kind == "synthetic" else [])
    common = {"format_version": 1, "batch_id": batch.batch_id, "dataset_kind": batch.dataset_kind, "labels": labels}
    records = []
    boundary = batch.metadata.get("long_meeting_boundary_seconds")
    for seq, (meeting, candidate) in enumerate(zip(batch.meetings, candidates), 1):
        row = record_template()
        row.update(sequence=seq, meeting_name=meeting.name, meeting_kind=meeting.meeting_kind,
                   duration_seconds=meeting.duration_seconds, notes=candidate["failure_reason"],
                   meeting_id=meeting.meeting_id, batch_id=batch.batch_id, dataset_kind=batch.dataset_kind,
                   score_path=f"{meeting.meeting_id}/score.template.json")
        records.append(row)
    write_json(output / "records.template.json", {**common, "records": records})
    with (output / "records.template.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECORD_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
    try:
        code_version = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        code_version = "unknown"
    # 同时记录执行链源码指纹；HEAD 不能代表尚未提交的工具实现。
    code_files = [Path(__file__), *sorted((ROOT / "src/mmwf/domain").rglob("*.py")),
                  ROOT / "src/mmwf/graph/build.py", ROOT / "src/mmwf/graph/nodes/extraction.py",
                  *sorted((ROOT / "src/mmwf/adapters/llm").glob("*.py")), ROOT / "src/mmwf/settings.py"]
    code_fingerprints = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files}
    from mmwf.domain.extraction import prompts
    prompt_fp = fingerprint({k: getattr(prompts, k) for k in ("SYSTEM_PROMPT", "EXTRACTION_TEMPLATE", "RETRY_PREFIX")})
    run = {**common, "start_point": "已核对记录稿及权威转写分段（不含阶段0音频/ASR/人工核对实测）",
           "manifest": thaw(batch.metadata), "manifest_fingerprint": hashlib.sha256(paths["manifest"].read_bytes()).hexdigest(),
           "code_version": code_version, "code_file_fingerprints": code_fingerprints,
           "elapsed_seconds": time.monotonic() - started, "driver_fingerprint": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           "prompt_fingerprint": prompt_fp, "example_library_fingerprint": "unknown",
           "gaps": ["示例库独立版本 unknown", "模型真实版本未取到时为 unknown；需单位补证", "真实≥20场及人工比对 PENDING"],
           "real_run_status": "PENDING", "real_meetings_counted": 0, "eligible_for_stage2": False,
           "meetings": [{"meeting_id": m.meeting_id, "meeting_kind": m.meeting_kind,
                         "duration_seconds": m.duration_seconds,
                         "duration_class": "unknown" if boundary is None else ("long" if m.duration_seconds >= boundary else "short"),
                         "status": c["status"], "failure_reason": c["failure_reason"], "elapsed_seconds": c["elapsed_seconds"]}
                        for m, c in zip(batch.meetings, candidates)],
           "complete": all(c["status"] == "generated" for c in candidates)}
    write_json(output / "run.json", run)
    return run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run = run_batch(args.manifest, args.output)
    except (ValueError, OSError, TypeError) as exc:
        print(f"影子输入校验失败：{exc}", file=sys.stderr)
        return 2
    print(f"{WARNING}；dataset_kind={run['dataset_kind']}；生成 {sum(m['status'] == 'generated' for m in run['meetings'])}/{len(run['meetings'])}")
    print("真实 ≥20 场：PENDING；本机未运行真实会议；计入真实验收 0；须使用单位受控环境人工比对")
    return 0 if run["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
