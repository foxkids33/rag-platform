from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from app.services.query_signals import (
    contains_exact_identifier,
    extract_exact_identifiers,
    requests_live_operational_state,
)
from app.services.rag_quality import evidence_status, source_quality_score

PAGE_MARKER_RE = re.compile(r"^\s*\d{1,4}\s+из\s+\d{1,4}\s*$", re.IGNORECASE)
STANDALONE_PAGE_RE = re.compile(r"^\s*\d{1,4}\s*$")
DOT_LEADER_RE = re.compile(r"\.{4,}")
CODE_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
URL_OR_EMAIL_RE = re.compile(
    r"(?:https?://|www\.|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[.\-][0-9A-Za-zА-Яа-яЁё]+)*")
QUERY_STOP_WORDS = {
    "что",
    "такое",
    "какой",
    "какая",
    "какие",
    "какого",
    "какому",
    "каким",
    "каком",
    "который",
    "которая",
    "которые",
    "которого",
    "как",
    "для",
    "чего",
    "это",
    "есть",
    "или",
    "его",
    "ее",
    "её",
    "про",
    "при",
    "под",
    "над",
    "где",
    "когда",
    "зачем",
    "ответь",
    "приведи",
    "покажи",
    "укажи",
    "перечисли",
    "дословно",
    "сохрани",
    "порядок",
    "сначала",
    "затем",
    "обе",
    "оба",
    "обоих",
}

QUERY_ANCHOR_WEIGHT = 2.0
QUERY_LONG_TERM_WEIGHT = 1.25
QUERY_ANCHOR_SHARE = 0.35
BREADCRUMB_SIGNAL_WEIGHT = 0.15
RERANK_QUERY_SELECTION_MAX_WEIGHT = 0.10
SOFT_CYRILLIC_PREFIX_CHARS = 5
QUERY_RELEVANCE_RESERVED_SOURCES = 2
VERIFICATION_QUERY_RE = re.compile(
    r"\b(?:провер\w*|признак\w*|убед\w*|подтверд\w*|успеш\w*|результат\w*)\b",
    re.IGNORECASE,
)
EXACT_TRANSCRIPTION_MARKERS = (
    "дословно",
    "сохрани порядок",
    "точно как",
)


class SearchResultLike(Protocol):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    parent_text: str | None
    heading: str | None
    heading_breadcrumb: str | None
    page_start: int | None
    page_end: int | None
    retrieval_rank: int
    dense_score: float | None
    lexical_score: float | None
    exact_score: float | None
    rrf_score: float
    rerank_score: float | None
    rerank_fusion_score: float | None
    source_scope: str
    knowledge_base_name: str | None
    knowledge_base_version: int | None


@dataclass(frozen=True)
class ContextSource:
    index: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    heading: str | None
    page_start: int | None
    page_end: int | None
    excerpt: str
    retrieval_rank: int
    rerank_score: float | None
    rerank_fusion_score: float | None
    source_scope: str
    knowledge_base_name: str | None
    knowledge_base_version: int | None
    quality_score: float
    query_relevance_score: float
    selection_score: float
    source_url: str | None = None
    source_page_id: str | None = None
    source_version: str | None = None

    @property
    def citation(self) -> str:
        return f"[{self.index}]"


@dataclass(frozen=True)
class BuiltContext:
    text: str
    sources: list[ContextSource]
    char_count: int
    candidate_count: int
    evidence_status: str
    evidence_score: float | None
    rejected_low_score: int
    rejected_duplicate: int
    rejected_document_cap: int
    required_anchor_count: int = 0
    matched_anchor_count: int = 0


def _closes_code_fence(line: str, opening: str) -> bool:
    marker = CODE_FENCE_RE.fullmatch(line)
    return bool(
        marker
        and marker[1][0] == opening[0]
        and len(marker[1]) >= len(opening)
        and not marker[2].strip()
    )


def clean_context_text(value: str) -> str:
    cleaned_lines: list[str] = []
    previous_line: str | None = None
    fence: str | None = None

    for raw_line in value.replace("\x0c", "\n").splitlines():
        marker = CODE_FENCE_RE.fullmatch(raw_line)
        if fence is not None:
            cleaned_lines.append(raw_line)
            if _closes_code_fence(raw_line, fence):
                fence = None
            continue
        if marker:
            fence = marker[1]
            cleaned_lines.append(raw_line)
            continue
        line = " ".join(raw_line.split()).strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if PAGE_MARKER_RE.fullmatch(line) or STANDALONE_PAGE_RE.fullmatch(line):
            continue
        if DOT_LEADER_RE.search(line):
            continue
        if re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", line):
            continue
        if line.casefold() in {"оглавление", "содержание"}:
            continue
        if line == previous_line:
            continue
        cleaned_lines.append(line)
        previous_line = line

    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()
    # Blank lines inside a configuration/code fence are significant.
    return "\n".join(cleaned_lines).strip()


