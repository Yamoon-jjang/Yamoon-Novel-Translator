from __future__ import annotations
import re
from .providers.base import ProviderError, ErrorKind

TAG_RE = re.compile(r'(?m)^<P\d{6}>[ \t]*$')
ID_LINE_RE = re.compile(r'(?m)^<P(\d{6})>[ \t]*$')   # an ID on its own line, capturing the number
TAG_CAPTURE_RE = re.compile(r'(?m)^<P(\d{6})>[ \t]*\n?')


# Reasoning that "thinking" models (Qwen3, DeepSeek-R1 …) sometimes print even when
# thinking is switched off: complete <think>…</think> blocks, or just a closing tag.
_THINK_BLOCK_RE = re.compile(r'<(think|thinking|reasoning)>.*?</\1>\s*', re.S | re.I)
_THINK_CLOSE_RE = re.compile(r'</(?:think|thinking|reasoning|thinl)>', re.I)
# Model chatter that is never part of a translation (checked against the source paragraph).
_META_LEAK_RE = re.compile(
    r'</?(?:think|thinking|reasoning|thinl)>|\\boxed\{|\bfinal answer\b|\bstep by step\b|'
    r'\bthe user (?:provided|asked|wants|mentioned|is asking)\b|\blet me (?:think|check|analy[sz]e)\b|'
    r"^\s*(?:okay|alright|hmm)[,.]?\s+(?:let'?s|so|first|the user)\b", re.I | re.M)


def strip_reasoning(text: str) -> str:
    """Remove leaked reasoning before the translation starts."""
    out = _THINK_BLOCK_RE.sub('', text or '')
    first_id = re.search(r'(?m)^[ \t]*[<＜]P[0-9０-９]{1,6}[>＞]', out)
    head_end = first_id.start() if first_id else len(out)
    closes = list(_THINK_CLOSE_RE.finditer(out, 0, head_end))
    if closes:
        out = out[closes[-1].end():]   # everything up to the last stray </think> is reasoning
    return out


def meta_leak(source_para: str, out_para: str) -> str:
    """Model chatter inside a translated paragraph that the source does not contain."""
    m = _META_LEAK_RE.search(out_para or '')
    if not m:
        return ''
    return '' if _META_LEAK_RE.search(source_para or '') else m.group(0).strip()


def has_meta_leak(source: str, translation: str) -> bool:
    """True when a stored tagged translation contains model chatter (old results
    saved before the check existed are re-translated instead of reused)."""
    if not translation or not _META_LEAK_RE.search(translation):
        return False
    src = parse_tagged_translation(source or ''); out = parse_tagged_translation(translation)
    if not out:
        return True
    return any(meta_leak(src.get(k, ''), body) for k, body in out.items())


def normalize_translation_output(text: str, source: str | None = None) -> str:
    out=strip_reasoning((text or '').strip().lstrip('\ufeff')).strip()
    m=re.fullmatch(r'```(?:text|txt|markdown)?\s*\n?(.*?)\n?```',out,re.I|re.S)
    if m: out=m.group(1).strip()
    # Recover a common harmless formatting drift: tag and translation on the same line.
    out=re.sub(r'(?m)^<P(\d{6})>[ \t]+(?=\S)',r'<P\1>\n',out)
    # Repair typography only when the IDs still identify the same paragraphs.
    # A colon right after the ID ("<P000001>: 번역문") is part of the delimiter, not prose.
    out=re.sub(r'(?m)^[ \t]*(?:\*\*)?[<＜]P([0-9０-９]{1,6})[>＞](?:\*\*)?[ \t]*(?:[:：][ \t]*)?',
               lambda m:f'<P{int(m.group(1)):06d}>\n',out)
    # XML-style closing IDs ("</P000001>") that some local models add after each paragraph.
    # Only IDs this answer actually opened are removed, so prose can never be touched.
    opened={int(x) for x in re.findall(r'(?m)^<P(\d{6})>',out)}
    if opened and re.search(r'[<＜]/P[0-9０-９]{1,6}[>＞]',out):
        def _close(m):
            return '' if int(m.group(1)) in opened else m.group(0)
        out=re.sub(r'[ \t]*[<＜]/P([0-9０-９]{1,6})[>＞][ \t]*',_close,out)
        out=re.sub(r'\n{3,}','\n\n',out)
    if source:
        out=_recover_displaced_ids(out, source)
        ids=paragraph_ids(source)
        # A single paragraph needs no inferred boundaries. Never assign missing
        # IDs in a multi-paragraph response or accept an apparent refusal.
        if len(ids)==1 and not paragraph_ids(out) and not re.search(r'[<＜]P\d',out):
            letters=len(re.findall(r'[A-Za-z가-힣一-龥ぁ-ゟ゠-ヿ]',out))
            hangul=len(re.findall(r'[가-힣]',out))
            if hangul>=2 and hangul/max(1,letters)>=0.5 and not _is_refusal(out, source):
                out=ids[0]+'\n'+out
    return out


