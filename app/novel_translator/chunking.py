from __future__ import annotations
from dataclasses import dataclass, replace
import re

# Anything in the source that looks like an internal paragraph ID (<P000001>, <P1>,
# full-width ＜P１＞) is shown to the model as 〈P1〉, which the output normalizer
# never turns into an ID: such a line can then be translated like any other.
_PROTOCOL_LITERAL_RE=re.compile(r'[<＜]P([0-9０-９]{1,6})[>＞]')


def escape_protocol_literals(text: str) -> str:
    # Prevent source prose from impersonating internal paragraph IDs.
    return _PROTOCOL_LITERAL_RE.sub(lambda m: f'〈P{m.group(1)}〉', text)


@dataclass(frozen=True)
class Paragraph:
    pid: int
    text: str
    separator_after: str = '\n\n'
    prefix_before: str = ''
    translatable: bool = True


@dataclass(frozen=True)
class Chunk:
    index: int
    paragraphs: list[Paragraph]
    estimated_tokens: int
    estimated_chars: int = 0

    @property
    def source_text(self) -> str:
        return '\n\n'.join(f'<P{p.pid:06d}>\n{escape_protocol_literals(p.text)}' for p in self.paragraphs)


def estimate_tokens(text: str, lang: str) -> int:
    if not text:
        return 0
    if lang in {'ja', 'zh'}:
        return max(1, int(len(text) * 0.95))
    return max(1, int(len(text) / 3.6))


def _script_counts(text: str) -> tuple[int,int,int,int]:
    hangul=len(re.findall(r'[가-힣]',text))
    kana=len(re.findall(r'[ぁ-ゟ゠-ヿｦ-ﾟ]',text))   # incl. half-width katakana (ﾄﾞｶｰﾝ)
    han=len(re.findall(r'[一-龥]',text))
    latin=len(re.findall(r'[A-Za-z]',text))
    return hangul,kana,han,latin


_AOZORA_NOTE = re.compile(r'［＃[^］\r\n]{1,200}］')


def should_translate_text(text: str, source_lang: str) -> bool:
    """Conservatively decide whether a paragraph still needs translation.

    The target is Korean. Existing Korean prose is passed through locally and never
    sent to the provider. A line without any Hangul is source text, however short
    (chapter headings, names, one-word replies). Lines mixing Korean with a few
    foreign characters are preserved unless the foreign part clearly dominates.
    """
    s=(text or '').strip()
    if not s:
        return False
    s=_AOZORA_NOTE.sub('', s).strip()   # a line that is only ［＃改ページ］ is markup, kept as is
    if not s:
        return False
    hangul,kana,han,latin=_script_counts(s)
    letters=hangul+kana+han+latin
    if letters == 0:
        return False
    if source_lang in ('zh', 'ja') and hangul == 0 and han == 0 and kana == 0:
        # A line that is only an English sound effect / system word in a Chinese or
        # Japanese novel (<WHACK>, “<Slurp, slurp>”, [System]) is translated too (v2.5.7).
        from .protected_literals import bracket_prose
        if bracket_prose(s):
            return True
    if source_lang == 'ja':
        if hangul == 0 and (kana >= 1 or han >= 1):
            return True   # no Korean at all: short headings/names/exclamations are Japanese too
        if kana >= 2:
            return True
        # Mixed Korean/Japanese without kana is ambiguous; preserve unless Han is dominant.
        return han >= 8 and han > hangul * 1.5
    if source_lang == 'zh':
        if han >= 1 and hangul == 0:
            return True   # no Korean at all: 第一章, 偏楼版1, a bare name, 嗯。 are Chinese too
        return han >= 8 and han > hangul * 1.25
    if source_lang == 'en':
        words=re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?",s)
        if hangul == 0 and latin >= 2:
            return True   # no Korean at all: "Chapter 3", "No.", "What?" are English too
        if latin >= 24 and len(words) >= 4 and hangul == 0:
            return True
        return latin >= 40 and len(words) >= 6 and latin > hangul * 2.0
    return True


