"""v2.5.8 (same-version fix): Chinese / Japanese / English files damaged in places.

Old mobile files cut at fixed byte counts (UTF-8, GBK, Big5, Shift-JIS, EUC-JP), BOM-less
UTF-16 Chinese / Japanese, and a few lines of characters the code page lacks used to stop
with "TXT 인코딩을 판별하지 못했습니다" or come out as Western garbage. They are now read with
only the damaged places marked, and the source language is detected for them. Files that
were read correctly before give exactly the same result (labels recorded from v2.5.8).
"""
from __future__ import annotations

import difflib

import pytest

from novel_translator.textio import decode_txt_bytes
from novel_translator.language_detection import detect_language_bytes

ZHS = '第一章 初遇\n那一年的冬天特别冷。林雨站在车站的出口，看着漫天飞舞的雪花，心里却是一片茫然。她已经在这个城市里生活了三年，可是直到今天，她才第一次觉得自己真的是一个人。\n“你是在等人吗？”一个低沉的声音从身后传来。她回过头，看见一个穿着黑色大衣的男人，正微笑着看着她。\n“不是，我只是在想事情。”她轻声说道，下意识地把手插进了口袋里。\n男人点了点头，没有再说什么，只是站在她的身边，和她一起看着外面的雪。过了很久，他才开口说：“我叫陈屿，住在前面那条街上。如果你没有地方去的话，可以先到我那里坐一坐，喝杯热茶。”\n林雨看了他一眼，犹豫了一下，最后还是跟着他走了。她不知道这个决定会改变她以后所有的生活。'
ZHT = '第一章 初遇\n那一年的冬天特別冷。林雨站在車站的出口，看著漫天飛舞的雪花，心裡卻是一片茫然。她已經在這個城市裡生活了三年，可是直到今天，她才第一次覺得自己真的是一個人。\n「你是在等人嗎？」一個低沉的聲音從身後傳來。她回過頭，看見一個穿著黑色大衣的男人，正微笑著看著她。\n「不是，我只是在想事情。」她輕聲說道，下意識地把手插進了口袋裡。\n男人點了點頭，沒有再說什麼，只是站在她的身邊，和她一起看著外面的雪。過了很久，他才開口說：「我叫陳嶼，住在前面那條街上。如果你沒有地方去的話，可以先到我那裡坐一坐，喝杯熱茶。」\n林雨看了他一眼，猶豫了一下，最後還是跟著他走了。她不知道這個決定會改變她以後所有的生活。'
JA = '第一章\u3000出会い\nその年の冬は特別に寒かった。駅の出口に立った彼女は、空一面に舞う雪を見上げながら、ぼんやりとしていた。この街に住んで三年になるが、今日初めて、自分は本当に一人なのだと感じた。\n「誰かを待っているんですか」と、後ろから低い声が聞こえた。振り返ると、黒いコートを着た男がほほえみながらこちらを見ていた。\n「いいえ、ちょっと考え事をしていただけです」と彼女は小さな声で答え、思わず手をポケットに入れた。\n男はうなずいて、それ以上は何も言わず、ただ彼女の隣に立って一緒に外の雪を眺めていた。しばらくして、彼はようやく口を開いた。'
JA_KANJI = '第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n第一回\u3000桃園結義\n話説天下大勢、分久必合、合久必分。周末七国分争、并入於秦。及秦滅之後、楚漢分争、又并入於漢。\n'
EN = 'It was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\nIt was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\n'
KO = "그는 창밖을 바라보며 오래된 편지를 다시 펼쳤다. 편지에는 익숙한 글씨로 짧은 문장이 적혀 있었다.\n" * 40
ZHS6, JA6, ZHT6 = (ZHS + "\n") * 6, (JA + "\n") * 6, (ZHT + "\n") * 6


def cut(data: bytes, width: int) -> bytes:
    return b"\r\n".join(data[i:i + width] for i in range(0, len(data), width))


def same_text(got: str, raw: bytes, codec: str) -> bool:
    strip = lambda x: "".join(c for c in x if c not in "\ufffd\r\n?")
    return difflib.SequenceMatcher(None, strip(got), strip(raw.decode(codec, "replace")), autojunk=False).ratio() >= 0.95


FIXED = [  # name, bytes, language, true codec, label start
    ("zhs.utf8-cut", cut(ZHS6.encode("utf-8"), 100), "zh", "utf-8", "UTF-8 (깨진 바이트"),
    ("ja.utf8-cut", cut(JA6.encode("utf-8"), 100), "ja", "utf-8", "UTF-8 (깨진 바이트"),
    ("en.utf8-cut", cut(("“Hello,” she said — it’s fine, really.\n" * 40).encode("utf-8"), 37), "en", "utf-8", "UTF-8 (깨진 바이트"),
    ("zhs.utf16le", ZHS.encode("utf-16-le"), "zh", "utf-16-le", "UTF-16 LE"),
    ("ja.utf16le", JA.encode("utf-16-le"), "ja", "utf-16-le", "UTF-16 LE"),
    ("zht.utf16be", ZHT.encode("utf-16-be"), "zh", "utf-16-be", "UTF-16 BE"),
    ("zhs.gbk-cut", cut(ZHS6.encode("gbk"), 101), "zh", "gb18030", "GB18030 / GBK (깨진 바이트"),
    ("zht.big5-cut", cut(ZHT6.encode("cp950"), 101), "zh", "cp950", "Big5 (깨진 바이트"),
    ("ja.sjis-cut", cut(JA6.encode("cp932"), 101), "ja", "cp932", "CP932 / Shift-JIS (깨진 바이트"),
    ("ja.eucjp-cut", cut(JA6.encode("euc_jp"), 101), "ja", "euc_jp", "EUC-JP (깨진 바이트"),
]