# A sent paragraph ID the model wrote without its own line: stuck to the end of the
# previous paragraph ("…했다.<P000012>"), with a full-width P, or in CJK/square brackets.
_ANY_ID_RE = re.compile(r'(?:\*\*)?[<＜〈《【\[][ \t]*[PＰ][ \t]*([0-9０-９]{1,6})[ \t]*[>＞〉》】\]](?:\*\*)?')


def _recover_displaced_ids(out: str, source: str) -> str:
    """Put a sent paragraph ID that the model wrote mid-line (or in a look-alike form)
    back on its own line. Only IDs that were sent and are missing as proper lines are
    touched, each only when it occurs exactly once, and never a form that the source
    text itself contains (source prose like 〈P12〉 stays prose)."""
    expected=[int(x) for x in ID_LINE_RE.findall(source)]
    if len(expected) < 2:
        return out
    present={int(x) for x in ID_LINE_RE.findall(out)}
    missing={pid for pid in expected if pid not in present}
    if not missing:
        return out
    prose=TAG_RE.sub('',source)   # the source text without its own ID lines
    hits={}
    for m in _ANY_ID_RE.finditer(out):
        pid=int(m.group(1))
        if pid in missing and m.group(0).strip('*') not in prose:
            hits.setdefault(pid,[]).append(m)
    spans=[ms[0] for pid,ms in hits.items() if len(ms)==1]
    if not spans:
        return out
    for m in sorted(spans, key=lambda x: x.start(), reverse=True):
        pid=int(m.group(1))
        before=out[:m.start()].rstrip(' \t'); after=out[m.end():].lstrip(' \t')
        if before and not before.endswith('\n'):
            before+='\n\n'
        out=before+f'<P{pid:06d}>'+('' if after.startswith('\n') else '\n')+after
    return out


def id_forms(raw: str, source: str) -> dict[str,int]:
    """How the sent paragraph IDs appear in a raw answer (diagnostics only, no text)."""
    forms={'own_line':0,'text_after':0,'mid_line':0,'look_alike':0,'absent':0}
    for pid in [int(x) for x in ID_LINE_RE.findall(source or '')]:
        tag=f'<P{pid:06d}>'
        if re.search(r'(?m)^[ \t]*'+re.escape(tag)+r'[ \t]*$', raw or ''):
            forms['own_line']+=1
        elif re.search(r'(?m)^[ \t]*'+re.escape(tag), raw or ''):
            forms['text_after']+=1
        elif tag in (raw or ''):
            forms['mid_line']+=1
        elif any(int(m.group(1))==pid for m in _ANY_ID_RE.finditer(raw or '')):
            forms['look_alike']+=1
        else:
            forms['absent']+=1
    return forms


SKIP_MARKER = '⟦NT_SKIP⟧'

_KO_SERVICE_TOPIC = r"(?:요청|콘텐츠|컨텐츠|내용|텍스트|번역|원문|문단|성적|노골적|선정적|해당 부분|이 부분)"
_KO_SERVICE_DENY = (r"(?:번역(?:해 드릴|해드릴|할|하기) 수 없|번역(?:해 드리기|해드리기|하기) 어렵|제공(?:할|해 드릴|해드릴) 수 없|"
                    r"처리(?:할|해 드릴) 수 없|도와(?:드릴|줄) 수 없|도와드리기 어렵|지원(?:할|하지) 수 없|진행(?:할|하기) 수 없|응(?:할|해 드릴) 수 없)")


