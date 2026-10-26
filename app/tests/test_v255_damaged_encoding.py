"""v2.5.5 보완: 이미 깨진 원문(잘못된 인코딩으로 열었다가 다시 저장한 파일) 경고와 한국어 CP949 TXT 읽기.

정상 중·일·영·한 원문에는 절대 경고하지 않고, 흔한 깨짐 유형은 모두 알아보는지 확인한다.
예문은 이 테스트를 위해 새로 쓴 문장이다."""
ZH_S = """第一章 雨夜
　　那天晚上下着很大的雨，林小雨一个人站在车站门口，看着街上来来往往的人。她已经等了快一个小时，可是哥哥还是没有来。
　　“你在这里干什么？”一个低沉的声音从身后传来。她回过头，看见一个穿着黑色大衣的年轻男人正在看她。
　　“我在等人。”她小声说道，往后退了一步。
　　男人笑了笑，把手里的伞递给她：“雨这么大，先拿着吧。我叫陈默，就住在前面那条街上。”
　　林小雨犹豫了一下，还是接过了伞。她知道自己不应该随便相信陌生人，可是这个人的眼睛里没有一点恶意。
　　第二天早上，她在学校门口又看见了他。他靠在一辆旧自行车旁边，好像在等什么人。看见她走过来，他抬起手打了个招呼。
　　“伞我带来了。”她把伞还给他，脸有些发红。
　　“不急，你留着用吧。这几天还会下雨。”陈默说完，骑上车慢慢地走了。她站在原地，心里有一种说不出来的感觉。
　　回到教室，同桌王小月马上凑过来问她刚才那个人是谁。她摇摇头说只是一个朋友，可是自己也不知道为什么要这么说。
　　放学以后，天又开始下雨了。她打开那把黑色的伞，走进了雨里。街道两边的灯一盏一盏地亮了起来，远处传来火车的声音。
"""
ZH_T = ZH_S.translate(str.maketrans('个时来说们这为过车门着见边还远后对会学问难东亲长开话钱让请谁样','個時來說們這為過車門著見邊還遠後對會學問難東親長開話錢讓請誰樣'))
JA = """第一章　雨の夜
　その夜は激しい雨が降っていた。美咲は駅の前に一人で立って、通りを行き交う人々をぼんやりと眺めていた。もう一時間近く待っているのに、兄はまだ来ない。
「こんなところで何をしているんだ？」背後から低い声がした。振り返ると、黒いコートを着た若い男がこちらを見ていた。
「人を待っているんです」彼女は小さな声で答え、一歩後ろに下がった。
　男は少し笑って、手に持っていた傘を差し出した。「この雨だ、とりあえず使ってくれ。俺は佐藤、この先の通りに住んでいる」
　美咲は少し迷ったが、結局その傘を受け取った。知らない人を簡単に信じてはいけないと分かっていたけれど、その目には悪意が少しもなかった。
　翌朝、学校の門の前でまた彼を見かけた。古い自転車にもたれて、誰かを待っているようだった。彼女が近づくと、彼は手を上げて挨拶した。
「傘、持ってきました」彼女は傘を返しながら、少し顔を赤くした。
「急がなくていい、持っていてくれ。しばらく雨が続くらしい」そう言うと、彼は自転車に乗ってゆっくりと去っていった。
"""
EN = """Chapter One
It was raining hard that night. Mia stood alone at the station entrance, watching people hurry along the street. She had been waiting for almost an hour, and her brother still hadn't come.
“What are you doing here?” a low voice asked behind her. She turned and saw a young man in a black coat looking at her.
“I’m waiting for someone,” she said quietly, taking a step back.
The man smiled and held out his umbrella. “It’s pouring. Take this for now. I’m Daniel — I live on the next street.”
Mia hesitated, then took it. She knew she shouldn’t trust strangers, but there was no malice in his eyes. The café on the corner was closing; its naïve little sign flickered.
"""
KO = """제1장 비 오는 밤
그날 밤에는 비가 세차게 내렸다. 미영은 역 앞에 혼자 서서 거리를 오가는 사람들을 멍하니 바라보았다. 벌써 한 시간 가까이 기다렸지만 오빠는 아직 오지 않았다.
"여기서 뭐 하고 있어?" 뒤에서 낮은 목소리가 들렸다. 돌아보니 검은 코트를 입은 젊은 남자가 그녀를 보고 있었다.
"사람을 기다리고 있어요." 그녀는 작은 목소리로 대답하고 한 걸음 물러섰다.
남자는 살짝 웃으며 손에 든 우산을 내밀었다. "비가 이렇게 오는데 일단 쓰세요. 저는 바로 앞 골목에 살아요."
"""
KO_HANJA = """第一章 비 오는 밤
그날 밤 美英은 驛 앞에 홀로 서서 거리를 오가는 사람들을 바라보았다. 그는 東京에서 온 大學 동창이었고, 두 사람은 오랫동안 消息을 모르고 지냈다. 時間은 이미 자정에 가까웠다.
"""
import pytest
from novel_translator.language_detection import detect_language_bytes
from novel_translator.textio import decode_txt_bytes, damaged_text_reason

