"""同一汇总生成记录表和可复核报告；不作任何授权操作。"""
from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from ._types import ProjectedRecords, cast
from .models import RECORD_FIELDS, ShadowSummary, thaw


def project_records(summary: ShadowSummary) -> ProjectedRecords:
    data = thaw(summary.data)
    return {"format_version": 1, "batch_id": cast(str, data["batch_id"]), "dataset_kind": cast(str, data["dataset_kind"]),
            "labels": cast(list[str], data["labels"]),
            "records": [{k: row[k] for k in RECORD_FIELDS} for row in cast(Sequence[Mapping[str, object]], data["records"])]}


def _escape(value: object) -> str:
    value = html.escape(str(value), quote=True)
    for char in ("\\", "`", "*", "_", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value.replace("|", "&#124;").replace("\r", "&#13;").replace("\n", "<br>")


def render_report(summary: ShadowSummary) -> str:
    data = thaw(summary.data)
    lines = ["# 影子人工评分报告", "", *cast(Sequence[str], data["labels"]), "",
             f"批次：{_escape(data['batch_id'])}；状态：{data['status']}",
             f"quality_pass={data['quality_pass']}；eligible_for_stage2={data['eligible_for_stage2']}",
             f"真实状态：{data['real_run_status']}；计入真实验收 {data['real_meetings_counted']}",
             _escape(data["recommendation"]), "",
             "阶段0：单位确认转写和记录稿；本工具从已核对记录稿开始。",
             "阶段1：生成不交付、人工盲写、第三方比较；无依据出现退回阶段1整改。",
             "阶段2：仅提供建议，由单位决定，须实际使用记录；阶段3：单位另行决定自主使用。",
             "工具完成不等于 Q1～Q5 或红线全通过，不自动批准或修改授权。", "",
             "## 记录表（03 §6.4）", "",
             "| " + " | ".join(RECORD_FIELDS) + " |", "| " + " | ".join("---" for _ in RECORD_FIELDS) + " |"]
    for row in project_records(summary)["records"]:
        lines.append("| " + " | ".join(_escape("N/A" if row[k] is None else
                     json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], list) else row[k]) for k in RECORD_FIELDS) + " |")
    lines += ["", "## 五维（原始整数判定，非逐会均值）", "", "| 维度 | 分子 | 分母 | 比值 | 阈值 | pass |", "| --- | --- | --- | --- | --- | --- |"]
    for key, m in cast(Mapping[str, Mapping[str, object]], data["metrics"]).items():
        lines.append(f"| {key} | {m['numerator']} | {m['denominator']} | {m['value'] if m['value'] is not None else 'N/A'} | {m['threshold']} | {m['passed']} |")
    lines += ["", "## 补充风险与证据边界", "",
              "风险是人工观测，无新增合格阈值；零告警/零可判标签为 N/A，不能证明无误报或无漏报。",
              "待办误降级按明确审查事件计数；人工真实待办全集及对应关系只列补充证据，不混用分母。"]
    for key, value in cast(Mapping[str, str], data["evidence_ledger"]).items():
        lines.append(f"- {_escape(key)}：{_escape(value)}")
    lines += ["", "## 完整同源数据（含有效约定、原项、全部失败与缺口）", "",
              "<pre>" + html.escape(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)) + "</pre>", ""]
    return "\n".join(lines)