# Apology / inability words in the SOURCE: a Korean "죄송하지만 도와드릴 수 없습니다" is then
# most likely the translated dialogue, not the model refusing.
_SRC_DENY_HINT = re.compile(r"抱歉|对不起|對不起|不好意思|不能|无法|無法|帮不了|幫不了|办不到|辦不到|"
                            r"すみません|すいません|ごめん|申し訳|できない|できません|無理|かねます|かねる|いたしません|致しません|"
                            r"お断り|断る|すまな|すまん|駄目|ダメ|できん|出来な|"
                            r"sorry|apolog|can'?t|cannot|unable|won'?t|not able|afraid|permitted|not allowed|refuse|forbid|help you", re.I)


_SERVICE_WORDS = re.compile(r"번역|요청|콘텐츠|컨텐츠|가이드라인|정책|인공지능|언어 모델|다른 (?:문장|부분|내용|텍스트|글)|"
                            r"도움이 필요|드릴게요|드리겠습니다|translat|content|policy|guideline|(?<![A-Za-z])AI(?![A-Za-z])")


def _is_refusal(text: str, source: str | None = None) -> bool:
    body=TAG_RE.sub('',text).strip()
    if not _refusal_opening(body):
        return False
    if source is None:
        return True
    src_body=TAG_RE.sub('',source).strip()
    if _SERVICE_WORDS.search(body):
        return True    # talks about translating / content / policy: the model, not a character
    if _SRC_DENY_HINT.search(src_body) and len(body) >= 0.3*len(src_body) and _narration_after(body) >= 6:
        return False   # the source says it too, and the sentence goes on (", 하고 그녀는 말했다"): dialogue
    if len(body) > 250 and len(body) >= 0.35*len(src_body):
        return False   # a full-length Korean text that merely opens with such words
    return True


def _narration_after(body: str) -> int:
    """Hangul after the "can't help" words: a bare refusal ends there ("…수 없습니다."),
    translated dialogue usually goes on (", 하고 그녀는 고개를 숙였다")."""
    m=re.search(_KO_SERVICE_DENY, body)
    if not m:
        return len(re.findall(r'[가-힣]', body))   # e.g. an English "I'm sorry, I can't…": no Korean at all -> 0
    return len(re.findall(r'[가-힣]', body[m.end():])) - 3   # minus the "습니다" ending


def _refusal_opening(body: str) -> bool:
    first=body[:220].lower().lstrip('*#> ')
    # Anchored service language avoids mistaking quoted fictional dialogue
    # containing words such as "도와드릴 수 없습니다" for a provider refusal.
    # Korean dialogue in novels normally starts with a quotation mark, so it never
    # matches these ^-anchored service patterns.
    return bool(re.search(r"^(?:죄송(?:합니다|하지만)[,.!， ]*|미안하지만\s*|sorry[,.! ]*)?(?:이 |해당 |요청하신 |제공된 |주신 )?"+_KO_SERVICE_TOPIC+r"[^.!?。\n]{0,40}?"+_KO_SERVICE_DENY,first) or
                re.search(r"^(?:죄송(?:합니다|하지만)[,.!， ]*)?(?:이 |해당 )?(?:요청은 |내용은 )?(?:도와드릴 수 없습니다|도와드리기 어렵습니다)",first) or
                re.search(r"^죄송(?:합니다|하지만)[,.!， ]*.{0,60}?"+_KO_SERVICE_DENY,first) or
                re.search(r"^(?:i'?m sorry|i am sorry|sorry|unfortunately|apologies)?[,.! ]*(?:but )?i (?:cannot|can't|can not|won't|will not|am unable to|'m unable to|am not able to|'m not able to) (?:assist|help|translate|comply|provide|fulfill|continue|produce|do that|process)",first) or
                re.search(r"^(?:i'?m sorry|i am sorry|sorry)[,.! ]*.{0,80}?(?:can't|cannot|unable to|not able to) (?:assist|help|translate|comply|provide|fulfill)",first) or
                re.search(r"^as an ai\b",first))


