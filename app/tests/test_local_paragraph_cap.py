"""Local LLM paragraph groups of up to 20 (was 12), the one-line shift check on the larger
groups, and the per-file step back to 12 when a model keeps misplacing paragraph IDs.

No network: fake providers inspect the actual prompts the worker builds.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

import pytest

from novel_translator.providers.base import TranslationResponse, ProviderError, ErrorKind
from novel_translator.orchestrator import TranslationOrchestrator, KeyState, format_failure_detail
from novel_translator.validation import dialogue_shift_run, SHIFT_RUN_MIN, LOCAL_SAFE_PARAGRAPHS
import novel_translator.ui.worker as worker_mod
from test_refusal_recovery import _source, TAG_BLOCK
from test_v20_review_fixes import Gemini, build, novel, _output, CAP

APP = Path(__file__).resolve().parents[1]
QUOTES = '“「"'
Q, N = '“가자.”', '그가 고개를 끄덕였다.'


def tagged(rows):
    return '\n\n'.join(f'<P{i + 1:06d}>\n{x}' for i, x in enumerate(rows))


def ko(pid: str, body: str) -> str:
    """Korean stand-in that keeps the source's opening quotation mark and names its paragraph."""
    lead = '“' if body.lstrip()[:1] in QUOTES else ''
    return f'{lead}번역{pid} 한국어번역문장입니다'


class QuoteLocal:
    """Translates every paragraph in place; `shift_above` paragraphs or more are answered one
    line early from the third paragraph on (every ID present, translations misplaced)."""
    name = 'local'; last_usage = None

    def __init__(self, shift_above: int | None = None, limit: int = 999):
        self.shift_above = shift_above; self.limit = limit; self.sizes = []; self.prompts = []

    def translate(self, request, api_key):
        self.prompts.append(request.user_prompt)
        rows = TAG_BLOCK.findall(_source(request.user_prompt)); self.sizes.append(len(rows))
        bodies = [ko(p, b) for p, b in rows]
        if self.shift_above is not None and len(rows) > self.shift_above:
            bodies = bodies[:2] + bodies[3:] + [bodies[-1]]
        if len(rows) > self.limit:
            rows = rows[:-1]; bodies = bodies[:-1]
        return TranslationResponse('\n\n'.join(f'<P{p}>\n{b}' for (p, _), b in zip(rows, bodies)), 'stop', {})


def dialogue_novel(n):
    """Short lines alternating dialogue and narration (where a shift is visible)."""
    return [('“你来了。”' if i % 3 == 0 else '她没有回头，声音很轻。') for i in range(n)]


def assert_aligned(tmp_path):
    out = _output(tmp_path)
    nums = [int(m) for m in re.findall(r'번역(\d{6})', out)]
    assert nums == sorted(nums) and len(nums) == len(set(nums)), 'a translation sits under the wrong paragraph'
    assert not re.search(r'[一-龥]', out)
    return nums


# ---------------------------------------------------------------- the shift check itself

def test_aligned_translations_are_not_a_shift():
    src = [N, N, Q, Q, N, Q, N, N, Q, Q, Q, N, Q, N]
    assert dialogue_shift_run(tagged(src), tagged(src)) == 0


@pytest.mark.parametrize('direction', ['early', 'late'])
def test_a_one_line_shift_is_found(direction):
    src = [N, N, Q, Q, N, Q, N, N, Q, Q, Q, N, Q, N]
    out = src[1:] + [src[-1]] if direction == 'early' else [src[0]] + src[:-1]
    assert dialogue_shift_run(tagged(src), tagged(out)) >= SHIFT_RUN_MIN


def test_a_shift_from_the_middle_is_found():
    src = [N, Q, N, Q, N, Q, N, Q, N, Q, N, Q, N, Q, N, Q]
    out = src[:6] + src[7:] + [src[-1]]
    assert dialogue_shift_run(tagged(src), tagged(out)) >= SHIFT_RUN_MIN


def test_speech_tags_moved_by_the_translator_are_not_a_shift():
    src = [N, N, Q, Q, N, Q, N, N, Q, Q, Q, N, Q, N]
    out = list(src); out[1] = Q; out[6] = Q; out[11] = Q      # "She said, 'Go.'" -> “가.” 그녀가 말했다.
    assert dialogue_shift_run(tagged(src), tagged(out)) < SHIFT_RUN_MIN


