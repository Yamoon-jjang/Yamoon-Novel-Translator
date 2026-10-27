"""v2.5.6: <br> from a web-copied TXT becomes a real line break in the result.

<br> is protected during translation (it comes back exactly), so the reading TXT and the
live result view must turn it into a line break instead of showing "<br>".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from novel_translator.rendering import html_line_breaks, render_snapshot
from novel_translator.protected_literals import protect_literals, restore_literals
from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks


@pytest.mark.parametrize('src,expected', [
    ('그녀가 웃었다.<br>그가 말했다.', '그녀가 웃었다.\n그가 말했다.'),
    ('그녀가 웃었다.<br/>그가 말했다.', '그녀가 웃었다.\n그가 말했다.'),
    ('그녀가 웃었다.<BR />그가 말했다.', '그녀가 웃었다.\n그가 말했다.'),
    ('그녀가 웃었다. <br> 그가 말했다.', '그녀가 웃었다.\n그가 말했다.'),
    ('그녀가 웃었다.<br>\n그가 말했다.', '그녀가 웃었다.\n그가 말했다.'),
    ('그녀가 웃었다.<br>\r\n그가 말했다.', '그녀가 웃었다.\n그가 말했다.'),
    ('그녀가 웃었다.<br><br>그가 말했다.', '그녀가 웃었다.\n\n그가 말했다.'),
    ('그녀가 웃었다.<br>', '그녀가 웃었다.'),
    ('<br>그녀가 웃었다.', '그녀가 웃었다.'),
    ('그녀가 웃었다.<br>\n', '그녀가 웃었다.'),
])
def test_br_becomes_line_break(src, expected):
    assert html_line_breaks(src) == expected


@pytest.mark.parametrize('text', [
    '', '평범한 문장.', '<찰싹> 소리가 났다.', '<b>굵게</b> 쓴 글', '<brother>', '<bring it on>',
    '[System] 레벨 업', 'a < b > c', '줄\n바꿈\n그대로', '\n\n앞뒤 줄바꿈\n\n',
])
def test_text_without_br_is_unchanged(text):
    assert html_line_breaks(text) is text or html_line_breaks(text) == text


def test_protected_br_round_trip_then_rendered(tmp_path):
    src = '他笑了。<br>她说：“走吧。”<br>\n天黑了。'
    protected = protect_literals(src)
    assert '<br>' not in protected.text   # the model never sees the tag
    assert restore_literals(protected.text, protected.mapping) == src
    assert html_line_breaks('그가 웃었다.<br>그녀가 말했다. "가자."<br>\n날이 저물었다.') == \
        '그가 웃었다.\n그녀가 말했다. "가자."\n날이 저물었다.'


def _render(tmp_path, source: str, translations: dict[int, str]):
    paragraphs = mark_translatable(split_paragraphs(source), 'zh')
    chunks = make_chunks(paragraphs, 4000)
    rows = []
    for c in chunks:
        body = '\n\n'.join(f'<P{p.pid:06d}>\n{translations.get(p.pid, p.text)}' for p in c.paragraphs)
        rows.append({'chunk_index': c.index, 'status': 'COMPLETED', 'source_text': c.source_text,
                     'translation': body, 'error_type': None, 'error_message': None})
    src_path = tmp_path / 'book.txt'
    src_path.write_text(source, encoding='utf-8')
    out, failed, report, _ = render_snapshot(chunks, rows, src_path, tmp_path, tmp_path / 'fb', paragraphs,
                                             korean_punct=True)
    return Path(out).read_text(encoding='utf-8-sig'), paragraphs


def test_render_snapshot_writes_line_breaks(tmp_path):
    source = '他笑了。<br>她说话了。\n\n第二段。<br><br>结束。'
    first, = [None]
    paragraphs = mark_translatable(split_paragraphs(source), 'zh')
    ids = [p.pid for p in paragraphs if p.translatable]
    tr = {ids[0]: '그가 웃었다.<br>그녀가 말했다.', ids[-1]: '둘째 문단.<br><br>끝.'}
    text, _ = _render(tmp_path, source, tr)
    assert '<br' not in text.lower()
    assert '그가 웃었다.\r\n그녀가 말했다.' in text or '그가 웃었다.\n그녀가 말했다.' in text
    assert '끝.' in text
