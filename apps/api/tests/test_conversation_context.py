from app.services.conversation_context import (
    HistoryMessage,
    answer_history_messages,
    derive_conversation_title,
    rewrite_messages,
    trim_history,
)


def test_derive_conversation_title_normalizes_and_truncates() -> None:
    assert derive_conversation_title("  Что   такое МБД.Х?  ") == "Что такое МБД.Х?"
    title = derive_conversation_title("длинный вопрос " * 20, max_chars=40)
    assert len(title) <= 40
    assert title.endswith("…")


def test_empty_title_uses_default() -> None:
    assert derive_conversation_title("   ") == "Новый диалог"


def test_trim_history_keeps_newest_messages_in_order() -> None:
    messages = [
        HistoryMessage(role="user", content="Первый вопрос"),
        HistoryMessage(role="assistant", content="Первый ответ"),
        HistoryMessage(role="user", content="Второй вопрос"),
        HistoryMessage(role="assistant", content="Второй ответ"),
    ]
    result = trim_history(messages, max_messages=2, max_chars=1000)
    assert result == messages[-2:]


def test_trim_history_respects_character_budget() -> None:
    messages = [
        HistoryMessage(role="user", content="A" * 30),
        HistoryMessage(role="assistant", content="B" * 30),
    ]
    result = trim_history(messages, max_messages=8, max_chars=35)
    assert sum(len(item.content) for item in result) <= 35
    assert result[-1].role == "assistant"


def test_rewrite_prompt_requests_standalone_query() -> None:
    history = [
        HistoryMessage(role="user", content="Что такое МБД.Х?"),
        HistoryMessage(role="assistant", content="Это программно-аппаратный комплекс."),
    ]
    messages = rewrite_messages("Какие у неё преимущества?", history)
    assert messages[0]["role"] == "system"
    assert "самостоятельный поисковый запрос" in messages[0]["content"]
    assert "Какие у неё преимущества?" in messages[1]["content"]
    assert "Что такое МБД.Х?" in messages[1]["content"]


def test_answer_history_preserves_roles() -> None:
    history = [
        HistoryMessage(role="user", content="Вопрос"),
        HistoryMessage(role="assistant", content="Ответ"),
    ]
    assert answer_history_messages(history) == [
        {"role": "user", "content": "Вопрос"},
        {"role": "assistant", "content": "Ответ"},
    ]


def test_branch_history_uses_only_selected_ancestors() -> None:
    from app.services.conversation_context import BranchMessage, branch_history

    messages = [
        BranchMessage(id="u1", parent_message_id=None, role="user", content="Корень"),
        BranchMessage(id="a1", parent_message_id="u1", role="assistant", content="Ответ 1"),
        BranchMessage(id="u2", parent_message_id="a1", role="user", content="Ветка A"),
        BranchMessage(id="a2", parent_message_id="u2", role="assistant", content="Ответ A"),
        BranchMessage(id="u3", parent_message_id="a1", role="user", content="Ветка B"),
        BranchMessage(id="a3", parent_message_id="u3", role="assistant", content="Ответ B"),
    ]

    result = branch_history(messages, "a3", max_messages=10, max_chars=1000)
    assert [item.content for item in result] == ["Корень", "Ответ 1", "Ветка B", "Ответ B"]


def test_branch_history_stops_on_cycle() -> None:
    from app.services.conversation_context import BranchMessage, branch_history

    messages = [
        BranchMessage(id="a", parent_message_id="b", role="assistant", content="A"),
        BranchMessage(id="b", parent_message_id="a", role="user", content="B"),
    ]
    result = branch_history(messages, "a", max_messages=10, max_chars=1000)
    assert len(result) == 2