# Omission/censorship notes the model may slip in instead of translating.
_OMISSION_NOTE_RE = re.compile(
    r'[\(\[（【〔〈<]\s*[^\)\]）】〕〉>\n]{0,24}?'
    r'(?:생략|중략|검열|삭제됨|삭제되었|편집됨|번역하지 않|번역 제외|omitted|redacted|censored|content removed)'
    r'[^\)\]）】〕〉>\n]{0,24}?[\)\]）】〕〉>]', re.I)
# If the source itself carries an omission/censorship note, a matching Korean note is fidelity.
# Whole English words only ("cute", "execute", "haircut" are not "cut").
_SOURCE_OMISSION_RE = re.compile(r'略|删|刪|伏字|検閲|審查|审查|\b(?:omit\w*|redact\w*|censor\w*|snip(?:ped)?|cut)\b', re.I)


def has_unsourced_omission_note(source: str, output: str) -> bool:
    """An omission/censorship note in a paragraph whose own source has no such note."""
    src = parse_tagged_translation(source or ''); out = parse_tagged_translation(output or '')
    if not src or not out:
        if _SOURCE_OMISSION_RE.search(TAG_RE.sub('',source or '')):
            return False
        return bool(_OMISSION_NOTE_RE.search(TAG_RE.sub('',output or '')))
    return any(_OMISSION_NOTE_RE.search(body) and not _SOURCE_OMISSION_RE.search(src.get(pid, ''))
               for pid, body in out.items())


_JA_RUN=re.compile(r'[ぁ-ゟ゠-ヿ一-龥々〆ー]{14,}')
_ZH_RUN=re.compile(r'[一-龥]{14,}')
_EN_RUN=re.compile(r"(?:[A-Za-z][A-Za-z'’-]*[ ,]+){7,}[A-Za-z][A-Za-z'’-]*")


def untranslated_residue(src_para: str, out_para: str) -> str:
    """Return a long source-script run copied verbatim into the Korean output.

    Models sometimes keep one explicit sentence in the original language instead of
    refusing outright. Requiring a verbatim match against this paragraph's own source
    keeps names, short sound effects and deliberately foreign words from tripping it.
    """
    for rx,needs_kana in ((_JA_RUN,True),(_ZH_RUN,False),(_EN_RUN,False)):
        for m in rx.finditer(out_para or ''):
            run=m.group(0).strip(' ,')
            if needs_kana and len(re.findall(r'[ぁ-ゟ゠-ヿ]',run)) < 3:
                continue
            if rx is _EN_RUN:
                words=re.findall(r"[A-Za-z][A-Za-z'’-]*",run)
                small={'a','an','the','of','and','or','in','on','at','to','for','by','with','from'}
                if sum(w[0].isupper() or w.lower() in small for w in words) >= 0.8*len(words):
                    continue   # a title or a list of names kept in English ("Harry Potter and the Order of the Phoenix")
            probe=run if len(run) <= 40 else run[:40]
            if probe in (src_para or ''):
                return run
    return ''


def skipped_paragraph_ids(text: str) -> list[int]:
    """Paragraphs the model marked with the narrow scope-exclusion marker."""
    return [pid for pid,body in parse_tagged_translation(text).items() if body.strip()==SKIP_MARKER]


def _rebuild(parsed: dict[int,str]) -> str:
    return '\n\n'.join(f'<P{pid:06d}>\n{body}' for pid,body in parsed.items())

def paragraph_ids(text: str) -> list[str]:
    return [m.group(0).strip() for m in TAG_RE.finditer(text)]

def parse_tagged_translation(text: str) -> dict[int, str]:
    matches=list(TAG_CAPTURE_RE.finditer(text))
    result={}
    for i,m in enumerate(matches):
        start=m.end(); end=matches[i+1].start() if i+1<len(matches) else len(text)
        pid=int(m.group(1)); result[pid]=text[start:end].strip('\r\n')
    return result

