"""Korean-only literary editing. Never touches foreign-language translation storage."""
from __future__ import annotations
from hashlib import sha256
from pathlib import Path
import re
import time
from .chunking import split_paragraphs, escape_protocol_literals
from .config import DATA_DIR, load_json, save_json
from .textio import atomic_write_text
from .providers.base import TranslationRequest, ProviderError, ErrorKind
from .refinement_links import find_reference, reference_guidance, reference_summary, GUIDANCE_MAX

_ID = re.compile(r'(?m)^<P([0-9]{6})>[ \t]*\r?\n')

# An assistant's inability-to-edit notice is not a revised passage. Keep the
# matcher narrow: it must be a present-tense, polite first-person refusal
# ("…드릴 수 없습니다"), not narration ("수정할 수 없었다") and not a quoted line.
_DENY = (r'(?:(?:다듬어|교정해|수정해|도와|제공해|처리해)[ \t]*드릴[ \t]*수[ \t]*없(?:습니다|어요|음)|'
         r'(?:교정|수정|윤문|제공)(?:할|하는[ \t]*것은?)[ \t]*수[ \t]*없(?:습니다|어요|음)|'
         r'요청을[ \t]*처리할[ \t]*수[ \t]*없(?:습니다|어요|음)|'
         r'cannot[ \t]+(?:help|assist|comply)|'
         r'can.t[ \t]+(?:help|assist|comply))')
_REFUSAL_NOTICE = re.compile(
    r'(?:^|\n)\s*(?:죄송하지만[ ,]*|미안하지만[ ,]*|유감스럽게도[ ,]*)?'
    r'(?:저는[ \t]*|제가[ \t]*|해당[ \t]*)?'
    r'[^\n]{0,90}?(?P<deny>' + _DENY + r')',
    re.IGNORECASE,
)
_REFUSAL_ALTERNATIVE = re.compile(
    r'(?:성적[ \t]*묘사를[ \t]*(?:제외|뺀)|'
    r'성적인[ \t]*부분을[ \t]*(?:제외|뺀)|'
    r'대신[ \t]*(?:일반|비성적)|'
    r'non.sexual[ \t]+(?:parts|content))',
    re.IGNORECASE,
)
_ALT_DENY = re.compile(r'(?:(?:도와|다듬어|교정해)[ \t]*드릴[ \t]*수[ \t]*없(?:습니다|어요|음)|교정[ \t]*불가)')
_QUOTED = re.compile(r'[“"「『][^”"」』\n]*[”"」』]')

# Foreign script left in a Korean text (the translation's leftover kanji/kana).
_FOREIGN = re.compile(r'[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]')
_FOREIGN_SPAN = re.compile(r'[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]+')

# Dialogue speech level: polite (존댓말) and plain (반말) sentence endings.
_DIALOGUE = re.compile(r'[“"「『]([^”"」』\n]{1,400})[”"」』]')
_POLITE_END = re.compile(r'(?:요|니다|니까|세요|십시오|십시요|지요|죠|소서)[.!?…~\s]*$')
_HANGUL_END = re.compile(r'[가-힣][.!?…~\s]*$')
_SENTENCE = re.compile(r'[^.!?…]+[.!?…]*')


class RefinementRefused(ValueError):
    """The provider returned a refusal notice rather than edited prose."""


class MissingParagraph(ValueError):
    """An otherwise valid reply that lacks exactly one paragraph (a small local model
    sometimes merges it into the paragraph before it)."""

    def __init__(self, accepted, missing):
        super().__init__(f'문단 {missing}: 응답에서 빠짐 · 나머지는 정상')
        self.accepted=accepted
        self.missing=missing


class PartialRefinementRefused(RefinementRefused):
    """Tagged response with independently valid edited paragraphs and failed ones.

    `rejected` maps a paragraph to why it failed; `refused` is the subset that the
    model refused (never re-requested), the rest failed validation (asked again alone)."""

    def __init__(self, accepted, rejected, refused=()):
        super().__init__('일부 문단의 윤문 거부 · 정상 교정문은 유지')
        self.accepted=accepted
        self.rejected=rejected
        self.refused=set(refused)


def _squash(text: str) -> str:
    return re.sub(r'\s+', '', text or '').lower()


