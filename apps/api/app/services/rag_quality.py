from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Protocol

CITATION_RE = re.compile(r"\[(\d{1,3})\]")


class QualityResultLike(Protocol):
    rerank_score: float | None
    dense_score: float | None
    lexical_score: float | None
    rrf_score: float


@dataclass(frozen=True)
class CitationAudit:
    valid: bool
    cited_source_indices: list[int]
    invalid_citations: list[int]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def source_quality_score(result: QualityResultLike) -> float:
    """Return a comparable 0..1 score for context selection and quality gates.

    Reranker scores are preferred. Dense cosine similarity is already exposed as
    a similarity-like value. Lexical and RRF scores are mapped monotonically so
    they remain useful when reranking is disabled without pretending to be
    calibrated probabilities.
    """
    if result.rerank_score is not None:
        return _clamp(float(result.rerank_score))
    if result.dense_score is not None:
        return _clamp(float(result.dense_score))
    if result.lexical_score is not None:
        return _clamp(1.0 - math.exp(-30.0 * max(0.0, float(result.lexical_score))))
    return _clamp(max(0.0, float(result.rrf_score)) * 25.0)


def evidence_status(
    score: float | None,
    *,
    strong_threshold: float,
    limited_threshold: float,
) -> str:
    if score is None or score < limited_threshold:
        return "insufficient"
    if score >= strong_threshold:
        return "strong"
    return "limited"


def audit_citations(answer: str, valid_source_indices: Iterable[int]) -> CitationAudit:
    valid_indices = set(valid_source_indices)
    cited = sorted({int(match) for match in CITATION_RE.findall(answer)})
    invalid = sorted(index for index in cited if index not in valid_indices)
    valid_cited = sorted(index for index in cited if index in valid_indices)
    return CitationAudit(
        valid=bool(valid_cited) and not invalid,
        cited_source_indices=valid_cited,
        invalid_citations=invalid,
    )