@pytest.mark.parametrize("name,raw,lang,codec,label", FIXED, ids=[c[0] for c in FIXED])
def test_damaged_files_are_read_and_their_language_detected(name, raw, lang, codec, label):
    assert detect_language_bytes(raw[:262144])[0] == lang
    text, got = decode_txt_bytes(raw, lang)
    assert got.startswith(label), got
    assert same_text(text, raw, codec)


UNCHANGED = {  # v2.5.8 labels per source-language setting, and the detected language
    "ko.utf8": (KO.encode("utf-8"), {"zh": "UTF-8", "ja": "UTF-8", "en": "UTF-8", None: "UTF-8"}, None),
    "en.ascii": (EN.encode("ascii"), {"zh": "UTF-8", "ja": "UTF-8", "en": "UTF-8", None: "UTF-8"}, "en"),
    "en.1252": ("“Smart quotes” café naïve — done.\n".encode("cp1252") * 30, {"zh": "ERROR", "ja": "ERROR", "en": "Windows-1252", None: "Windows-1252"}, "en"),
    "zhs.utf8": (ZHS.encode("utf-8"), {"zh": "UTF-8", "ja": "UTF-8", "en": "UTF-8", None: "UTF-8"}, "zh"),
    "ja.utf8": (JA.encode("utf-8"), {"zh": "UTF-8", "ja": "UTF-8", "en": "UTF-8", None: "UTF-8"}, "ja"),
    "en.utf16le": (EN.encode("utf-16-le"), {"zh": "UTF-16 LE (추정)", "ja": "UTF-16 LE (추정)", "en": "UTF-16 LE (추정)", None: "UTF-16 LE (추정)"}, "en"),
    "ja-kanji.sjis": (JA_KANJI.encode("cp932"), {"zh": "CP932", "ja": "CP932 / Shift-JIS", "en": "Windows-1252", None: "GB18030 / GBK"}, "zh"),
    "zht-as-gbk": (ZHT.encode("gbk"), {"zh": "GB18030 / GBK", "ja": "GB18030", "en": "Windows-1252", None: "GB18030 / GBK"}, "zh"),
    "zhs.gbk": (ZHS.encode("gbk"), {"zh": "GB18030 / GBK"}, "zh"),
    "zhs.gb18030-symbols": ("\n".join(l + ("❤✨" if i % 3 == 0 else "") for i, l in enumerate(ZHS6.split("\n"))).encode("gb18030"), {"zh": "GB18030 / GBK"}, "zh"),
    "zht.big5": (ZHT.encode("cp950"), {"zh": "Big5"}, "zh"),
    "zht.big5-hkscs": (ZHT6.replace("著", "着").encode("big5hkscs"), {"zh": "Big5-HKSCS"}, "zh"),
    "ja.sjis": (JA.encode("cp932"), {"ja": "CP932 / Shift-JIS"}, "ja"),
    "ja.eucjp": (JA.encode("euc_jp"), {"ja": "EUC-JP"}, "ja"),
    "short.gbk": (ZHS.split("\n")[1][:40].encode("gbk"), {"zh": "GB18030 / GBK", "ja": "GB18030", "en": "Windows-1252", None: "GB18030 / GBK"}, "zh"),
    "short.sjis": (JA.split("\n")[1][:30].encode("cp932"), {"zh": "GB18030 / GBK", "ja": "CP932 / Shift-JIS", "en": "Windows-1252", None: "CP932 / Shift-JIS"}, "ja"),
    "en.utf8-1stray": (("“Hello,” she said.\n" * 40).encode("utf-8") + b"\x93q\x94\n", {"zh": "UTF-8 (깨진 바이트 2개 대체)", None: "UTF-8 (깨진 바이트 2개 대체)"}, "en"),
}


@pytest.mark.parametrize("name", list(UNCHANGED))
def test_files_read_correctly_before_are_read_the_same(name):
    raw, labels, lang = UNCHANGED[name]
    for setting, want in labels.items():
        try:
            got = decode_txt_bytes(raw, setting)[1]
        except Exception:
            got = "ERROR"
        assert got == want, (setting, got)
    assert detect_language_bytes(raw[:262144])[0] == lang


def test_korean_cp949_text_is_not_taken_for_chinese_or_english():
    raw = KO.encode("cp949")
    assert detect_language_bytes(raw)[0] is None   # v2.5.8 said "en" (Windows-1252 garbage)
