"""Korean refinement: translation reference table, review safety nets, retry of
preserved paragraphs, refusal detection and leftover kanji/kana."""
import re
import pytest
from novel_translator.chunking import split_paragraphs
from novel_translator.providers.base import TranslationResponse
from novel_translator.refinement_runtime import (run_refinement, is_refusal_notice, retry_status,
                                                   peek_guidance, speech_level_changed, review_reason,
                                                   refinement_prompt, model_reply_to_text)
from novel_translator.refinement_links import (record_translation, find_reference, reference_guidance,
                                                 text_digest)

REVIEW = '두 한국어 장면을 비교'


class Editor:
    """Edits every paragraph; `body(pid, text)` decides the edited text."""
    name = 'local'

    def __init__(self, body=None, review='SAME'):
        self.calls = []
        self.body = body or (lambda pid, text: text.replace('어색한', '자연스러운'))
        self.review = review

    def translate(self, req, key):
        if REVIEW in req.system_prompt:
            self.calls.append('review')
            return TranslationResponse(self.review)
        if req.purpose == 'analysis':
            self.calls.append('analysis')
            return TranslationResponse('은지 → 민수: 반말')
        self.calls.append('edit')
        self.last_user = req.user_prompt
        hits = re.findall(r'(?m)^<P(\d{6})>\n([^\n]*)', req.user_prompt.split('교정 대상:')[1])
        return TranslationResponse('\n\n'.join(f'<P{pid}>\n{self.body(int(pid), t)}' for pid, t in hits))


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_bytes(text.encode('utf-8'))   # exact bytes on every OS (no CRLF translation)
    return p


# --- refusal detection -------------------------------------------------------------

@pytest.mark.parametrize('narration', [
    '그는 이미 벌어진 일을 되돌릴 수 없었다. 어떤 말로도 그 사실을 수정할 수 없었다.',
    '어머니는 그에게 더 이상 아무것도 제공할 수 없다는 듯 고개를 저었다.',
    '그 누구도 그를 도와드릴 수 없었다.',
    '그녀가 말했다. “저는 더는 도와드릴 수 없습니다.” 그리고 돌아섰다.',
])
def test_narration_and_quoted_dialogue_are_not_refusals(narration):
    assert not is_refusal_notice(narration, '전혀 다른 원문 문장입니다.')


@pytest.mark.parametrize('notice', [
    '죄송하지만 이 내용을 교정해 드릴 수 없습니다.',
    '이 문장은 다듬어 드릴 수 없습니다. 다른 부분은 도와드릴 수 있습니다.',
    "I'm sorry, but I can't help with that.",
    '해당 요청을 처리할 수 없습니다.',
])
def test_assistant_refusals_are_still_caught(notice):
    assert is_refusal_notice(notice, '일반적인 소설 문단입니다.')


# --- review safety nets ------------------------------------------------------------

def test_speech_level_change_is_detected():
    assert speech_level_changed('“어머니, 어디 가세요?” 그가 물었다.', '“엄마, 어디 가?” 그가 물었다.')
    assert speech_level_changed('“빨리 와.”', '“빨리 오세요.”')
    assert not speech_level_changed('“어머니, 어디 가세요?”', '“어머니, 어디로 가세요?”')
    assert not speech_level_changed('그는 걸었다.', '그는 천천히 걸었다.')
    assert not speech_level_changed('“엄마!” 그가 외쳤다.', '“어머니!” 그가 외쳤다.')
    assert speech_level_changed('“어디로 가시오?”', '“어디로 가세요?”')


def test_review_reason_covers_length_speech_and_names():
    parts = split_paragraphs('“은지야, 밥 먹었어?” 민수가 물었다.')
    assert review_reason(parts, ['“은지야, 밥 먹었어?” 민수가 물었다.'], ['은지', '민수']) == ''
    assert '존댓말' in review_reason(parts, ['“은지 씨, 식사하셨어요?” 민수가 물었다.'], [])
    assert '이름' in review_reason(parts, ['“너, 밥은 먹었어?” 그가 다정하게 물었다.'], ['은지', '민수'])
    assert '분량' in review_reason(parts, ['물었다.'], [])


