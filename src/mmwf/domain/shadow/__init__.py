"""影子工具输入合同；完整评分与汇总由 E4 扩展。"""
from .models import (
    MeetingScore, ShadowBatch, ShadowMeeting, RECORD_FIELDS, SCORE_FIELDS,
    WARNING, SYNTHETIC, parse_manifest, parse_scores, parse_records, record_template,
    score_template, thaw,
)

__all__ = [
    "MeetingScore", "ShadowBatch", "ShadowMeeting", "RECORD_FIELDS", "SCORE_FIELDS",
    "WARNING", "SYNTHETIC", "parse_manifest", "parse_scores", "parse_records", "record_template",
    "score_template", "thaw",
]

from .models import EvaluationBatch, Thresholds, Metric, ShadowSummary, parse_thresholds
from .scoring import validate_scores, aggregate_metrics
from .risks import index_issues, aggregate_risks
from .report import project_records, render_report

__all__ += ["EvaluationBatch", "Thresholds", "Metric", "ShadowSummary", "parse_thresholds",
            "validate_scores", "aggregate_metrics", "index_issues", "aggregate_risks",
            "project_records", "render_report"]