def _technical_excerpt(value: str, max_chars: int) -> str:
    """Return an ordered prefix, never a sliced or unclosed command block."""
    if len(value) <= max_chars:
        return value
    notice = "[Фрагмент инструкции: продолжение не вошло; полный порядок действий не подтверждён.]"
    if max_chars <= len(notice):
        return ""
    limit = max_chars - len(notice) - 2
    end = value.rfind("\n", 0, limit) + 1
    opened: str | None = None
    opening_offset = 0
    offset = 0
    for line in value[:end].splitlines(keepends=True):
        marker = CODE_FENCE_RE.fullmatch(line.removesuffix("\n"))
        if opened is None and marker:
            opened, opening_offset = marker[1], offset
        elif opened is not None and _closes_code_fence(line.removesuffix("\n"), opened):
            opened = None
        offset += len(line)
    if opened is not None:
        end = opening_offset
    prefix = value[:end].rstrip()
    return (prefix + "\n\n" if prefix else "") + notice


def _technical_units(value: str) -> list[str]:
    """Split prose and fenced blocks without ever cutting a fence in half."""
    units: list[str] = []
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if current and (unit := "\n".join(current).strip()):
            units.append(unit)
        current.clear()

    for line in value.splitlines():
        marker = CODE_FENCE_RE.fullmatch(line)
        if fence is not None:
            current.append(line)
            if _closes_code_fence(line, fence):
                fence = None
                flush()
            continue
        if marker:
            flush()
            fence = marker[1]
            current.append(line)
            continue
        if line.strip():
            current.append(line)
        else:
            flush()
    flush()
    return units


def _focused_technical_excerpt(value: str, query: str, max_chars: int) -> str | None:
    """Select a contiguous verification passage while preserving code fences."""
    lowered_query = query.casefold()
    if not VERIFICATION_QUERY_RE.search(query) or any(
        marker in lowered_query for marker in EXACT_TRANSCRIPTION_MARKERS
    ):
        return None

    units = _technical_units(value)
    if not units:
        return None
    signal_weights, anchors = _query_signal_weights(query)
    exact_identifiers = extract_exact_identifiers(query)

    def unit_score(unit: str) -> tuple[float, int]:
        tokens = {
            _normalise_signal_token(raw_token)
            for raw_token in WORD_RE.findall(unit)
        }
        matched_weight = 0.0
        for token, weight in signal_weights.items():
            if token in tokens or (
                token not in anchors
                and any(_soft_lexical_match(token, content_token) for content_token in tokens)
            ):
                matched_weight += weight
        matched_weight += 5.0 * sum(
            contains_exact_identifier(unit, identifier)
            for identifier in exact_identifiers
        )
        return matched_weight, -len(unit)

    best_index = max(range(len(units)), key=lambda index: unit_score(units[index]))
    if unit_score(units[best_index])[0] <= 0.0 or len(units[best_index]) > max_chars:
        return None

    left = right = best_index
    excerpt = units[best_index]
    # The following verification result usually completes the preceding check,
    # so prefer the right neighbour before adding setup from the left.
    while left > 0 or right + 1 < len(units):
        added = False
        for next_left, next_right in (
            (left, right + 1),
            (left - 1, right),
        ):
            if next_left < 0 or next_right >= len(units):
                continue
            candidate = "\n\n".join(units[next_left : next_right + 1])
            if len(candidate) <= max_chars:
                left, right, excerpt = next_left, next_right, candidate
                added = True
                break
        if not added:
            break
    return excerpt if len(excerpt) >= 40 else None


def _query_terms(query: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(query)
        if len(token) >= 3 and token.casefold() not in QUERY_STOP_WORDS
    }


def _normalise_signal_token(value: str) -> str:
    return value.casefold().replace("ё", "е").replace(".", "").replace("-", "")


