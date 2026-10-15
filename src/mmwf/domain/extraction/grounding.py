"""旧抽取路径的精确 API 兼容转发；不会自动启用模糊匹配。"""
from mmwf.domain.grounding.binder import (
    MATCH_EXACT, MATCH_NONE, MATCH_NORMALIZED, GroundingResult,
    bind_grounding_exact, normalize_punct,
)

__all__ = ["MATCH_EXACT", "MATCH_NONE", "MATCH_NORMALIZED", "GroundingResult",
           "bind_grounding_exact", "normalize_punct"]
