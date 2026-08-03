from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    content: str


def derive_conversation_title(question: str, *, max_chars: int = 80) -> str:
    title = re.sub(r"\s+", " ", question).strip()
    if not title:
        return "Новый диалог"
    if len(title) <= max_chars:
        return title
    return title[: max_chars - 1].rstrip(" ,.;:-") + "…"


def trim_history(
    messages: list[HistoryMessage],
    *,
    max_messages: int,
    max_chars: int,
) -> list[HistoryMessage]:
    if max_messages <= 0 or max_chars <= 0:
        return []

    selected: list[HistoryMessage] = []
    used_chars = 0
    for message in reversed(messages[-max_messages:]):
        content = re.sub(r"\s+", " ", message.content).strip()
        if not content or message.role not in {"user", "assistant"}:
            continue
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[:remaining].rstrip()
        selected.append(HistoryMessage(role=message.role, content=content))
        used_chars += len(content)
        if used_chars >= max_chars:
            break

    selected.reverse()
    return selected


def rewrite_messages(question: str, history: list[HistoryMessage]) -> list[dict[str, str]]:
    history_text = "\n".join(
        f"{('Пользователь' if item.role == 'user' else 'Ассистент')}: {item.content}"
        for item in history
    )
    return [
        {
            "role": "system",
            "content": (
                "Преобразуй последний вопрос в самостоятельный поисковый запрос. "
                "Разреши местоимения и ссылки на предыдущие сообщения. "
                "Не отвечай на вопрос, не добавляй пояснений и не используй кавычки. "
                "Верни только один самостоятельный запрос на языке пользователя."
            ),
        },
        {
            "role": "user",
            "content": f"ИСТОРИЯ:\n{history_text}\n\nПОСЛЕДНИЙ ВОПРОС:\n{question}",
        },
    ]


def answer_history_messages(history: list[HistoryMessage]) -> list[dict[str, str]]:
    return [{"role": item.role, "content": item.content} for item in history]