@pytest.mark.parametrize('every', [Q, N])
def test_a_model_that_adds_or_drops_every_quote_is_not_a_shift(every):
    src = [N, Q, N, Q, N, Q, N, Q, N, Q, N, Q, N, Q]
    assert dialogue_shift_run(tagged(src), tagged([every] * len(src))) < SHIFT_RUN_MIN


def test_language_quote_styles_are_the_same_opening():
    src = ['「先輩、遅いですよ」', '彼女は頬を膨らませた。', '『本』を読む。', '"Late," she said.', '‘Oi,’ he said.', 'Narration.']
    out = ['“선배, 늦었잖아요.”', '그녀가 볼을 부풀렸다.', '『책』을 읽는다.', '“늦었네.” 그녀가 말했다.', '‘어이.’ 그가 말했다.', '서술.']
    assert dialogue_shift_run(tagged(src), tagged(out)) == 0


def test_skipped_and_tiny_requests_are_not_checked():
    src = [N, Q, N, Q]
    assert dialogue_shift_run(tagged(src[:3]), tagged(src[1:] + [N])) == 0
    big = [N, Q, N, Q, N, Q, N, Q]
    shifted = big[1:] + [big[-1]]
    assert dialogue_shift_run(tagged(big), tagged(shifted), ignore_pids=range(1, 9)) == 0


def test_shift_failure_has_its_own_log_label():
    assert format_failure_detail('문단 밀림 의심 · x').startswith('문단 밀림 의심')


# ---------------------------------------------------------------- orchestrator: which requests are checked

class OneShot:
    name = 'local'; last_usage = None

    def __init__(self, text): self.text = text; self.calls = 0

    def translate(self, request, api_key):
        self.calls += 1
        return TranslationResponse(self.text, 'stop', {})


def _profile():
    from novel_translator.profiles import ProfileManager
    return ProfileManager().set_active('zh-ko')


def _orch(provider, provider_id='local'):
    return TranslationOrchestrator(provider, [KeyState('LOCAL', 'local', 'local', True)], 'm', retries=1,
                                   sleep_fn=lambda s: None, target_rpm=1e9, min_rpm=1e9, max_rpm=1e9,
                                   provider_id=provider_id)


def _shifted_pair(n):
    src_lines = [('“你来了。”' if i % 2 else '她没有回头，声音很轻。') for i in range(n)]
    source = tagged(src_lines)
    good = [('“' if l.startswith('“') else '') + '한국어 번역 문장입니다 확인' for l in src_lines]
    return source, tagged(good[1:] + [good[-1]])


def test_large_local_request_with_a_shift_is_sent_back_at_once():
    source, shifted = _shifted_pair(LOCAL_SAFE_PARAGRAPHS + 4)
    prov = OneShot(shifted); orch = _orch(prov)
    with pytest.raises(ProviderError) as e:
        orch.translate_chunk(_profile(), source, {})
    assert e.value.kind == ErrorKind.FORMAT and e.value.message.startswith('문단 밀림')
    assert prov.calls == 1                       # not re-sent at the same size


def test_requests_of_twelve_or_fewer_paragraphs_are_never_checked():
    source, shifted = _shifted_pair(LOCAL_SAFE_PARAGRAPHS)
    out, _, _ = _orch(OneShot(shifted)).translate_chunk(_profile(), source, {})
    assert out


def test_recovery_requests_and_cloud_engines_are_never_checked():
    source, shifted = _shifted_pair(LOCAL_SAFE_PARAGRAPHS + 4)
    assert _orch(OneShot(shifted)).translate_chunk(_profile(), source, {}, recovery_mode=True)[0]
    assert _orch(OneShot(shifted), provider_id='gemini').translate_chunk(_profile(), source, {})[0]


# ---------------------------------------------------------------- worker: sizes, shift recovery, step back

def test_default_is_twenty_in_code_and_in_the_shipped_config():
    assert worker_mod.TranslationWorker.LOCAL_DEFAULT_PARAGRAPHS == CAP == 20
    cfg = json.loads((APP / 'config' / 'app.json').read_text(encoding='utf-8'))
    assert cfg['local_llm']['max_paragraphs_per_request'] == 20