def test_partial_failure_no_longer_skips_the_continuity_review(tmp_path):
    src = write(tmp_path, 'p.txt', '\n'.join(['그는 어머니에게 오랫동안 하지 못했던 이야기를 천천히 꺼내기 시작했다. 방 안은 조용했다.'] * 4))
    # First paragraph too short (validation failure), the rest shrunk below 72%.
    ed = Editor(body=lambda pid, t: '짧음' if pid == 1 else '바뀐 문장입니다만 아주 많이 줄었다', review='CHANGED')
    res = run_refinement(src, tmp_path / 'p.out.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         links_root=tmp_path)
    assert 'review' in ed.calls
    assert res['changed'] == 0 and res['preserved'] == 4


def test_new_kanji_in_an_edit_is_rejected_and_leftovers_are_requested():
    paragraphs = split_paragraphs('그는 剑을 들었다.')
    with pytest.raises(ValueError, match='한자'):
        model_reply_to_text('<P000001>\n그는 劍을 쥐었다.', paragraphs)
    assert model_reply_to_text('<P000001>\n그는 검을 들었다.', paragraphs) == ['그는 검을 들었다.']
    _, user = refinement_prompt(paragraphs)
    assert '剑' in user and '한국어로 옮기세요' in user


def test_leftover_counts_are_reported(tmp_path):
    src = write(tmp_path, 'h.txt', '어색한 그는 剑을 들었다.\n어색한 둘째 문단.')
    ed = Editor(body=lambda pid, t: t.replace('剑', '검').replace('어색한', '자연스러운'))
    res = run_refinement(src, tmp_path / 'h.out.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert res['residue_before'] == 1 and res['residue_after'] == 0
    assert '재정비 전 1개 → 재정비 후 0개' in (tmp_path / 'h.out.txt.review.txt').read_text(encoding='utf-8-sig')


# --- translation reference table ---------------------------------------------------

def test_reference_found_by_text_then_path(tmp_path):
    out = write(tmp_path, 'book.ko.txt', '어색한 은지의 문장.\n어색한 민수의 문장.')
    rows = [{'speaker': '은지', 'listener': '민수', 'level': '반말', 'note': '연인'}]
    assert record_translation(out, out.read_text(encoding='utf-8'), rows, {'恩智': '은지', '敏洙': '민수'}, 'book.txt', root=tmp_path)
    moved = write(tmp_path, 'moved.txt', out.read_text(encoding='utf-8'))
    assert find_reference(moved, moved.read_text(encoding='utf-8'), tmp_path)['names'] == ['은지', '민수']
    assert find_reference(out, '손으로 고친 내용', tmp_path) is not None
    assert find_reference(tmp_path / 'other.txt', '다른 책', tmp_path) is None
    table = reference_guidance(find_reference(out, out.read_text(encoding='utf-8'), tmp_path))
    assert '은지 → 민수: 반말 (연인)' in table and '은지, 민수' in table


def test_nothing_is_recorded_without_speech_or_terms(tmp_path):
    assert not record_translation(tmp_path / 'x.ko.txt', '본문', [], {}, root=tmp_path)
    assert not (tmp_path / 'refinement_links.json').exists()


def test_translation_reference_replaces_the_sample_analysis(tmp_path):
    src = write(tmp_path, 'novel.ko.txt', '어색한 은지의 문장입니다.\n어색한 민수의 문장입니다.')
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '은지', 'listener': '민수', 'level': '해요체'}], {'恩智': '은지'}, root=tmp_path)
    where, summary, table = peek_guidance(src, checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert where == 'translation' and '말투 1쌍' in summary and '은지 → 민수: 해요체' in table
    ed = Editor()
    analysis = Editor()
    res = run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         analysis_engine=(analysis, 'a', 'k'), links_root=tmp_path)
    assert analysis.calls == [] and res['guidance_source'] == 'translation'
    assert '번역할 때 확정된 값이므로 그대로 따름' in ed.last_user and '은지 → 민수: 해요체' in ed.last_user
    report = (tmp_path / 'r.txt.review.txt').read_text(encoding='utf-8-sig')
    assert '기준표 출처: 번역 기록' in report and '은지 → 민수: 해요체' in report


def test_without_a_record_the_sample_analysis_is_used_and_shown(tmp_path):
    src = write(tmp_path, 'ext.txt', '어색한 은지의 문장입니다.\n어색한 민수의 문장입니다.')
    assert peek_guidance(src, checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)[0] == 'none'
    ed = Editor(); analysis = Editor()
    res = run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         analysis_engine=(analysis, 'a', 'k'), links_root=tmp_path)
    assert analysis.calls == ['analysis'] and res['guidance_source'] == 'analysis'
    assert '자동 분석 참고용' in ed.last_user
    assert '은지 → 민수: 반말' in (tmp_path / 'r.txt.review.txt').read_text(encoding='utf-8-sig')


# --- resume and retry --------------------------------------------------------------

def test_changing_the_analysis_model_keeps_finished_paragraphs(tmp_path):
    src = write(tmp_path, 'big.txt', '\n'.join(f'어색한 문단입니다. {i}' for i in range(8)))
    ed = Editor()
    stopped = run_refinement(src, tmp_path / 'o.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp', max_items=2,
                             analysis_engine=(Editor(), 'first-analysis', 'k'), links_root=tmp_path,
                             stop=lambda: ed.calls.count('edit') >= 2)
    assert stopped['status'] == 'stopped' and stopped['done'] == 4
    ed2 = Editor(); analysis2 = Editor()
    res = run_refinement(src, tmp_path / 'o.txt', ed2, 'm', 'k', checkpoint_dir=tmp_path / 'cp', max_items=2,
                         analysis_engine=(analysis2, 'another-analysis', 'k'), links_root=tmp_path)
    assert res['status'] == 'completed' and ed2.calls.count('edit') == 2 and analysis2.calls == []