def _soft_lexical_match(query_token: str, content_token: str) -> bool:
    """Match common Russian inflections without weakening exact anchors.

    This is intentionally only a soft context-selection signal. Retrieval still
    comes from PostgreSQL's Russian text-search configuration, while identifiers,
    numbers and abbreviations continue to require exact matching.
    """

    if query_token == content_token:
        return True
    if len(query_token) < 6 or len(content_token) < 6:
        return False
    if not query_token.isalpha() or not content_token.isalpha():
        return False
    if not all("а" <= character <= "я" for character in query_token + content_token):
        return False
    return (
        query_token[:SOFT_CYRILLIC_PREFIX_CHARS]
        == content_token[:SOFT_CYRILLIC_PREFIX_CHARS]
    )


def _query_signal_weights(query: str) -> tuple[dict[str, float], set[str]]:
    """Return weighted query terms and high-precision anchors.

    Numbers, versions, dates, and uppercase abbreviations carry more intent than
    ordinary words. Every signal comes from the current query; the selector does
    not depend on product-specific aliases.
    """

    weights: dict[str, float] = {}
    anchors: set[str] = set()
    for raw_token in WORD_RE.findall(query):
        token = _normalise_signal_token(raw_token)
        if not token or token in QUERY_STOP_WORDS:
            continue

        has_digit = any(char.isdigit() for char in raw_token)
        is_abbreviation = len(token) >= 3 and raw_token.isupper()
        has_compound_marker = any(marker in raw_token for marker in (".", "-"))
        if len(token) < 3 and not has_digit:
            continue

        is_anchor = has_digit or is_abbreviation or has_compound_marker
        if is_anchor:
            weight = QUERY_ANCHOR_WEIGHT
            anchors.add(token)
        elif len(token) >= 8:
            weight = QUERY_LONG_TERM_WEIGHT
        else:
            weight = 1.0
        weights[token] = max(weights.get(token, 0.0), weight)
    return weights, anchors


def _query_relevance_score(
    result: SearchResultLike,
    signal_weights: dict[str, float],
    anchors: set[str],
) -> float:
    if not signal_weights:
        return 0.0

    direct_tokens = {
        token
        for value in (result.text, result.heading or "")
        for raw_token in WORD_RE.findall(value)
        if (token := _normalise_signal_token(raw_token))
    }
    breadcrumb_tokens = {
        token
        for raw_token in WORD_RE.findall(result.heading_breadcrumb or "")
        if (token := _normalise_signal_token(raw_token))
    }

    def matched_tokens(content_tokens: set[str]) -> set[str]:
        return {
            token
            for token in signal_weights
            if token in content_tokens
            or (
                token not in anchors
                and any(
                    _soft_lexical_match(token, content_token)
                    for content_token in content_tokens
                )
            )
        }

    direct_matches = matched_tokens(direct_tokens)
    breadcrumb_only_matches = matched_tokens(breadcrumb_tokens) - direct_matches
    total_weight = sum(signal_weights.values())
    matched_weight = sum(signal_weights[token] for token in direct_matches) + (
        BREADCRUMB_SIGNAL_WEIGHT
        * sum(signal_weights[token] for token in breadcrumb_only_matches)
    )
    weighted_coverage = matched_weight / total_weight if total_weight else 0.0
    if not anchors:
        return weighted_coverage

    direct_anchor_count = len(anchors & direct_matches)
    breadcrumb_anchor_count = len(anchors & breadcrumb_only_matches)
    anchor_coverage = (
        direct_anchor_count
        + BREADCRUMB_SIGNAL_WEIGHT * breadcrumb_anchor_count
    ) / len(anchors)
    return (
        (1.0 - QUERY_ANCHOR_SHARE) * weighted_coverage
        + QUERY_ANCHOR_SHARE * anchor_coverage
    )


def _selection_score(
    result: SearchResultLike,
    *,
    quality_score: float,
    query_relevance_score: float,
    query_selection_weight: float,
) -> float:
    effective_weight = query_selection_weight
    if result.rerank_score is not None:
        effective_weight = min(effective_weight, RERANK_QUERY_SELECTION_MAX_WEIGHT)
    return (
        (1.0 - effective_weight) * quality_score
        + effective_weight * query_relevance_score
    )


