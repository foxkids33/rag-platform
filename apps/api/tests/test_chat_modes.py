from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes import answer as route
from app.db.models import Document, Workspace
from app.services import article_summary as summaries
from app.services.llm import LLMOutput, LLMStreamEnd


def workspace():
    return Workspace(id=uuid4(), user_id="alice", tenant_id="tenant", source_mode="USER_DOCUMENTS")


def article(name):
    return Document(id=uuid4(), filename=name + ".md", title=name)


@pytest.mark.parametrize("question,count", [
    ("собери выжимку из трёх статей", 3),
    ("сделать сумаризацию по статье", 1),
    ("пожалуйста, сделай краткую выжимку", 1),
    ("собрать выживку из трех статей", 3),
])
def test_natural_summary_selection(question, count):
    docs = [article("Спутник " + str(i)) for i in range(count)]
    assert summaries.asks_for_summary(question)
    assert summaries.select_articles(question, docs) == docs
    assert summaries.select_articles(question, [*docs, article("Другой материал")]) == []
    assert summaries.select_articles("сделай резюме по статье «Спутник 0»", docs) == [docs[0]]
    assert not summaries.is_greeting("Привет, как работает система?")
    assert not summaries.asks_for_summary("Что означает слово суммаризация в документации?")


@pytest.mark.asyncio
async def test_json_and_stream_general_chat_and_greeting(monkeypatch):
    w = workspace()
    db = AsyncMock()
    db.scalar.return_value = False
    monkeypatch.setattr(route, "workspace_for_principal", AsyncMock(return_value=w))
    monkeypatch.setattr(route, "search", AsyncMock(side_effect=AssertionError("search in general chat")))
    monkeypatch.setattr(route.llm, "chat", AsyncMock(return_value=LLMOutput("28", "model", "stop")))

    async def stream(*args, **kwargs):
        yield "28"
        yield LLMStreamEnd("stop")

    monkeypatch.setattr(route.llm, "stream_chat", stream)
    for mode, question, expected in [("auto", "Чему равно четыре раза по семь?", "28"),
                                     ("general", "Чему равно 4 × 7?", "28"),
                                     ("sources", "Привет!", summaries.GREETING)]:
        request = route.AnswerRequest(question=question, chat_mode=mode)
        reply = await route.answer(w.id, request, db, None)
        assert reply.answer == expected
        assert not reply.abstained and reply.sources == []
        assert reply.retrieval_mode == "conversation" and reply.citation_valid

        streamed = await route.answer_stream(w.id, request, db, None)
        data = "".join([c async for c in streamed.body_iterator])
        assert expected in data and "event: done" in data and '"abstained": false' in data
    assert not await route._auto_general("Что сказано в этой документации?", w, db, None)


@pytest.mark.asyncio
async def test_general_history_excludes_questions_and_answers_from_grounded_exchanges():
    w, session = workspace(), uuid4()
    rows = []
    for mode, user, assistant in [("hybrid", "private request", "private answer"),
                                  ("conversation", "мой цвет", "синий")]:
        for role, content in [("user", user), ("assistant", assistant)]:
            rows.append(SimpleNamespace(
                id=uuid4(), parent_message_id=rows[-1].id if rows else None, role=role, content=content,
                metadata_={"retrieval_mode": mode},
            ))
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id=session, workspace_id=w.id)
    db.execute.return_value = Mock(scalars=lambda: rows)
    _, history, _ = await route._load_conversation_history(db, w.id, session, rows[-1].id, general_only=True)
    assert [msg.content for msg in history] == ["мой цвет", "синий"]


@pytest.mark.asyncio
async def test_selected_articles_have_fair_ordered_evidence_and_complete_citations(monkeypatch):
    w = workspace()
    docs = [article(name) for name in ("Альфа", "Бета", "Гамма")]
    rows = [SimpleNamespace(id=uuid4(), document_id=d.id, chunk_index=0, page_start=1, page_end=1,
                            text=f"НАЧАЛО {d.title}" + " A" * 2000 + " СЕРЕДИНА " + " Б" * 2000 + " КОНЕЦ")
            for d in docs]
    db = AsyncMock()
    db.scalars.return_value = Mock(all=lambda: docs)
    db.execute.return_value = Mock(all=lambda: rows)
    monkeypatch.setattr(route, "workspace_for_principal", AsyncMock(return_value=w))
    monkeypatch.setattr(route.settings, "rag_context_max_chars", 1200)
    request = route.AnswerRequest(question="выжимка", summary_document_ids=[d.id for d in docs[::-1]])
    prepared = await route._prepare_answer(w.id, request, db, None)
    assert prepared.context.char_count <= 1200 and prepared.excerpted_summary
    assert prepared.context.text.count("НАЧАЛО") == 3
    assert prepared.context.text.count("СЕРЕДИНА") == 3
    assert prepared.context.text.count("КОНЕЦ") == 3
    sources = route._answer_sources(prepared.context.sources)
    assert [s.document_id for s in sources] == request.summary_document_ids
    resolved, audit, _ = route._finish_generated("[[ANSWER]] Одна статья [1]", prepared, sources)
    assert not resolved.abstained and audit.valid and audit.cited_source_indices == [1, 2, 3]
    assert summaries.EXCERPT_NOTE in resolved.text
    resolved, audit, _ = route._finish_generated(
        "[[ANSWER]] Вывод А [1]. Б [2]. В [3].", replace(prepared, excerpted_summary=False), sources)
    assert "Вывод А" in resolved.text and audit.valid

    with pytest.raises(HTTPException) as missing:
        await summaries.summary_evidence(db, w, [uuid4()], "выжимка", 1200)
    assert missing.value.status_code == 404
    w.source_mode = "KNOWLEDGE_BASE"
    with pytest.raises(HTTPException) as disabled:
        await summaries.summary_evidence(db, w, [docs[0].id], "выжимка", 1200)
    assert disabled.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("repair_valid", [False, True])
