import asyncio
import uuid

from app.api.routes import answer as answer_route
from app.api.routes.answer import (
    INSUFFICIENT_EVIDENCE_ANSWER,
    PreparedAnswer,
    _messages,
)
from app.api.routes.search import SearchResponse, SearchResult
from app.services.answer_contract import ABSTAIN_MARKER, ANSWER_MARKER
from app.services.context_builder import (
    BuiltContext,
    ContextSource,
    build_context,
    clean_context_text,
)
from app.services.conversation_context import HistoryMessage
from app.services.llm import LLMOutput, chat_completions_url
from app.services.workspace_sources import WorkspaceSourceMode


def _result(
    *,
    text: str,
    rank: int,
    filename: str = "document.pdf",
    page: int | None = 10,
) -> SearchResult:
    return SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename=filename,
        chunk_index=rank,
        text=text,
        parent_text=None,
        parent_text_truncated=False,
        heading="Введение",
        heading_breadcrumb="2. Введение",
        page_start=page,
        page_end=page,
        score=0.7,
        dense_score=0.7,
        lexical_score=0.2,
        rrf_score=0.03,
        dense_rank=rank,
        lexical_rank=rank,
        retrieval_rank=rank,
        rerank_score=0.9,
        rerank_rank=rank,
        rerank_fusion_score=0.02,
        rerank_penalty=0.0,
    )


def _prepared_answer() -> PreparedAnswer:
    source = ContextSource(
        index=1,
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="document.txt",
        chunk_index=0,
        heading="Раздел",
        page_start=None,
        page_end=None,
        excerpt="Фрагмент не содержит запрошенного факта.",
        retrieval_rank=1,
        rerank_score=None,
        rerank_fusion_score=None,
        source_scope="workspace",
        knowledge_base_name=None,
        knowledge_base_version=None,
        quality_score=0.8,
        query_relevance_score=0.6,
        selection_score=0.74,
    )
    context = BuiltContext(
        text="[1] Источник: document.txt\nФрагмент не содержит запрошенного факта.",
        sources=[source],
        char_count=72,
        candidate_count=1,
        evidence_status="strong",
        evidence_score=0.8,
        rejected_low_score=0,
        rejected_duplicate=0,
        rejected_document_cap=0,
    )
    search_response = SearchResponse(
        query="Какой срок гарантии?",
        mode="hybrid",
        workspace_source_mode=WorkspaceSourceMode.USER_DOCUMENTS,
        knowledge_base_id=None,
        knowledge_base_version_id=None,
        candidate_limit=8,
        rerank_requested=False,
        rerank_applied=False,
        rerank_model=None,
        rerank_error=None,
        results=[],
    )
    return PreparedAnswer(
        question="Какой срок гарантии?",
        retrieval_query="Какой срок гарантии?",
        search_response=search_response,
        context=context,
        messages=[{"role": "user", "content": "test"}],
        max_tokens=128,
        temperature=0.1,
        query_selection_weight=0.3,
        conversation=None,
        parent_message_id=None,
        history=[],
        abstained=False,
    )


def test_clean_context_text_removes_pdf_noise() -> None:
    value = """
ОГЛАВЛЕНИЕ
Введение ........................................ 10
10 из 50
info@example.com

Полезный факт о системе.
"""
    assert clean_context_text(value) == "Полезный факт о системе."


def test_build_context_numbers_sources_and_respects_budget() -> None:
    results = [
        _result(
            text=(
                "МБД.Х — это программно-аппаратный комплекс, предназначенный "
                "для создания централизованного и безопасного хранилища данных. "
            )
            * 20,
            rank=1,
        ),
        _result(text="Вторая подтверждающая часть документа. " * 40, rank=2),
    ]

    context = build_context(
        "Что такое МБД.Х?",
        results,
        max_context_chars=900,
        max_source_chars=500,
        max_sources=2,
    )

    assert context.sources
    assert context.sources[0].citation == "[1]"
    assert context.text.startswith("[1] Источник: document.pdf, стр. 10, 2. Введение")
    assert context.char_count <= 900


