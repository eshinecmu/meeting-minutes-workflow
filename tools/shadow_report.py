"""T2-05 E4：读取显式人工评分，生成隔离的影子报告文件。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mmwf.domain.shadow import (  # noqa: E402
    EvaluationBatch, RECORD_FIELDS, aggregate_metrics, index_issues, parse_records,
    parse_thresholds, parse_scores, project_records, render_report, thaw,
)
from mmwf.domain.shadow.models import envelope, freeze, identifier  # noqa: E402
from mmwf.domain.minutes.issues import canonical_json  # noqa: E402


def read_json(path):
    def reject_constant(value):
        raise ValueError(f"JSON 不允许 {value}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON 键重复：{key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant, object_pairs_hook=unique)


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def _member(batch_dir, relative):
    path = batch_dir / relative
    resolved = path.resolve(strict=True)
    if batch_dir not in resolved.parents:
        raise ValueError("批次文件符号链接越界")
    return resolved


def load_batch(folder, scores_path, scores):
    """只读计划清单中的作业文件和人工显式引用的源文件，不扫描 data。"""
    run = read_json(_member(folder, "run.json"))
    envelope(run)
    records = read_json(_member(folder, "records.template.json"))
    parse_records(records)
    if (records["batch_id"], records["dataset_kind"]) != (run["batch_id"], run["dataset_kind"]):
        raise ValueError("记录表混批")
    planned = run["meetings"]
    if [m["meeting_id"] for m in planned] != [r["meeting_id"] for r in records["records"]]:
        raise ValueError("计划会议与记录表不一致")
    meetings, protected = [], [folder, scores_path.parent]
    for plan, record in zip(planned, records["records"]):
        mid = identifier(plan["meeting_id"], "meeting_id")
        candidate = read_json(_member(folder, f"{mid}/candidate.json"))
        issues = read_json(_member(folder, f"{mid}/issues.json"))
        for artifact in (candidate, issues):
            envelope(artifact)
            if (artifact["batch_id"], artifact["dataset_kind"], artifact["meeting_id"]) != (run["batch_id"], run["dataset_kind"], mid):
                raise ValueError("候选稿/issues 混批")
        if candidate["status"] != plan["status"] or plan["status"] not in ("generated", "blocked", "failed"):
            raise ValueError("会议状态不一致")
        if record["meeting_kind"] != plan["meeting_kind"] or record["duration_seconds"] != plan["duration_seconds"]:
            raise ValueError("记录表会议元信息不一致")
        context = candidate.get("context", {})
        if context:
            for field in ("source_fingerprint", "template_fingerprint"):
                if context.get(field) != candidate[field]:
                    raise ValueError("候选元信息与版本指纹不一致")
            template = context.get("template_snapshot")
            if template is not None and hashlib.sha256(canonical_json(template).encode()).hexdigest() != candidate["template_fingerprint"]:
                raise ValueError("模板内容与指纹不一致")
        actual = hashlib.sha256(canonical_json(candidate["minutes"]).encode()).hexdigest()
        if actual != candidate["candidate_fingerprint"]:
            raise ValueError("候选稿内容指纹不一致")
        indexed = index_issues(issues["issues"])
        for raw in issues["issues"]:
            if raw.get("issue_id") != index_issues([raw])[0]["issue_id"]:
                raise ValueError("原始 issue_id 与四元组不一致")
        docs = {"candidate.json": candidate}
        for score in scores.get("scores", []):
            if not isinstance(score, dict) or score.get("meeting_id") != mid:
                continue
            for statement in score.get("statements") or []:
                for location in statement.get("source_locations") or []:
                    if not isinstance(location, str) or "#" not in location:
                        raise ValueError("源定位格式非法")
                    name = location.split("#", 1)[0]
                    path = Path(name)
                    if path.is_absolute() or ".." in path.parts or not name:
                        raise ValueError("源定位须为相对评分文件的显式路径，不得穿越")
                    source = (scores_path.parent / path).resolve(strict=True)
                    if source.suffix != ".json":
                        raise ValueError("源定位须引用权威分段 JSON")
                    content = source.read_bytes()
                    if hashlib.sha256(content).hexdigest() != candidate["source_file_fingerprints"].get("segments_path"):
                        raise ValueError("权威源分段已改动或不属于本会议")
                    docs[name] = read_json(source)
                    protected.append(source.parent)
        meetings.append({"meeting_id": mid, "name": record["meeting_name"], "meeting_kind": plan["meeting_kind"],
                         "duration_seconds": plan["duration_seconds"], "status": plan["status"],
                         "failure_reason": candidate["failure_reason"], "candidate": candidate,
                         "issues": indexed, "issues_complete": issues.get("complete") is True, "documents": docs})
    provenance = {**run, "scores_path": str(scores_path),
                  "scores_file_fingerprint": hashlib.sha256(scores_path.read_bytes()).hexdigest()}
    return EvaluationBatch(run["batch_id"], run["dataset_kind"], tuple(freeze(m) for m in meetings), freeze(provenance)), protected


def write_reports(output, summary):
    """所有校验和序列化先完成，新目录发布失败则清理，不复用旧结论。"""
    report = thaw(summary.data)
    records = project_records(summary)
    parse_records(records)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=RECORD_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in records["records"]:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, bool)) else v for k, v in row.items()})
    files = {"report.md": render_report(summary), "records.csv": stream.getvalue(),
             "report.json": json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
             "records.json": json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".shadow-report-", dir=output.parent))
    reserved = False
    try:
        for name, content in files.items():
            (stage / name).write_text(content, encoding="utf-8")
        output.mkdir(exist_ok=False)
        reserved = True
        for name in ("records.csv", "records.json", "report.md", "report.json"):
            os.replace(stage / name, output / name)
    except BaseException:
        if reserved:
            shutil.rmtree(output)
        raise
    finally:
        shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("batch", "scores", "thresholds", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        batch_dir = args.batch.resolve(strict=True)
        scores_path = args.scores.resolve(strict=True)
        thresholds_path = args.thresholds.resolve(strict=True)
        if ".." in args.output.parts or args.output.is_symlink():
            raise ValueError("报告输出路径穿越或符号链接")
        output = args.output.resolve()
        if output.exists():
            raise ValueError("报告输出必须为新目录，禁止旧报告覆盖")
        scores = read_json(scores_path)
        envelope(scores)
        parse_scores(scores)
        thresholds = parse_thresholds(read_json(thresholds_path))
        batch, protected = load_batch(batch_dir, scores_path, scores)
        protected += [thresholds_path.parent, ROOT / "data", Path.cwd() / "data"]
        for directory in batch.metadata.get("manifest", {}).get("production_data_dirs", ()):
            path = Path(directory)
            if not path.is_absolute():
                # E3 未保存 manifest 所在目录，无法可靠还原相对正式目录。
                raise ValueError("报告需要 production_data_dirs 为绝对路径以验证隔离")
            protected.append(path.resolve())
        if any(overlaps(output, p) for p in protected):
            raise ValueError("报告输出与批次、评分、阈值、源或正式目录重叠")
        batch = EvaluationBatch(batch.batch_id, batch.dataset_kind, batch.meetings,
            freeze({**batch.metadata, "thresholds_path": str(thresholds_path),
                    "thresholds_file_fingerprint": hashlib.sha256(thresholds_path.read_bytes()).hexdigest(),
                    "report_code_file_fingerprints": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in [Path(__file__), *sorted((ROOT / "src/mmwf/domain/shadow").glob("*.py"))]}}))
        summary = aggregate_metrics(batch, scores, thresholds)
        # 已有的固定名称投影只能与权威评分一致，不能凭改表改判。
        expected_records = project_records(summary)
        projection = batch_dir / "records.json"
        if projection.exists():
            supplied = read_json(_member(batch_dir, "records.json"))
            parse_records(supplied)
            if any(supplied[k] != expected_records[k] for k in ("batch_id", "dataset_kind", "records")):
                raise ValueError("records.json 与权威 scores 投影不一致")
        projection_csv = batch_dir / "records.csv"
        if projection_csv.exists():
            with _member(batch_dir, "records.csv").open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                if reader.fieldnames != list(RECORD_FIELDS):
                    raise ValueError("records.csv 表头不合约")
            expected = [{k: "" if v is None else json.dumps(v, ensure_ascii=False) if isinstance(v, (list, bool)) else str(v)
                         for k, v in row.items()} for row in expected_records["records"]]
            if rows != expected:
                raise ValueError("records.csv 与权威 scores 投影不一致")
        write_reports(output, summary)
        data = summary.data
        print(f"{data['status']} quality_pass={data['quality_pass']} eligible_for_stage2={data['eligible_for_stage2']}")
        print(f"真实状态 {data['real_run_status']}；计入真实验收 {data['real_meetings_counted']}；工具报告不修改授权")
        return 2 if not data["complete"] else (0 if data["eligible_for_stage2"] else 1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"影子报告校验失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