async def test_short_multi_article_repair_is_bounded_and_hidden(monkeypatch, streaming, repair_valid):
    w = workspace()
    docs = [article(name) for name in ("Развёртывание", "Владельцы", "Проверка")]
    rows = [SimpleNamespace(id=uuid4(), document_id=d.id, chunk_index=0, text=f"Ключевой факт {i}",
                            page_start=None, page_end=None) for i, d in enumerate(docs, 1)]
    db = AsyncMock()
    db.scalars.return_value = Mock(all=lambda: docs)
    db.execute.return_value = Mock(all=lambda: rows)
    monkeypatch.setattr(route, "workspace_for_principal", AsyncMock(return_value=w))
    first = LLMOutput("[[ANSWER]] Черновая формулировка [1].", "model", "length")
    final = LLMOutput("[[ANSWER]] Главное А [1]. Главное Б [2]. Главное В [3].", "model", "stop")
    repaired = final if repair_valid else first
    chat = AsyncMock(side_effect=[repaired] if streaming else [first, repaired])
    monkeypatch.setattr(route.llm, "chat", chat)

    async def stream(*args, **kwargs):
        yield first.text
        yield LLMStreamEnd("length")

    monkeypatch.setattr(route.llm, "stream_chat", stream)
    payload = route.AnswerRequest(question="Сравни статьи", summary_document_ids=[d.id for d in docs], max_tokens=800)
    if streaming:
        res = await route.answer_stream(w.id, payload, db, None)
        body = "".join([event async for event in res.body_iterator])
        assert chat.await_count == 1 and "event: done" in body
    else:
        response = await route.answer(w.id, payload, db, None)
        body = response.answer
        assert chat.await_count == 2
        assert response.citation_valid and response.cited_source_indices == [1, 2, 3]
    assert chat.await_args.kwargs["max_tokens"] <= 512
    assert "Черновая формулировка" not in body
    assert ("Главное В [3]" if repair_valid else "Ключевые фрагменты выбранных материалов") in body


@pytest.mark.asyncio
async def test_cancelled_summary_starts_no_repair_and_saves_no_rejected_text(monkeypatch):
    import asyncio

    w = workspace()
    docs = [article("A"), article("B")]
    row = lambda d: SimpleNamespace(id=uuid4(), document_id=d.id, chunk_index=0, text=d.title,
                                    page_start=None, page_end=None)
    db = AsyncMock()
    db.scalars.return_value = Mock(all=lambda: docs)
    db.execute.return_value = Mock(all=lambda: [row(d) for d in docs])
    monkeypatch.setattr(route, "workspace_for_principal", AsyncMock(return_value=w))

    async def cancel(*args, **kwargs):
        yield "[[ANSWER]] Неполный черновик [1]"
        raise asyncio.CancelledError()

    repair = AsyncMock(side_effect=AssertionError("retry after disconnect"))
    save = AsyncMock()
    monkeypatch.setattr(route.llm, "chat", repair)
    monkeypatch.setattr(route.llm, "stream_chat", cancel)
    monkeypatch.setattr(route, "_persist_stream_assistant", save)
    payload = route.AnswerRequest(question="Сравни", summary_document_ids=[d.id for d in docs])
    response = await route.answer_stream(w.id, payload, db, None)
    emitted = []
    with pytest.raises(asyncio.CancelledError):
        async for event in response.body_iterator:
            emitted.append(event)
    assert not any("Неполный черновик" in event for event in emitted)
    assert save.await_args.args[1] == ""
    repair.assert_not_called()


