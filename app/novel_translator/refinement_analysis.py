"""Reference table for refining a Korean TXT that has no translation record (v2.6.2 보완 3).

The same way the translation builds its glossary and 인물 말투표, done on Korean text:
1. Count name candidates over the WHOLE book (2~4-letter Hangul stems before a particle,
   an honorific or a speech verb), as the translation counts 2~4-character candidates.
2. One request asks which candidates are names (people, places, groups, terms).
3. Dialogue mentioning those people is sampled evenly over the whole book and the speech
   table is built with the translation's own prompt, JSON mode and strict validation
   (speech_levels), split into three batches only when the whole sample fails.

The result is written in the same form as a translation record's table, so the window shows
it the same way and the refinement prompt reads it as before.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter

from .providers.base import TranslationRequest, TranslationCancelled, ProviderError, ErrorKind

NAME_CANDIDATES = 150
NAME_MIN_COUNT = 5
NAMES_MAX = 80

# Particles and endings that follow a noun (longest first). A name is almost always used
# with one of them, so the stem in front is the candidate.
_PARTICLES = sorted(['에게서', '한테서', '께서는', '께서도', '에게는', '에게도', '한테는', '이라고', '이라는', '이랑',
                     '에게', '한테', '께서', '라고', '라는', '하고', '처럼', '보다', '까지', '부터', '에서', '으로',
                     '로서', '이와', '이나', '마저', '조차', '밖에', '이여', '이야', '씨는', '씨가', '씨를', '씨의',
                     '님은', '님이', '님의', '씨', '님', '랑', '로', '은', '는', '이', '가', '을', '를', '의', '에',
                     '와', '과', '도', '만', '야', '아', '여'], key=len, reverse=True)
_HONORIFIC_NEXT = re.compile(r'^(?:씨|님|양|군|상|선배|선생|부인|여사|사장|과장|부장|대리|형|누나|언니|오빠)')
_SPEECH_NEXT = re.compile(r'^(?:말했|물었|대답|소리|중얼|외쳤|속삭|웃었|웃으며|고개|외치|말하|되물|덧붙|불렀)')
# Pronouns and words a Korean novel uses everywhere: they only take room in the list.
_STOP = frozenset('''그녀 그는 그가 자신 자기 사람 남자 여자 얼굴 모습 생각 마음 시간 우리 당신 여기 거기 저기 지금 정말 그것 이것
저것 무엇 아무 모두 때문 다시 조금 하나 이번 그런 이런 저런 그때 이때 순간 정도 다음 처음 마지막 앞에 뒤에 사이 위에 아래 안에
밖에 사실 이상 이제 그래 그냥 아직 벌써 가슴 머리 눈물 손가락 엉덩이 다리 허리 입술 목소리 표정 몸을 몸이 몸은 하루 오늘 내일
어제 이야기 일이 것이 것을 것은 수가 수도 줄을 줄도 데가 데를 듯이 채로 대로 만큼 동안 이후 이전 주위 근처 방안 문을 문이
부인 아가씨 남편 아내 엄마 아빠 어머니 아버지 언니 오빠 누나 형님 선생 선생님 사장 사모님 마님 주인 손님 아이 아들 딸이 소녀
소년 노인 녀석 놈이 년이 계집 사내 상대 친구 동생'''.split())
_HANGUL_RUN = re.compile(r'[가-힣]{2,}')
_ADDRESS = frozenset({'엄마', '아빠', '어머니', '아버지', '어머님', '아버님', '언니', '오빠', '누나', '누님', '형님', '형',
                      '선생님', '선생', '사장님', '사장', '부인', '여보', '당신', '아가씨', '아저씨', '아줌마', '할머니',
                      '할아버지', '사모님', '마님', '주인님', '도련님', '아씨', '사부님', '선배', '후배', '아들', '딸'})
# Address words that reveal who speaks to whom (the translation's cue words for the sample).
_CUES = ('씨', '님', '선생님', '부인', '여보', '언니', '오빠', '누나', '형님', '엄마', '아빠', '어머니', '아버지',
         '아가씨', '마님', '사장님', '선배')


def _stem(word: str) -> tuple[str, bool]:
    """(noun stem, True when a particle was cut off)."""
    for p in _PARTICLES:
        if word.endswith(p) and len(word) - len(p) >= 2:
            return word[:-len(p)], True
    return word, False


def name_candidates(text: str, limit: int = NAME_CANDIDATES) -> list[tuple[str, int]]:
    """Frequency-ranked name candidates from the whole Korean book (stems used as nouns)."""
    noun, bare, bonus = Counter(), Counter(), Counter()
    words = _HANGUL_RUN.findall(text or '')
    for i, w in enumerate(words):
        stem, cut = _stem(w)
        if len(stem) > 4 or (cut and stem.endswith('으') and w.endswith('으로')):
            continue
        if cut:
            noun[stem] += 1
        elif len(w) <= 4:
            bare[w] += 1
        nxt = words[i + 1] if i + 1 < len(words) else ''
        if nxt and len(stem) >= 2:
            if _HONORIFIC_NEXT.match(nxt) or (cut and w[len(stem):] in ('가', '이', '는', '은') and _SPEECH_NEXT.match(nxt)):
                bonus[stem] += 2
    scored = Counter()
    for stem, n in noun.items():
        if stem in _STOP or stem in _ADDRESS:
            continue
        scored[stem] = n + bare.get(stem, 0) + bonus.get(stem, 0)
    top = [(k, v) for k, v in scored.most_common() if v >= NAME_MIN_COUNT][:limit * 2]
    # A shorter stem that is nearly always the start of a longer one (시즈 in 시즈코) goes.
    out = []
    for k, v in top:
        if any(len(t) > len(k) and t.startswith(k) and tv >= v * 0.8 for t, tv in top):
            continue
        out.append((k, v))
    return out[:limit]


def candidate_lines(text: str, cands, width: int = 18) -> dict[str, str]:
    """One short line from the book for each candidate, so the model can tell a name from a word."""
    out = {}
    for k, _ in cands:
        i = (text or '').find(k)
        if i < 0:
            continue
        a, b = max(0, i - width), min(len(text), i + len(k) + width)
        out[k] = re.sub(r'\s+', ' ', text[a:b]).strip()
    return out


def name_prompts(cands, lines) -> tuple[str, str]:
    system = ('You build a name list for editing a Korean novel. You receive frequency-ranked words from the book, each with '
              'one short line where it appears. Select only personal names and nicknames, place names, organizations, and terms '
              'particular to this novel. Be complete: include EVERY character name, main and minor, family names and given '
              'names as separate entries. Do not select ordinary words, pronouns, kinship or address words (엄마, 언니, 오빠, '
              '부인, 사장님, 선생님), job titles, or verbs and adjectives. When unsure whether a word is a character\'s name, '
              'include it. Return ONLY one JSON object: {"names": [{"name": "...", "kind": "인물|장소|단체|용어"}]}. No commentary.')
    rows = '\n'.join(f'{k}\t{n}\t{lines.get(k, "")}' for k, n in cands)
    user = f'WORDS (word<TAB>count<TAB>example line):\n{rows}\n\nReturn the JSON object now.'
    return system, user


_KINDS = {'인물': '인물', 'person': '인물', 'character': '인물', 'name': '인물', '사람': '인물',
          '장소': '장소', 'place': '장소', 'location': '장소', '지명': '장소',
          '단체': '단체', 'organization': '단체', 'group': '단체', '조직': '단체',
          '용어': '용어', 'term': '용어', 'item': '용어'}


def parse_names(raw: str, text: str) -> dict[str, str]:
    """{name: kind} from the answer, strictly: Hangul only, in the book at least 3 times,
    never an address word. Accepts {"names": [...]}, a list of strings, or {name: kind}."""
    raw = re.sub(r'(?is)<think>.*?</think>', '', str(raw or ''))
    found = []
    for m in re.finditer(r'\{[^{}]*\}', raw):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get('name') or obj.get('이름') or obj.get('word')
        if isinstance(name, str):
            found.append((name, str(obj.get('kind') or obj.get('type') or obj.get('종류') or '인물')))
        else:
            found += [(k, str(v)) for k, v in obj.items() if isinstance(v, str) and k not in ('names',)]
    if not found:
        try:
            data = json.loads(raw[raw.find('['):raw.rfind(']') + 1]) if '[' in raw else []
            found = [(x, '인물') for x in data if isinstance(x, str)]
        except ValueError:
            found = []
    out = {}
    for name, kind in found:
        name = re.sub(r'\s+', ' ', str(name)).strip().strip('"\'“”‘’*`')
        if not re.fullmatch(r'[가-힣]{2,6}(?: [가-힣]{1,6})?', name) or name in _ADDRESS or name in _STOP:
            continue
        if (text or '').count(name) < 3 or name in out:
            continue
        out[name] = _KINDS.get(kind.strip().lower(), _KINDS.get(kind.strip(), '인물'))
        if len(out) >= NAMES_MAX:
            break
    return out


def _request(ask, provider, model, key, stop, system, user, local_json: bool, max_output: int):
    return ask(provider, TranslationRequest(model, system, user, temperature=.1, timeout=180,
                                            purpose='json' if local_json else 'analysis',
                                            max_output=max_output, partial_ok=True), key, stop)


def _fatal(e: ProviderError) -> bool:
    return e.kind in (ErrorKind.AUTH, ErrorKind.KEY_BLOCKED, ErrorKind.QUOTA, ErrorKind.FATAL)


def render_table(rows, names: dict[str, str]) -> str:
    """The table in the same form as a translation record's (refinement_links.reference_guidance)."""
    parts = ['[샘플 분석 기준표 · 책 전체의 이름과 대사로 자동 추론 · 원고와 다르면 원고를 따름]']
    if rows:
        parts.append('인물 말투(화자 → 상대: 말투):')
        parts += [f"- {r['speaker']} → {r['listener']}: {r['level']}" + (f" ({r['note']})" if r.get('note') else '')
                  for r in rows]
    if names:
        parts.append('이름·용어(표기를 바꾸지 말 것): ' + ', '.join(f'{n}({k})' for n, k in names.items()))
    return '\n'.join(parts) if (rows or names) else ''


