from __future__ import annotations

import re

SIGNAL_TOKEN_RE = re.compile(
    r"[0-9A-Za-zА-Яа-яЁё]+(?:[._:/\-][0-9A-Za-zА-Яа-яЁё]+)*"
)
LEXICAL_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
NON_IDENTIFIER_CHARACTER = r"[^0-9a-zа-я]"
LEXICAL_STOP_WORDS = {
    "что",
    "такое",
    "какой",
    "какая",
    "какие",
    "какого",
    "какому",
    "каким",
    "каком",
    "который",
    "которая",
    "которые",
    "которого",
    "как",
    "для",
    "чего",
    "это",
    "есть",
    "или",
    "его",
    "ее",
    "её",
    "про",
    "при",
    "под",
    "над",
    "где",
    "когда",
    "зачем",
    "ли",
    "и",
    "а",
    "в",
    "во",
    "на",
    "с",
    "со",
    "к",
    "ко",
    "из",
    "от",
    "до",
    "по",
    "у",
}

LIVE_TIME_MARKERS = (
    "прямо сейчас",
    "в реальном времени",
    "живой телеметр",
    "сегодня",
    "за последн",
    "текущ",
    "right now",
    "real-time",
    "realtime",
    "live telemetry",
    "today",
    "current",
    "last ",
)
LIVE_STATE_MARKERS = (
    "загруз",
    "объем",
    "объём",
    "инцидент",
    "статус",
    "состояни",
    "телеметр",
    "метрик",
    "настрой",
    "выполн",
    "потер",
    "данн",
    "load",
    "incident",
    "status",
    "state",
    "telemetry",
    "metric",
    "config",
    "executed",
    "lost",
    "written",
)
PERSONAL_SCOPE_RE = re.compile(
    r"\b(?:мо(?:й|я|е|ё|его|ей|ю|их|им|ём|ем)|"
    r"наш(?:а|е|его|ей|у|и|их|им)|my|our)\b"
)
LIVE_INSTRUCTION_RE = re.compile(
    r"(?:\bкак\s+(?:посмотреть|проверить|узнать|получить|измерить)\b|"
    r"\bкак(?:ой|ую|им|ими)\s+команд|"
    r"\bhow\s+to\s+(?:check|view|get|measure)\b)"
)
EXACT_CODE_REQUEST_MARKERS = (
    "дословн",
    "точное содержим",
    "точный блок",
    "точную команд",
    "командой",
    "командами",
    "команду",
    "команды",
    "verbatim",
    "exact command",
    "exact configuration",
)


def requests_live_operational_state(query: str) -> bool:
    """Detect user-scoped live observations that static documents cannot prove.

    Procedural questions asking *how* to inspect a system are deliberately not
    blocked. The guard applies only when the requested answer itself is a current
    observation about the user's environment.
    """

    normalized = " ".join(query.casefold().replace("ё", "е").split())
    if LIVE_INSTRUCTION_RE.search(normalized):
        return False
    return (
        PERSONAL_SCOPE_RE.search(normalized) is not None
        and any(marker in normalized for marker in LIVE_TIME_MARKERS)
        and any(marker in normalized for marker in LIVE_STATE_MARKERS)
    )


def requests_exact_code(query: str) -> bool:
    """Return whether the user explicitly asks to reproduce code or commands."""

    normalized = " ".join(query.casefold().replace("ё", "е").split())
    return any(marker in normalized for marker in EXACT_CODE_REQUEST_MARKERS)


def build_disjunctive_lexical_query(query: str, *, limit: int = 32) -> str:
    """Build a safe, recall-oriented PostgreSQL web-search expression.

    A user's natural-language question is too restrictive when passed directly
    to ``websearch_to_tsquery`` because its terms are combined with AND. Split
    punctuation-heavy identifiers into searchable components and combine the
    remaining terms with OR; ranking still rewards chunks that cover more of
    the question.
    """

    tokens: list[str] = []
    seen: set[str] = set()
    for raw_token in LEXICAL_TOKEN_RE.findall(query):
        token = raw_token.casefold().replace("ё", "е")
        if len(token) < 2 or token in LEXICAL_STOP_WORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return " OR ".join(tokens)


def normalize_exact_text(value: str) -> str:
    """Normalize text for punctuation-insensitive exact identifier matching."""

    return "".join(character for character in value.casefold().replace("ё", "е") if character.isalnum())


def exact_identifier_pattern(identifier: str) -> str:
    """Build a PostgreSQL/Python-compatible, punctuation-tolerant exact pattern."""

    normalized = normalize_exact_text(identifier)
    if not normalized:
        raise ValueError("Exact identifier must contain at least one letter or digit")
    separated = f"{NON_IDENTIFIER_CHARACTER}*".join(
        re.escape(character) for character in normalized
    )
    return (
        f"(^|{NON_IDENTIFIER_CHARACTER})"
        f"{separated}"
        f"({NON_IDENTIFIER_CHARACTER}|$)"
    )


def contains_exact_identifier(value: str, identifier: str) -> bool:
    """Match an identifier across punctuation without accepting longer tokens."""

    normalized_value = value.casefold().replace("ё", "е")
    return re.search(exact_identifier_pattern(identifier), normalized_value) is not None


def extract_exact_identifiers(query: str, *, limit: int = 8) -> list[str]:
    """Extract high-precision identifiers without product-specific dictionaries.

    Codes containing digits (``BLUE-KB-942``, ``v2.4``), uppercase compound
    abbreviations (``МБД.Х``), and multi-part technical names
    (``ostor-agentd.env``) are reliable exact-match signals. Ordinary
    hyphenated prose, including mixed tokens such as ``S3-запросов``, is
    intentionally ignored.
    """

    identifiers: list[str] = []
    seen: set[str] = set()
    for raw_token in SIGNAL_TOKEN_RE.findall(query):
        normalized = normalize_exact_text(raw_token)
        if len(normalized) < 3:
            continue

        has_digit = any(character.isdigit() for character in raw_token)
        separator_count = sum(
            raw_token.count(marker) for marker in (".", "_", ":", "/", "-")
        )
        has_separator = separator_count > 0
        letters = "".join(character for character in raw_token if character.isalpha())
        uppercase_compound = has_separator and len(letters) >= 2 and letters.isupper()
        multipart_technical_name = separator_count >= 2
        has_lowercase_cyrillic = any(
            "а" <= character <= "я" or character == "ё" for character in raw_token
        )
        digit_identifier = has_digit and not (
            has_separator and has_lowercase_cyrillic and not uppercase_compound
        )
        if not (digit_identifier or uppercase_compound or multipart_technical_name):
            continue
        if normalized in seen:
            continue

        seen.add(normalized)
        identifiers.append(normalized)
        if len(identifiers) >= limit:
            break
    return identifiers