def _source_signal(text: str, source_lang: str) -> bool:
    hangul, kana, han, latin = _script_counts(text)
    if source_lang == 'zh':
        return han >= 4
    if source_lang == 'ja':
        return kana >= 2 or (han >= 4 and hangul == 0)
    if source_lang == 'en':
        words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", text)
        return len(words) >= 4 and latin >= 24
    return False


def _split_mixed_paragraph_text(text: str, source_lang: str) -> list[tuple[str, bool]]:
    """Send only clear source-language islands; reconstruct Korean verbatim.

    Literal ranges are excluded from island detection, so a URL, code fragment or
    path containing foreign characters cannot be partially translated.
    """
    whole = should_translate_text(text, source_lang)
    if not _script_counts(text)[0] or not _source_signal(text, source_lang):
        return [(text, whole)]
    if source_lang in {'zh', 'ja'}:
        script = '一-龥' if source_lang == 'zh' else 'ぁ-ゟ゠-ヿ一-龥'
        pattern = re.compile('[' + script + '][' + script + r'0-9０-９，。！？、；：…—·・“”‘’「」『』（）()\[\]《》〈〉 \t\n]{0,2400}')
    elif source_lang == 'en':
        pattern = re.compile(r"(?<![A-Za-z])(?:[A-Za-z]+(?:['’-][A-Za-z]+)?(?:[\s,;:!?\.\"'“”‘’()\[\]-]+|$)){4,}")
    else:
        return [(text, whole)]
    from .protected_literals import literal_spans
    protected = literal_spans(text)
    ranges = []
    pos = 0
    for a, b in protected:
        ranges.append((pos, a))
        pos = b
    ranges.append((pos, len(text)))
    hits = []
    for start, end in ranges:
        for match in pattern.finditer(text[start:end]):
            candidate = match.group(0)
            # Keep surrounding whitespace local to retain exact line layout.
            trimmed = candidate.rstrip()
            if _source_signal(trimmed, source_lang):
                hits.append((start + match.start(), start + match.start() + len(trimmed)))
    if not hits:
        return [(text, False)]
    pieces = []
    pos = 0
    for a, b in hits:
        if a > pos:
            pieces.append((text[pos:a], False))
        pieces.append((text[a:b], True))
        pos = b
    if pos < len(text):
        pieces.append((text[pos:], False))
    return pieces


_NOT_PROSE_LINE = re.compile(r'\s*(?:[-*•|#>\[]|\d+[.)]\s)')   # list, table, markup, heading, numbered line
_ALL_CAPS = re.compile(r'[^a-z]*[A-Z]{3}[^a-z]*$')
_WRAP_END_OK = re.compile(r'[.!?:;"\'”’)\]…—–]\s*$')


def unwrap_hard_wrapped(paragraphs: list[Paragraph]) -> list[Paragraph]:
    """Join hard-wrapped English lines (Project Gutenberg, PDF text: a newline every
    ~70 characters, blank lines between real paragraphs) back into paragraphs.

    A block of lines is joined only when it clearly looks wrapped: every line but the
    last is 45-100 characters long and most of them stop mid-sentence. Web-novel
    TXT (one long paragraph per line) and poems/lists (short lines) are left alone.
    """
    out: list[Paragraph] = []
    block: list[Paragraph] = []
    # Decide for the whole file first: hard-wrapped text has most of its lines in one
    # narrow width band (60-80 characters for Gutenberg / PDF exports).
    lines = [len(p.text.strip()) for p in paragraphs if len(p.text.strip()) >= 20]
    file_wrapped = bool(lines) and sum(55 <= n <= 85 for n in lines) >= 0.6 * len(lines)

    def flush():
        if not block:
            return
        body = block[:-1]
        # Only plain English prose lines (each one translated anyway) are ever joined, so a
        # line that would be copied unchanged into the result is never altered.
        prose = all(should_translate_text(x.text, 'en') and not re.search(r'[가-힣ぁ-ゟ゠-ヿ一-龥]', x.text)
                    and not _NOT_PROSE_LINE.match(x.text) and not _ALL_CAPS.match(x.text) for x in block)
        cont = block[1:]
        wrapped = (file_wrapped and prose and len(block) >= 2 and all(45 <= len(x.text.strip()) <= 100 for x in body)
                   and sum(not _WRAP_END_OK.search(x.text) for x in body) >= 0.6 * len(body)
                   # wrapped prose continues mid-sentence: most next lines start in lower case
                   # (verse, dialogue and lists start each line with a capital, quote or mark)
                   and sum(bool(re.match(r'\s*[a-z]', x.text)) for x in cont) >= 0.5 * len(cont))
        if not wrapped:
            out.extend(block)
        else:
            text = block[0].text.rstrip()
            for x in block[1:]:
                nxt = x.text.strip()
                if re.search(r'[A-Za-z]-$', text) and re.match(r'[a-z]', nxt):
                    text = text[:-1] + nxt          # "neigh-" + "bourhood"
                else:
                    text = text + ' ' + nxt
            out.append(Paragraph(block[0].pid, text, block[-1].separator_after, block[0].prefix_before, block[0].translatable))
        block.clear()

    for para in paragraphs:
        block.append(para)
        if para.separator_after != '\n':   # a blank line (or the end) closes the block
            flush()
    flush()
    return out


