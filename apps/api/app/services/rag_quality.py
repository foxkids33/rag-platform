from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

CITATION_RE = re.compile(r"\[(\d{1,3})\]")


class QualityResultLike(Protocol):
    rerank_score: float | None
    exact_score: float | None
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

    Reranker scores are preferred unless a deterministic exact identifier match
    is stronger. Dense cosine similarity is already exposed as a similarity-like
    value. PostgreSQL ``ts_rank_cd`` is only meaningful within one lexical result
    list, so lexical-only candidates use their RRF rank contribution instead of
    being compared directly with dense similarities.
    """
    exact_score = getattr(result, "exact_score", None)
    if result.rerank_score is not None:
        scores = [float(result.rerank_score)]
        if exact_score is not None:
            scores.append(float(exact_score))
        return _clamp(max(scores))
    direct_scores = [
        float(score)
        for score in (exact_score, result.dense_score)
        if score is not None
    ]
    if direct_scores:
        return _clamp(max(direct_scores))
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