def build_reference(text: str, provider, model: str, key: str, *, ask, stop, event, limit: int = 3000) -> str:
    """The reference table for a Korean TXT with no translation record ('' when nothing was found).
    `ask(provider, request, key, stop)` sends one request (refinement_runtime._ask)."""
    from .speech_levels import (sample_dialogue, build_prompts, parse_table, split_samples, rank_names,
                                role_entries, role_parse_names, clean_rows, ROSTER_MAX, MAX_ROWS, ANSWER_TOKENS)
    local = getattr(provider, 'name', '') == 'local'
    started = time.monotonic()
    # 1. Names, counted over the whole book.
    cands = name_candidates(text)
    if not cands:
        event('번역 기록 없음 · 이름 후보를 찾지 못해 기준표 없이 윤문 진행')
        return ''
    event(f'번역 기록 없음 · 책 전체에서 이름 후보 {len(cands)}개를 세어 인물·용어 확인 요청 중')
    asked = time.monotonic()
    system, user = name_prompts(cands, candidate_lines(text, cands))
    try:
        reply = _request(ask, provider, model, key, stop, system, user, local, 4096)
        names = parse_names(reply.text, text)
    except TranslationCancelled:
        return ''
    except ProviderError as e:
        if _fatal(e):
            raise
        event(f'인물·용어 확인 실패({e.kind.value}) · 기준표 없이 윤문 진행')
        return ''
    except Exception as e:      # never stops the editing itself
        event(f'인물·용어 확인 실패({type(e).__name__}) · 기준표 없이 윤문 진행')
        return ''
    if stop():
        return ''
    people = {n: n for n, k in names.items() if k == '인물'}
    shown = ', '.join(list(names)[:8]) + (' …' if len(names) > 8 else '')
    event(f'인물·용어 확인 · 인물 {len(people)}명 · 장소·단체·용어 {len(names) - len(people)}개'
          + (f' ({shown})' if names else '') + f' · 응답 {time.monotonic() - asked:.0f}초')
    # 2. The speech table, as the translation builds it.
    rows = []
    roles = role_entries(text, 'ko', taken=people.keys())
    roster = {**roles, **rank_names(text, people, limit=max(2, ROSTER_MAX - len(roles)))}
    parse_names_ = {**role_parse_names(roles), **people}
    samples = sample_dialogue(text, roster, cues=_CUES) if len(roster) >= 2 else ''
    if not samples:
        event('인물 말투표 · 인물 이름이 나오는 대사를 찾지 못해 말투표 없이 진행')
    else:
        event(f'인물 말투표 만들기 · 대사에 많이 나오는 인물 {len(roster) - len(roles)}명'
              + (f' + 화자·가족 호칭 {len(roles)}개' if roles else '')
              + f' · 대사 표본 {len(samples):,}자')
        asked = time.monotonic()

        def table(sample_text):
            s, u = build_prompts(sample_text, roster, 'ko', as_json=local)
            r = _request(ask, provider, model, key, stop, s, u, local, ANSWER_TOKENS)
            return parse_table(r.text, parse_names_)

        how = '전체 문맥 1회 분석'
        try:
            rows = table(samples)
            if not rows and not stop():
                # A failed/empty whole sample alone is not enough: split only now, and drop the
                # pairs whose level the batches disagree on (as the translation does).
                parts = split_samples(samples)
                grouped, conflicts, ok = {}, set(), 0
                for part in parts:
                    if stop():
                        break
                    try:
                        got = table(part)
                    except TranslationCancelled:
                        raise
                    except ProviderError as e:
                        if _fatal(e):
                            raise
                        got = []
                    except Exception:
                        got = []
                    ok += bool(got)
                    for row in got:
                        k = (row['speaker'], row['listener'])
                        if k not in grouped:
                            grouped[k] = row
                        elif grouped[k]['level'] != row['level']:
                            conflicts.add(k)
                rows = [r for k, r in grouped.items() if k not in conflicts][:MAX_ROWS]
                how = f'분할 분석 {len(parts)}묶음 중 {ok}묶음 성공' + (f' · 말투 충돌 {len(conflicts)}쌍 제외' if conflicts else '')
        except TranslationCancelled:
            return ''
        except ProviderError as e:
            if _fatal(e):
                raise
            event(f'인물 말투표 만들기 실패({e.kind.value}) · 이름만 기준표로 씀')
            rows = []
        except Exception as e:
            event(f'인물 말투표 만들기 실패({type(e).__name__}) · 이름만 기준표로 씀')
            rows = []
        if stop():
            return ''
        rows = clean_rows(rows)
        if rows:
            event(f'인물 말투표 · {len(rows)}쌍 · {how} · 응답 {time.monotonic() - asked:.0f}초 · '
                  + ', '.join(f"{r['speaker']}→{r['listener']} {r['level']}" for r in rows[:6]) + (' …' if len(rows) > 6 else ''))
        else:
            event(f'인물 말투표 · 대사에서 확실한 관계를 찾지 못함 · {how}')
    out = render_table(rows, names)
    if len(out) > limit:
        cut = out[:limit]
        out = cut[:cut.rfind('\n')] if '\n' in cut else cut
    return out
