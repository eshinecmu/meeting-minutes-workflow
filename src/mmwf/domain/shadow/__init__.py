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
