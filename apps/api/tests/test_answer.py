import asyncio
import json
import uuid
from dataclasses import replace

from app.api.routes import answer as answer_route
from app.api.routes.answer import (
    INSUFFICIENT_EVIDENCE_ANSWER,
    PreparedAnswer,
    _answer_candidate_limit,
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
from app.services.llm import LLMOutput, LLMStreamEnd, chat_completions_url
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


def test_answer_candidate_pool_is_larger_than_selected_source_limit() -> None:
    assert _answer_candidate_limit(8, 5, 20) == 20
    assert _answer_candidate_limit(20, 5, 10) == 20
    assert _answer_candidate_limit(3, 2, 4) == 4


def test_clean_context_text_removes_pdf_noise() -> None:
    value = """
ОГЛАВЛЕНИЕ
Введение ........................................ 10
10 из 50
info@example.com

Полезный факт о системе.
"""
    assert clean_context_text(value) == "Полезный факт о системе."


def test_context_preserves_verbatim_commands_and_configuration() -> None:
    value = 'Проверка:\n\n```bash\ncurl -I https://example.test \\\n  --header "X:  two spaces"\n\n\n  value: true\n```'
    assert clean_context_text(value) == value
    assert 'curl https://example.test' in clean_context_text('curl https://example.test')


def test_indented_markdown_fences_preserve_original_code_through_context_and_audit() -> None:
    from app.services.code_integrity import audit_code_integrity

    code = '  service:\n    readiness:\n      timeout_seconds: 3  \n\n      enabled: true'
    for indentation in (" ", "  ", "   "):
        for opening, closing in (("```yaml", "````"), ("~~~yaml", "~~~~")):
            block = f"{indentation}{opening}\n{code}\n{indentation}{closing}"
            value = f"  Before   text.\n\n{block}\n\n  After   text."
            assert clean_context_text(value) == f"Before text.\n\n{block}\n\nAfter text."
            context = build_context(
                "Покажи точное содержимое конфигурации readiness.",
                [_result(text=value, rank=1, filename="runbook.md")],
                max_context_chars=2000,
                max_source_chars=1000,
                max_sources=1,
            )
            assert len(context.sources) == 1
            assert block in context.sources[0].excerpt
            answer = f"```yaml\n{code}\n```\n[1]"
            audit = audit_code_integrity(answer, context.sources)
            assert audit.status == "exact_match"
            altered = answer.replace("      timeout_seconds: 3  ", "timeout_seconds: 3")
            assert audit_code_integrity(altered, context.sources).status == "unverified"


def test_indented_technical_fences_are_never_sliced_at_budget_boundaries() -> None:
    from app.services.context_builder import focused_context_excerpt

    value = ('Предусловия\n\n  ```bash\n' + '  command --exact-value  \n' * 80
             + '  ````\n\nПроверьте результат.')
    for budget in range(90, 250):
        excerpt = focused_context_excerpt(value, 'Приведи команды дословно.', budget)
        assert len(excerpt) <= budget
        assert '```' not in excerpt
        assert 'command --exact-value' not in excerpt


def test_focused_technical_excerpt_preserves_indented_fence_code() -> None:
    from app.services.context_builder import focused_context_excerpt

    block = '  ```bash\ncheck --ready \\\n    --timeout 3  \n  ````'
    value = 'Описание системы. ' * 80 + '\n\nПроверка результата.\n\n' + block
    excerpt = focused_context_excerpt(value, 'Как проверить результат командой check?', 250)
    assert 'check --ready \\\n    --timeout 3  \n  ````' in excerpt


def _demo_markdown_leaf_and_parent() -> tuple[str, str, str]:
    from pathlib import Path

    fixture = (Path(__file__).resolve().parents[3] / "evaluation" / "datasets"
               / "demo-troubleshooting" / "demo-742-readiness.md")
    parent = fixture.read_text(encoding="utf-8").strip()
    leaf, remaining = parent.split("\n```bash\n", 1)
    code = remaining.split("\n```", 1)[0]
    return leaf.strip(), parent, code


def test_fenced_markdown_prose_leaf_expands_to_parent_command_witness() -> None:
    from app.services.code_integrity import audit_code_integrity

    leaf, parent, code = _demo_markdown_leaf_and_parent()
    assert len(leaf) > 240  # The former short-leaf fallback cannot recover this code.
    question = "Для DEMO-TS-742 приведи дословно полный исходный блок диагностики."
    for filename in ("demo-742-readiness.md", "runbook.MD", "runbook.markdown"):
        result = _result(text=leaf, rank=1, filename=filename).model_copy(
            update={"parent_text": parent, "parent_text_truncated": False},
        )
        context = build_context(
            question, [result], max_context_chars=5000,
            max_source_chars=500, max_sources=3,
        )
        assert len(context.sources) == 1
        assert f"```bash\n{code}\n```" in context.sources[0].excerpt
        assert audit_code_integrity(f"```bash\n{code}\n```\n[1]", context.sources).status == "exact_match"
        assert context.char_count <= 5000


def test_markdown_parent_expansion_uses_only_remaining_budget_without_evicting_evidence() -> None:
    from app.services.code_integrity import audit_code_integrity

    leaf, parent, code = _demo_markdown_leaf_and_parent()
    markdown = _result(text=leaf, rank=1, filename="runbook.md").model_copy(
        update={"parent_text": parent + "\n\n" + "Дальнейшие ограничения. " * 2000},
    )
    other = _result(
        text="Независимая рекомендация для DEMO-TS-742: сохраните наблюдения отдельно от документации.",
        rank=2, filename="other.txt",
    )
    context = build_context(
        "Для DEMO-TS-742 приведи команды дословно.", [markdown, other],
        max_context_chars=1700, max_source_chars=500, max_sources=3,
    )
    assert len(context.sources) == 2
    assert any("Независимая рекомендация" in source.excerpt for source in context.sources)
    assert audit_code_integrity(f"```bash\n{code}\n```\n[1]", context.sources).status == "exact_match"
    assert "Фрагмент инструкции" in context.sources[0].excerpt
    assert context.char_count <= 1700


def test_truncated_markdown_parent_is_not_used_as_a_command_witness() -> None:
    from app.services.code_integrity import audit_code_integrity

    leaf, parent, code = _demo_markdown_leaf_and_parent()
    result = _result(text=leaf, rank=1, filename="runbook.md").model_copy(
        update={"parent_text": parent, "parent_text_truncated": True},
    )
    context = build_context(
        "Для DEMO-TS-742 приведи команды дословно.", [result],
        max_context_chars=5000, max_source_chars=500, max_sources=3,
    )
    assert context.sources
    assert code not in context.sources[0].excerpt
    assert audit_code_integrity(f"```bash\n{code}\n```\n[1]", context.sources).status == "unverified"


def test_technical_context_never_slices_code_or_claims_a_full_procedure() -> None:
    from app.services.context_builder import focused_context_excerpt

    value = 'Сначала сделайте резервную копию.\n\n```bash\n' + 'command --exact-value\n' * 80 + '```\n\nПроверьте результат.'
    excerpt = focused_context_excerpt(value, 'command', 200)
    assert len(excerpt) <= 200
    assert '```' not in excerpt
    assert 'Фрагмент инструкции' in excerpt
    assert 'резервную копию' in excerpt


def test_technical_context_fences_are_balanced_at_every_budget_boundary() -> None:
    from app.services.context_builder import focused_context_excerpt

    value = 'Предусловия\n\n```bash\n  echo exact\n```\n\n' + 'Продолжение\n' * 50
    for budget in range(90, 200):
        excerpt = focused_context_excerpt(value, 'echo', budget)
        assert len(excerpt) <= budget
        assert excerpt.count('```') % 2 == 0


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


def test_build_context_never_exceeds_budget_for_long_source_header() -> None:
    result = _result(text="Подтверждённый полезный факт. " * 20, rank=1).model_copy(
        update={"heading_breadcrumb": "Очень длинный заголовок " * 40}
    )

    context = build_context(
        "Какой полезный факт подтверждён?",
        [result],
        max_context_chars=200,
        max_source_chars=100,
        max_sources=1,
    )

    assert context.char_count <= 200
    assert len(context.text) <= 200


def test_build_context_rejects_strong_semantic_noise_when_exact_id_is_absent() -> None:
    context = build_context(
        "Что означает BLUE-KB-942?",
        [_result(text="Общее описание похожей базы знаний без нужного кода.", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    assert context.evidence_status == "insufficient"
    assert context.sources == []
    assert context.required_anchor_count == 1
    assert context.matched_anchor_count == 0


def test_build_context_matches_exact_id_across_case_and_punctuation() -> None:
    context = build_context(
        "Что означает BLUE-KB-942?",
        [_result(text="Код blue_kb.942 относится к целевому продукту.", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    assert context.sources
    assert context.required_anchor_count == 1
    assert context.matched_anchor_count == 1


def test_build_context_requires_every_exact_identifier() -> None:
    context = build_context(
        "Сравни BLUE-KB-942 и RED-KB-777",
        [_result(text="Подтверждён только код BLUE-KB-942 в этой записи.", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    assert context.evidence_status == "insufficient"
    assert context.sources == []
    assert context.required_anchor_count == 2
    assert context.matched_anchor_count == 1


def test_build_context_rejects_stale_document_output_as_live_user_state() -> None:
    context = build_context(
        "Какова текущая загрузка CPU каждого узла моего кластера прямо сейчас?",
        [_result(text="MDS 1 CPU 0.2%; MDS 2 CPU 0.8%", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    assert context.evidence_status == "insufficient"
    assert context.sources == []


def test_build_context_does_not_match_exact_id_inside_longer_identifier() -> None:
    context = build_context(
        "Что означает BLUE-KB-942?",
        [_result(text="Код BLUE-KB-9421 относится к другому продукту.", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    assert context.evidence_status == "insufficient"
    assert context.matched_anchor_count == 0


def test_build_context_keeps_exact_match_ahead_of_reranked_noise() -> None:
    exact = _result(
        text=(
            "Код blue_kb.942 обозначает целевой регламент восстановления и "
            "содержит проверенную процедуру для дежурной команды."
        ),
        rank=2,
        filename="exact.txt",
    ).model_copy(update={"exact_score": 1.0, "rerank_score": 0.2})
    noise = _result(
        text="Общий текст о другом регламенте без запрошенного идентификатора.",
        rank=1,
        filename="noise.txt",
    ).model_copy(update={"rerank_score": 0.99})

    context = build_context(
        "Что означает BLUE-KB-942?",
        [noise, exact],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    assert context.sources[0].filename == "exact.txt"
    assert context.matched_anchor_count == 1


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
    assert "не останавливайся на первом найденном факте" in messages[0]["content"]
    assert "называй конкретные" in messages[0]["content"]
    assert "всеми частями вопроса" in messages[0]["content"]
    assert "уровня не является названием алгоритма" in messages[0]["content"]
    assert "[1]" in messages[1]["content"]
    assert "Что такое МБД.Х?" in messages[1]["content"]


def test_messages_require_a_fenced_block_for_one_requested_command() -> None:
    context = build_context(
        "Какой командой посмотреть настройки?",
        [_result(text="```shell\ncommand --show\n```", rank=1)],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=1,
    )

    messages = _messages("Какой командой посмотреть настройки?", context)

    assert "даже если команда одна" in messages[-1]["content"]
    assert "Не заменяй команды перечнем пакетов" in messages[-1]["content"]
    assert "Один исходный блок — один блок ответа" in messages[-1]["content"]
    assert "не разбивай его на отдельный блок для каждой команды" in messages[0]["content"]


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


def test_followup_rewrite_cannot_drop_exact_identifier(monkeypatch) -> None:
    async def chat(*_args, **_kwargs):
        return LLMOutput(
            text="Какая версия продукта?",
            model="rewrite-test",
            finish_reason="stop",
        )

    monkeypatch.setattr(answer_route.llm, "chat", chat)
    question = "А какая версия BLUE-KB-942?"
    history = [HistoryMessage(role="assistant", content="Речь шла о продукте.")]

    rewritten = asyncio.run(
        answer_route._standalone_retrieval_query(question, history)
    )

    assert rewritten == question


def test_followup_rewrite_cannot_invent_exact_identifier(monkeypatch) -> None:
    async def chat(*_args, **_kwargs):
        return LLMOutput(
            text="Что известно о RED-KB-777?",
            model="rewrite-test",
            finish_reason="stop",
        )

    monkeypatch.setattr(answer_route.llm, "chat", chat)
    question = "Расскажи подробнее"
    history = [HistoryMessage(role="user", content="Что такое BLUE-KB-942?")]

    rewritten = asyncio.run(
        answer_route._standalone_retrieval_query(question, history)
    )

    assert rewritten == question


def test_followup_rewrite_can_restore_identifier_from_history(monkeypatch) -> None:
    async def chat(*_args, **_kwargs):
        return LLMOutput(
            text="Какая версия у BLUE-KB-942?",
            model="rewrite-test",
            finish_reason="stop",
        )

    monkeypatch.setattr(answer_route.llm, "chat", chat)
    history = [HistoryMessage(role="user", content="Что такое BLUE-KB-942?")]

    rewritten = asyncio.run(
        answer_route._standalone_retrieval_query("А какая версия?", history)
    )

    assert rewritten == "Какая версия у BLUE-KB-942?"


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


def test_non_stream_answer_fails_closed_on_missing_citation(monkeypatch) -> None:
    prepared = _prepared_answer()
    persisted: dict = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def chat(*_args, **_kwargs):
        return LLMOutput(
            text=f"{ANSWER_MARKER} Неподтверждённый ответ без ссылки.",
            model="test-model",
            finish_reason="stop",
        )

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
    assert response.abstained is True
    assert response.abstention_reason == "citation_validation_failed"
    assert response.citation_valid is False
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


def test_stream_answer_persists_and_emits_length_finish_reason(monkeypatch) -> None:
    prepared = _prepared_answer()
    persisted = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def persist_user(*_args, **_kwargs):
        return None

    async def persist_assistant(*_args, **kwargs):
        persisted.update(kwargs)

    async def stream_chat(*_args, **_kwargs):
        yield "[[ANSWER]] Часть ответа [1]."
        yield LLMStreamEnd("length")

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route, "_persist_stream_user", persist_user)
    monkeypatch.setattr(answer_route, "_persist_stream_assistant", persist_assistant)
    monkeypatch.setattr(answer_route.llm, "stream_chat", stream_chat)

    async def collect():
        response = await answer_route.answer_stream(
            uuid.uuid4(), answer_route.AnswerRequest(question="Вопрос"), None, None,
        )
        return "".join([chunk async for chunk in response.body_iterator])

    assert '"finish_reason": "length"' in asyncio.run(collect())
    assert persisted["output"].finish_reason == "length"
    assert answer_route._assistant_metadata(
        prepared, persisted["output"], status="complete", sources=[],
        citation_audit=persisted["citation_audit"], abstained=False, abstention_reason=None,
    )["finish_reason"] == "length"


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


def test_stream_answer_never_emits_content_with_invalid_citation(monkeypatch) -> None:
    prepared = _prepared_answer()
    persisted: dict = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def persist_user(*_args, **_kwargs):
        return None

    async def persist_assistant(*_args, **kwargs):
        persisted.update(kwargs)

    async def stream_chat(*_args, **_kwargs):
        yield f"{ANSWER_MARKER} Опасный неподтверждённый ответ "
        yield "с выдуманной ссылкой [99]."

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

    assert "Опасный неподтверждённый ответ" not in body
    assert INSUFFICIENT_EVIDENCE_ANSWER in body
    assert '"abstention_reason": "citation_validation_failed"' in body
    assert '"citation_valid": false' in body
    assert persisted["abstained"] is True


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


def _prepared_code_answer(question: str) -> PreparedAnswer:
    prepared = _prepared_answer()
    source = replace(
        prepared.context.sources[0],
        excerpt="```bash\ncommand --check\n```",
        source_page_id="42",
        source_version="7",
        source_url="https://confluence.example/pages/42",
    )
    return replace(prepared, question=question, context=replace(prepared.context, sources=[source]))


def test_non_stream_exact_code_is_verified_and_persisted_with_provenance(monkeypatch) -> None:
    prepared = _prepared_code_answer("Приведи команду дословно.")
    persisted = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def chat(*_args, **_kwargs):
        return LLMOutput(text="[[ANSWER]]\n```sh\ncommand --check\n```\n[1]", model="test-model", finish_reason="stop")

    async def persist(*args, **kwargs):
        persisted.update(kwargs)
        persisted["citation_audit"] = args[4]
        return None, None

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route.llm, "chat", chat)
    monkeypatch.setattr(answer_route, "_persist_exchange", persist)
    response = asyncio.run(answer_route.answer(uuid.uuid4(), answer_route.AnswerRequest(question=prepared.question), None, None))

    assert response.abstained is False
    assert response.code_integrity.status == "exact_match"
    witness = response.code_integrity.blocks[0].witnesses[0]
    assert witness.source_index == 1
    assert witness.source_page_id == "42" and witness.source_version == "7"
    assert persisted["code_integrity"] == response.code_integrity
    metadata = answer_route._assistant_metadata(
        prepared, LLMOutput(response.answer, response.model, response.finish_reason),
        status="complete", sources=response.sources, citation_audit=persisted["citation_audit"],
        abstained=False, abstention_reason=None, code_integrity=response.code_integrity,
    )
    assert metadata["code_integrity"] == response.code_integrity.model_dump(mode="json")


def test_general_command_extract_is_unverified_but_not_withheld() -> None:
    prepared = _prepared_code_answer("Какая команда нужна для проверки?")
    source = replace(prepared.context.sources[0], excerpt="```bash\nsetup\ncommand --check\n```")
    resolved, citation, audit = answer_route._validate_generated_answer(
        answer_route.ResolvedAnswer("```bash\ncommand --check\n```\n[1]", False, None),
        answer_route._answer_sources([source]), prepared.question,
    )
    assert citation.valid is True
    assert audit.status == "unverified"
    assert resolved.abstained is False
    assert "command --check" in resolved.text


def test_explicit_literal_command_without_fence_is_withheld_despite_valid_citation() -> None:
    prepared = _prepared_code_answer("Приведи команду дословно.")
    resolved, citation, audit = answer_route._validate_generated_answer(
        answer_route.ResolvedAnswer("Выполни `invented --unsafe` [1].", False, None),
        answer_route._answer_sources(prepared.context.sources), prepared.question,
    )
    assert citation.valid is True
    assert audit.status == "no_code"
    assert resolved.abstained is True
    assert resolved.abstention_reason == "code_integrity_validation_failed"
    assert resolved.text == INSUFFICIENT_EVIDENCE_ANSWER


def test_explicit_literal_prose_quote_without_fence_is_not_withheld() -> None:
    prepared = _prepared_code_answer("Приведи описание статьи дословно.")
    text = "Описание статьи дословно [1]."
    resolved, citation, audit = answer_route._validate_generated_answer(
        answer_route.ResolvedAnswer(text, False, None),
        answer_route._answer_sources(prepared.context.sources), prepared.question,
    )
    assert citation.valid is True
    assert audit.status == "no_code"
    assert resolved.abstained is False
    assert resolved.text == text


def test_literal_command_model_abstention_without_fence_keeps_original_reason() -> None:
    prepared = _prepared_code_answer("Приведи команду дословно.")
    resolved, _citation, audit = answer_route._validate_generated_answer(
        answer_route.ResolvedAnswer(
            INSUFFICIENT_EVIDENCE_ANSWER, True, "model_reported_insufficient_context",
        ),
        answer_route._answer_sources(prepared.context.sources), prepared.question,
    )
    assert audit.status == "no_code"
    assert resolved.abstention_reason == "model_reported_insufficient_context"


def test_stream_exact_code_mismatch_is_withheld_before_tokens_and_persisted(monkeypatch) -> None:
    prepared = _prepared_code_answer("Приведи команду дословно.")
    persisted = {}

    async def prepare(*_args, **_kwargs):
        return prepared

    async def persist_user(*_args, **_kwargs):
        return None

    async def persist_assistant(*args, **kwargs):
        persisted.update(kwargs)
        persisted["content"] = args[1]

    async def stream_chat(*_args, **_kwargs):
        yield "[[ANSWER]]\n```bash\ncommand "
        yield "--unsafe\n```\n[1]"
        yield LLMStreamEnd("stop")

    monkeypatch.setattr(answer_route, "_prepare_answer", prepare)
    monkeypatch.setattr(answer_route, "_persist_stream_user", persist_user)
    monkeypatch.setattr(answer_route, "_persist_stream_assistant", persist_assistant)
    monkeypatch.setattr(answer_route.llm, "stream_chat", stream_chat)

    async def collect():
        response = await answer_route.answer_stream(uuid.uuid4(), answer_route.AnswerRequest(question=prepared.question), None, None)
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(collect())
    events = [json.loads(event.split("\ndata: ", 1)[1]) for event in body.strip().split("\n\n")]
    assert events[0]["code_integrity"] is None  # Validation is pending.
    assert "command --unsafe" not in body
    assert events[-1]["abstained"] is True
    assert events[-1]["abstention_reason"] == "code_integrity_validation_failed"
    assert events[-1]["finish_reason"] == "code_integrity_validation_failed"
    assert events[-1]["code_integrity"]["status"] == "unverified"
    assert events[-1]["code_integrity"]["blocks"][0]["witnesses"] == []
    assert persisted["content"] == INSUFFICIENT_EVIDENCE_ANSWER
    assert persisted["code_integrity"].status == "unverified"
