import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api.routes import answer
from app.core.config import settings
from app.services import conversation_memory as memory
from app.services.conversation_context import BranchMessage
from app.services.llm import LLMError, LLMOutput


def chain(*texts):
    result = []
    for i, text in enumerate(texts):
        result.append(BranchMessage(uuid4(), result[-1].id if result else None, "assistant" if i % 2 else "user", text))
    return result


def ok(summary):
    return LLMOutput(json.dumps({"summary": summary}, ensure_ascii=False), "fake", "stop")


@pytest.fixture
def checkpoints(monkeypatch):
    stored = {}

    async def latest(db, session, ids, scope):
        for anchor in reversed(ids):
            if (session, scope, anchor) in stored:
                return anchor, stored[session, scope, anchor]

    async def save(db, session, anchor, scope, summary):
        return stored.setdefault((session, scope, anchor), summary)

    monkeypatch.setattr(memory.history_checkpoint, "latest_checkpoint", latest)
    monkeypatch.setattr(memory.history_checkpoint, "save_checkpoint", save)
    monkeypatch.setattr(settings, "rag_history_messages", 2)
    monkeypatch.setattr(settings, "rag_history_max_chars", 200)
    return stored


@pytest.mark.asyncio
async def test_old_fact_rolls_into_checkpoint_recent_correction_stays_verbatim(checkpoints, monkeypatch):
    session = uuid4()
    rows = chain("Код ZK-918, дата 12 мая", "Запомнил.", "Осталось пять этапов.", "Верно.",
                 "Поправка: срок 2 июня.", "Принято.", "И ещё одна задача.", "Что дальше?")
    model = AsyncMock(side_effect=[ok("Проект ZK-918, срок 12 мая."), ok("Код ZK-918, срок перенесён на 2 июня.")])
    monkeypatch.setattr(memory.llm, "chat", model)
    initial = await memory.prompt_history(None, session, rows[:4], "general")
    assert "ZK-918" in initial[0].content
    assert [r.content for r in initial[1:]] == [r.content for r in rows[2:4]]

    cached = await memory.prompt_history(None, session, rows[:4], "general")
    carried = await memory.prompt_history(None, session, rows[:6], "general")
    assert cached == initial
    assert model.await_count == 1
    assert [r.content for r in carried[1:]] == [r.content for r in rows[2:6]]

    rolled = await memory.prompt_history(None, session, rows, "general")
    assert "12 мая" not in rolled[0].content and "2 июня" in rolled[0].content
    assert [r.content for r in rolled[1:]] == [r.content for r in rows[-2:]]
    prompt = model.await_args.args[0][-1]["content"]
    assert "Проект ZK-918" in prompt and "Поправка" in prompt and "Что дальше?" not in prompt


@pytest.mark.asyncio
async def test_sibling_ancestors_and_scopes_are_isolated(checkpoints, monkeypatch):
    w, session = uuid4(), uuid4()
    root = chain("Эту информацию можно помнить.", "Понятно.")
    def msg(parent, role, text, mode):
        return SimpleNamespace(id=uuid4(), parent_message_id=parent.id, role=role, content=text, metadata_={"retrieval_mode":mode})

    rows = [SimpleNamespace(**vars(r), metadata_={"retrieval_mode":"conversation"}) for r in root]
    a_user = msg(rows[-1], "user", "Секрет ветки А", "conversation")
    a = msg(a_user, "assistant", "Сведения А", "conversation")
    private = msg(rows[-1], "user", "Закрытый документ", "hybrid")
    b = msg(private, "assistant", "Индекс чата Б", "hybrid")
    b_user = msg(b, "user", "Общий Б", "conversation")
    end = msg(b_user, "assistant", "Последний Б", "conversation")
    rows.extend([a_user, a, private, b, b_user, end])

    async def generate(messages, **kwargs):
        assert "Секрет ветки А" not in messages[-1]["content"]
        return ok("Отдельная выжимка.")
    model = AsyncMock(side_effect=generate)
    monkeypatch.setattr(memory.llm, "chat", model)
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id=session, workspace_id=w)
    db.execute.return_value = Mock(all=lambda: rows)
    await answer._load_conversation_history(db, w, session, end.id)
    assert "Закрытый документ" in model.await_args.args[0][-1]["content"]
    projected = [SimpleNamespace(id=r.id, parent_message_id=r.parent_message_id, role=r.role,
                                 content=r.content if r.metadata_["retrieval_mode"] == "conversation" else "")
                 for r in rows]
    db.execute.return_value = Mock(all=lambda: projected)
    _, history, _ = await answer._load_conversation_history(db, w, session, end.id, general_only=True)
    assert model.await_count == 2
    assert "Закрытый документ" not in model.await_args.args[0][-1]["content"]
    assert all("Закрытый документ" not in row.content for row in history)
    assert (session, "grounded", b.id) in checkpoints and (session, "general", root[-1].id) in checkpoints


