"""v2.6.2 보완: each refined paragraph is checked on its own before it is kept.

The examples are real edits of a local model (Gemma 4 26B-A4B) on a novel. The continuity
review is asked only for a whole group and often answers "SAME", so these reached the result:
a line of dialogue dropped or added, narration cut, a line moved into the next paragraph,
반말 turned into 존댓말, a heading rewritten. A paragraph that fails keeps its original text
and is recorded as 원문 보존 ("보존한 문단만 다시 시도" can try it again).
"""
from __future__ import annotations

import pytest

from novel_translator.chunking import split_paragraphs
from novel_translator.refinement_runtime import (paragraph_guard, guard_paragraphs, speech_level_changed,
                                                   run_refinement)
from test_refinement_reference_and_safety import Editor, write


@pytest.mark.parametrize('orig,body,why', [
    ('안절부절못하며 걷던 그녀는 대로변으로 나가 택시를 세우고는, "롯폰기의 야마자키 탐정 사무소로, 서둘러 가 주세요."라고 말했다.',
     '안절부절못하며 걷던 그녀는 대로변으로 나가 택시를 세우더니,', '대사 수 변화'),
    ('"놓치게 둘 것 같아?" 한 명이 시즈코 부인의 느슨해진 오비에 손을 뻗어 힘껏 잡아챘다.',
     '"어딜 도망가? 놓치게 둘 것 같아?" 한 명이 시즈코 부인의 느슨해진 오비에 손을 뻗어 힘껏 잡아챘다.', '대사 늘어남'),
    ('"그래, 그래, 여기 있는 동안은 개나 고양이처럼 취급해 줄게." 여자들은 일제히 비웃었다.',
     '"그래, 그래. 여기 있는 동안은 개나 고양이처럼 다뤄줄게."', '서술 줄어듦'),
    ('"어이, 대장님, 기분은 좀 어때?"라며 말을 걸어왔다.', '"어이, 대장님! 기분은 좀 어때요?"라며 말을 걸어왔다.', '존댓말·반말 변화'),
    ('유괴된 레이부진', '납치된 시즈코 부인', '소제목·짧은 줄'),
    ('옷을 벗겨내다', '옷을 벗겨내기 위해', '소제목·짧은 줄'),
    ('그는 오랫동안 아무 말도 하지 않고 창밖의 비 내리는 거리를 바라보며 담배를 피웠다.', '그는 담배를 피웠다.', '서술 줄어듦'),
    ('"정말 몰랐어요. 그 사람이 거기 있는 줄은 정말 몰랐다니까요." 그녀가 고개를 저었다.', '"몰랐어요." 그녀가 고개를 저었다.', '대사 줄어듦'),
])
def test_guard_catches_real_failures(orig, body, why):
    assert paragraph_guard(orig, body) == why


@pytest.mark.parametrize('orig,body', [
    ('카와다는 예전에 도쿄의 번화가를 거점으로 활동하던 불량배였는데, 여자 꼬시는 일 전문으로 꽤나 이름을 날렸으나 벌이가 그리 쏠쏠하지는 않았다.',
     '카와다는 과거 도쿄 번화가를 거점으로 활동하던 불량배였다. 여자를 꼬시는 데는 일가견이 있어 이름 좀 날렸으나, 벌이가 그리 짭짤하지는 않았다.'),
    ('"부인과 따님은 어디에 있어?"', '"부인이랑 따님은 어디 있어?"'),
    ('"어디 가." 그녀가 물었다.', '"어디 가?" 그녀가 물었다.'),
    ('"뭐, 그렇게 서두를 것 없어. 소중한 인질이니까 말이야."', '"뭐, 그렇게 서두를 것 없어. 소중한 인질이니까."'),     # 니까 is 반말 here
    ('토야마 타카요시라는 명패가 걸린 저택에서 나온 여성은 시즈코 부인이다.', "'토야마 타카요시'라는 명패가 걸린 저택에서 나온 여인은 시즈코 부인이었다."),
    ('"엣!"', '"아앗!"'),
    ('제1장 발단', '제1장 발단'),
])
def test_guard_keeps_good_edits(orig, body):
    assert paragraph_guard(orig, body) == ''


def test_niikka_is_plain_unless_after_b_batchim():
    assert not speech_level_changed('"소중한 인질이니까 말이야."', '"소중한 인질이니까."')
    assert speech_level_changed('"어디 가는 겁니까?"', '"어디 가는 거야?"')
    assert speech_level_changed('"그렇습니까? 정말 몰랐습니다."', '"그래? 정말 몰랐어."')


def test_a_line_moved_into_the_next_paragraph_holds_both():
    class P:
        def __init__(self, pid, text): self.pid, self.text = pid, text
    parts = [P(70, '시즈코 부인은 아연실색한 표정으로 "저, 저를 굳이 묶지 않아도 되잖아요."'),
             P(71, '라며 몸을 굳혔지만, "흥, 이제 게이코 대신 긴코가 이 하자쿠라단의 우두머리야. 자, 얌전히 손을 뒤로 돌려."'),
             P(72, '즈베 무리는 어느샌가 삼베로 만든 밧줄을 준비해 시즈코 부인의 주위를 에워쌌다.')]
    bodies = ['시즈코 부인은 아연실색하여 "저, 저를 굳이 묶지 않아도 되잖아요."라며 몸을 굳혔다.',
              '"흥, 이제 게이코 대신 긴코가 이 하자쿠라단의 우두머리야. 자, 얌전히 손을 뒤로 돌려."',
              '즈베 무리는 어느새 삼베로 만든 밧줄을 준비해 시즈코 부인을 에워쌌다.']
    held = guard_paragraphs(parts, bodies)
    assert held == {70: '이웃 문단과 섞임', 71: '서술 줄어듦'}                 # 72 is a good edit, kept


def test_held_paragraphs_keep_the_original_and_are_preserved(tmp_path):
    book = ('"놓치게 둘 것 같아?" 한 명이 오비에 손을 뻗어 힘껏 잡아챘다.\n'
            '그는 어색한 웃음을 지으며 고개를 끄덕였다.\n'
            '유괴된 레이부진')
    src = write(tmp_path, 'g.ko.txt', book)
    def edit(pid, t):
        return {1: '"어딜 도망가? 놓치게 둘 것 같아?" 한 명이 오비에 손을 뻗어 힘껏 잡아챘다.',
                2: '그는 어색하게 웃으며 고개를 끄덕였다.', 3: '납치된 시즈코 부인'}[pid]
    lines = []
    r = run_refinement(src, tmp_path / 'g.r.txt', Editor(edit), 'm', '', checkpoint_dir=tmp_path / 'cp',
                       links_root=tmp_path / 'l', on_log=lines.append)
    out = (tmp_path / 'g.r.txt').read_text(encoding='utf-8-sig').split('\n')
    assert out == ['"놓치게 둘 것 같아?" 한 명이 오비에 손을 뻗어 힘껏 잡아챘다.', '그는 어색하게 웃으며 고개를 끄덕였다.', '유괴된 레이부진']
    assert r['preserved'] == 2 and r['changed'] == 1 and r['retry_available'] == 2
    assert any(l.startswith('원문 유지 · 2문단 (') and '대사 늘어남 1' in l and '소제목·짧은 줄 1' in l for l in lines)
    review = (tmp_path / 'g.r.txt.review.txt').read_text(encoding='utf-8-sig')
    assert '문단 1: 대사 늘어남 · 원문 보존' in review and '문단 3: 소제목·짧은 줄 · 원문 보존' in review