N = 12
# Characters Big5 has no code for become '?' so the sample is a valid Big5 file.
ZH_B5 = ZH_T.encode('cp950', 'replace').decode('cp950')
RARE = '龍嘯天 慕容雪 獨孤劍 歐陽鋒 蕭炎 淩霄閣 青雲峰 萬劍宗 玄冥殿 赤焰谷 寒冰淵 紫霞仙子 '*80


def _short_id(value):
    """Short test names: Windows refuses an environment variable (PYTEST_CURRENT_TEST)
    longer than 32,767 characters, and a sample book is far longer."""
    return f'text{len(value)}' if isinstance(value, str) and len(value) > 24 else str(value)


def _broken(text, real, wrong):
    return text.encode(real, 'replace').decode(wrong, 'replace')


@pytest.mark.parametrize('text,lang', [
    (ZH_S*N, 'zh'), (ZH_T*N, 'zh'), (JA*N, 'ja'), (EN*N, 'en'), (KO*N, 'zh'), (KO_HANJA*N, 'zh'),
    (JA*N, 'zh'), (ZH_S*N, 'ja'), (EN*N, 'zh'), (RARE, 'zh'), (RARE, 'ja'), ('', 'zh'), (ZH_S, 'zh'),
], ids=_short_id)
def test_normal_text_never_warns(text, lang):
    assert damaged_text_reason(text, lang) == ''


@pytest.mark.parametrize('text,real,wrong,lang,word', [
    (ZH_S*N, 'gbk', 'cp949', 'zh', '한글'), (ZH_T*N, 'gbk', 'cp949', 'zh', '한글'),
    (ZH_T*N, 'cp950', 'cp949', 'zh', '한글'), (JA*N, 'cp932', 'cp949', 'ja', '한글'),
    (ZH_T*N, 'cp950', 'gbk', 'zh', 'Big5'), (ZH_S*N, 'gbk', 'cp950', 'zh', 'Big5'),
    (ZH_S*N, 'utf-8', 'cp1252', 'zh', '서유럽'), (ZH_S*N, 'gbk', 'latin-1', 'zh', '서유럽'),
    (JA*N, 'cp932', 'latin-1', 'ja', '서유럽'), (EN*N, 'utf-8', 'cp1252', 'en', 'â€'),
], ids=_short_id)
def test_damaged_text_is_noticed(text, real, wrong, lang, word):
    assert word in damaged_text_reason(_broken(text, real, wrong), lang)


def test_damaged_with_lost_characters_still_noticed():
    # Saved again after the wrong reading: some characters became '?' for good.
    broken = _broken(ZH_T*N, 'gbk', 'cp949')
    broken = ''.join('?' if i % 7 == 0 and '가' <= ch <= '힣' else ch for i, ch in enumerate(broken))
    assert damaged_text_reason(broken, 'zh')