def _codeish(text: str) -> bool:
    """Code, markup or data rather than prose (kept as is by a correct translation)."""
    t = (text or '').strip()
    return bool(t) and sum(ch in '{}();=<>[]#$_|\\/*+&%@`' for ch in t) >= max(3, len(t) * 0.05)


def validate_translation(source: str, output: str, ignore_pids=None, check_residue: bool = True) -> None:
    out = normalize_translation_output(output)
    if not out:
        raise ProviderError(ErrorKind.FORMAT, '번역 결과가 비어 있습니다.')
    if _is_refusal(out, source):
        raise ProviderError(ErrorKind.REFUSAL, 'API가 번역문 대신 거부 안내를 반환했습니다.')
    src_ids = paragraph_ids(source)
    out_ids = paragraph_ids(out)
    if src_ids != out_ids:
        missing=[x for x in src_ids if x not in out_ids]
        extra=[x for x in out_ids if x not in src_ids]
        reason=('표식 없는 응답' if not out_ids else '누락/추가/순서 오류')
        raise ProviderError(ErrorKind.FORMAT, f'문단 ID 불일치: source={len(src_ids)} output={len(out_ids)} · {reason} · 누락 {", ".join(missing[:3]) or "없음"} · 추가 {", ".join(extra[:3]) or "없음"}')
    src_map=parse_tagged_translation(source); out_map=parse_tagged_translation(out)
    for pid,body in out_map.items():
        leak=meta_leak(src_map.get(pid,''),body)
        if leak:
            raise ProviderError(ErrorKind.FORMAT, f'모델의 생각·설명이 번역문에 섞여 있습니다 (P{pid:06d}: {leak[:30]}) · 다시 요청')
    if SKIP_MARKER in out and any(SKIP_MARKER in body and body.strip()!=SKIP_MARKER for body in parse_tagged_translation(out).values()):
        raise ProviderError(ErrorKind.FORMAT, '문단 제외 표식이 번역문과 섞여 있습니다.')
    # Paragraphs excluded by the narrow scope marker (or already restored to source
    # after such an exclusion) are reported separately; validate only the rest.
    skip=set(skipped_paragraph_ids(out))|{int(x) for x in (ignore_pids or ())}
    if skip:
        out_parsed={k:v for k,v in parse_tagged_translation(out).items() if k not in skip}
        if not out_parsed:
            raise ProviderError(ErrorKind.REFUSAL, '모든 문단이 제외 표식으로 반환되었습니다.')
        source={k:v for k,v in parse_tagged_translation(source).items() if k not in skip}
        source=_rebuild(source); out=_rebuild(out_parsed)
    if has_unsourced_omission_note(source,out):
        raise ProviderError(ErrorKind.REFUSAL, '원문에 없는 생략/검열 표기가 번역문에 들어 있습니다 · 임의 순화 의심')
    body = TAG_RE.sub('', out).strip()
    # Code / data lines that are correctly left unchanged do not count against the Korean ratio.
    src_paras = parse_tagged_translation(source); out_paras = parse_tagged_translation(out)
    prose = '\n'.join(b for k, b in out_paras.items()
                      if not (_codeish(b) and re.sub(r'\s+', '', b) == re.sub(r'\s+', '', src_paras.get(k, ''))))
    hangul = len(re.findall(r'[가-힣]', prose))
    letters = len(re.findall(r'[A-Za-z가-힣一-龥ぁ-ゟ゠-ヿ]', prose))
    if len(prose) > 100 and letters > 0 and hangul / max(1, letters) < 0.15:
        raise ProviderError(ErrorKind.FORMAT, '응답에 한국어가 너무 적습니다(원문이 그대로 남은 듯).')
    src_body = TAG_RE.sub('', source)
    if len(src_body) > 500 and len(body) < max(100, int(len(src_body) * 0.18)):
        raise ProviderError(ErrorKind.FORMAT, f'번역 결과가 원문에 비해 비정상적으로 짧습니다. 원문 {len(src_body):,}자 → 응답 {len(body):,}자 ({len(body)/len(src_body):.1%}) · 누락/요약 또는 거부 응답 의심')
    parsed=parse_tagged_translation(out)
    if any(not str(v).strip() for v in parsed.values()):
        raise ProviderError(ErrorKind.FORMAT, '빈 번역 문단이 있습니다.')

    # Global length/language checks can miss one silently omitted paragraph in a
    # large chunk. Apply deliberately conservative paragraph-level guards only to
    # substantial paragraphs so headings, sound effects and name-only lines do not
    # become false positives.
    source_parsed=parse_tagged_translation(source)
    for pid, src_para in source_parsed.items():
        out_para=str(parsed.get(pid,'')).strip()
        src_text=str(src_para).strip()
        if len(src_text) >= 300 and len(out_para) < max(45, int(len(src_text)*0.10)):
            raise ProviderError(ErrorKind.FORMAT, f'문단 P{pid:06d} 번역이 원문에 비해 비정상적으로 짧습니다. 원문 {len(src_text):,}자 → 응답 {len(out_para):,}자 · 누락/요약 또는 거부 응답 의심')
        if check_residue:
            residue=untranslated_residue(src_text,out_para)
            if residue:
                raise ProviderError(ErrorKind.REFUSAL, f'문단 P{pid:06d}에 원문 문장이 번역되지 않고 남아 있습니다 · 일부 문장 회피 의심: {residue[:30]}…')
        if len(out_para) >= 180:
            hangul_p=len(re.findall(r'[가-힣]',out_para))
            source_script=len(re.findall(r'[一-龥ぁ-ゟ゠-ヿ]',out_para))
            if source_script >= 80 and hangul_p < max(12, int(source_script*0.08)):
                raise ProviderError(ErrorKind.FORMAT, f'문단 P{pid:06d}에 미번역 원문이 과도하게 남아 있습니다.')
    # A refusal sentence returned in place of ONE paragraph. Checked per paragraph: an
    # apology in some other paragraph's dialogue must not switch the check off.
    refusal_phrases=('도와드릴 수 없습니다','제공할 수 없습니다','번역할 수 없습니다','요청을 처리할 수 없습니다',
                     "i can't assist",'i cannot assist',"i'm unable to",'cannot comply')
    for pid, src_para in source_parsed.items():
        out_para=str(parsed.get(pid,'')).strip(); src_text=str(src_para).strip()
        if not any(x in out_para.lower() for x in refusal_phrases) or _SRC_DENY_HINT.search(src_text):
            continue
        if re.search(r'["“”「」『』]', src_text) and re.search(r'["“”「」『』]', out_para):
            continue   # quoted speech in the source and the translation: a character says it
        latin=len(re.findall(r'[A-Za-z]',src_text)) > 0.5*max(1,len(src_text))
        if len(src_text) > 60 and len(out_para) < max(120, int(len(src_text)*(0.25 if latin else 0.45))):
            raise ProviderError(ErrorKind.REFUSAL,f'문단 P{pid:06d}: 번역 대신 거부 문구가 반환되었습니다.')


# Kana and Han left in Korean text. ー (long-vowel mark), ・ and ゛゜ are punctuation-like
# and are tidied by the punctuation step (・ -> ·, ー -> ~), so they never cost a repair request.
_LEFTOVER_RE = re.compile(r'[\u3040-\u309a\u309d-\u30fa\u30fd-\u30ff\u3400-\u9fff\uf900-\ufaffｦ-ｯｱ-ﾟ]')
# 一 used as an elongation dash right after Hangul ("안 돼一!") is not a leftover Han character.
_ELONGATION_ONE = re.compile(r'(?<=[가-힣~!?…])一+(?=$|[\s!?…~.,\'"”’」』)\]])')
_HANGUL_RE = re.compile(r'[가-힣]')


def leftover_script(body: str, allowed: str = '') -> list[str]:
    """Chinese characters / kana left inside a Korean paragraph (characters the user
    deliberately put in confirmed terminology are allowed)."""
    if not body or not _HANGUL_RE.search(body):
        return []
    ok = set(allowed or '')
    body = re.sub(r'［＃[^］\r\n]{1,200}］', '', _ELONGATION_ONE.sub('', body))   # Aozora markup kept on purpose
    return [ch for ch in _LEFTOVER_RE.findall(body) if ch not in ok]
