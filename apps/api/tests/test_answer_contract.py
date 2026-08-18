from app.services.answer_contract import (
    ABSTAIN_MARKER,
    ANSWER_MARKER,
    INSUFFICIENT_EVIDENCE_ANSWER,
    AbstentionStreamFilter,
    resolve_generated_answer,
)


def test_abstain_marker_preserves_contextual_explanation() -> None:
    resolved = resolve_generated_answer(
        f"  {ABSTAIN_MARKER}\nТочное значение не указано [2]."
    )

    assert resolved.text == "Точное значение не указано [2]."
    assert resolved.abstained is True
    assert resolved.abstention_reason == "model_reported_insufficient_context"
    assert resolved.marker_used is True
    assert resolved.has_citations is True


def test_marker_only_resolves_to_deterministic_abstention() -> None:
    resolved = resolve_generated_answer(ABSTAIN_MARKER)

    assert resolved.text == INSUFFICIENT_EVIDENCE_ANSWER
    assert resolved.abstained is True
    assert resolved.has_citations is False


def test_trailing_abstain_marker_is_authoritative_and_removed() -> None:
    text = f"Точное значение в контексте отсутствует [1].\n{ABSTAIN_MARKER}"

    resolved = resolve_generated_answer(text)

    assert resolved.text == "Точное значение в контексте отсутствует [1]."
    assert resolved.abstained is True
    assert resolved.marker_used is True
    assert ABSTAIN_MARKER not in resolved.text


def test_legacy_refusal_prefix_remains_machine_readable() -> None:
    text = "В предоставленном контексте нет информации о сроке гарантии."

    resolved = resolve_generated_answer(text)

    assert resolved.text == text
    assert resolved.abstained is True
    assert resolved.marker_used is False


def test_supported_answer_is_not_misclassified_when_secondary_detail_is_missing() -> None:
    text = "Основной срок — 12 месяцев [1]. Точный день начала не указан."

    resolved = resolve_generated_answer(text)

    assert resolved.text == text
    assert resolved.abstained is False
    assert resolved.abstention_reason is None


def test_cited_partial_answer_is_not_misclassified_as_legacy_refusal() -> None:
    text = (
        f"{ANSWER_MARKER}\nВ предоставленном контексте нет точной даты, "
        "но продукт использует Picodata [2]."
    )

    resolved = resolve_generated_answer(text)

    assert resolved.text == (
        "В предоставленном контексте нет точной даты, "
        "но продукт использует Picodata [2]."
    )
    assert resolved.abstained is False
    assert resolved.abstention_reason is None
    assert resolved.marker_used is True


def test_cited_legacy_refusal_without_answer_marker_is_an_abstention() -> None:
    text = "В контексте нет точного значения; оно зависит от параметров ЦОД [2]."

    resolved = resolve_generated_answer(text)

    assert resolved.text == text
    assert resolved.abstained is True
    assert resolved.has_citations is True


def test_stream_filter_hides_split_leading_marker_and_preserves_explanation() -> None:
    stream_filter = AbstentionStreamFilter()

    visible = [
        stream_filter.feed("  [[ABS"),
        stream_filter.feed("TAIN]]"),
        stream_filter.feed("Точное значение не указано [1]."),
        stream_filter.finish(),
    ]

    assert "".join(visible).strip() == "Точное значение не указано [1]."
    assert stream_filter.marker_detected is True
    assert stream_filter.control_marker == ABSTAIN_MARKER
    assert ABSTAIN_MARKER in stream_filter.raw_text


def test_stream_filter_removes_trailing_marker_split_across_tokens() -> None:
    stream_filter = AbstentionStreamFilter()

    visible = [
        stream_filter.feed("Точное значение не указано [1]. [[ABS"),
        stream_filter.feed("TAIN]]"),
        stream_filter.finish(),
    ]

    assert "".join(visible) == "Точное значение не указано [1]. "
    assert stream_filter.control_marker == ABSTAIN_MARKER
    assert ABSTAIN_MARKER not in "".join(visible)


def test_stream_filter_removes_answer_marker_and_continues_streaming() -> None:
    stream_filter = AbstentionStreamFilter()

    visible = [
        stream_filter.feed("[[ANS"),
        stream_filter.feed("WER]]Подтверждённый факт [1]."),
        stream_filter.finish(),
    ]

    assert "".join(visible) == "Подтверждённый факт [1]."
    assert stream_filter.control_marker == ANSWER_MARKER


def test_stream_filter_only_delays_a_normal_bracket_prefix() -> None:
    stream_filter = AbstentionStreamFilter()

    visible = [
        stream_filter.feed("["),
        stream_filter.feed("1] Подтверждённый факт"),
        stream_filter.feed(" продолжается."),
        stream_filter.finish(),
    ]

    assert "".join(visible) == "[1] Подтверждённый факт продолжается."
    assert stream_filter.marker_detected is False