@pytest.mark.parametrize('text,enc,lang', [
    (ZH_S*N, 'gb18030', 'zh'), (ZH_T*N, 'gb18030', 'zh'), (ZH_B5*N, 'cp950', 'zh'), (JA*N, 'cp932', 'ja'),
    (JA*N, 'euc_jp', 'ja'), (EN*N, 'cp1252', 'en'), (ZH_S*N, 'utf-8', 'zh'), (JA*N, 'utf-8', 'ja'),
], ids=_short_id)
def test_correctly_read_legacy_files_do_not_warn(text, enc, lang):
    raw = text.encode(enc)
    detected, _conf, _label = detect_language_bytes(raw[:262144])
    assert detected == lang
    out, _ = decode_txt_bytes(raw, lang)
    assert out == text and damaged_text_reason(out, lang) == ''


def test_korean_cp949_txt_reads_for_refinement(tmp_path):
    from novel_translator.textio import read_txt
    from novel_translator.ui.refinement_run_dialog import korean_share
    for enc in ('cp949', 'utf-8', 'utf-8-sig', 'utf-16'):
        p = tmp_path / f'ko_{enc}.txt'; p.write_bytes((KO*N).encode(enc))
        assert read_txt(p, 'ko')[0].replace('﻿', '') == KO*N
        assert korean_share(p) > 0.9


def test_korean_setting_does_not_change_other_languages():
    # Only the Korean setting considers CP949: Chinese/Japanese settings keep their reading.
    for text, enc, lang in ((ZH_S*N, 'gb18030', 'zh'), (ZH_B5*N, 'cp950', 'zh'), (JA*N, 'cp932', 'ja'), (JA*N, 'euc_jp', 'ja')):
        assert decode_txt_bytes(text.encode(enc), lang)[0] == text


from novel_translator.ui import main_window as ui
from test_alpha17 import _window, qt_app, sandbox  # noqa: F401,E402 (pytest fixtures)


def test_opening_a_damaged_file_warns_once_and_start_asks(qt_app, sandbox, monkeypatch, tmp_path):
    good = tmp_path / 'good.txt'; good.write_bytes((ZH_S*N).encode('gb18030'))
    bad = tmp_path / 'bad.txt'; bad.write_text(_broken(ZH_T*N, 'gbk', 'cp949'), 'utf-8')
    w = _window(monkeypatch)
    warned = []
    monkeypatch.setattr(ui.QMessageBox, 'warning', lambda *a, **k: warned.append(a[1]))
    try:
        w._open_txt_path(str(good))
        assert warned == [] and w._damaged_source_reason() == ''
        assert w._language_mismatch_warning() is True, 'a normal book starts without any question'
        w._open_txt_path(str(bad))
        assert warned == ['원문 글자가 깨져 있음'] and '깨진' in w.status.text()
        asked = []
        monkeypatch.setattr(ui.QMessageBox, 'exec', lambda self: (asked.append(self.text()), ui.QMessageBox.No)[1])
        assert w._language_mismatch_warning() is False and '그래도 번역을 시작할까요?' in asked[0]
    finally:
        w.close()


def test_folder_start_lists_damaged_files(qt_app, sandbox, monkeypatch, tmp_path):
    shelf = tmp_path / 'shelf'; shelf.mkdir()
    (shelf / 'a.txt').write_bytes((ZH_S*N).encode('gb18030'))
    (shelf / 'b.txt').write_text(_broken(ZH_S*N, 'gbk', 'cp949'), 'utf-8')
    w = _window(monkeypatch)
    try:
        w._open_folder_path(str(shelf))
        asked = []
        monkeypatch.setattr(ui.QMessageBox, 'exec', lambda self: (asked.append(self.text()), ui.QMessageBox.Yes)[1])
        monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a, **k: ui.QMessageBox.Yes)
        assert w._language_mismatch_warning() is True
        assert len(asked) == 1 and 'b.txt' in asked[0] and 'a.txt' not in asked[0]
        (shelf / 'b.txt').write_bytes((ZH_S*N).encode('gb18030')); asked.clear()
        w._open_folder_path(str(shelf))
        assert w._language_mismatch_warning() is True and asked == []
    finally:
        w.close()
