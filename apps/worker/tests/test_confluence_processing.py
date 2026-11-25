import hashlib
import json

import pytest

from app.confluence_processing import storage_blocks
from app.document_processing import parse_document


def macro(code: str, language: str = "bash") -> str:
    return ('<ac:structured-macro ac:name="code">'
            f'<ac:parameter ac:name="language">{language}</ac:parameter>'
            f'<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body>'
            '</ac:structured-macro>')


def test_code_cdata_is_verbatim_including_entities_blank_lines_and_fences():
    code = 'curl -I "https://example.test/?a=1&amp;b=2" \\\n  --header "X:  two spaces"\n\n\n```\n  value: true\n'
    blocks, diagnostics = storage_blocks(macro(code), "TLS")
    assert len(blocks) == 1
    assert blocks[0].kind == "code"
    assert blocks[0].text == f"````bash\n{code}\n````"
    assert diagnostics["code_sha256"] == [hashlib.sha256(code.encode()).hexdigest()]


def test_macro_parameters_do_not_pollute_text_and_hidden_explanations_survive():
    storage = '''<ac:structured-macro ac:name="aura-panel">
      <ac:parameter ac:name="styles">{"secret_style_noise":123}</ac:parameter>
      <ac:rich-text-body><p>Порядок работ</p>
        <ac:structured-macro ac:name="handy-tip"><ac:rich-text-body>
          <p>Важное пояснение TLS</p>
        </ac:rich-text-body></ac:structured-macro>
      </ac:rich-text-body></ac:structured-macro>'''
    blocks, _ = storage_blocks(storage, "Статья")
    text = "\n".join(b.text for b in blocks)
    assert "Важное пояснение TLS" in text
    assert "secret_style_noise" not in text


def test_ordered_steps_warning_and_visual_limitations():
    storage = '''<h2>Восстановление</h2>
      <ac:structured-macro ac:name="warning"><ac:rich-text-body>
        <p>Возможна потеря данных</p></ac:rich-text-body></ac:structured-macro>
      <ol start="3"><li>Резервная копия</li><li>Проверка результата</li></ol>
      <ac:image><ri:attachment ri:filename="screen.png"/></ac:image>
      <ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">x</ac:parameter></ac:structured-macro>'''
    blocks, diagnostics = storage_blocks(storage, "NS", ["KBR"])
    text = "\n".join(b.text for b in blocks)
    assert "WARNING:" in text and "потеря данных" in text
    assert text.index("3. Резервная копия") < text.index("4. Проверка результата")
    assert all(b.breadcrumb == "KBR > NS > Восстановление" for b in blocks)
    assert "images_not_recognized" in diagnostics["warnings"]
    assert "unresolved_macro:drawio" in diagnostics["warnings"]


def test_markup_and_entity_declarations_are_not_executed():
    blocks, _ = storage_blocks('<p>ok&nbsp;text</p><script>bad()</script>', 'T')
    assert "bad()" not in " ".join(b.text for b in blocks)
    with pytest.raises(ValueError, match="forbidden"):
        storage_blocks('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><p>&x;</p>', 'T')
    with pytest.raises(ValueError, match="refusing lossy"):
        storage_blocks('<p>broken', 'T')


def test_xml_declarations_inside_cdata_are_preserved_but_real_dtd_is_rejected():
    code = '<!DOCTYPE html>\n<!ENTITY example "literal">\n  <html />'
    blocks, _ = storage_blocks(macro(code), 'HTML example')
    assert blocks[0].text == f'```bash\n{code}\n```'
    with pytest.raises(ValueError, match='forbidden'):
        storage_blocks(macro(code) + '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]>', 'T')


def test_nested_inline_code_in_table_cannot_capture_neighboring_rows():
    nested = '<code>systemctl status<br/><strong>node: </strong><code>one</code><code> two</code></code>'
    storage = f'<table><tr><td>A</td><td>{nested}</td></tr><tr><td>B</td><td>{nested}</td></tr></table>'
    blocks, diagnostics = storage_blocks(storage, 'Nested code')
    code = [b.text for b in blocks if b.kind == 'code']
    assert code == ['```\nsystemctl status\nnode: one two\n```'] * 2
    assert len(diagnostics['code_sha256']) == 2
    assert all('[Строка таблицы]' not in b for b in code)
    inline, _ = storage_blocks('<p><code>cmd <code>--flag</code></code><code/></p>', 'T')
    assert inline[0].text == '`cmd --flag`'


def test_pre_with_br_preserves_line_breaks_once():
    blocks, _ = storage_blocks('<pre><code>one<br/>  two</code></pre>', 'T')
    assert blocks[0].text == '```\none\n  two\n```'


def test_heading_and_inline_formatting_cannot_change_code_boundaries():
    storage = '<h2>' + macro('first\n  exact') + '</h2><p><strong>' + macro('second') + '</strong></p>'
    blocks, diagnostics = storage_blocks(storage, 'T')
    assert [b.text for b in blocks if b.kind == 'code'] == ['```bash\nfirst\n  exact\n```', '```bash\nsecond\n```']
    assert len(diagnostics['code_sha256']) == 2


def test_confluence_chunker_keeps_oversized_code_atomic(monkeypatch):
    from app import document_processing

    monkeypatch.setattr(document_processing.settings, 'chunk_max_chars', 100)
    code = '  echo "Exact  spacing"\n' * 100
    payload = {"format": "confluence_storage_v1", "id": "42", "version": "7", "title": "Runbook", "storage_html": '<h1>Step</h1>' + macro(code)}
    chunks = parse_document('Runbook-42.confluence.json', json.dumps(payload).encode())
    containing = [chunk for chunk in chunks if code in chunk.text]
    assert len(containing) == 1
    assert containing[0].metadata['oversized_atomic_block']
    assert containing[0].metadata['source_page_id'] == '42'
    assert containing[0].metadata['source_version'] == '7'


def test_empty_landing_page_is_indexable_by_title():
    payload = {"format": "confluence_storage_v1", "title": "Инструкции", "storage_html": ""}
    chunks = parse_document('landing.confluence.json', json.dumps(payload).encode())
    assert chunks[0].text == 'Инструкции'


@pytest.mark.parametrize('wrapper', [
    '<table><tbody><tr><td>Команда</td><td>{}</td></tr></tbody></table>',
    '<ol><li>{}</li></ol>',
])
def test_code_inside_tables_and_lists_stays_atomic(wrapper):
    code = 'curl https://example.test \\\n  --head\n\n  spaces'
    blocks, _ = storage_blocks(wrapper.format(macro(code)), 'Nested')
    assert sum(block.kind == 'code' and code in block.text for block in blocks) == 1


def test_heading_paths_follow_html_levels_when_first_or_intermediate_level_is_missing():
    html = "<h2>Requirements</h2><h4>CPU</h4><h4>Memory</h4><h2>Verification</h2>"
    blocks, _ = storage_blocks(html, "Setup", ["Guide"])
    assert [block.breadcrumb for block in blocks] == [
        "Guide > Setup > Requirements",
        "Guide > Setup > Requirements > CPU",
        "Guide > Setup > Requirements > Memory",
        "Guide > Setup > Verification",
    ]