def is_refusal_notice(body: str, original: str = '') -> bool:
    """A first-person "I cannot edit this" notice instead of edited prose.

    `original` is the paragraph that was sent for editing. When the same words are
    already in it (a character says "더는 수정할 수 없습니다"), the edited paragraph is
    simply keeping that dialogue, not refusing. Narration ("수정할 수 없었다") and
    quoted dialogue never count.
    """
    cleaned = (body or '').strip()
    if not cleaned or len(cleaned) > 700:
        return False
    # Never classify an embedded quote in an otherwise substantial narrative
    # just because its characters discuss a refusal.
    if cleaned.startswith(('“','"','「','『','\'')):
        return False
    unquoted = _QUOTED.sub(' ', cleaned)
    original_squashed = _squash(original)
    m = _REFUSAL_NOTICE.search(unquoted)
    if m:
        return not (original_squashed and _squash(m.group('deny')) in original_squashed)
    alt = _REFUSAL_ALTERNATIVE.search(unquoted)
    deny = _ALT_DENY.search(unquoted)
    if not (alt and deny):
        return False
    return not (original_squashed and (_squash(alt.group(0)) in original_squashed
                                       or _squash(deny.group(0)) in original_squashed))


def _body_problem(body: str, original: str) -> str:
    """Why an edited paragraph cannot be used ('' when it is fine)."""
    text = (body or '').strip()
    if not text or len(text) < max(1, int(len(original.strip()) * .25)):
        return '출력이 지나치게 짧습니다'
    if len(body) > max(200, len(original)*3):
        return '출력이 지나치게 길어졌습니다'
    # The editor may turn leftover kanji/kana into Korean, never add new ones.
    added = set(_FOREIGN.findall(text)) - set(_FOREIGN.findall(original))
    if added:
        return '원문에 없던 한자·가나가 생겼습니다'
    return ''


def model_reply_to_text(reply: str, paragraphs) -> list[str]:
    """Require exact ID sequence and substantial body for *each* paragraph."""
    reply = (reply or '').strip()
    hits = list(_ID.finditer(reply))
    # A tagged reply is judged paragraph by paragraph against each original below; the
    # whole-reply check is only for an untagged notice.
    if not hits and is_refusal_notice(reply, '\n'.join(p.text for p in paragraphs)):
        raise RefinementRefused('모델이 문학적 윤문을 거부했습니다 · 원문 보존')
    if len(hits) != len(paragraphs) or not hits or reply[:hits[0].start()].strip():
        raise ValueError('문단 ID가 누락되거나 설명 문구가 섞였습니다.')
    if [int(m.group(1)) for m in hits] != [p.pid for p in paragraphs]:
        raise ValueError('문단 ID가 중복되었거나 순서가 다릅니다.')
    result = []
    for i, hit in enumerate(hits):
        end = hits[i+1].start() if i+1 < len(hits) else len(reply)
        body = reply[hit.end():end].strip('\r\n')
        original = paragraphs[i].text
        if is_refusal_notice(body, original):
            raise RefinementRefused(f'문단 {paragraphs[i].pid}: 모델 윤문 거부 · 원문 보존')
        problem = _body_problem(body, original)
        if problem:
            raise ValueError(f'문단 {paragraphs[i].pid}: {problem}.')
        if _ID.search(body):
            raise ValueError('응답 안에 예상 밖 문단 ID가 있습니다.')
        result.append(body)
    return result


def verified_partial_reply(reply: str, paragraphs):
    """Extract only independently valid ID bodies; never publish a refusal notice.

    Returns (accepted, rejected, refused): rejected maps every unusable paragraph
    to a reason, refused is the subset the model refused."""
    reply=(reply or '').strip()
    matches=list(_ID.finditer(reply))
    if not matches or reply[:matches[0].start()].strip():
        return {}, {}, set()
    wanted={p.pid:p for p in paragraphs}
    accepted={}
    rejected={}
    refused=set()
    seen=set()
    for i,hit in enumerate(matches):
        pid=int(hit.group(1))
        if pid in seen or pid not in wanted:
            return {}, {}, set()
        seen.add(pid)
        end=matches[i+1].start() if i+1<len(matches) else len(reply)
        body=reply[hit.end():end].strip('\r\n')
        p=wanted[pid]
        if is_refusal_notice(body, p.text):
            rejected[pid]='모델 윤문 거부 · 원문 보존'; refused.add(pid)
        elif _body_problem(body, p.text):
            rejected[pid]='문단 본문 검증 실패 · 원문 보존'
        else:
            accepted[pid]=body
    return accepted,rejected,refused


