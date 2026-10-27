from __future__ import annotations
import json, re

_EXTRA_CACHE: dict = {}
_EXCLUDE_CACHE: dict = {}


def extra_pack(lang: str) -> dict[str, str]:
    """Supplementary language pack (config/lexicon_extra.json). Kept out of the profile so
    adding hints never changes a book's job identity (no re-translation)."""
    if 'data' not in _EXTRA_CACHE:
        try:
            from .config import CONFIG_DIR
            _EXTRA_CACHE['data'] = json.loads((CONFIG_DIR / 'lexicon_extra.json').read_text(encoding='utf-8'))
        except Exception:
            _EXTRA_CACHE['data'] = {}
    d = _EXTRA_CACHE['data'].get(lang, {})
    return dict(d) if isinstance(d, dict) else {}


def exclusions(lang: str) -> dict[str, list[str]]:
    """config/lexicon_exclude.json: words that contain a pack entry but mean something else
    (主人公 contains 主人, 在下面 contains 在下). Occurrences inside them are not hints."""
    if 'data' not in _EXCLUDE_CACHE:
        try:
            from .config import CONFIG_DIR
            _EXCLUDE_CACHE['data'] = json.loads((CONFIG_DIR / 'lexicon_exclude.json').read_text(encoding='utf-8'))
        except Exception:
            _EXCLUDE_CACHE['data'] = {}
    d = _EXCLUDE_CACHE['data'].get(lang, {})
    if not isinstance(d, dict):
        return {}
    return {str(k): [str(x) for x in v if str(x)] for k, v in d.items() if isinstance(v, list)}


def _spans(text: str, word: str) -> list[tuple[int, int]]:
    out = []; i = text.find(word)
    while i >= 0:
        out.append((i, i + len(word))); i = text.find(word, i + 1)
    return out


_EN_WORD = re.compile(r"[A-Za-z][A-Za-z '\u2019-]*")


def _word_spans(text: str, word: str) -> list[tuple[int, int]]:
    """Occurrences of an English pack word as a whole word, ignoring case (v2.5.7)."""
    pat = r'(?<![A-Za-z])' + re.escape(word) + r'(?![A-Za-z])'
    return [m.span() for m in re.finditer(pat, text, re.I)]


def _free_occurrence(text: str, word: str, covers: list[str]) -> bool:
    """True when `word` occurs at least once outside every occurrence of the `covers` words."""
    if _EN_WORD.fullmatch(word):
        covered = [sp for c in covers if c.lower() != word.lower() for sp in _word_spans(text, c)]
        spans = _word_spans(text, word)
    else:
        covered = [sp for c in covers if word in c and c != word for sp in _spans(text, c)]
        spans = _spans(text, word)
    for a, b in spans:
        if not any(ca <= a and b <= cb for ca, cb in covered):
            return True
    return False


def relevant_guidance(profile, source_text: str, limit: int = 60) -> dict[str,str]:
    """Return only language-pack hints actually present in the current source.

    These are context-sensitive suggestions, not blind substitutions. User-confirmed
    terminology remains a separate, higher-priority channel.
    """
    data=getattr(profile,'lexical_guidance',{}) or {}
    if not isinstance(data,dict):
        return {}
    # Built-in pack first, then the supplement, then the reader's shared glossary
    # (lexical_extra, set per job); an earlier source wins for the same word.
    extra=extra_pack(getattr(profile,'source_language',''))
    merged=dict(getattr(profile,'lexical_extra',None) or {})
    merged.update(extra)
    merged.update(data)
    curated=set(data)|set(extra)
    data=merged
    hits=[]
    for src,target in data.items():
        s=str(src).strip(); t=str(target).strip()
        # English language-pack words must not match inside another word
        # (e.g. "ass" in "class" or "guild" in "guildhall").
        present = bool(re.search(r'(?<![A-Za-z])'+re.escape(s)+r'(?![A-Za-z])', source_text, re.I)) if re.fullmatch(r"[A-Za-z][A-Za-z '\u2019-]*", s) else s in source_text
        if s and t and present:
            hits.append((s,t))
    if hits:
        # CJK entries match inside other words. Drop a hint whose every occurrence lies
        # inside an excluded word (主人 in 主人公) or inside a longer hint that also
        # matched (騎士 when the text only has 騎士団), so it neither misleads the
        # model nor takes a place in the budget.
        excl=exclusions(getattr(profile,'source_language',''))
        longer=[s for s,_ in hits]
        kept=[]
        for s,t in hits:
            if _EN_WORD.fullmatch(s):
                # English: a shorter word inside a longer hint that also matched as a whole
                # phrase ("jerk" in "jerk off", "cheat" in "cheat on") is dropped there, the
                # same rule as for CJK entries; elsewhere in the text it stays a hint.
                covers=[o for o in longer if len(o)>len(s) and _EN_WORD.fullmatch(o)
                        and re.search(r'(?<![A-Za-z])'+re.escape(s)+r'(?![A-Za-z])', o, re.I)]
                if not covers or _free_occurrence(source_text, s, covers):
                    kept.append((s,t))
                continue
            covers=list(excl.get(s,[]))+[o for o in longer if len(o)>len(s) and s in o]
            if not covers or _free_occurrence(source_text, s, covers):
                kept.append((s,t))
        hits=kept
    # Curated pack hints first (the shared glossary never crowds them out), then
    # longer phrases first so useful compounds win the prompt budget.
    hits.sort(key=lambda x:(x[0] not in curated,-len(x[0]),x[0]))
    return dict(hits[:max(1,int(limit))])