@pytest.mark.parametrize("claim,quote", [
    ("Сотрудники проверяют состояние сервера.", "Эта цитата отсутствует в выбранной статье."),
    ("Всем пользователям разрешили вывод средств.", "Инженеры проверяют состояние сервера перед релизом."),
    ("Инженеры проверяют состояние 99 серверов.", "Инженеры проверяют состояние сервера перед релизом."),
    ("Инженеры не проверяют состояние сервера.", "Инженеры проверяют состояние сервера перед релизом."),
    ("[[ANSWER]] Инженеры проверяют состояние сервера.", "Инженеры проверяют состояние сервера перед релизом."),
    ("Инженеры проверяют состояние сервера [2].", "Инженеры проверяют состояние сервера перед релизом."),
])
def test_single_article_repair_rejects_unverified_claim(claim, quote):
    import json
    source = "Регламент. Инженеры проверяют состояние сервера перед релизом."
    assert summaries.verified_summary_claim(json.dumps({"summary": claim, "quote": quote}), source) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_single_article_repair_uses_verbatim_grounding_before_attaching_citation(monkeypatch, streaming):
    import json

    w = workspace()
    doc = article("Проверка")
    text = "Перед релизом инженеры проверяют состояние сервера."
    claim = "Инженеры проверяют состояние сервера перед релизом."
    db = AsyncMock()
    db.scalars.return_value = Mock(all=lambda: [doc])
    db.execute.return_value = Mock(all=lambda: [SimpleNamespace(id=uuid4(), document_id=doc.id,
                                   chunk_index=0, text=text, page_start=None, page_end=None)])
    monkeypatch.setattr(route, "workspace_for_principal", AsyncMock(return_value=w))
    provisional = LLMOutput("Черновик без ссылок", "model", "length")
    proof = LLMOutput(json.dumps({"summary": claim, "quote": text}, ensure_ascii=False), "model", "stop")
    chat = AsyncMock(side_effect=[proof] if streaming else [provisional, proof])
    monkeypatch.setattr(route.llm, "chat", chat)

    async def stream(*args, **kwargs):
        yield provisional.text
        yield LLMStreamEnd("length")

    monkeypatch.setattr(route.llm, "stream_chat", stream)
    payload = route.AnswerRequest(question="Сделай выжимку", summary_document_ids=[doc.id], max_tokens=800)
    if streaming:
        reply = await route.answer_stream(w.id, payload, db, None)
        body = "".join([event async for event in reply.body_iterator])
        assert chat.await_count == 1
    else:
        reply = await route.answer(w.id, payload, db, None)
        body = reply.answer
        assert reply.cited_source_indices == [1] and reply.citation_valid
        assert chat.await_count == 2
    assert claim + " [1]" in body and provisional.text not in body
    assert '"summary"' in chat.await_args.args[0][0]["content"]
    assert chat.await_args.kwargs["max_tokens"] <= 512


def test_extractive_summary_keeps_substantive_complete_sentences_from_across_article():
    intro = "Партнёр передаёт инженеру документацию для приёмки сборки."
    body = "Инженеры проверяют контрольную сумму образа перед испытаниями."
    end = "В итоговом отчёте менеджер фиксирует согласованные результаты."
    title = "Как сотрудники проводят приёмку новой сборки?"
    lines = [
        title, "Автор: Администратор портала управления проектами.",
        "Перейти к списку разделов корпоративного портала.", "![Снимок экрана](https://img.test/i)",
        intro, "[Макрос Jira: содержимое не извлечено]", intro, "```python",
        "Инженеры исполняют код, который нельзя цитировать в выжимке.", "```",
        body, "Это обрезанный фрагмент без завершающего знака", end,
    ]
    text = "\n".join(lines)
    chosen = summaries.representative_sentences(text, title)
    assert chosen == [intro, body, end]
    assert all(sentence in text for sentence in chosen)
    assert summaries.representative_sentences(intro + "\n" + intro) == []


@pytest.mark.asyncio
async def test_single_article_extractive_summary_keeps_one_verified_source(monkeypatch):
    doc, w, db = article("Приёмка"), workspace(), AsyncMock()
    text = ("Инженеры сравнивают контрольную сумму перед испытаниями.\n"
            "Менеджер регистрирует результат в рабочей карточке.")
    db.scalars.return_value = Mock(all=lambda: [doc])
    db.execute.return_value = Mock(all=lambda: [SimpleNamespace(id=uuid4(), document_id=doc.id, chunk_index=0,
                                                               text=text, page_start=None, page_end=None)])
    monkeypatch.setattr(route, "workspace_for_principal", AsyncMock(return_value=w))
    prepared = await route._prepare_answer(w.id, route.AnswerRequest(question="Выжимка", summary_document_ids=[doc.id]), db, None)
    assert "Тезис статьи 1 [1]." in prepared.messages[0]["content"] and "[2]" not in prepared.messages[0]["content"]
    resolved, audit, _ = route._finish_generated("Ответ без ссылок", prepared, route._answer_sources(prepared.context.sources))
    assert resolved.text.startswith("Выжимка из предложений статьи")
    assert all(sentence + " [1]" in resolved.text for sentence in text.splitlines())
    assert audit.valid and audit.cited_source_indices == [1]