def focused_context_excerpt(value: str, query: str, max_chars: int) -> str:
    cleaned = clean_context_text(value)
    if len(cleaned) <= max_chars:
        return cleaned
    if re.search(r"(?m)^ {0,3}(`{3,}|~{3,})", cleaned):
        if focused := _focused_technical_excerpt(cleaned, query, max_chars):
            return focused
        return _technical_excerpt(cleaned, max_chars)

    terms = _query_terms(query)
    exact_identifiers = extract_exact_identifiers(query)
    paragraphs = [part.strip() for part in cleaned.split("\n\n") if part.strip()]
    if not paragraphs:
        return cleaned[:max_chars].rstrip()

    def paragraph_score(paragraph: str) -> tuple[float, int]:
        paragraph_terms = {token.casefold() for token in WORD_RE.findall(paragraph)}
        overlap = len(terms & paragraph_terms)
        score = float(overlap)
        score += 5.0 * sum(
            contains_exact_identifier(paragraph, identifier)
            for identifier in exact_identifiers
        )
        lowered = paragraph.casefold()
        if query.casefold().startswith("что такое") and any(
            marker in lowered
            for marker in (" — это ", " это ", "предназначен", "предназначена")
        ):
            score += 1.5
        return score, -len(paragraph)

    best_index = max(range(len(paragraphs)), key=lambda index: paragraph_score(paragraphs[index]))
    selected_indices = {best_index}
    left = best_index - 1
    right = best_index + 1

    while left >= 0 or right < len(paragraphs):
        candidates: list[int] = []
        if right < len(paragraphs):
            candidates.append(right)
        if left >= 0:
            candidates.append(left)

        added = False
        for index in candidates:
            trial_indices = sorted(selected_indices | {index})
            candidate = "\n\n".join(paragraphs[item] for item in trial_indices)
            if len(candidate) <= max_chars:
                selected_indices.add(index)
                added = True
                if index == left:
                    left -= 1
                else:
                    right += 1
                break
        if not added:
            break

    excerpt = "\n\n".join(paragraphs[index] for index in sorted(selected_indices))
    return excerpt[:max_chars].rstrip()