def test_groups_are_even_and_never_above_the_cap(tmp_path):
    gem = Gemini(block=lambda p, s: True)
    loc = QuoteLocal()
    w, chunks, events = build(tmp_path, dialogue_novel(90), gem, loc)
    w.run()
    assert max(loc.sizes) <= CAP
    assert len(loc.sizes) == sum(-(-len(c.paragraphs) // CAP) for c in chunks)
    for c in chunks:                              # evenly sized: 36 -> 18 + 18, not 20 + 16
        n = len(c.paragraphs); g = -(-n // CAP)
        assert all(abs(s - n / g) < 1 for s in loc.sizes[:g]); loc.sizes = loc.sizes[g:]
    assert not any('문단 밀림' in e or '문단 묶음' in e for e in events)
    assert_aligned(tmp_path)


def test_long_paragraph_books_send_exactly_what_they_sent_before(tmp_path):
    """Chunks of 12 paragraphs or fewer: one request per chunk with cap 12 and with cap 20."""
    lines = ['林雨推开门，看见苏晴坐在窗边，手里攥着那封信。窗外的雨越下越大，打在玻璃上啪啪作响。' * 3] * 30
    runs = []
    for cap in (12, None):
        d = tmp_path / f'cap{cap}'; d.mkdir()
        loc = QuoteLocal(shift_above=0)           # would "shift" any request it gets: must never be checked
        w, chunks, events = build(d, lines, Gemini(block=lambda p, s: True), loc, cap=cap)
        assert all(len(c.paragraphs) <= LOCAL_SAFE_PARAGRAPHS for c in chunks)
        w.run()
        runs.append(loc.sizes)
        assert not any('문단 밀림' in e for e in events)
    assert runs[0] == runs[1] == [len(c.paragraphs) for c in chunks]


def test_a_shifted_group_is_retranslated_in_halves_and_lands_in_place(tmp_path):
    gem = Gemini(block=lambda p, s: True)
    loc = QuoteLocal(shift_above=LOCAL_SAFE_PARAGRAPHS)
    w, chunks, events = build(tmp_path, dialogue_novel(36), gem, loc)
    w.run()
    first = loc.sizes[0]
    assert first > LOCAL_SAFE_PARAGRAPHS
    assert all(s <= LOCAL_SAFE_PARAGRAPHS for s in loc.sizes[1:3])   # halves, as recovery requests
    nums = assert_aligned(tmp_path)
    assert len(nums) == sum(len(c.paragraphs) for c in chunks)       # nothing left as source
    log = (tmp_path / 'data' / 'logs' / worker_mod.TranslationWorker.DIAGNOSTIC_FILE).read_text(encoding='utf-8')
    assert '문단 밀림 의심' in log
    from novel_translator.storage import Database
    with Database(tmp_path / 'data' / 'translations.db').connect() as con:
        assert {r[0] for r in con.execute('SELECT status FROM chunks')} == {'COMPLETED'}


def test_frequent_shifts_send_the_rest_of_the_file_in_twelves(tmp_path):
    gem = Gemini(block=lambda p, s: True)
    loc = QuoteLocal(shift_above=LOCAL_SAFE_PARAGRAPHS)
    w, chunks, events = build(tmp_path, dialogue_novel(400), gem, loc)
    w.run()
    step = [e for e in events if '로컬 LLM 문단 묶음' in e]
    assert len(step) == 1                          # said once
    assert w._wide_misfits == worker_mod.TranslationWorker.WIDE_MISFIT_MIN
    wide = [i for i, s in enumerate(loc.sizes) if s > LOCAL_SAFE_PARAGRAPHS]
    assert len(wide) == worker_mod.TranslationWorker.WIDE_MISFIT_MIN   # no larger request after stepping back
    assert all(s <= LOCAL_SAFE_PARAGRAPHS for s in loc.sizes[wide[-1] + 1:])
    assert_aligned(tmp_path)


def test_missing_ids_count_toward_the_step_back_too(tmp_path):
    gem = Gemini(block=lambda p, s: True)
    loc = QuoteLocal(limit=15)                     # drops the last paragraph above 15 IDs
    w, chunks, events = build(tmp_path, dialogue_novel(400), gem, loc)
    w.run()
    assert any('로컬 LLM 문단 묶음' in e for e in events) and w._local_cap() == LOCAL_SAFE_PARAGRAPHS
    wide = [i for i, s in enumerate(loc.sizes) if s > LOCAL_SAFE_PARAGRAPHS]
    assert all(s <= LOCAL_SAFE_PARAGRAPHS for s in loc.sizes[wide[-1] + 1:])
    nums = assert_aligned(tmp_path)
    assert len(nums) == sum(len(c.paragraphs) for c in chunks)


def test_rare_failures_do_not_step_back(tmp_path):
    w, chunks, events = build(tmp_path, dialogue_novel(40), Gemini(), QuoteLocal())
    c = chunks[0]
    big = type(c)(c.index, c.paragraphs[:CAP], 0, 0)
    err = ProviderError(ErrorKind.FORMAT, '문단 ID 불일치: x')
    for k in range(100):
        w._note_wide_request(big, err if k in (10, 50, 90) else None)
    assert w._wide_misfits == 3 and not w._wide_stepped_down and w._local_cap() == CAP
    small = type(c)(c.index, c.paragraphs[:LOCAL_SAFE_PARAGRAPHS], 0, 0)
    for _ in range(10):
        w._note_wide_request(small, err)            # requests of 12 or fewer never count
    assert w._wide_misfits == 3 and w._local_cap() == CAP


def test_other_failures_do_not_count(tmp_path):
    w, chunks, events = build(tmp_path, dialogue_novel(40), Gemini(), QuoteLocal())
    big = type(chunks[0])(0, chunks[0].paragraphs[:CAP], 0, 0)
    for msg, kind in (('응답에 한국어가 너무 적습니다', ErrorKind.FORMAT), ('timeout', ErrorKind.TRANSIENT),
                      ('거부', ErrorKind.REFUSAL)):
        for _ in range(5):
            w._note_wide_request(big, ProviderError(kind, msg))
    assert w._wide_misfits == 0 and not w._wide_stepped_down


def test_cap_twelve_or_zero_in_config_never_steps_or_counts(tmp_path):
    for cap in (12, 0):
        d = tmp_path / str(cap); d.mkdir()
        w, chunks, events = build(d, dialogue_novel(40), Gemini(), QuoteLocal(), cap=cap)
        big = type(chunks[0])(0, chunks[0].paragraphs[:30], 0, 0)
        for _ in range(10):
            w._note_wide_request(big, ProviderError(ErrorKind.FORMAT, '문단 ID 불일치: x'))
        assert w._wide_requests == 0 and w._local_cap() == cap


def test_cap_is_not_part_of_the_job_identity(tmp_path):
    keys = []
    for cap in (12, 20, 0):
        d = tmp_path / str(cap); d.mkdir()
        w, _, _ = build(d, dialogue_novel(60), Gemini(), QuoteLocal(), cap=cap)
        keys.append((w._fingerprint(), w._settings_key()))
    assert keys[0] == keys[1] == keys[2]          # a half-done book continues; translation memory still matches


def test_moving_speech_to_the_front_never_looks_like_a_shift():
    """Chinese narration that carries the speech (他说：“走吧。”) is often translated speech
    first (“가자.” 그가 말했다.). Every source pattern and every set of such moves, up to 9
    paragraphs, stays below the threshold: one-direction moves can never line up as a shift."""
    import itertools
    src = {1: '“对话。”', 0: '他说：“走吧。”'}; out = {1: '“대사.”', 0: '서술.'}
    for n in range(SHIFT_RUN_MIN + 1, 10):
        for s in itertools.product((0, 1), repeat=n):
            narr = [i for i, v in enumerate(s) if v == 0]
            for k in range(len(narr) + 1):
                for moved in itertools.combinations(narr, k):
                    o = [1 if i in moved else v for i, v in enumerate(s)]
                    assert dialogue_shift_run(tagged([src[v] for v in s]), tagged([out[v] for v in o])) < SHIFT_RUN_MIN
