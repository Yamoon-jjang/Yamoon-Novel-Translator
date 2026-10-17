"""PDF and EPUB: text taken out on opening, translated like a TXT, saved as .ko.txt by
default and as a reading PDF/EPUB only from [결과 저장]."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

import novel_translator.ui.main_window as ui
from novel_translator.pdf_text import clean_pdf_pages, extract_pdf_text, is_chapter_title, PdfNeedsOcrError, PdfTextError
from novel_translator.epub_text import extract_epub_text, EpubNoTextError, EpubTextError
from novel_translator.source_formats import document_kind, NoTextError, SourceTextError
from test_alpha17 import qt_app, sandbox, _window, _fake_worker_class, _wait  # noqa: F401,F811


# ---------------------------------------------------------------- helpers

def _qt_pdf(path: Path, pages: list[list[str]], header: str = '', numbers: bool = True, family: str = '') -> Path:
    """A PDF laid out like a printed book: running header, body lines, page number."""
    from PySide6.QtGui import QPdfWriter, QPainter, QFont, QPageSize
    w = QPdfWriter(str(path)); w.setPageSize(QPageSize(QPageSize.PageSizeId.A5)); w.setResolution(72)
    p = QPainter(w)
    font = QFont(family) if family else QFont(); font.setPointSize(10); p.setFont(font)
    for i, lines in enumerate(pages):
        if i: w.newPage()
        if header: p.drawText(150, 30, header)
        y = 70
        for line in lines:
            p.drawText(30, y, line); y += 15
        if numbers: p.drawText(200, 570, str(i + 1))
    p.end()
    return path


def _picture_pdf(path: Path, pages: int = 3) -> Path:
    from PySide6.QtGui import QPdfWriter, QPainter, QPageSize, QColor
    w = QPdfWriter(str(path)); w.setPageSize(QPageSize(QPageSize.PageSizeId.A5)); w.setResolution(72)
    p = QPainter(w)
    for i in range(pages):
        if i: w.newPage()
        p.fillRect(20, 20, 300, 500, QColor(120, 120, 120))   # a "scanned page": no text at all
    p.end()
    return path


def _cjk_family() -> str:
    from PySide6.QtGui import QFontDatabase
    fams = QFontDatabase.families(QFontDatabase.WritingSystem.SimplifiedChinese)
    return fams[0] if fams else ''


def _epub(path: Path, pages: dict[str, str], spine: list[str], encryption: str = '') -> Path:
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        items = ''.join(f'<item id="{n.split(".")[0]}" href="{n}" media-type="application/xhtml+xml"/>' for n in pages)
        refs = ''.join(f'<itemref idref="{n.split(".")[0]}"/>' for n in spine)
        z.writestr('OPS/book.opf', '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   f'<metadata/><manifest>{items}</manifest><spine>{refs}</spine></package>')
        for n, body in pages.items():
            z.writestr(f'OPS/{n}', f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f'<head><title>무시</title><style>p{{color:red}}</style></head><body>{body}</body></html>')
        if encryption:
            z.writestr('META-INF/encryption.xml', encryption)
    return path


# ---------------------------------------------------------------- PDF text clean-up (no PDF needed)

_ZH_LINES = ['他站在城墙上，望着远处的群山，心中百感交集。风从北方吹来，', '带着雪的气息，也带着战争的味道。这一夜，注定无人入眠。',
             '她推开窗，看见院子里的梅花开了，香气一阵阵地飘进屋里，', '让人想起很多年前的那个冬天。那时候他们都还很年轻。',
             '远处传来马蹄声，越来越近，最后停在了门外。有人敲门，', '敲了三下，又停住了。屋里的人谁也没有动，都屏住了呼吸。',
             '灯花爆了一下。老人慢慢站起来，扶着桌子走到门边，', '问了一句是谁。门外没有回答，只有风吹过树梢的声音。',
             '第二天早上，雪停了。院子里留下一行脚印，一直通到', '后山的小路上。谁也不知道那个人是什么时候走的。']


def test_running_headers_page_numbers_and_broken_lines_are_cleaned():
    pages = []
    for n in range(5):
        a, b = _ZH_LINES[2 * n], _ZH_LINES[2 * n + 1]
        head = ['一阵风吹过。'] if n else []          # the end of the paragraph cut by the previous page
        pages.append('\n'.join(['星河长歌', f'- {n + 1} -'] + head + ['\u3000' + a, b, f'\u3000“第{n + 1}次了吗？”她轻声问道。',
                                '\u3000他沉默了很久，才慢慢开口说话，声音低得几乎听不见，像是' if n < 4 else '\u3000完。']))
    text, removed = clean_pdf_pages(pages)
    paras = text.strip().split('\n\n')
    assert '星河长歌' not in text and not re.search(r'- \d -', text) and removed == 10
    assert paras[0] == _ZH_LINES[0] + _ZH_LINES[1]
    # a paragraph cut by the page end continues on the next page (no paragraph break there)
    assert paras[2] == '他沉默了很久，才慢慢开口说话，声音低得几乎听不见，像是一阵风吹过。'
    assert paras[3] == _ZH_LINES[2] + _ZH_LINES[3]
    assert sum(p.startswith('“第') for p in paras) == 5 and paras[-1] == '完。'


def test_chapter_titles_stay_on_their_own_line_even_when_repeated_as_header():
    pages = [
        '第三章 风起\n　风起了。他推开门，看见一个陌生人站在院子里，手里提着一盏灯。\n　“你是谁？”',
        '第三章 风起\n　那人没有回答，只是看着他。月光很亮，照在两个人的脸上。\n　他退了一步。',
        '第三章 风起\n　灯灭了。院子里忽然安静下来，只剩下风吹过树叶的声音。\n　他屏住呼吸。',
        '第三章 风起\n　过了很久，那人才开口说话。声音沙哑，像是很久没有说过话。\n　“我来找你。”',
    ]
    text, _ = clean_pdf_pages(pages)
    paras = text.strip().split('\n\n')
    assert paras[0] == '第三章 风起' and text.count('第三章 风起') == 1   # the heading stays once, the running header goes
    assert is_chapter_title('第一章 出征') and is_chapter_title('Chapter 12') and is_chapter_title('제3장 귀환')
    assert not is_chapter_title('第三章他走了很远的路，终于来到了海边的小镇上。')


def test_english_lines_are_joined_with_spaces_and_hyphenation_undone():
    page = ('He stood on the city wall, looking at the distant moun-\n'
            'tains, his heart full of mixed feelings. The wind blew from\n'
            'the north.\n'
            '"Are you leaving?" she asked.\n')
    text, _ = clean_pdf_pages([page])
    assert text.strip().split('\n\n') == [
        'He stood on the city wall, looking at the distant mountains, his heart full of mixed feelings. The wind blew from the north.',
        '"Are you leaving?" she asked.']


def test_spaced_out_cjk_is_rejoined_but_real_spaces_stay():
    text, _ = clean_pdf_pages(['这 是 一 个 很 长 的 句 子 。\n第一章 出征'])
    assert '这是一个很长的句子。' in text and '第一章 出征' in text


def test_repeated_dialogue_at_page_edges_is_not_taken_for_a_header():
    pages = [f'　他走进屋里，坐在桌边，把灯拨亮了一些，又看了一眼窗外的夜色。{i}\n　“好。”' for i in range(6)]
    text, removed = clean_pdf_pages(pages)
    assert text.count('“好。”') == 6 and removed == 0


# ---------------------------------------------------------------- real PDF files

def test_pdf_file_text_is_taken_out_and_cleaned(qt_app, tmp_path):
    body = [['He stood on the city wall, looking at the distant mountains, his',
             'heart full of mixed feelings. The wind blew from the north and',
             'carried the smell of snow.',
             f'"Are you really leaving?" she asked quietly. ({n})']
            for n in range(4)]
    pdf = _qt_pdf(tmp_path / 'road.pdf', [['Chapter 1 Departure'] + body[0]] + body[1:], header='The Long Road')
    r = extract_pdf_text(pdf)
    assert r.pages == 4 and r.label.startswith('PDF 4쪽')
    assert 'The Long Road' not in r.text and not re.search(r'^\d$', r.text, re.M)
    paras = r.text.strip().split('\n\n')
    assert paras[0] == 'Chapter 1 Departure'
    assert paras[1] == ('He stood on the city wall, looking at the distant mountains, his heart full of mixed feelings. '
                        'The wind blew from the north and carried the smell of snow.')
    assert len([p for p in paras if p.startswith('"Are you really leaving?"')]) == 4


def test_chinese_pdf_file(qt_app, tmp_path):
    fam = _cjk_family()
    if not fam:
        pytest.skip('no Chinese font on this machine')
    lines = ['　他站在城墙上，望着远处的群山，心中百感交集。风从北方吹', '来，带着雪的气息。', '　“你真的要走吗？”她轻声问道。']
    pdf = _qt_pdf(tmp_path / 'zh.pdf', [lines] * 3, header='星河长歌', family=fam)
    text = extract_pdf_text(pdf).text
    assert '星河长歌' not in text
    assert text.count('他站在城墙上，望着远处的群山，心中百感交集。风从北方吹来，带着雪的气息。') == 3


def test_scanned_pdf_asks_for_ocr(qt_app, tmp_path):
    with pytest.raises(PdfNeedsOcrError) as e:
        extract_pdf_text(_picture_pdf(tmp_path / 'scan.pdf'))
    assert '문자 인식(OCR)이 필요한 PDF' in str(e.value)
    assert isinstance(e.value, NoTextError)


def test_password_and_damaged_pdfs_are_reported(qt_app, tmp_path):
    from pypdf import PdfReader, PdfWriter
    src = _qt_pdf(tmp_path / 'a.pdf', [['Some text on the page for the test.']])
    w = PdfWriter(); w.append(PdfReader(str(src))); w.encrypt('secret', algorithm='RC4-128')
    locked = tmp_path / 'locked.pdf'
    with locked.open('wb') as f: w.write(f)
    with pytest.raises(PdfTextError) as e:
        extract_pdf_text(locked)
    assert '암호' in str(e.value)
    broken = tmp_path / 'broken.pdf'; broken.write_bytes(b'%PDF-1.4 not really a pdf')
    with pytest.raises(SourceTextError):
        extract_pdf_text(broken)


# ---------------------------------------------------------------- EPUB

def test_epub_text_follows_the_spine_and_drops_ruby_and_scripts(tmp_path):
    book = _epub(tmp_path / 'book.epub', {
        'c1.xhtml': '<h1>第一章 旅立ち</h1><p><ruby>彼<rt>かれ</rt></ruby>は城壁の上に\n立った。</p>'
                    '<p>「本当に行くの？」<br/>と彼女は尋ねた。</p><script>alert(1)</script>',
        'c0.xhtml': '<div><p>まえがき</p></div><img src="x.png" alt="挿絵"/>',
        'c2.xhtml': '<section><h2>Chapter 2</h2><p>He came back &amp; <i>smiled</i>.</p></section>',
    }, spine=['c0.xhtml', 'c1.xhtml', 'c2.xhtml'])
    r = extract_epub_text(book)
    assert r.text.strip().split('\n\n') == ['まえがき', '第一章 旅立ち', '彼は城壁の上に立った。', '「本当に行くの？」',
                                            'と彼女は尋ねた。', 'Chapter 2', 'He came back & smiled.']
    assert r.label == 'EPUB 3개 문서 · 글자 추출'


def test_picture_only_drm_and_broken_epubs_are_reported(tmp_path):
    pics = _epub(tmp_path / 'comic.epub', {'p1.xhtml': '<div><img src="1.jpg"/></div>'}, ['p1.xhtml'])
    with pytest.raises(EpubNoTextError):
        extract_epub_text(pics)
    enc = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
           '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
           '<enc:CipherData><enc:CipherReference URI="OPS/c1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>')
    drm = _epub(tmp_path / 'drm.epub', {'c1.xhtml': '<p>x</p>'}, ['c1.xhtml'], encryption=enc)
    with pytest.raises(EpubTextError) as e:
        extract_epub_text(drm)
    assert 'DRM' in str(e.value)
    fonts_only = enc.replace('http://www.w3.org/2001/04/xmlenc#aes128-cbc', 'http://www.idpf.org/2008/embedding').replace('OPS/c1.xhtml', 'OPS/font.otf')
    ok = _epub(tmp_path / 'fonts.epub', {'c1.xhtml': '<p>本文です。</p>'}, ['c1.xhtml'], encryption=fonts_only)
    assert extract_epub_text(ok).text.strip() == '本文です。'     # obfuscated fonts are not DRM
    bad = tmp_path / 'bad.epub'; bad.write_bytes(b'PK not a zip')
    with pytest.raises(EpubTextError):
        extract_epub_text(bad)


# ---------------------------------------------------------------- reading files, folders, names

def test_read_source_file_gives_utf8_bytes_for_books_and_keeps_txt_as_before(qt_app, tmp_path):
    from novel_translator.textio import read_source_file, decode_txt_bytes
    t = tmp_path / 'a.txt'; t.write_bytes('日本語テスト\n次'.encode('cp932'))
    assert read_source_file(t, 'ja')[:2] == decode_txt_bytes(t.read_bytes(), 'ja') and read_source_file(t, 'ja')[2] == t.read_bytes()
    e = _epub(tmp_path / 'b.epub', {'c.xhtml': '<p>他笑了。</p>'}, ['c.xhtml'])
    text, label, raw = read_source_file(e, 'zh', 'gb18030')          # the TXT encoding is not used
    assert text == '他笑了。\n' and raw == text.encode('utf-8') and label.startswith('EPUB')
    assert document_kind('x.PDF') == 'PDF' and document_kind('x.epub') == 'EPUB' and document_kind('x.txt') == ''


def test_folder_discovery_includes_books_but_not_saved_results(tmp_path):
    from novel_translator.batch import discover_txt_files
    for n in ('1.txt', '2.pdf', '3.epub', '3.ko.txt', '2.ko.pdf', '3.ko.epub', '4.ko.partial.txt', 'x.docx'):
        (tmp_path / n).write_bytes(b'x')
    assert [p.name for p in discover_txt_files(tmp_path)] == ['1.txt', '2.pdf', '3.epub']


def test_same_name_txt_pdf_epub_results_do_not_overwrite_each_other(tmp_path):
    from novel_translator.rendering import output_stem, safe_output_filename
    (tmp_path / 'solo.pdf').write_bytes(b'x')
    assert safe_output_filename(tmp_path / 'solo.pdf', '.ko.txt') == 'solo.ko.txt'
    for n in ('book.txt', 'book.pdf', 'book.epub'):
        (tmp_path / n).write_bytes(b'x')
    assert [output_stem(tmp_path / n) for n in ('book.txt', 'book.pdf', 'book.epub')] == ['book', 'book.pdf', 'book.epub']
    assert safe_output_filename(tmp_path / 'book.pdf', '.ko.partial.txt') == 'book.pdf.ko.partial.txt'


# ---------------------------------------------------------------- the real worker: .ko.txt, partial, resume

def test_pdf_book_is_translated_to_ko_txt_with_partial_and_resume(qt_app, tmp_path, monkeypatch):
    import novel_translator.ui.worker as worker_mod
    from job_flow_audit import FakeProvider
    from novel_translator.profiles import ProfileManager
    from novel_translator.chunking import split_paragraphs, make_chunks
    from novel_translator.textio import read_source_file
    lines = [f'　これは第{i}段落です。人物は静かに話しました。' for i in range(1, 9)] + ['　BLOCKME を含む段落です。']
    src = _qt_pdf(tmp_path / 'novel.pdf', [lines[:5], lines[5:]], family=_cjk_family())
    text, _, raw = read_source_file(src, 'ja')
    if 'BLOCKME' not in text:
        pytest.skip('no Japanese font on this machine')
    monkeypatch.setattr(worker_mod, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(worker_mod, 'load_json', lambda _p: {'modes': {'balanced': {'thinking_level': 'medium'}},
                        'rate_limiter': {'start_rpm': 10**9, 'min_rpm': 10**9, 'max_rpm': 10**9}})

    def run(provider):
        monkeypatch.setattr(worker_mod, 'GeminiProvider', lambda: provider)
        profile = ProfileManager().set_active('ja-ko')
        chunks = make_chunks(split_paragraphs(text), 'ja', target_tokens=40, min_tokens=10, max_tokens=80)
        w = worker_mod.TranslationWorker(src, raw, profile, chunks, 'gemini-3.8-flash',
                                         [{'id': 'k', 'secret': 'dummy', 'project_group': 'P1', 'enabled': True}], {}, 'balanced', tmp_path / 'out')
        w.run()
        return provider

    first = run(FakeProvider('BLOCKME'))
    assert (tmp_path / 'out' / 'novel.ko.partial.txt').exists() and not (tmp_path / 'out' / 'novel.ko.txt').exists()
    second = run(FakeProvider())
    assert (tmp_path / 'out' / 'novel.ko.txt').exists() and not (tmp_path / 'out' / 'novel.ko.partial.txt').exists()
    assert second.calls < first.calls                      # resumed: only the unfinished part was sent again
    assert src.read_bytes()[:5] == b'%PDF-'                 # the original PDF is untouched


# ---------------------------------------------------------------- the window

def test_opening_a_pdf_fills_the_source_and_keeps_ko_txt_as_the_result(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    pdf = _qt_pdf(root / 'road.pdf', [['Chapter 1 Departure', 'He stood on the city wall and looked at the',
                                        'distant mountains for a long time.'], ['"Are you leaving?" she asked.'],
                                       ['"I have to," he said without turning around.']], header='The Long Road')
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(pdf))
        assert w.input_mode == 'single' and w.source_path == pdf and w._source_doc_kind() == 'PDF'
        assert w.preview.toPlainText().startswith('Chapter 1 Departure\n\nHe stood on the city wall and looked at the distant mountains')
        assert 'PDF 3쪽 · 글자 추출' in w.file_label.text() and w.paragraphs and 'The Long Road' not in w.source_text
        assert w.output_label.text().split('  ·')[0].endswith('road.ko.txt')
        before = w.source_text
        w.encoding.setCurrentIndex(w.encoding.findData('cp932')); w._encoding_changed()
        assert w.source_text == before and w.paragraphs   # the TXT encoding does not touch a PDF
    finally:
        w.close()


def test_scanned_pdf_is_refused_with_the_ocr_message(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    shown = []
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: shown.append((a[1], a[2])))
    try:
        w._open_txt_path(str(_picture_pdf(root / 'scan.pdf')))
        assert shown and shown[0][0] == '문자 인식(OCR)이 필요한 PDF' and w.source_path is None
    finally:
        w.close()


def _save_with(w, monkeypatch, path, chosen):
    seen = {}
    def fake(parent, title, start, filters, selected=''):
        seen.update(start=start, filters=filters, selected=selected); return str(path), chosen
    monkeypatch.setattr(ui.QFileDialog, 'getSaveFileName', fake)
    w._save_result()
    return seen


def test_save_result_offers_pdf_or_epub_only_for_such_books(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    try:
        txt = root / 'a.txt'; txt.write_text('他笑了。', encoding='utf-8')
        w._open_txt_path(str(txt)); w._set_result('그가 웃었다.')
        seen = _save_with(w, monkeypatch, root / 'a.ko.txt', w.TXT_FILTER)
        assert seen['filters'] == w.TXT_FILTER and (root / 'a.ko.txt').read_text(encoding='utf-8-sig') == '그가 웃었다.'

        pdf = _qt_pdf(root / 'b.pdf', [['Chapter 1', 'He smiled at her and walked away into the night.']])
        w._open_txt_path(str(pdf)); w._set_result('제1장\n\n그는 그녀에게 웃어 보이고 밤 속으로 걸어갔다.')
        seen = _save_with(w, monkeypatch, root / 'b.ko.txt', w.TXT_FILTER)
        assert seen['selected'] == w.TXT_FILTER and w.PDF_FILTER in seen['filters'] and seen['start'].endswith('b.ko.txt')
        assert (root / 'b.ko.txt').exists() and not (root / 'b.ko.pdf').exists()        # TXT stays the default
        _save_with(w, monkeypatch, root / 'b.ko.txt', w.PDF_FILTER)                      # PDF chosen in the dialog
        out = root / 'b.ko.pdf'
        assert out.read_bytes()[:5] == b'%PDF-'
        assert re.sub(r'\s', '', extract_pdf_text(out).text).startswith('제1장그는그녀에게웃어보이고')

        e = _epub(root / 'c.epub', {'c.xhtml': '<h1>第一章</h1><p>他笑了。</p>'}, ['c.xhtml'])
        w._open_txt_path(str(e)); w._set_result('제1장\n\n그가 웃었다.')
        seen = _save_with(w, monkeypatch, root / 'c.ko.epub', w.EPUB_FILTER)
        assert w.EPUB_FILTER in seen['filters'] and w.PDF_FILTER not in seen['filters']
        assert extract_epub_text(root / 'c.ko.epub').text.strip().split('\n\n') == ['제1장', '그가 웃었다.']
    finally:
        w.close()


def test_reading_pdf_has_chapter_pages_and_page_numbers(qt_app, tmp_path):
    from novel_translator.pdf_export import write_reading_pdf
    from pypdf import PdfReader
    body = '\n\n'.join('그는 성벽 위에 서서 먼 산들을 바라보았다.' for _ in range(5))
    n = write_reading_pdf(tmp_path / 'r.pdf', '제1장 출정\n\n' + body + '\n\n제2장 귀환\n\n' + body, '시험')
    pages = [re.sub(r'[\s\x00-\x1f]', '', p.extract_text()) for p in PdfReader(str(tmp_path / 'r.pdf')).pages]
    assert n == len(pages) == 2                                   # chapter 2 starts a new page
    assert pages[0].startswith('제1장출정') and pages[1].startswith('제2장귀환')
    assert pages[0].endswith('-1-') and pages[1].endswith('-2-')


def test_reading_epub_is_a_valid_book_with_a_table_of_contents(tmp_path):
    from novel_translator.epub_export import write_reading_epub
    import xml.etree.ElementTree as ET
    out = tmp_path / 'r.epub'
    n = write_reading_epub(out, '머리말.\n\n제1장 출정\n\n<그는> & 섰다.\n\n\n\n장면 뒤.\n\n제2장 귀환\n\n돌아왔다.', '시험 책')
    assert n == 3
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == 'mimetype' and first.compress_type == zipfile.ZIP_STORED
        for name in z.namelist():
            if name.endswith(('.xhtml', '.opf', '.ncx', '.xml')):
                ET.fromstring(z.read(name))                        # every file is well-formed XML
        nav = z.read('OEBPS/nav.xhtml').decode('utf-8')
        assert '제1장 출정' in nav and '제2장 귀환' in nav
    assert extract_epub_text(out).text.strip().split('\n\n') == ['머리말.', '제1장 출정', '<그는> & 섰다.', '*', '장면 뒤.', '제2장 귀환', '돌아왔다.']


def test_folder_run_translates_books_and_skips_picture_only_files(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _fake_worker_class(started))
    src = root / 'shelf'; src.mkdir()
    (src / '1.txt').write_text('他笑了。' * 20, encoding='utf-8')
    _epub(src / '2.epub', {'c.xhtml': '<p>' + '她哭了。' * 20 + '</p>'}, ['c.xhtml'])
    _picture_pdf(src / '3.pdf')
    w = _window(monkeypatch)
    try:
        w._open_folder_path(str(src))
        assert [p.name for p in w.batch_files] == ['1.txt', '2.epub', '3.pdf']
        assert 'TXT 1개 · PDF 1개 · EPUB 1개' in w.file_label.text()
        w.start_translation()
        summary = src / '한국어_번역' / '번역_요약.txt'
        assert _wait(qt_app, lambda: summary.exists() and not (w.worker and w.worker.isRunning()), 30)   # the whole queue ended
        assert [s.source_path.name for s in started] == ['1.txt', '2.epub']
        assert w.batch_results[str(src / '3.pdf')][0].startswith('파일 오류') and 'OCR' in w.batch_results[str(src / '3.pdf')][1]
        assert (src / '한국어_번역' / '2.ko.txt').exists()
    finally:
        w.close()


def test_an_edited_pdf_text_can_still_be_saved_as_pdf_but_new_pasted_text_cannot(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    pdf = _qt_pdf(root / 'd.pdf', [['Chapter 1', 'He smiled at her and walked away into the night.']])
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(pdf))
        w.preview.setPlainText(w.preview.toPlainText() + '\nOne more line.')
        assert w._use_preview_as_source() and w._paste_active and w._source_doc_kind() == ''
        w._set_result('제1장\n\n그가 웃었다.')
        assert w.PDF_FILTER in _save_with(w, monkeypatch, root / 'd2.ko.txt', w.TXT_FILTER)['filters']
        w.preview.setPlainText('完全に新しい文章です。')                 # new pasted text: no book format any more
        assert w._use_preview_as_source()
        assert _save_with(w, monkeypatch, root / 'd3.ko.txt', w.TXT_FILTER)['filters'] == w.TXT_FILTER
    finally:
        w.close()


def test_reading_pdf_reads_back_as_the_same_paragraphs(qt_app, tmp_path):
    """Wide line spacing and word spaces drawn as gaps must not break paragraphs or words."""
    from novel_translator.pdf_export import write_reading_pdf
    paras = ['제1장 출정'] + [f'그는 성벽 위에 서서 먼 산들을 바라보았다. 북쪽에서 불어오는 바람은 눈 냄새를 실어 왔고, 전쟁의 냄새도 함께 실어 왔다. {i}번째 밤이었다.'
                             for i in range(30)] + ['"가야 해." 그는 돌아보지 않았다.']
    write_reading_pdf(tmp_path / 'k.pdf', '\n\n'.join(paras), '시험')
    assert extract_pdf_text(tmp_path / 'k.pdf').text.strip().split('\n\n') == paras


def test_a_line_drawn_in_several_fonts_stays_one_line(qt_app, tmp_path):
    """Font fallback (a Latin font asked to draw Chinese) splits a line into pieces."""
    if not _cjk_family():
        pytest.skip('no Chinese font on this machine')
    lines = ['　他站在城墙上，望着远处的群山，心中百感交集。风从北方吹', '来，带着雪的气息。', '　“你真的要走吗？”她轻声问道。']
    pdf = _qt_pdf(tmp_path / 'fb.pdf', [lines] * 3, header='星河长歌', family='Arial')
    assert extract_pdf_text(pdf).text.strip().split('\n\n') == ['他站在城墙上，望着远处的群山，心中百感交集。风从北方吹来，带着雪的气息。', '“你真的要走吗？”她轻声问道。'] * 3


def test_two_column_pages_keep_the_reading_order():
    from novel_translator.pdf_text import _two_columns, _even_out_line_gaps
    cols = '\n'.join(f'left column line {i} text here          right column line {i} text' for i in range(8))
    assert _two_columns(cols) and not _two_columns('one line\nanother line\n' * 4)
    assert _even_out_line_gaps('a\n\nb\n\nc\n\n\n\nd\n\ne') == 'a\nb\nc\n\nd\ne'
    assert _even_out_line_gaps('a\nb\n\nc') == 'a\nb\n\nc'


def test_korean_text_in_a_pdf_keeps_its_word_spaces():
    text, _ = clean_pdf_pages(['그는 성벽 위에 서서 먼 산들을 바라보았다. 북쪽에서 불어오는\n바람은 눈 냄새를 실어 왔다.'])
    assert text.strip() == '그는 성벽 위에 서서 먼 산들을 바라보았다. 북쪽에서 불어오는 바람은 눈 냄새를 실어 왔다.'