def refinement_prompt(paragraphs, previous: str = '', following: str = '', guidance: str = '',
                      confirmed: bool = False) -> tuple[str, str]:
    # Literary fluency is the primary objective; preserve story facts, not wording.
    system = (
        '당신은 이미 작성되어 한국어로 번역된 소설의 문장을 다듬는 문학 교정자입니다. 새 이야기를 쓰는 작업이 아니라 기존 문장의 언어적 교정입니다. '
        '핵심 임무는 최소한의 교정이 아니라 읽기 좋은 한국어 소설의 문장 흐름을 만드는 것입니다. '
        '번역투, 직역 어순, 어색한 조사·어미, 부자연스러운 대사와 기계적인 문장 연결을 적극적으로 다시 쓰세요. '
        '어색한 문장은 단어 몇 개만 치환하지 말고 문장 구조와 호흡을 자연스럽게 재구성하세요. '
        '문단 안에서는 문장을 합치거나 나눌 수 있으며 불필요한 주어·대명사·반복은 덜어내고 '
        '문맥상 필요한 연결은 부드럽게 보완하세요. '
        '서술 시점과 장면의 긴장감, 감정 흐름에 어울리는 자연스러운 대사를 쓰되, '
        '대사의 존댓말·반말 수준과 호칭, 인물 이름의 표기는 원고 그대로 지키세요. 어미는 자연스럽게 고쳐도 존댓말을 반말로, 반말을 존댓말로 바꾸지 마세요. '
        '그러나 줄거리, 사건의 진행, 인물 관계, 행동의 주체, 말하는 사람, 중요한 사실과 대사의 의도·표현 수위는 바꾸지 마세요. '
        '성적인 장면이나 거친 표현도 원고의 수위 그대로 두고 순화하거나 생략하지 마세요. '
        '새 사건을 만들거나 장면을 요약·삭제하지 말고 원문의 장르와 분위기를 존중하세요. '
        '이미 자연스러운 문장은 억지로 바꾸지 않아도 되지만, 원문 표현을 유지하는 것을 목표로 삼지 마세요. '
        '입력된 문단 ID는 문서 조립을 위한 표식입니다. 문단 내부 문장 재구성은 자유롭지만 '
        '문단 ID·개수·순서를 유지하고 서로 다른 문단의 내용을 섞지 마세요. '
        '각 <P000001> 형식 ID를 독립된 줄로 출력하고 바로 아래에 교정문을 작성하세요. '
        '앞뒤 문맥은 참고만 하고 결과에 포함하지 마세요. 설명·주석·머리말·코드 펜스 없이 결과만 출력하세요.'
    )
    tagged = '\n\n'.join(f'<P{p.pid:06d}>\n{escape_protocol_literals(p.text)}' for p in paragraphs)
    leftovers = []
    for p in paragraphs:
        for span in _FOREIGN_SPAN.findall(p.text):
            if span not in leftovers:
                leftovers.append(span)
    if guidance:
        head = ('인물·용어·말투 기준표(번역할 때 확정된 값이므로 그대로 따름):\n' if confirmed
                else '인물·용어·말투 기준표(자동 분석 참고용 · 불확실한 관계는 단정하지 않음 · 원고와 다르면 원고를 따름):\n')
    user = ('다음 번역 소설을 자연스러운 한국어 소설 문체로 적극적으로 윤문하세요. 어색한 문장은 문장 구조부터 다시 쓰되 줄거리와 사실관계는 유지하세요. 문단 내부 문장 구분은 자유롭지만 문단 ID·순서는 그대로 지키세요.\n'
            + (head+guidance[:GUIDANCE_MAX]+'\n' if guidance else '')
            + ('번역에서 남은 한자·가나가 있습니다: ' + ', '.join(leftovers[:40])
               + '. 문맥에 맞는 한국어로 옮기세요(인명·지명은 앞뒤 문맥과 기준표의 한글 표기를 따름).\n' if leftovers else '')
            + ('앞 문맥(교정 대상 아님): '+previous[-900:]+'\n' if previous else '')
            + ('뒤 문맥(교정 대상 아님): '+following[:650]+'\n' if following else '')
            + '\n교정 대상:\n'+tagged)
    return system, user


def format_duration(seconds: float) -> str:
    """12초 / 4분 05초 / 1시간 02분 03초"""
    total = max(0, int(round(float(seconds or 0))))
    h, rest = divmod(total, 3600); m, sec = divmod(rest, 60)
    if h:
        return f'{h}시간 {m:02d}분 {sec:02d}초'
    if m:
        return f'{m}분 {sec:02d}초'
    return f'{sec}초'


MAX_REFINEMENT_BYTES = 10_000_000  # 10 MB decimal; applies to refinement only.