@pytest.mark.asyncio
async def test_oversized_turn_and_backlog_have_bounded_prompts(checkpoints, monkeypatch):
    session = uuid4()
    rows = chain(*(["Начало " + "z" * 12000 + " Конец"] * 8))
    model = AsyncMock(return_value=ok("Сведения из обработанной части."))
    monkeypatch.setattr(memory.llm, "chat", model)
    out = await memory.prompt_history(None, session, rows, "general")
    assert len(checkpoints) == model.await_count == memory.MAX_BATCHES
    for call in model.await_args_list:
        transcript = call.args[0][-1]["content"].split("Новые реплики:\n", 1)[1]
        assert len(transcript) <= memory.BATCH_CHARS
        assert "Начало" in transcript and "Конец" in transcript
    assert any("не вошла" in m.content for m in out)
    assert len(out[-1].content) == settings.rag_history_max_chars


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [LLMError("unavailable"), LLMOutput("not JSON", "fake", "stop"), ok("Я не могу помочь.")])
async def test_failure_preserves_cache_and_retries(checkpoints, monkeypatch, bad):
    session = uuid4()
    rows = chain(*[str(i) for i in range(12)])
    checkpoints[session,"grounded",rows[1].id] = "Ранее согласованная дата."
    model = AsyncMock(side_effect=[bad, ok("Обновлённые факты.")])
    monkeypatch.setattr(memory.llm, "chat", model)
    result = await memory.prompt_history(None, session, rows, "grounded")
    assert len(checkpoints) == 1 and "согласованная дата" in result[0].content
    assert any("не вошла" in m.content for m in result)
    result = await memory.prompt_history(None, session, rows, "grounded")
    assert "Обновлённые факты" in result[0].content and len(checkpoints) == 2


@pytest.mark.asyncio
async def test_small_conversation_no_checkpoint_or_extra_model_calls(monkeypatch):
    read = AsyncMock(side_effect=AssertionError("should not read checkpoint"))
    monkeypatch.setattr(memory.history_checkpoint, "latest_checkpoint", read)
    monkeypatch.setattr(memory.llm, "chat", AsyncMock(side_effect=AssertionError("unexpected LLM call")))
    result = await memory.prompt_history(None, uuid4(), chain("Привет", "Здравствуйте"), "general")
    assert [m.content for m in result] == ["Привет","Здравствуйте"]


@pytest.mark.asyncio
async def test_existing_summary_respects_a_reduced_context_budget(checkpoints, monkeypatch):
    session = uuid4()
    rows = chain(*[f"реплика {i}" for i in range(12)])
    checkpoints[session, "general", rows[1].id] = "a" * 1000
    monkeypatch.setattr(settings, "rag_history_summary_chars", 30)
    initial = await memory.prompt_history(None, session, rows[:4], "general")
    assert len(initial[0].content.split("\n", 1)[1]) <= 30

    model = AsyncMock(return_value=ok("Краткая выжимка."))
    monkeypatch.setattr(memory.llm, "chat", model)
    await memory.prompt_history(None, session, rows, "general")
    old_part = model.await_args.args[0][-1]["content"].split("Предыдущая выжимка:\n", 1)[1].split("\n\nНовые", 1)[0]
    assert len(old_part) <= 30