def test_v24_resume_file_is_still_accepted(tmp_path):
    from hashlib import sha256
    from novel_translator.config import save_json
    from novel_translator.refinement_runtime import _legacy_identity, content_checkpoint_path
    text = '어색한 첫 문단.\n어색한 둘째 문단.'
    src = write(tmp_path, 'old.txt', text)
    digest = sha256(text.encode('utf-8')).hexdigest()
    ident = _legacy_identity(digest, 'local', 'm', 3500, 36, 'a', 'local')
    save_json(content_checkpoint_path(digest, tmp_path / 'cp'),
              {'schema': 1, 'identity': ident, 'completed': {'1': '이전 실행에서 다듬은 첫 문단.'}, 'issues': {},
               'guidance': '표', 'guidance_done': True})
    ed = Editor()
    res = run_refinement(src, tmp_path / 'o.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         analysis_engine=(Editor(), 'a', 'k'), links_root=tmp_path)
    assert res['done'] == 2 and ed.calls.count('edit') == 1
    assert '이전 실행에서 다듬은 첫 문단.' in (tmp_path / 'o.txt').read_text(encoding='utf-8-sig')


def test_preserved_paragraphs_can_be_retried_into_the_same_result(tmp_path):
    src = write(tmp_path, 'r.txt', '어색한 첫 문단입니다.\n어색한 둘째 문단입니다.')
    out = tmp_path / 'r.ko.refined.txt'
    refusing = Editor(body=lambda pid, t: '죄송하지만 이 문장은 교정해 드릴 수 없습니다.' if pid == 2 else t.replace('어색한', '자연스러운'))
    first = run_refinement(src, out, refusing, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert first['preserved'] == 1 and first['retry_available'] == 1
    assert retry_status(src, out, 'local', 'm', checkpoint_dir=tmp_path / 'cp') == 1
    with pytest.raises(FileExistsError):
        run_refinement(src, out, Editor(), 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    ed = Editor()
    second = run_refinement(src, out, ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path,
                            update_existing=True)
    assert ed.calls == ['edit'] and second['preserved'] == 0
    assert out.read_text(encoding='utf-8-sig').count('자연스러운') == 2
    assert retry_status(src, out, 'local', 'm', checkpoint_dir=tmp_path / 'cp') == 0
    assert not list((tmp_path / 'cp').glob('*.json'))


def test_a_hand_edited_result_is_never_overwritten_by_a_retry(tmp_path):
    src = write(tmp_path, 'r.txt', '어색한 첫 문단입니다.\n어색한 둘째 문단입니다.')
    out = tmp_path / 'r.ko.refined.txt'
    refusing = Editor(body=lambda pid, t: '죄송하지만 이 문장은 교정해 드릴 수 없습니다.' if pid == 2 else t)
    run_refinement(src, out, refusing, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    out.write_text(out.read_text(encoding='utf-8-sig') + '\n내가 직접 고친 줄', encoding='utf-8')
    assert retry_status(src, out, 'local', 'm', checkpoint_dir=tmp_path / 'cp') == 0
    with pytest.raises(FileExistsError):
        run_refinement(src, out, Editor(), 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path,
                       update_existing=True)
    assert '내가 직접 고친 줄' in out.read_text(encoding='utf-8')


def test_link_digest_ignores_line_endings(tmp_path):
    from hashlib import sha256
    text = '본문 한 줄.\n둘째 줄.'
    assert text_digest(text) == sha256(text.encode('utf-8')).hexdigest()
    assert text_digest(text.replace('\n', '\r\n')) == text_digest(text)
    out = tmp_path / 'crlf.ko.txt'
    out.write_bytes(text.replace('\n', '\r\n').encode('utf-8'))
    record_translation(out, text, [{'speaker': '갑', 'listener': '을', 'level': '반말'}], {}, root=tmp_path)
    moved = write(tmp_path, 'moved.txt', text)
    from novel_translator.textio import read_txt
    assert find_reference(moved, read_txt(moved)[0], tmp_path) is not None


def test_translation_result_is_linked_for_refinement(tmp_path, monkeypatch):
    import novel_translator.refinement_links as links
    from novel_translator.ui.main_window import MainWindow
    monkeypatch.setattr(links, 'DATA_DIR', tmp_path)
    out = write(tmp_path, 'book.ko.txt', '“오빠, 어디 가?” 소영이 물었다.')

    class W:
        speech_table = [{'speaker': '소영', 'listener': '오빠', 'level': '반말', 'note': ''}]
        terms = {'小英': '소영'}

    class Fake:
        worker = W()
        source_path = tmp_path / 'book.txt'

    MainWindow._link_result_for_refinement(Fake(), str(out))
    ref = links.find_reference(out, out.read_text(encoding='utf-8'))
    assert ref['source'] == 'book.txt' and ref['names'] == ['소영']
    # No worker or no path: nothing happens, never an error.
    Fake.worker = None
    MainWindow._link_result_for_refinement(Fake(), str(out))
    MainWindow._link_result_for_refinement(Fake(), '')