def mark_translatable(paragraphs: list[Paragraph], source_lang: str, unwrap: bool = True) -> list[Paragraph]:
    if source_lang == 'en' and unwrap:
        paragraphs = unwrap_hard_wrapped(paragraphs)
    out = []
    next_pid = 1
    for paragraph in paragraphs:
        pieces = _split_mixed_paragraph_text(paragraph.text, source_lang)
        for index, (body, flag) in enumerate(pieces):
            out.append(Paragraph(next_pid, body,
                                 paragraph.separator_after if index == len(pieces)-1 else '',
                                 paragraph.prefix_before if index == 0 else '', flag))
            next_pid += 1
    return out


def split_paragraphs(text: str) -> list[Paragraph]:
    """Split for translation while preserving original paragraph/line separators."""
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    if not normalized:
        return []
    paragraphs: list[Paragraph] = []
    blank_sep = re.compile(r'(\n(?:[ \t\u3000]*\n)+)')
    if blank_sep.search(normalized):
        parts = blank_sep.split(normalized)
        pid = 1
        i = 0
        pending_prefix = ''
        while i < len(parts):
            block = parts[i]
            sep = parts[i+1] if i+1 < len(parts) else ''
            if block != '':
                # Many CJK TXT files separate real paragraphs with a single newline
                # and use blank lines only between chapters/scenes. Treat every line
                # inside a blank-line block as its own paragraph so a whole chapter
                # never becomes one giant unit (one provider block would otherwise
                # leave the entire chapter untranslated). Layout is preserved exactly.
                lines = block.split('\n')
                for li, line in enumerate(lines):
                    last = li == len(lines)-1
                    line_sep = sep if last else '\n'
                    if line == '':
                        # Only at the very start of the file (a single leading
                        # newline) or a trailing newline on the final block.
                        if paragraphs:
                            prev=paragraphs[-1]
                            paragraphs[-1]=replace(prev,separator_after=prev.separator_after+line_sep)
                        else:
                            pending_prefix += line_sep
                        continue
                    paragraphs.append(Paragraph(pid, line, line_sep, pending_prefix))
                    pending_prefix = ''
                    pid += 1
            elif not paragraphs and sep:
                pending_prefix += sep
            elif paragraphs and sep:
                prev=paragraphs[-1]
                paragraphs[-1]=replace(prev,separator_after=prev.separator_after+sep)
            i += 2
    else:
        lines = normalized.splitlines(keepends=True)
        pid=1
        pending_prefix=''
        for line in lines:
            if line.endswith('\n'):
                body=line[:-1]; sep='\n'
            else:
                body=line; sep=''
            if body != '':
                paragraphs.append(Paragraph(pid, body, sep, pending_prefix)); pid += 1
                pending_prefix=''
            elif not paragraphs:
                pending_prefix+=sep
    if not paragraphs and normalized:
        return [Paragraph(1, normalized, '', '', False)]
    return paragraphs


