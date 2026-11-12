import hashlib
from dataclasses import dataclass

import pytest

from app.services.code_integrity import audit_code_integrity, requires_exact_code_integrity


@dataclass(frozen=True)
class Source:
    excerpt: str
    index: int = 1
    source_page_id: str | None = "page-42"
    source_version: str | None = "7"
    source_url: str | None = "https://confluence.example/pages/42"


def test_full_block_match_has_exact_hash_and_all_source_witnesses() -> None:
    code = 'tool --header "X:  two spaces"\n  --check'
    answer = f"```sh\n{code}\n```\n[1]"
    sources = [
        Source(f"Before\n```bash\n{code}\n```\nAfter"),
        Source(f"~~~shell\n{code}\n~~~", index=3, source_page_id="page-99", source_version="2"),
    ]
    audit = audit_code_integrity(answer, sources)

    assert audit.status == "exact_match"
    assert audit.total_block_count == audit.verified_block_count == 1
    block = audit.blocks[0]
    assert block.index == 1
    assert block.sha256 == hashlib.sha256(code.encode()).hexdigest()
    assert [witness.source_index for witness in block.witnesses] == [1, 3]
    assert block.witnesses[1].source_page_id == "page-99"
    assert block.witnesses[1].source_version == "2"
    assert block.witnesses[0].source_url == sources[0].source_url


def test_only_crlf_and_fence_separator_newline_are_normalized() -> None:
    code = 'config:\n  value: "ABC"\n\n'
    source = Source(f"```yaml\n{code}\n```\n".replace("\n", "\r\n"))
    assert audit_code_integrity(f"~~~\n{code}\n~~~", [source]).status == "exact_match"
    # A blank line belongs to the configuration; it is not stripped.
    assert audit_code_integrity('```yaml\nconfig:\n  value: "ABC"\n```', [source]).status == "unverified"


@pytest.mark.parametrize("changed", [
    "tool --unsafe\nsecond-command",
    "TOOL --check\nsecond-command",
    "tool  --check\nsecond-command",
    "tool --check\n second-command",
    "second-command\ntool --check",
    "tool --check\nsecond-command\nextra-command",
])
def test_parameters_case_whitespace_order_and_extra_commands_cannot_match(changed: str) -> None:
    audit = audit_code_integrity(
        f"```bash\n{changed}\n```",
        [Source("```bash\ntool --check\nsecond-command\n```")],
    )
    assert audit.status == "unverified"
    assert audit.verified_block_count == 0
    assert audit.blocks[0].witnesses == []


def test_contiguous_extract_is_not_a_full_source_block() -> None:
    audit = audit_code_integrity(
        "```bash\ntool --check\n```",
        [Source("```bash\nsetup-command\ntool --check\nteardown-command\n```")],
    )
    assert audit.status == "unverified"


def test_combining_different_sources_or_blocks_is_not_a_witness() -> None:
    answer = "```bash\nfirst-command\nsecond-command\n```"
    sources = [Source("```bash\nfirst-command\n```"), Source("```bash\nsecond-command\n```", index=2)]
    assert audit_code_integrity(answer, sources).status == "unverified"
    assert audit_code_integrity(answer, [Source("\n\n".join(s.excerpt for s in sources))]).status == "unverified"


def test_mixed_output_audits_every_block_without_repeating_duplicate_witnesses() -> None:
    source = Source("```bash\nverified-command\n```\n\n```bash\nverified-command\n```")
    answer = "```bash\nverified-command\n```\n\n```bash\nother-command\n```"
    audit = audit_code_integrity(answer, [source])
    assert audit.status == "unverified"
    assert audit.total_block_count == 2
    assert audit.verified_block_count == 1
    assert [block.index for block in audit.blocks] == [1, 2]
    assert len(audit.blocks[0].witnesses) == 1
    assert audit.blocks[1].witnesses == []


def test_longer_fence_preserves_nested_short_fences_as_code() -> None:
    content = 'example = "```"\n```\ninside'
    source = Source(f"````text\n{content}\n````")
    assert audit_code_integrity(f"````\n{content}\n`````", [source]).status == "exact_match"


@pytest.mark.parametrize("unfinished", [
    "```bash\ntool --check",
    "```bash\ntool --check\n",
    "````bash\ntool --check\n```",
])
def test_incomplete_answer_fences_cannot_be_verified(unfinished: str) -> None:
    audit = audit_code_integrity(unfinished, [Source("```bash\ntool --check\n```")])
    assert audit.total_block_count == 1
    assert audit.status == "unverified"


def test_incomplete_source_or_plain_prose_cannot_be_a_witness() -> None:
    answer = "```bash\ntool --check\n```"
    for source in [Source("```bash\ntool --check\n"), Source("tool --check")]:
        assert audit_code_integrity(answer, [source]).status == "unverified"


def test_no_fenced_code_has_no_code_status_not_verified_status() -> None:
    audit = audit_code_integrity("A prose answer with `inline code` [1].", [Source("```\ncommand\n```")])
    assert audit.model_dump() == {
        "status": "no_code", "total_block_count": 0, "verified_block_count": 0, "blocks": [],
    }


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\v", "\f"])
def test_unicode_separators_do_not_create_fence_line_boundaries(separator: str) -> None:
    source = Source("```bash\ncommand --check\n```")
    # The frontend treats this as a fence info string, not as a command line.
    answer = f"```bash{separator}command --check\n```"
    audit = audit_code_integrity(answer, [source])
    assert audit.status == "unverified"
    assert audit.total_block_count == 1
    assert audit.verified_block_count == 0
    assert audit.blocks[0].sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\v", "\f"])
def test_unicode_separators_within_code_remain_significant_text(separator: str) -> None:
    code = f'printf "first{separator}second"'
    source = Source(f"```bash\n{code}\n```")
    audit = audit_code_integrity(f"```bash\n{code}\n```", [source])
    assert audit.status == "exact_match"
    assert audit.blocks[0].sha256 == hashlib.sha256(code.encode()).hexdigest()
    changed = code.replace(separator, "\n")
    assert audit_code_integrity(f"```bash\n{changed}\n```", [source]).status == "unverified"


@pytest.mark.parametrize("question", [
    "Приведи команды дословно.",
    "Покажи точное содержимое configuration.yaml",
    "Show the exact command from the article.",
])
def test_explicit_literal_request_enables_strict_contract(question: str) -> None:
    assert requires_exact_code_integrity(question)


@pytest.mark.parametrize("question", [
    "Какой командой проверить состояние сервиса?",
    "Как адаптировать пример к моему адресу?",
    "Explain this configuration.",
])
def test_general_command_questions_allow_reported_extracts(question: str) -> None:
    assert not requires_exact_code_integrity(question)
