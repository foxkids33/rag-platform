import uuid

from app.api.routes.search import (
    SearchResult,
    _boilerplate_penalty,
    _build_rerank_document,
    _candidate_limit,
    _clean_rerank_text,
    _focused_excerpt,
    _rerank_candidate_limit,
    _rerank_fusion_score,
    _reranked_result_sort_key,
    _vector_literal,
)
from app.services.query_signals import (
    build_disjunctive_lexical_query,
    contains_exact_identifier,
    extract_exact_identifiers,
    normalize_exact_text,
    requests_exact_code,
    requests_live_operational_state,
)


def test_candidate_limit_has_retrieval_headroom() -> None:
    assert _candidate_limit(1) == 40
    assert _candidate_limit(8) == 64
    assert _candidate_limit(20) == 160


def test_rerank_candidate_limit_is_bounded_by_retrieval_pool() -> None:
    assert _rerank_candidate_limit(5, 40) == 20
    assert _rerank_candidate_limit(20, 20) == 20


def test_vector_literal_is_pgvector_compatible() -> None:
    assert _vector_literal([0.5, -1.25, 0.0]) == "[0.5,-1.25,0]"


def test_rerank_document_prefers_breadcrumb_and_child_text() -> None:
    result = SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="doc.pdf",
        chunk_index=1,
        text="Direct answer",
        parent_text="Very large parent context",
        parent_text_truncated=False,
        heading="Section",
        heading_breadcrumb="Chapter > Section",
        page_start=2,
        page_end=2,
        score=0.7,
        dense_score=0.7,
        lexical_score=0.2,
        rrf_score=0.03,
        dense_rank=1,
        lexical_rank=2,
        retrieval_rank=1,
        rerank_score=None,
    )

    assert _build_rerank_document(result, "What is it?") == (
        "Chapter > Section\n\nDirect answer"
    )


def test_clean_rerank_text_removes_pdf_boilerplate_without_domain_title_rules() -> None:
    source = """
Машина больших данных Скала^р МБД.Х. Технический обзор

2 из 50

ОГЛАВЛЕНИЕ
Введение .......................................................... 10
info@skala-r.ru

МБД.Х — это программно-аппаратный комплекс для хранения данных.
"""

    cleaned = _clean_rerank_text(source)

    assert "Технический обзор" in cleaned
    assert "МБД.Х — это программно-аппаратный комплекс для хранения данных." in cleaned
    assert "2 из 50" not in cleaned
    assert "ОГЛАВЛЕНИЕ" not in cleaned


def test_exact_identifiers_are_domain_agnostic_and_punctuation_insensitive() -> None:
    assert extract_exact_identifiers("Сравни BLUE-KB-942 и МБД.Х с версией v2.4") == [
        "bluekb942",
        "мбдх",
        "v24",
    ]
    assert normalize_exact_text("Blue_KB.942") == "bluekb942"
    assert extract_exact_identifiers("программно-аппаратный комплекс") == []
    assert extract_exact_identifiers("страница S3-запросов") == []
    assert extract_exact_identifiers("содержимое ostor-agentd.env") == [
        "ostoragentdenv"
    ]
    assert contains_exact_identifier("Код blue_kb.942 зарегистрирован", "bluekb942")
    assert not contains_exact_identifier("Код BLUE-KB-9421 зарегистрирован", "bluekb942")


def test_disjunctive_lexical_query_splits_identifiers_and_removes_stop_words() -> None:
    assert build_disjunctive_lexical_query(
        "Что показано в ostor-agentd.env для multimount?"
    ) == "показано OR ostor OR agentd OR env OR multimount"


def test_live_state_detection_separates_observation_from_instructions() -> None:
    assert requests_live_operational_state(
        "Какова текущая загрузка CPU каждого узла моего кластера прямо сейчас?"
    )
    assert requests_live_operational_state(
        "Какой объём был записан в мою корзину за последние пять минут?"
    )
    assert requests_live_operational_state(
        "Назови последний инцидент сегодня в моей системе мониторинга и его статус."
    )
    assert requests_live_operational_state(
        "Подтверди по текущему состоянию моего стенда, что данные не потеряны."
    )
    assert not requests_live_operational_state(
        "Как проверить текущее состояние моего кластера?"
    )
    assert not requests_live_operational_state(
        "Какой командой посмотреть текущие настройки алиасов?"
    )


def test_exact_code_request_detection_is_explicit() -> None:
    assert requests_exact_code("Приведи обе команды дословно")
    assert requests_exact_code("Какой командой посмотреть настройки?")
    assert requests_exact_code("Приведи точное содержимое файла")
    assert not requests_exact_code("Какие пакеты нужны для установки?")


def test_focused_excerpt_prefers_definition_over_unrelated_text() -> None:
    source = (
        "Техническая поддержка предоставляется круглосуточно.\n\n"
        + "Шумный раздел. " * 200
        + "\n\nМБД.Х — это программно-аппаратный комплекс, предназначенный "
        "для создания централизованного хранилища данных.\n\n"
        + "Дополнительные сведения. " * 200
    )

    excerpt = _focused_excerpt(
        source,
        "Что такое машина больших данных Скала МБД.Х?",
        500,
    )

    assert "МБД.Х — это программно-аппаратный комплекс" in excerpt
    assert len(excerpt) <= 500


def test_boilerplate_penalty_marks_title_and_toc_chunk() -> None:
    result = SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="doc.txt",
        chunk_index=0,
        text=(
            "ОГЛАВЛЕНИЕ\n"
            "Введение ........................................ 10\n"
            "Архитектура ..................................... 20\n"
            "Поддержка ........................................ 40"
        ),
        parent_text=None,
        parent_text_truncated=False,
        heading=None,
        heading_breadcrumb=None,
        page_start=None,
        page_end=None,
        score=0.5,
        dense_score=0.5,
        lexical_score=None,
        rrf_score=0.01,
        dense_rank=10,
        lexical_rank=None,
        retrieval_rank=9,
    )

    assert _boilerplate_penalty(result) == 0.55


def test_rank_fusion_prefers_retrieval_backed_definition_over_toc() -> None:
    title_score = _rerank_fusion_score(
        rerank_rank=1,
        retrieval_rank=9,
        penalty=0.55,
    )
    definition_score = _rerank_fusion_score(
        rerank_rank=2,
        retrieval_rank=1,
        penalty=0.0,
    )

    assert definition_score > title_score


def test_reranker_sort_keeps_exact_identifier_candidate() -> None:
    exact = SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="exact.txt",
        chunk_index=0,
        text="BLUE-KB-942",
        parent_text=None,
        parent_text_truncated=False,
        heading=None,
        heading_breadcrumb=None,
        page_start=None,
        page_end=None,
        score=1.0,
        dense_score=0.2,
        lexical_score=None,
        exact_score=1.0,
        rrf_score=0.05,
        dense_rank=20,
        lexical_rank=None,
        exact_rank=1,
        retrieval_rank=2,
        rerank_score=0.1,
        rerank_rank=2,
        rerank_fusion_score=0.01,
    )
    noise = exact.model_copy(
        update={
            "chunk_id": uuid.uuid4(),
            "filename": "noise.txt",
            "exact_score": None,
            "exact_rank": None,
            "rerank_score": 0.99,
            "rerank_rank": 1,
            "rerank_fusion_score": 0.03,
            "retrieval_rank": 1,
        }
    )

    assert min([noise, exact], key=_reranked_result_sort_key) is exact
