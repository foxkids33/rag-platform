"""Build document-level evidence for chat summaries of indexed workspace articles."""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, Workspace
from app.services.context_builder import BuiltContext, ContextSource
from app.services.workspace_sources import includes_workspace_documents

GREETING = "Привет! Могу обсудить вопрос, помочь с текстом или сделать выжимку по вашим статьям. Чем помочь?"
CHOOSE_ARTICLES = "Какие статьи взять для выжимки? Выберите в чате от одной до трёх статей или укажите их полные названия."
EXCERPT_NOTE = "Для длинных материалов выжимка составлена по фрагментам из начала, середины и конца текста."


def representative_sentences(article_text: str, title: str | None = None) -> list[str]:
    """Choose complete, verbatim sentences from different parts of bounded model context."""
    candidates: list[tuple[str, set[str]]] = []
    seen = {" ".join((title or "").casefold().split())}
    stop = {"когда", "также", "этого", "этой", "этом", "котор", "после", "перед", "своих",
            "which", "with", "that", "from", "this", "their", "этот", "чтобы"}
    for section_no, section in enumerate(article_text.split("\n[…]\n")):
        fence = ""
        for line_no, raw_line in enumerate(section.splitlines()):
            line = raw_line.strip()
            if line.startswith(("```", "~~~")):
                fence = "" if fence == line[:3] else fence or line[:3]
                continue
            if fence or not line or line.startswith(("#", "|", "!", "<", "[", "$ ", "> ", "{")):
                continue
            if re.match(
                r"(?:Автор|Владелец страницы|Дата (?:создания|изменения)|Версия страницы|"
                r"Навигация|Содержание|Оглавление|См\. также|Метки|Источник|Заголовок|Page ID|"
                r"Last updated|Table of contents)\s*[:—]|(?:Перейти к|Back to top|Skip to)\b",
                line, re.IGNORECASE,
            ):
                continue
            line = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
            for index, sentence in enumerate(re.split(r"(?<=[.!?])\s+", line)):
                # The second and third sampled sections may start mid-sentence.
                if section_no and line_no == index == 0:
                    continue
                if not 30 <= len(sentence) <= 470 or sentence[-1] not in ".!?" or not sentence[0].isupper():
                    continue
                if any(marker in sentence for marker in ("[", "]", "`", "{", "}", "<", "://", "|", "…")):
                    continue
                words = re.findall(r"[^\W\d_]{4,}", sentence.casefold())
                terms = {word[:5] for word in words if word[:5] not in stop}
                key = " ".join(sentence.casefold().split())
                if len(terms) >= 3 and key not in seen:
                    candidates.append((sentence, terms))
                    seen.add(key)
    if len(candidates) < 2:
        return []
    frequency = Counter(term for _, terms in candidates for term in terms)
    ranks = [sum(frequency[word] for word in terms) / len(terms) for _, terms in candidates]
    size = min(3, len(candidates))
    chosen = []
    # Rank by recurring terms inside each third, so a conclusion or final topic
    # can survive even when a long introduction repeats the same vocabulary.
    for group in range(size):
        region = range(group * len(candidates) // size, (group + 1) * len(candidates) // size)
        for idx in sorted(region, key=lambda i: ranks[i], reverse=True):
            words = candidates[idx][1]
            if all(len(words & candidates[j][1]) / len(words | candidates[j][1]) < .6 for j in chosen):
                chosen.append(idx)
                break
    return [candidates[i][0] for i in chosen] if len(chosen) >= 2 else []


def verified_summary_claim(raw: str, article_text: str) -> str | None:
    """Accept one concise claim paired with a real, relevant quotation from this article."""
    block = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw.strip(), re.DOTALL | re.IGNORECASE)
    try:
        data = json.loads(block[1] if block else raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"summary", "quote"}:
        return None
    summary, quote = data["summary"], data["quote"]
    if not isinstance(summary, str) or not isinstance(quote, str):
        return None
    summary, quote = " ".join(summary.split()), " ".join(quote.split())
    if not (12 <= len(summary) <= 420 and 12 <= len(quote) <= 600):
        return None
    if re.search(r"\[\[|\[\s*\d|<[^>]+>|https?://|\]\(|```", summary + " " + quote, re.IGNORECASE):
        return None
    if quote not in " ".join(article_text.split()):
        return None
    # A real quote pasted alongside an unrelated claim is insufficient support.
    # Use shared word stems for Russian inflection; numbers must match literally.
    def anchors(text: str) -> set[str]:
        return {w[:5] for w in re.findall(r"[^\W\d_]{4,}", text.casefold())
                if w not in {"этого", "этот", "которые", "который", "также", "этой", "статья", "статье",
                             "this", "that", "with", "article", "which"}}

    claims = anchors(summary)
    if len(claims & anchors(quote)) < max(2, (len(claims) + 2) // 3):
        return None
    numbers = r"\d+(?:[.,]\d+)*"
    if set(re.findall(numbers, summary)) - set(re.findall(numbers, quote)):
        return None
    # Reversing a negation changes the claim even when the keywords match.
    negation = r"\b(?:не|ни|нельзя|запрещ\w*|без|нет|no|not|never|without)\b"
    if bool(re.search(negation, summary, re.IGNORECASE)) != bool(re.search(negation, quote, re.IGNORECASE)):
        return None
    return summary


def is_greeting(question: str) -> bool:
    return question.casefold().strip(" \n\t.,!?…👋") in {
        "привет", "приветствую", "здравствуйте", "здравствуй", "добрый день",
        "добрый вечер", "доброе утро", "hi", "hello",
    }


def asks_for_summary(question: str) -> bool:
    # Match requests for a summary, not questions about the meaning of those words.
    return bool(re.search(
        r"^(?:(?:привет|здравствуйте|пожалуйста|можешь|можно|ты можешь|please)[,!?\s]+)*"
        r"(?:(?:сдела(?:й|ть)|состав(?:ь|ить)|подготов(?:ь|ить)|соб(?:ери|рать)|напиш(?:и|ите)|"
        r"дай|покажи|нужн[ао])\s+(?:мне\s+)?(?:кратк\w*\s+|общ\w*\s+)?"
        r"(?:вы[жш]и[мв]к\w*|саммари|резюме|обзор|пересказ|сводк\w*|сумм?ари[зс]\w*|содержани\w*)"
        r"|саммари|вы[жш]и[мв]к\w*|кратк\w*\s+(?:содержани\w*|обзор|пересказ|резюме)|"
        r"кратко\s+(?:перескажи|изложи)|перескажи|сумм?ари[зс](?:ируй|ация)|summari[sz]e|summary)\b",
        question.casefold().strip(),
    ))


def _requested_count(question: str) -> int | None:
    match = re.search(
        r"\b(\d+|тр[еиё]х|тр[ие]|две|двух|два|одн[ауой]+|three|two|one)\s+"
        r"(?:выбранн\w*\s+|эт\w*\s+)?(?:стат|материал|документ|источник|страниц|article|document|source)",
        question.casefold(),
    )
    if not match:
        return None
    token = match.group(1)
    if token.isdecimal():
        return int(token) if len(token) < 3 else 100
    if token.startswith(("тр", "three")):
        return 3
    return 2 if token.startswith(("дв", "two")) else 1


def select_articles(question: str, candidates: Sequence[Document]) -> list[Document]:
    count = _requested_count(question)
    folded = question.casefold()
    named = [doc for doc in candidates if any(
        re.search(r"(?<!\w)" + re.escape(label.strip().casefold()) + r"(?!\w)", folded)
        for label in (doc.title, doc.filename) if label and label.strip()
    )]
    if named:
        # An ambiguous identical title must be disambiguated via the picker.
        names = [(d.title or d.filename).casefold() for d in named]
        if len(set(names)) < len(names) or len(named) > 3 or count not in (None, len(named)):
            return []
        return named
    if len(candidates) == (count or 1) and 1 <= len(candidates) <= 3:
        return list(candidates)
    return []


def empty_context() -> BuiltContext:
    return BuiltContext("", [], 0, 0, "not_applicable", None, 0, 0, 0)


def article_excerpt(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    # Supply text from across the document, including its conclusion. Never
    # splice non-contiguous source text into a single apparent quotation.
    divider = "\n[…]\n"
    width = max(1, (budget - 2 * len(divider)) // 3)
    middle = (len(text) - width) // 2
    pieces = [text[:width], text[middle:middle + width], text[-width:]]
    return divider.join(piece.strip() for piece in pieces)


async def summary_evidence(
    db: AsyncSession, workspace: Workspace, ids: list[uuid.UUID], question: str, budget: int,
) -> tuple[BuiltContext, str | None, bool]:
    if not includes_workspace_documents(workspace.source_mode):
        if ids:
            raise HTTPException(404, "Одна из выбранных статей недоступна для этого чата.")
        return empty_context(), "Включите документы рабочего пространства в источниках чата и выберите статьи для выжимки.", False
    eligible = (
        Document.workspace_id == workspace.id, Document.status == "READY", Document.search_enabled.is_(True),
    )
    query = select(Document).where(*eligible)
    if ids:
        query = query.where(Document.id.in_(ids))
    candidates = list((await db.scalars(query.order_by(Document.created_at, Document.id))).all())
    if ids:
        by_id = {doc.id: doc for doc in candidates}
        if any(key not in by_id for key in ids):
            raise HTTPException(404, "Одна из выбранных статей недоступна для этого чата.")
        chosen = [by_id[key] for key in ids]
    else:
        chosen = select_articles(question, candidates)
    if not chosen:
        reply = CHOOSE_ARTICLES if candidates else "Добавьте в рабочее пространство хотя бы одну статью и дождитесь индексации. Тогда я смогу сделать выжимку."
        return empty_context(), reply, False

    # Read chunk text in page order, omitting the duplicated parent text and
    # large embedding vectors. Repeat the scope predicates on the content read.
    rows = (await db.execute(select(
        DocumentChunk.document_id, DocumentChunk.id, DocumentChunk.chunk_index,
        DocumentChunk.text, DocumentChunk.page_start, DocumentChunk.page_end,
    ).join(Document, Document.id == DocumentChunk.document_id).where(
        *eligible, Document.id.in_([doc.id for doc in chosen]),
    ).order_by(DocumentChunk.document_id, DocumentChunk.chunk_index))).all()
    grouped = defaultdict(list)
    for row in rows:
        if row.text.strip():
            grouped[row.document_id].append(row)
    if any(not grouped[doc.id] for doc in chosen):
        raise HTTPException(409, "Для одной из статей ещё нет индексированного текста. Обновите список материалов.")
    texts = {d.id: "\n\n".join(row.text.strip() for row in grouped[d.id]) for d in chosen}
    headers = {d.id: f"[{i}] {(d.title or d.filename)[:160]}\n" for i, d in enumerate(chosen, 1)}
    remaining = budget - sum(map(len, headers.values())) - 2 * (len(chosen) - 1)
    if remaining < 32 * len(chosen):
        raise HTTPException(503, "Размер контекста слишком мал для выжимки выбранных статей.")
    excerpts = {}
    # Reallocate unused room from short articles to longer ones.
    for i, d in enumerate(sorted(chosen, key=lambda doc: len(texts[doc.id]))):
        excerpts[d.id] = article_excerpt(texts[d.id], remaining // (len(chosen) - i))
        remaining -= len(excerpts[d.id])
    sources, sections = [], []
    for i, doc in enumerate(chosen, 1):
        first, last = grouped[doc.id][0], grouped[doc.id][-1]
        sections.append(headers[doc.id] + excerpts[doc.id])
        sources.append(ContextSource(
            index=i, chunk_id=first.id, document_id=doc.id, filename=doc.filename,
            chunk_index=first.chunk_index, heading=doc.title, page_start=first.page_start,
            page_end=last.page_end, excerpt=excerpts[doc.id][:1200],
            retrieval_rank=i, rerank_score=None, rerank_fusion_score=None,
            source_scope="workspace", knowledge_base_name=None, knowledge_base_version=None,
            quality_score=1.0, query_relevance_score=1.0, selection_score=1.0,
            source_url=doc.source_url, source_page_id=doc.external_id, source_version=doc.external_version,
        ))
    rendered = "\n\n".join(sections)
    return (
        BuiltContext(rendered, sources, len(rendered), len(chosen), "selected_documents", None, 0, 0, 0),
        None, any(excerpts[d.id] != texts[d.id] for d in chosen),
    )