def _content_terms(value: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(value)
        if len(token) >= 4 and token.casefold() not in QUERY_STOP_WORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _page_label(page_start: int | None, page_end: int | None) -> str | None:
    if page_start is None:
        return None
    if page_end is None or page_end == page_start:
        return f"стр. {page_start}"
    return f"стр. {page_start}–{page_end}"


def _source_header(source: ContextSource) -> str:
    if source.source_scope == "knowledge_base":
        knowledge_base = source.knowledge_base_name or "База знаний"
        if source.knowledge_base_version is not None:
            knowledge_base = f"{knowledge_base}, версия {source.knowledge_base_version}"
        details = [f"база знаний: {knowledge_base}", source.filename]
    else:
        details = [source.filename]
    page_label = _page_label(source.page_start, source.page_end)
    if page_label:
        details.append(page_label)
    if source.heading:
        details.append(source.heading)
    if source.source_version:
        details.append(f"Confluence v{source.source_version}")
    if source.source_url:
        details.append(source.source_url)
    return f"{source.citation} Источник: " + ", ".join(details)


def build_context(
    question: str,
    results: Iterable[SearchResultLike],
    *,
    max_context_chars: int,
    max_source_chars: int,
    max_sources: int,
    min_source_score: float = 0.05,
    relative_source_score: float = 0.25,
    source_similarity_threshold: float = 0.78,
    max_sources_per_document: int = 3,
    strong_evidence_score: float = 0.50,
    limited_evidence_score: float = 0.15,
    query_selection_weight: float = 0.30,
) -> BuiltContext:
    candidates = list(results)
    if requests_live_operational_state(question):
        return BuiltContext(
            text="",
            sources=[],
            char_count=0,
            candidate_count=len(candidates),
            evidence_status="insufficient",
            evidence_score=None,
            rejected_low_score=len(candidates),
            rejected_duplicate=0,
            rejected_document_cap=0,
        )
    required_identifiers = set(extract_exact_identifiers(question))
    candidate_matched_identifiers = {
        identifier
        for candidate in candidates
        for identifier in required_identifiers
        if contains_exact_identifier(
            " ".join(
                value
                for value in (
                    candidate.text,
                    candidate.heading or "",
                    candidate.heading_breadcrumb or "",
                )
                if value
            ),
            identifier,
        )
    }
    candidate_scores = [source_quality_score(result) for result in candidates]
    best_score = max(candidate_scores, default=None)
    status = evidence_status(
        best_score,
        strong_threshold=strong_evidence_score,
        limited_threshold=limited_evidence_score,
    )
    if required_identifiers and candidate_matched_identifiers != required_identifiers:
        status = "insufficient"

    if status == "insufficient":
        return BuiltContext(
            text="",
            sources=[],
            char_count=0,
            candidate_count=len(candidates),
            evidence_status=status,
            evidence_score=best_score,
            rejected_low_score=len(candidates),
            rejected_duplicate=0,
            rejected_document_cap=0,
            required_anchor_count=len(required_identifiers),
            matched_anchor_count=len(candidate_matched_identifiers),
        )

    sources: list[ContextSource] = []
    blocks: list[str] = []
    selected_terms: list[set[str]] = []
    document_counts: dict[uuid.UUID, int] = {}
    used_chars = 0
    expansion_texts: dict[uuid.UUID, tuple[str, str | None]] = {}
    rejected_low_score = 0
    rejected_duplicate = 0
    rejected_document_cap = 0
    score_floor = max(min_source_score, (best_score or 0.0) * relative_source_score)

    signal_weights, anchors = _query_signal_weights(question)
    ranked_candidates: list[tuple[SearchResultLike, float, float, float, int]] = []
    for position, (result, quality_score) in enumerate(
        zip(candidates, candidate_scores, strict=True)
    ):
        query_relevance_score = _query_relevance_score(
            result,
            signal_weights,
            anchors,
        )
        selection_score = _selection_score(
            result,
            quality_score=quality_score,
            query_relevance_score=query_relevance_score,
            query_selection_weight=query_selection_weight,
        )
        ranked_candidates.append(
            (
                result,
                quality_score,
                query_relevance_score,
                selection_score,
                position,
            )
        )
    ranked_candidates.sort(key=lambda item: (-item[3], item[4]))

    # A long, high-similarity section can otherwise consume the character budget
    # before a more literal leaf section is considered. Reserve at most two of
    # the available source slots for the strongest direct query matches while
    # retaining at least two slots ordered by the blended quality score.
    reserved_count = min(
        QUERY_RELEVANCE_RESERVED_SOURCES,
        max(0, max_sources - 2),
    )
    if reserved_count and query_selection_weight > 0.0:
        relevance_leaders = sorted(
            (
                item
                for item in ranked_candidates
                if item[1] >= score_floor and item[2] > 0.0
            ),
            key=lambda item: (-item[2], -item[3], item[4]),
        )[:reserved_count]
        reserved_positions = {item[4] for item in relevance_leaders}
        ranked_candidates = [
            *relevance_leaders,
            *(
                item
                for item in ranked_candidates
                if item[4] not in reserved_positions
            ),
        ]

    # A fenced block may be indivisible and larger than its fair share. Retry
    # such candidates after compact evidence, using only the remaining budget.
    selection_candidates = [(candidate, False) for candidate in ranked_candidates]
    for candidate, deferred in selection_candidates:
        result, quality_score, query_relevance_score, selection_score, _ = candidate
        if len(sources) >= max_sources:
            break
        if quality_score < score_floor:
            rejected_low_score += 1
            continue
        if document_counts.get(result.document_id, 0) >= max_sources_per_document:
            rejected_document_cap += 1
            continue

        # Parent-deduped Markdown results may be prose leaves whose commands
        # live in a sibling code chunk. Use the same bounded, leaf-first
        # expansion as Confluence, not an unconditional large-parent fallback.
        technical = result.filename.endswith(".confluence.json") or (
            result.filename.lower().endswith((".md", ".markdown"))
            and any(
                re.search(r"(?m)^ {0,3}(`{3,}|~{3,})", value)
                for value in (result.text, result.parent_text or "")
            )
        )
        source_text = result.text
        if not technical and len(source_text.strip()) < 240 and result.parent_text:
            source_text = result.parent_text

        # Select compact evidence first. A parent section must not consume the
        # budget before other relevant leaf chunks have had a chance to enter.
        source_budget = max_source_chars
        if technical:
            source_budget = min(
                10000,
                max(0, max_context_chars // max(1, min(max_sources, len(candidates))) - 300),
            )
            if deferred:
                source_budget = min(10000, max(0, max_context_chars - used_chars - 300))

        excerpt = focused_context_excerpt(source_text, question, source_budget)
        if technical and not excerpt.split("[Фрагмент инструкции:", 1)[0].strip():
            if not deferred:
                selection_candidates.append((candidate, True))
            continue  # A truncation notice alone is not evidence.
        if len(excerpt) < 40:
            rejected_low_score += 1
            continue

        terms = _content_terms(excerpt)
        if any(_jaccard(terms, existing) >= source_similarity_threshold for existing in selected_terms):
            rejected_duplicate += 1
            continue

        heading = result.heading_breadcrumb or result.heading
        source = ContextSource(
            index=len(sources) + 1,
            chunk_id=result.chunk_id,
            document_id=result.document_id,
            filename=result.filename,
            chunk_index=result.chunk_index,
            heading=heading,
            page_start=result.page_start,
            page_end=result.page_end,
            excerpt=excerpt,
            retrieval_rank=result.retrieval_rank,
            rerank_score=result.rerank_score,
            rerank_fusion_score=result.rerank_fusion_score,
            quality_score=quality_score,
            query_relevance_score=query_relevance_score,
            selection_score=selection_score,
            source_scope=getattr(result, "source_scope", "workspace"),
            knowledge_base_name=getattr(result, "knowledge_base_name", None),
            knowledge_base_version=getattr(result, "knowledge_base_version", None),
            source_url=getattr(result, "source_url", None),
            source_page_id=getattr(result, "source_page_id", None),
            source_version=getattr(result, "source_version", None),
        )
        block = f"{_source_header(source)}\n{source.excerpt}"
        separator_chars = 2 if blocks else 0
        remaining = max_context_chars - used_chars - separator_chars
        if remaining < 120:
            break
        if len(block) > remaining:
            shortened_excerpt_chars = remaining - len(_source_header(source)) - 1
            if shortened_excerpt_chars < 40:
                continue
            shortened_excerpt = focused_context_excerpt(source.excerpt, question, shortened_excerpt_chars)
            if not shortened_excerpt:
                continue
            source = replace(source, excerpt=shortened_excerpt)
            block = f"{_source_header(source)}\n{source.excerpt}"
            if len(block) > remaining:
                continue

        sources.append(source)
        blocks.append(block)
        selected_terms.append(_content_terms(source.excerpt))
        document_counts[result.document_id] = document_counts.get(result.document_id, 0) + 1
        used_chars += len(block) + separator_chars
        if technical:
            expansion_texts[source.chunk_id] = (
                result.text,
                result.parent_text
                if result.parent_text and not getattr(result, "parent_text_truncated", False)
                else None,
            )

    # Expand in evidence-priority order: first this leaf, then its parent. Lower
    # priority continuations must not consume the leading instruction's tail.
    # Keep ordered prefixes and complete fences; never evict other selected evidence.
    for index, source in enumerate(sources):
        for expansion_level in (0, 1):
            options = expansion_texts.get(source.chunk_id)
            if not options or not options[expansion_level]:
                continue
            full_text = clean_context_text(options[expansion_level])
            prefix = source.excerpt.split("[Фрагмент инструкции:", 1)[0].rstrip()
            if not prefix or prefix not in full_text:
                continue
            budget = min(10000, max_context_chars - used_chars + len(source.excerpt))
            expanded = _technical_excerpt(full_text, budget)
            expanded_content = expanded.split("[Фрагмент инструкции:", 1)[0].rstrip()
            if prefix not in expanded_content or len(expanded_content) <= len(prefix):
                continue
            if any(
                other.document_id == source.document_id
                and other.chunk_id != source.chunk_id
                and other.excerpt == expanded
                for other in sources
            ):
                continue
            used_chars += len(expanded) - len(source.excerpt)
            source = replace(source, excerpt=expanded)
            sources[index] = source
            blocks[index] = f"{_source_header(source)}\n{source.excerpt}"

    if not sources:
        status = "insufficient"

    context_text = "\n\n".join(blocks)
    selected_matched_identifiers = {
        identifier
        for source in sources
        for identifier in required_identifiers
        if contains_exact_identifier(
            " ".join(value for value in (source.heading or "", source.excerpt) if value),
            identifier,
        )
    }
    if required_identifiers and selected_matched_identifiers != required_identifiers:
        return BuiltContext(
            text="",
            sources=[],
            char_count=0,
            candidate_count=len(candidates),
            evidence_status="insufficient",
            evidence_score=best_score,
            rejected_low_score=rejected_low_score,
            rejected_duplicate=rejected_duplicate,
            rejected_document_cap=rejected_document_cap,
            required_anchor_count=len(required_identifiers),
            matched_anchor_count=len(selected_matched_identifiers),
        )
    return BuiltContext(
        text=context_text,
        sources=sources,
        char_count=len(context_text),
        candidate_count=len(candidates),
        evidence_status=status,
        evidence_score=best_score,
        rejected_low_score=rejected_low_score,
        rejected_duplicate=rejected_duplicate,
        rejected_document_cap=rejected_document_cap,
        required_anchor_count=len(required_identifiers),
        matched_anchor_count=len(selected_matched_identifiers),
    )
