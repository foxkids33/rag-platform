import uuid

from app.api.routes.answer import _messages
from app.api.routes.search import SearchResult
from app.services.context_builder import build_context, clean_context_text
from app.services.conversation_context import HistoryMessage
from app.services.llm import chat_completions_url


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