def group_paragraphs(paragraphs, max_chars=4500, max_items=36):
    """Keep consecutive dialogue together without splitting source paragraph IDs."""
    groups, current, chars = [], [], 0
    for p in paragraphs:
        size = len(p.text)
        chapter = bool(re.match(r'^\s*(?:제\s*\d+\s*[장화회편]|프롤로그|에필로그|외전)', p.text))
        scene_gap = bool(current and current[-1].separator_after.count('\n') >= 2)
        if current and (len(current) >= max_items or chars + size > max_chars or
                        (chapter and chars >= max_chars // 3) or
                        (scene_gap and chars >= max_chars * 0.75)):
            groups.append(current)
            current, chars = [], 0
        current.append(p)
        chars += size
    if current:
        groups.append(current)
    return groups


def analysis_sample(paragraphs, limit=11000):
    """Bounded evidence from beginning, middle and end of a long novel."""
    if not paragraphs:
        return ''
    indices=list(range(min(18,len(paragraphs))))
    if len(paragraphs)>35:
        mid=len(paragraphs)//2
        indices+=list(range(max(0,mid-10),min(len(paragraphs),mid+10)))
        indices+=list(range(max(0,len(paragraphs)-18),len(paragraphs)))
    out=[]; chars=0
    for i in dict.fromkeys(indices):
        p=paragraphs[i]
        text=f'문단 {p.pid}: {p.text[:300]}'
        if chars+len(text)>limit:
            break
        out.append(text);chars+=len(text)
    return '\n'.join(out)


def _clip_lines(text: str, limit: int) -> str:
    """Cut a table at a line boundary so no row is left half-written."""
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind('\n')
    return (cut[:nl] if nl > limit // 2 else cut).rstrip()


def prepare_guidance(paragraphs, provider, model, key, *, stop, event):
    """One bounded model pass, never a full-book LLM scan."""
    if stop(): return ''
    sample=analysis_sample(paragraphs)
    if not sample: return ''
    event('번역 기록 없음 · 인물·용어·말투 샘플 분석 요청 중')
    system=('한국어 소설 분석 편집자입니다. 이름·별명·호칭·인물 관계·말투를 표로 간결하게 정리하세요. '
            '말투는 "화자 → 상대: 존댓말/반말" 형식으로 한 줄에 하나씩 쓰세요. '
            '직접 확인되는 사실만 쓰고 불확실한 관계는 미확인이라고 표시하세요. 본문을 재작성하지 마세요. '
            f'전체 {GUIDANCE_MAX}자 안으로 쓰세요.')
    try:
        reply=provider.translate(TranslationRequest(model,system,sample,temperature=.1,timeout=180,purpose='analysis'),key)
    except ProviderError as e:
        if e.kind in (ErrorKind.AUTH,ErrorKind.KEY_BLOCKED,ErrorKind.QUOTA,ErrorKind.FATAL):
            raise
        event('인물 분석 응답 실패 · 문학적 윤문은 계속 진행')
        return ''
    if stop(): return ''
    return _clip_lines(reply.text or '', GUIDANCE_MAX)


def _speech_levels(text: str) -> tuple[int, int]:
    """(polite, plain) sentence endings inside the quoted dialogue of a passage."""
    polite = plain = 0
    for m in _DIALOGUE.finditer(text or ''):
        for s in _SENTENCE.findall(m.group(1)):
            s = s.strip()
            if not s:
                continue
            if _POLITE_END.search(s):
                polite += 1
            elif _HANGUL_END.search(s) and (len(s.split()) >= 2 or len(re.findall(r'[가-힣]', s)) >= 3):
                plain += 1     # a sentence of its own (not a bare call like "엄마!")
    return polite, plain


def speech_level_changed(original: str, edited: str) -> bool:
    """존댓말 dialogue turned into 반말 (or the reverse) in one paragraph."""
    op, oc = _speech_levels(original)
    ep, ec = _speech_levels(edited)
    if op + oc == 0 or ep + ec == 0:
        return False
    if op and not ep and ec:
        return True
    if oc and not op and ep:
        return True
    return False


def review_reason(parts, bodies, names=()) -> str:
    """Why an edited passage needs the continuity review ('' when it does not)."""
    original = '\n'.join(p.text for p in parts)
    edited = '\n'.join(bodies)
    if not (len(edited) >= len(original)*.72 and len(edited) <= len(original)*1.55):
        return '분량 변화'
    if any(speech_level_changed(p.text, b) for p, b in zip(parts, bodies)):
        return '존댓말·반말 변화'
    lost = [n for n in names if len(n) >= 2 and n in original and n not in edited]
    if lost:
        return '이름 사라짐(' + ', '.join(lost[:3]) + ')'
    return ''


def continuity_check(original, edited, provider, model, key, *, stop, event, reason='분량 변화'):
    """Bounded selective review; never replace edited text from a reviewer response."""
    if stop(): return True  # Preserve the already accepted output; skip optional remote review.
    if not reason:
        return True
    event(f'내용 변경 가능성 감지({reason}) · 연속성 확인 요청 중')
    system=('두 한국어 장면을 비교하고 중요한 인물, 이름, 호칭, 존댓말·반말, 말하는 사람, 행동, 대화의 뜻이나 사건, '
            '표현 수위가 삭제·추가·반전·변경되었는지 판단하세요. 문장 표현과 어순만 다른 것은 문제가 아닙니다. '
            '문제가 있으면 CHANGED, 없으면 SAME만 답하세요.')
    response=provider.translate(TranslationRequest(model,system,purpose='analysis',user_prompt=
        '원문:\n'+original[:5000]+'\n\n수정문:\n'+edited[:6500],temperature=.1,timeout=180),key)
    return response.text.strip().upper().startswith('SAME')


def checkpoint_path(source: Path, root: Path | None = None):
    """Resume file of builds before v2.1 (keyed by the file's path; kept for migration)."""
    identity = sha256(str(source.resolve()).casefold().encode('utf-8')).hexdigest()[:24]
    return (root or DATA_DIR / 'refinement_jobs') / (identity + '.json')


def content_checkpoint_path(text_digest: str, root: Path | None = None):
    """Resume file keyed by the book's text, so a renamed or moved TXT still resumes."""
    return (root or DATA_DIR / 'refinement_jobs') / ('text-' + text_digest[:32] + '.json')


def default_max_chars(provider_name: str) -> int:
    # Long generations are notably less reliable on local GGUF models.
    return 3500 if provider_name == 'local' else 4500


def job_identity(text_digest: str, provider_name: str, model: str, max_chars: int, max_items: int) -> str:
    """What makes finished paragraphs reusable: the text and the editing engine.
    The analysis engine is not part of it (its table is kept in the resume file)."""
    return sha256((text_digest+'\0'+str(model)+'\0'+str(provider_name)+
                   '\0refinement-v5-fluent-' + str(max_chars) + '-' + str(max_items)).encode()).hexdigest()


def _legacy_identity(text_digest, provider_name, model, max_chars, max_items, analysis_model, analysis_name) -> str:
    """Identity of v2.4 and earlier (included the analysis engine)."""
    return sha256((text_digest+'\0'+str(model)+'\0'+str(provider_name)+
                   '\0refinement-v4-fluent-' + str(max_chars) + '-' + str(max_items) + '\0' + str(analysis_model) + '\0' + str(analysis_name)).encode()).hexdigest()


def _file_sha(path: Path) -> str:
    try:
        return sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ''


def retry_status(source: Path, output: Path, provider_name: str, model: str, *,
                 checkpoint_dir: Path | None = None, max_items: int = 36) -> int:
    """Paragraphs a finished run could not edit and can try again into the same,
    untouched result file (0 when there is nothing to retry that way)."""
    try:
        from .textio import read_txt
        text, _ = read_txt(Path(source))
        digest = sha256(text.encode('utf-8','surrogatepass')).hexdigest()
        cp = content_checkpoint_path(digest, checkpoint_dir)
        if not cp.exists() or not Path(output).exists():
            return 0
        state = load_json(cp)
        identity = job_identity(digest, provider_name, model, default_max_chars(provider_name), max_items)
        if state.get('identity') != identity or not state.get('finished'):
            return 0
        if state.get('output_sha') != _file_sha(Path(output)):
            return 0     # the result was edited or replaced since: never overwrite it
        done = {str(k) for k in (state.get('completed') or {})}
        return sum(1 for p in split_paragraphs(text) if str(p.pid) not in done)
    except Exception:
        return 0


def peek_guidance(source: Path, *, checkpoint_dir: Path | None = None, links_root: Path | None = None) -> tuple[str, str, str]:
    """(where, summary, table) of the reference table a run on this TXT would use, before
    it starts: the translation record, a sample analysis kept from an earlier run, or none."""
    from .textio import read_txt
    text, _ = read_txt(Path(source))
    ref = find_reference(source, text, links_root)
    if ref:
        return 'translation', reference_summary(ref), reference_guidance(ref)
    digest = sha256(text.encode('utf-8','surrogatepass')).hexdigest()
    cp = content_checkpoint_path(digest, checkpoint_dir)
    try:
        state = load_json(cp) if cp.exists() else {}
    except Exception:
        state = {}
    if state.get('guidance_done') and str(state.get('guidance') or '').strip():
        return 'analysis', '지난 실행의 샘플 분석', str(state.get('guidance'))
    return 'none', '', ''


def _residue_paragraphs(paragraphs, texts=None) -> int:
    texts = texts or {}
    return sum(1 for p in paragraphs if _FOREIGN.search(texts.get(str(p.pid), p.text)))


def assemble(paragraphs, completed):
    return ''.join(p.prefix_before + completed.get(str(p.pid), p.text) + p.separator_after for p in paragraphs)


_SOURCE_LABEL = {'translation': '번역 기록', 'analysis': '샘플 분석', 'none': '없음'}


def run_refinement(source: Path, output: Path, provider, model: str, key: str,
                   *, stop=lambda: False, progress=lambda done, total, issues: None,
                   checkpoint_dir: Path | None = None, max_chars=None, max_items=36,
                   analysis_engine=None, event=lambda message: None,
                   links_root: Path | None = None, update_existing: bool = False):
    """Save each accepted group durably; invalid answers preserve original text.

    Source and any pre-existing output remain untouched until a complete pass.
    `update_existing` lets a finished run retry its failed paragraphs into its own
    result file, only while that file is exactly what the run wrote.
    """
    started = time.monotonic()
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError('원본과 결과 파일 경로가 같습니다.')
    # Reject oversized books *before* loading or contacting a paid model.
    if source.stat().st_size >= MAX_REFINEMENT_BYTES:
        raise ValueError('한국어 소설 재정비는 10MB 미만 TXT만 지원합니다. 큰 소설은 장별로 분리해 주세요.')
    from .textio import read_txt
    text, _encoding = read_txt(source)
    if not text.strip():
        raise ValueError('교정할 본문이 없습니다.')
    paragraphs = split_paragraphs(text)
    provider_name = str(getattr(provider, 'name', ''))
    if max_chars is None:
        max_chars = default_max_chars(provider_name)
    groups = group_paragraphs(paragraphs, max_chars, max_items)
    analysis_provider,analysis_model,analysis_key = analysis_engine or (provider,model,key)
    digest = sha256(text.encode('utf-8','surrogatepass')).hexdigest()
    identity = job_identity(digest, provider_name, model, max_chars, max_items)
    legacy_identity = _legacy_identity(digest, provider_name, model, max_chars, max_items,
                                       analysis_model, getattr(analysis_provider,'name',''))
    cp = content_checkpoint_path(digest, checkpoint_dir)
    legacy = checkpoint_path(source, checkpoint_dir)
    if not cp.exists() and legacy.exists():
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            legacy.replace(cp)          # an earlier build's run on this file continues
        except OSError:
            cp = legacy
    state = {'schema':1, 'identity':identity, 'completed':{}, 'issues':{}, 'source':str(source)}
    old = {}
    if cp.exists():
        try:
            old = load_json(cp)
        except Exception:
            old = {}
        if old.get('schema') == 1 and old.get('identity') in (identity, legacy_identity):
            by_pid = {str(p.pid): p.text for p in paragraphs}
            state['completed'] = {str(k):v for k,v in old.get('completed',{}).items()
                                  if isinstance(v,str) and v.strip() and not is_refusal_notice(v, by_pid.get(str(k),''))}
            state['issues'] = {str(k):str(v) for k,v in old.get('issues',{}).items()}
            state['guidance']=_clip_lines(str(old.get('guidance') or ''), GUIDANCE_MAX)
            state['guidance_done']=bool(old.get('guidance_done'))
            state['guidance_source']=str(old.get('guidance_source') or ('analysis' if state['guidance'] else 'none'))
            # Time already spent on this book in earlier runs (stopped and resumed).
            state['elapsed']=float(old.get('elapsed') or 0.0)
            if old.get('finished'):
                state['output_sha']=str(old.get('output_sha') or '')
        else:
            old = {}
    # Never overwrite an earlier result, except this run's own untouched result.
    if output.exists() and output.read_bytes():
        if not (update_existing and old.get('finished') and state.get('output_sha')
                and state['output_sha'] == _file_sha(output)):
            raise FileExistsError('이미 같은 이름의 결과가 있습니다. 다른 출력 파일명을 선택하세요.')
    earlier = float(state.get('elapsed') or 0.0)

    def elapsed() -> float:
        return earlier + (time.monotonic() - started)

    def save():
        state['elapsed'] = round(elapsed(), 1)
        save_json(cp, state)
    valid_ids = {str(p.pid) for p in paragraphs}
    state['completed']={k:v for k,v in state['completed'].items() if k in valid_ids}
    residue_before = _residue_paragraphs(paragraphs)

    # Reference table: the translation's own speech table and glossary when this TXT is
    # a result of this app; otherwise one bounded sample analysis.
    ref = find_reference(source, text, links_root)
    names = [str(n) for n in ((ref or {}).get('names') or [])]
    if not state.get('guidance_done'):
        if ref:
            state['guidance'] = reference_guidance(ref)
            state['guidance_source'] = 'translation'
            event('번역 기준표 사용 · ' + reference_summary(ref))
        else:
            state['guidance'] = prepare_guidance(paragraphs,analysis_provider,analysis_model,analysis_key,stop=stop,event=event)
            state['guidance_source'] = 'analysis' if state['guidance'] else 'none'
            if stop():
                save()
                return {'status':'stopped','done':len(state['completed']),'total':len(paragraphs),
                        'issues':len(state['issues']),'seconds':elapsed()}
        state['guidance_done']=True
        save()
    else:
        event('기준표: ' + _SOURCE_LABEL.get(state.get('guidance_source',''), '없음') + ' (이전 실행에서 이어 씀)')
    guidance = state.get('guidance','')
    confirmed = state.get('guidance_source') == 'translation'
    for index, group in enumerate(groups):
        if stop():
            save()
            return {'status':'stopped', 'done':len(state['completed']), 'total':len(paragraphs),
                    'issues':len(state['issues']), 'seconds':elapsed()}
        if all(str(p.pid) in state['completed'] for p in group):
            progress(len(state['completed']),len(paragraphs),len(state['issues']))
            continue
        previous = ''.join(p.text + p.separator_after for p in paragraphs[max(0,group[0].pid-7):group[0].pid-1])
        next_index = group[-1].pid
        following = ''.join(p.text + p.separator_after for p in paragraphs[next_index:next_index+5])
        def request(ps):
            system,user = refinement_prompt(ps,previous,following,guidance,confirmed)
            event(f'윤문 요청 중 · {ps[0].pid}~{ps[-1].pid}문단 · {getattr(provider,"name","AI")}')
            # 'edit' + the paragraph IDs: a local LLM is held to every ID (structured output).
            response = provider.translate(TranslationRequest(model,system,user,temperature=.2,timeout=300,
                                                            purpose='edit',
                                                            paragraph_ids=tuple(f'P{p.pid:06d}' for p in ps)),key)
            accepted,rejected,refused=verified_partial_reply(response.text,ps)
            if rejected and len(accepted)+len(rejected)==len(ps):
                raise PartialRefinementRefused(accepted,rejected,refused)
            if not rejected and len(ps) >= 3 and len(accepted) == len(ps)-1:
                missing=next(p.pid for p in ps if p.pid not in accepted)
                raise MissingParagraph(accepted, missing)
            return model_reply_to_text(response.text,ps)
        def recover(parts):
            if stop():
                return
            try:
                bodies = request(parts)
            except Exception as exc:
                if isinstance(exc, MissingParagraph):
                    # Keep the other paragraphs; ask again only for the missing one and the
                    # paragraph before it (where its text was likely merged), one at a time.
                    order=[p.pid for p in parts]
                    i=order.index(exc.missing)
                    neighbour=order[i-1] if i>0 else order[i+1]
                    keep=[p for p in parts if p.pid in exc.accepted and p.pid!=neighbour]
                    redo=[p for p in parts if p.pid in (exc.missing, neighbour)]
                    event(f'문단 1개 누락 · 나머지 {len(keep)}문단 유지, {len(redo)}문단만 한 문단씩 다시 요청')
                    if keep:
                        finish(keep,[exc.accepted[p.pid] for p in keep])
                    for p in redo:
                        recover([p])
                    return
                if isinstance(exc, PartialRefinementRefused):
                    n_ref=len(exc.refused); n_bad=len(exc.rejected)-n_ref
                    event(f'일부 문단 실패 · 정상 윤문 {len(exc.accepted)}문단 검토 후 유지, '
                          f'거부 {n_ref}문단 원문 보존, 검증 실패 {n_bad}문단 다시 요청')
                    keep=[p for p in parts if p.pid in exc.accepted]
                    if keep:
                        # Same continuity review as a fully valid reply (never skipped).
                        finish(keep,[exc.accepted[p.pid] for p in keep])
                    for p in parts:
                        if p.pid in exc.refused:
                            state['issues'][str(p.pid)]=exc.rejected[p.pid]
                        elif p.pid in exc.rejected:
                            if len(parts) > 1:
                                recover([p])          # a formatting slip: ask once more alone
                            else:
                                state['issues'][str(p.pid)]=exc.rejected[p.pid]
                    return
                if isinstance(exc, RefinementRefused):
                    event(f'윤문 거부 감지 · {parts[0].pid}~{parts[-1].pid}문단 원문 보존')
                    for p in parts:
                        state['issues'][str(p.pid)] = '모델 윤문 거부 · 원문 보존'
                    return
                if isinstance(exc, ProviderError):
                    if exc.kind in (ErrorKind.AUTH, ErrorKind.KEY_BLOCKED, ErrorKind.QUOTA,
                                    ErrorKind.FATAL):
                        save()
                        raise
                    # Policy/explicit refusals are not formatting mistakes;
                    # retrying smaller excerpts wastes time and API requests.
                    if exc.kind in (ErrorKind.POLICY, ErrorKind.REFUSAL, ErrorKind.SAFETY):
                        event(f'모델 정책 거부 · {parts[0].pid}~{parts[-1].pid}문단 원문 보존')
                        for p in parts:
                            state['issues'][str(p.pid)]='모델 정책 거부 · 원문 보존'
                        return
                    save()
                    raise
                if not isinstance(exc, ValueError):
                    save()
                    raise
                event(f'문단 형식 검증 실패 · {len(parts)}문단 묶음 재시도')
                # Only malformed paragraph formatting is split, not refusals or service errors.
                if len(parts) > 1:
                    midpoint = len(parts) // 2
                    recover(parts[:midpoint])
                    recover(parts[midpoint:])
                else:
                    state['issues'][str(parts[0].pid)] = type(exc).__name__ + ': ' + str(exc)[:250]
                return
            finish(parts, bodies)

        def finish(parts, bodies):
            """Continuity review of validated edits, then store them."""
            original = '\n'.join(p.text for p in parts)
            rewritten = '\n'.join(bodies)
            reason = review_reason(parts, bodies, names)
            if reason and stop():
                # A suspect edit is never kept unreviewed: left unfinished, it is edited
                # again when the same TXT is resumed.
                return
            try:
                consistent=continuity_check(original,rewritten,provider,model,key,stop=stop,event=event,reason=reason)
            except ProviderError:
                # The review was needed (length, speech level or a name changed) but could
                # not be done: keep the original so a possible change of meaning never
                # slips into the result. "보존한 문단만 다시 시도" retries these later.
                event(f'연속성 검토 요청 실패 · {len(parts)}문단 원문 보존(나중에 다시 시도 가능)')
                for p in parts:
                    state['issues'][str(p.pid)]='연속성 검토 실패('+reason+') · 원문 보존'
                return
            if not consistent:
                event(f'연속성 검토 불일치 · {len(parts)}문단 분할 검증')
                if len(parts)>1:
                    middle=len(parts)//2
                    recover(parts[:middle]);recover(parts[middle:])
                else:
                    state['issues'][str(parts[0].pid)]='연속성 검토 불확실('+reason+') · 원문 보존'
                return
            for p, body in zip(parts, bodies):
                state['completed'][str(p.pid)] = body
                state['issues'].pop(str(p.pid), None)

        remaining = [p for p in group if str(p.pid) not in state['completed']]
        if remaining:
            recover(remaining)
        save()
        progress(len(state['completed']),len(paragraphs),len(state['issues']))
    if stop():
        return {'status':'stopped','done':len(state['completed']),'total':len(paragraphs),
                'issues':len(state['issues']),'seconds':elapsed()}
    result=assemble(paragraphs,state['completed'])
    if not result:
        raise ValueError('교정 결과가 비어 있습니다.')
    atomic_write_text(output,result,backup_existing=False)
    report=output.with_name(output.name+'.review.txt')
    changed=sum(1 for p in paragraphs if str(p.pid) in state['completed'] and state['completed'][str(p.pid)].strip()!=p.text.strip())
    identical=len(state['completed'])-changed
    preserved=len(paragraphs)-len(state['completed'])
    residue_after=_residue_paragraphs(paragraphs, state['completed'])
    source_label=_SOURCE_LABEL.get(state.get('guidance_source',''),'없음')
    if state.get('guidance_source')=='translation' and ref:
        source_label+=' · '+reference_summary(ref)
    elif state.get('guidance_source')=='analysis':
        source_label+=f' · {getattr(analysis_provider,"name","")} {analysis_model}'
    lines=[f'한국어 재정비 결과 · 전체 {len(paragraphs)}문단 · 실제 변경 {changed}문단 · 원문과 동일 {identical}문단 · 미교정 원문 보존 {preserved}문단 · 검토 기록 {len(state["issues"])}건',
           f'소요 시간 {format_duration(elapsed())} · 윤문 {getattr(provider,"name","")} {model}',
           f'기준표 출처: {source_label}',
           f'한자·가나가 남은 문단: 재정비 전 {residue_before}개 → 재정비 후 {residue_after}개',
           '검증 실패 또는 모델 오류가 난 문단은 원문 그대로 보존했습니다.']
    if preserved:
        lines.append('같은 TXT와 같은 윤문 엔진으로 다시 시작하면, 이 결과 파일을 손대지 않은 경우 보존한 문단만 다시 시도합니다.')
    lines += [f'문단 {k}: {v}' for k,v in sorted(state['issues'].items(),key=lambda kv:int(kv[0]))]
    if guidance:
        lines += ['', '--- 윤문에 쓴 기준표 ---', guidance]
    atomic_write_text(report,'\n'.join(lines))
    if preserved:
        # Keep what was finished so only the preserved paragraphs are tried again later.
        state['finished']=True
        state['output_sha']=_file_sha(output)
        save()
    else:
        cp.unlink(missing_ok=True)
        if legacy != cp:
            legacy.unlink(missing_ok=True)
    return {'status':'completed','output':str(output),'report':str(report),
            'done':len(state['completed']), 'total':len(paragraphs),
            'changed':changed,'identical':identical,'preserved':preserved,
            'issues':len(state['issues']), 'seconds':elapsed(),
            'guidance_source':state.get('guidance_source','none'), 'guidance':guidance,
            'residue_before':residue_before, 'residue_after':residue_after,
            'retry_available':preserved}
