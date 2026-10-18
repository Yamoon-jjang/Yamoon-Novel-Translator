"""v2.1.1: name + following-word phrases never become glossary terms, and opening a TXT
picks the source language automatically when the text makes it clear."""
from __future__ import annotations

from novel_translator.term_extraction import parse_glossary, drop_name_phrases
from test_alpha17 import qt_app, sandbox, _window  # noqa: F401,F811  (pytest fixtures)


# ---------------------------------------------------------------- glossary guard

CANDS = [(k, 9) for k in ['陈屿', '沈疏影', '陈屿放', '沈疏影身', '陈屿一眼', '陈志远', '陈屿明', '陈家', '陈家村',
                          '沈疏影才', '陈屿怀里', '陈屿手', '小雪', '青云宗']]


def test_name_phrases_from_a_model_are_dropped_and_real_names_kept():
    reply = ('{"陈屿":"진서","沈疏影":"심소영","陈屿放":"진서가 놓다","沈疏影身":"심소영의 몸",'
             '"陈屿一眼":"진서가 한 번 보다","陈志远":"진지원","陈屿明":"진서명","陈家":"진가","陈家村":"진가촌",'
             '"沈疏影才":"심소영이 그제야","陈屿怀里":"진서의 품 안","陈屿手":"진서의 손","小雪":"소설","青云宗":"청운종"}')
    got = parse_glossary(reply, CANDS)
    assert got == {'陈屿': '진서', '沈疏影': '심소영', '陈志远': '진지원', '陈屿明': '진서명',
                   '陈家': '진가', '陈家村': '진가촌', '小雪': '소설', '青云宗': '청운종'}


def test_existing_glossary_names_count_as_bases():
    got = parse_glossary('{"陈屿放":"진서가 놓다","陈屿明":"진서명"}', CANDS, known={'陈屿': '진서'})
    assert got == {'陈屿明': '진서명'}


def test_a_name_whose_rendering_only_looks_like_a_particle_is_kept():
    # 陈屿怡 -> 진서이: "이" is part of the name (no space after it), not a particle phrase
    assert drop_name_phrases({'陈屿': '진서', '陈屿怡': '진서이'}) == {'陈屿': '진서', '陈屿怡': '진서이'}


def test_speech_roster_ignores_name_phrases_already_in_a_glossary(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    w._merge_terms({'陈屿': '진서', '沈疏影': '심소영', '陈屿放': '진서가 놓다', '沈疏影身': '심소영의 몸'}, replace=False)
    assert w._speech_names() == {'陈屿': '진서', '沈疏影': '심소영'}


# ---------------------------------------------------------------- source language on open

ZH = '林雨推开门，看见苏晴坐在窗边。她没有回头，声音很轻。窗外的雨越下越大。\n' * 40
JA = '「先輩、遅いですよ」と彼女は頬を膨らませた。窓の外では雨が降り続いていた。\n' * 40
EN = 'She looked out of the window while the rain kept falling on the quiet street below.\n' * 40


def _open(w, tmp, name, text):
    path = tmp / name
    path.write_text(text, encoding='utf-8')
    w._open_txt_path(str(path))
    return path


def test_opening_a_file_selects_its_language(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    w._apply_profile('zh-ko')
    _open(w, root, 'ja.txt', JA)
    assert w.pm.active.id == 'ja-ko' and w.lang.currentData() == 'ja-ko'
    assert '일본어로 자동 설정' in w.status.text()
    assert '先輩' in w.source_text
    _open(w, root, 'en.txt', EN)
    assert w.pm.active.id == 'en-ko'
    _open(w, root, 'zh.txt', ZH)
    assert w.pm.active.id == 'zh-ko'


def test_a_language_chosen_before_for_this_book_is_kept(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    w._apply_profile('zh-ko')
    path = _open(w, root, 'ja.txt', JA)
    assert w.pm.active.id == 'ja-ko'
    w.lang.setCurrentIndex(w.lang.findData('zh-ko'))   # the user deliberately switches this book to Chinese
    _open(w, root, 'en.txt', EN)          # another book in between
    w._apply_profile('zh-ko')
    w._open_txt_path(str(path))           # reopening keeps the user's choice
    assert w.pm.active.id == 'zh-ko'


def test_unclear_text_keeps_the_current_language(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    w._apply_profile('ja-ko')
    _open(w, root, 'ko.txt', '그는 조용히 창밖을 바라보았다. 비가 계속 내렸다.\n' * 40)
    assert w.pm.active.id == 'ja-ko'


def test_settings_saved_without_a_choice_do_not_block_detection(qt_app, sandbox, monkeypatch):
    """v2.2 fix: a book opened under another language before (older build, settings reset)
    still gets its detected language; only a choice in the language box is kept."""
    root, _ = sandbox
    w = _window(monkeypatch)
    w._apply_profile('zh-ko')
    path = _open(w, root, 'ja.txt', JA)
    w.lang.blockSignals(True); w.lang.setCurrentIndex(w.lang.findData('zh-ko')); w.lang.blockSignals(False)
    w.pm.set_active('zh-ko'); w._save_workspace()   # state under zh without a choice (as an older build left it)
    _open(w, root, 'zh.txt', ZH)
    w._open_txt_path(str(path))
    assert w.pm.active.id == 'ja-ko' and '일본어로 자동 설정' in w.status.text()


def test_a_later_choice_replaces_an_earlier_one(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    w._apply_profile('zh-ko')
    path = _open(w, root, 'ja.txt', JA)
    w.lang.setCurrentIndex(w.lang.findData('en-ko'))   # chosen: English
    w.lang.setCurrentIndex(w.lang.findData('ja-ko'))   # then back to Japanese
    _open(w, root, 'en.txt', EN)
    assert w.pm.active.id == 'en-ko'
    w._open_txt_path(str(path))                        # English is no longer this book's choice
    assert w.pm.active.id == 'ja-ko'


def test_shared_symbols_do_not_hide_chinese():
    from novel_translator.language_detection import detect_language
    assert detect_language(ZH.replace('苏晴', '苏・晴'))[0] == 'zh'
    assert detect_language(ZH.replace('雨', '雨ー'))[0] == 'zh'
    assert detect_language(JA)[0] == 'ja' and detect_language(EN)[0] == 'en'
    assert detect_language('「ねえ、ジョン・スミスさん」と私は言った。'*20)[0] == 'ja'