def make_chunks(paragraphs: list[Paragraph], lang: str, target_tokens: int = 2000,
                min_tokens: int = 700, max_tokens: int = 3500,
                target_chars: int | None = None, min_chars: int | None = None,
                max_chars: int | None = None) -> list[Chunk]:
    """Build sequential chunks.

    Novel UI paths use character budgets because source-script density differs from
    tokenizer estimates and short source passages recover more predictably. Token
    arguments remain supported for tests/compatibility and as a secondary safety cap.
    Paragraphs marked non-translatable are omitted from API chunks but remain in the
    render plan and are copied byte-for-byte (after newline normalization) to output.
    """
    if target_tokens <= 0 or min_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError('invalid chunk token settings')
    use_chars=target_chars is not None
    if use_chars:
        target_chars=int(target_chars or 0); min_chars=int(min_chars or max(1,target_chars//3)); max_chars=int(max_chars or max(target_chars,target_chars*2))
        if target_chars<=0 or min_chars<=0 or max_chars<target_chars:
            raise ValueError('invalid chunk character settings')
    chunks: list[Chunk] = []
    cur: list[Paragraph] = []
    cur_tokens = 0; cur_chars=0
    for p in paragraphs:
        if not p.translatable:
            continue
        pt = estimate_tokens(p.text, lang); pc=len(p.text)
        if cur:
            would_exceed=(cur_chars+pc > target_chars and cur_chars >= min_chars) if use_chars else (cur_tokens+pt > target_tokens and cur_tokens >= min_tokens)
            if would_exceed:
                chunks.append(Chunk(len(chunks), cur, cur_tokens,cur_chars))
                cur, cur_tokens, cur_chars = [], 0, 0
        cur.append(p); cur_tokens += pt; cur_chars += pc
        hard=(cur_chars >= max_chars) if use_chars else (cur_tokens >= max_tokens)
        if hard:
            chunks.append(Chunk(len(chunks), cur, cur_tokens,cur_chars)); cur, cur_tokens, cur_chars = [], 0, 0
    if cur:
        chunks.append(Chunk(len(chunks), cur, cur_tokens,cur_chars))
    return chunks


def split_chunk_for_retry(chunk: Chunk, lang: str) -> list[Chunk]:
    if len(chunk.paragraphs) > 1:
        mid = max(1, len(chunk.paragraphs) // 2)
        groups = [chunk.paragraphs[:mid], chunk.paragraphs[mid:]]
        return [Chunk(i, g, sum(estimate_tokens(p.text, lang) for p in g),sum(len(p.text) for p in g)) for i, g in enumerate(groups) if g]
    p = chunk.paragraphs[0]
    pattern = r'(?<=[。！？!?])\s*' if lang in {'ja', 'zh'} else \
        r'(?<!\bMr\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bvs\.)(?<!\bMrs\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\b[A-Z]\.)(?<=[.!?])\s+'
    parts = [x.strip() for x in re.split(pattern, p.text) if x.strip()]
    indent = re.match(r'[ \t\u3000]*', p.text).group(0)
    if parts and indent:
        parts[0] = indent + parts[0]   # the paragraph's leading 　 stays on its first piece
    if len(parts) < 2:
        text=p.text
        if len(text) < 500:
            return [chunk]
        mid=len(text)//2
        if lang=='en':
            left=text.rfind(' ',0,mid+1); right=text.find(' ',mid)
            cuts=[x for x in (left,right) if x>0]
            if cuts: mid=min(cuts,key=lambda x:abs(x-mid))
        if mid<=0 or mid>=len(text): return [chunk]
        pieces=[text[:mid],text[mid:]]
        out=[]
        for i,t in enumerate(pieces):
            sep=p.separator_after if i==len(pieces)-1 else ''
            pp=Paragraph(p.pid,t,sep,p.prefix_before if i==0 else '',True); out.append(Chunk(i,[pp],estimate_tokens(t,lang),len(t)))
        return out
    mid = len(parts) // 2
    texts = [' '.join(parts[:mid]), ' '.join(parts[mid:])]
    if lang in {'ja', 'zh'}:
        texts = [''.join(parts[:mid]), ''.join(parts[mid:])]
    out=[]
    for i,t in enumerate(texts):
        sep = p.separator_after if i == len(texts)-1 else ''
        pp=Paragraph(p.pid, t, sep, p.prefix_before if i == 0 else '',True)
        out.append(Chunk(i,[pp],estimate_tokens(t,lang),len(t)))
    return out