def test_messages_require_grounded_citations() -> None:
    context = build_context(
        "Что такое МБД.Х?",
        [_result(text="МБД.Х — это программно-аппаратный комплекс.", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    messages = _messages("Что такое МБД.Х?", context)

    assert messages[0]["role"] == "system"
    assert "только на основании" in messages[0]["content"]
    assert ANSWER_MARKER in messages[0]["content"]
    assert ABSTAIN_MARKER in messages[0]["content"]
    assert "Частичный подтверждённый ответ не заменяй полным отказом" in messages[0]["content"]
    assert "одного точного числа, даты или версии" in messages[0]["content"]
    assert "Не переноси характеристики" in messages[0]["content"]
    assert "[1]" in messages[1]["content"]
    assert "Что такое МБД.Х?" in messages[1]["content"]


def test_chat_completions_url_supports_base_and_v1_urls() -> None:
    assert chat_completions_url("http://llm:8000") == (
        "http://llm:8000/v1/chat/completions"
    )
    assert chat_completions_url("http://llm:8000/v1/") == (
        "http://llm:8000/v1/chat/completions"
    )
    assert chat_completions_url("http://192.0.2.10:8000") == (
        "http://192.0.2.10:8000/v1/chat/completions"
    )


def test_messages_include_bounded_conversation_history() -> None:
    context = build_context(
        "Какие у неё преимущества?",
        [_result(text="МБД.Х снижает TCO и масштабируется.", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )
    history = [
        HistoryMessage(role="user", content="Что такое МБД.Х?"),
        HistoryMessage(role="assistant", content="Это программно-аппаратный комплекс."),
    ]

    messages = _messages("Какие у неё преимущества?", context, history)

    assert [item["role"] for item in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "АКТУАЛЬНЫЙ КОНТЕКСТ" in messages[-1]["content"]
    assert "Какие у неё преимущества?" in messages[-1]["content"]


def test_non_stream_answer_exposes_model_abstention(monkeypatch) -> None:
    prepared = _prepared_answer()
    persisted: dict = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def chat(*_args, **kwargs):
        assert kwargs["temperature"] == 0.1
        return LLMOutput(text=ABSTAIN_MARKER, model="test-model", finish_reason="stop")

    async def persist(*_args, **kwargs):
        persisted.update(kwargs)
        return None, None

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route.llm, "chat", chat)
    monkeypatch.setattr(answer_route, "_persist_exchange", persist)

    response = asyncio.run(
        answer_route.answer(
            uuid.uuid4(),
            answer_route.AnswerRequest(question="Вопрос"),
            None,
            None,
        )
    )

    assert response.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert response.generation_temperature == 0.1
    assert response.query_selection_weight == 0.3
    assert response.sources[0].query_relevance_score == 0.6
    assert response.sources[0].selection_score == 0.74
    assert response.abstained is True
    assert response.abstention_reason == "model_reported_insufficient_context"
    assert response.citation_valid is True
    assert persisted["abstained"] is True


def test_stream_answer_hides_marker_and_finalizes_abstention(monkeypatch) -> None:
    prepared = _prepared_answer()
    persisted: dict = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def persist_user(*_args, **_kwargs):
        return None

    async def persist_assistant(*_args, **kwargs):
        persisted.update(kwargs)

    async def stream_chat(*_args, **_kwargs):
        yield " [[ABS"
        yield "TAIN]]"

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route, "_persist_stream_user", persist_user)
    monkeypatch.setattr(answer_route, "_persist_stream_assistant", persist_assistant)
    monkeypatch.setattr(answer_route.llm, "stream_chat", stream_chat)

    async def collect() -> str:
        response = await answer_route.answer_stream(
            uuid.uuid4(),
            answer_route.AnswerRequest(question="Вопрос"),
            None,
            None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(collect())

    assert ABSTAIN_MARKER not in body
    assert INSUFFICIENT_EVIDENCE_ANSWER in body
    assert '"abstained": true' in body
    assert '"abstention_reason": "model_reported_insufficient_context"' in body
    assert persisted["abstained"] is True


def test_stream_answer_preserves_cited_partial_answer(monkeypatch) -> None:
    prepared = _prepared_answer()
    persisted: dict = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def persist_user(*_args, **_kwargs):
        return None

    async def persist_assistant(*_args, **kwargs):
        persisted.update(kwargs)

    async def stream_chat(*_args, **_kwargs):
        yield "[[ANS"
        yield "WER]]В предоставленном контексте нет точной даты, "
        yield "но продукт использует Picodata [1]."

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route, "_persist_stream_user", persist_user)
    monkeypatch.setattr(answer_route, "_persist_stream_assistant", persist_assistant)
    monkeypatch.setattr(answer_route.llm, "stream_chat", stream_chat)

    async def collect() -> str:
        response = await answer_route.answer_stream(
            uuid.uuid4(),
            answer_route.AnswerRequest(question="Вопрос"),
            None,
            None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(collect())

    assert "продукт использует Picodata [1]" in body
    assert '"abstained": false' in body
    assert '"cited_source_indices": [1]' in body
    assert persisted["abstained"] is False


def test_stream_answer_hides_trailing_marker_and_keeps_contextual_abstention(
    monkeypatch,
) -> None:
    prepared = _prepared_answer()
    persisted: dict = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def persist_user(*_args, **_kwargs):
        return None

    async def persist_assistant(*_args, **kwargs):
        persisted.update(kwargs)

    async def stream_chat(*_args, **_kwargs):
        yield "Точное значение не указано [1]. [[ABS"
        yield "TAIN]]"

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route, "_persist_stream_user", persist_user)
    monkeypatch.setattr(answer_route, "_persist_stream_assistant", persist_assistant)
    monkeypatch.setattr(answer_route.llm, "stream_chat", stream_chat)

    async def collect() -> str:
        response = await answer_route.answer_stream(
            uuid.uuid4(),
            answer_route.AnswerRequest(question="Вопрос"),
            None,
            None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(collect())

    assert ABSTAIN_MARKER not in body
    assert "Точное значение не указано [1]." in body
    assert '"abstained": true' in body
    assert '"cited_source_indices": [1]' in body
    assert persisted["abstained"] is True
