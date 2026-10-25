"""Proper-name / recurring-term extraction for the confirmed glossary.

Only a frequency-ranked CANDIDATE LIST is sent to the model, never the novel's
prose. That keeps the request tiny and free of scene content (so it is not
blocked), while the model only has to decide which candidates are names,
places, organizations or recurring fictional terms and how to write them in
Korean. The user reviews the result before it is used.
"""
from __future__ import annotations
import json
import re
from collections import Counter

# Very common Chinese function/content characters that rarely start or end a name.
_ZH_STOP = set('的了是在我你他她它们这那不一有和就也都而着过说道很没吗呢吧啊么要会能可去来到上下中大小个对把被让给从向又还只再已与及或但却并就所以因为如果虽然自己什么怎么这么那么时候现在今天一个没有知道看到出来起来已经')
# Particles that never occur inside a name.
_ZH_PARTICLE = set('的了着过吗呢吧啊么地得和把被让给')
# A name before an honorific: 2+ kanji (佐藤さん) or 2+ katakana (アリスさん). One kanji is
# almost always an ordinary word (お兄さん, 皆さん, 同様, 異様, 彼氏, 王様, 神様).
_JA_HONORIFIC = re.compile(r'((?<![一-龥々])[一-龥々]{2,8}|(?<![ァ-ヴー])[ァ-ヴ][ァ-ヴー]{1,15})(?:さん|くん|君|ちゃん|様|さま|先輩|先生|殿|氏)')
# Katakana names may be long and joined with ・ or ＝ (アリシア・フォン・ローゼンベルク).
_KATAKANA = re.compile(r'[ァ-ヴ][ァ-ヴー・＝]{1,31}')
# Capitalized name words, incl. McGonagall, MacLeod, O'Brien, D'Artagnan; up to three in a row.
_EN_NAME_SEQ = re.compile(r"(?<![A-Za-z'’])(?:(?:Mc|Mac|O['’]|D['’])?[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)?)(?:[ ](?:(?:Mc|Mac|O['’]|D['’])?[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)?)){0,2}")
_EN_TITLE_BEFORE = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Prof|Capt|Col|Gen|Lt|Sgt)\.$")
_EN_TITLES = {'Mr','Mrs','Ms','Dr','St','Prof','Capt','Col','Gen','Lt','Sgt','Lord','Lady','Sir','Dame','Miss','Mister','Master','Madam','Madame','King','Queen','Prince','Princess','Duke','Duchess',
              'Count','Countess','Baron','Captain','Professor','Doctor','Father','Mother','Uncle','Aunt','Saint','General','Colonel'}
_EN_CALENDAR = {'January','February','March','April','May','June','July','August','September','October','November','December',
                'Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday','Christmas','Easter'}
_EN_CAP = re.compile(r"(?<![.!?\"“]\s)(?<!^)\b([A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,}){0,2})\b", re.M)
_EN_COMMON = {'The','This','That','There','Then','They','Their','What','When','Where','Which','While','With','Will','Would',
              'Could','Should','Have','Has','Had','His','Her','Him','She','And','But','For','Not','You','Your','Yes','Well',
              'Now','Just','Even','Still','After','Before','Because','Maybe','Perhaps','Chapter','Mister','Miss','Mrs','Lord','Lady','Sir'}


# Ordinary vocabulary that frequency counting picks up but that is never a name.
# (A glossary entry forces one Korean rendering everywhere, so a common word there
# would make every occurrence wooden: 公司 -> 회사 even where 직장/업체 fits better.)
COMMON_WORDS = set('''
公司 学校 医院 老师 先生 小姐 同学 朋友 妈妈 爸爸 父亲 母亲 哥哥 姐姐 弟弟 妹妹 儿子 女儿 丈夫 妻子 老公 老婆 孩子 男人 女人
东西 时间 事情 问题 地方 声音 眼睛 身体 手机 电话 房间 门口 外面 里面 晚上 早上 今天 明天 昨天 现在 以后 以前 一下 一点 一直
已经 还是 可是 但是 因为 所以 如果 虽然 然后 突然 终于 似乎 好像 应该 可能 知道 觉得 看见 听到 开始 回来 出来 进来 起来 下来
山庄 别墅 小区 庄园 学院 大学 中学 公司里 酒吧 会所 宾馆 卧室 客厅 浴室 厨房 阳台
工作 生活 世界 社会 国家 城市 学生 老板 经理 总裁 警察 医生 护士 司机 客人 大家 自己 别人 他们 她们 我们 你们 什么 怎么 这样
那样 这里 那里 这个 那个 一个 两个 没有 不是 就是 还有 只是 心里 脸上 嘴里 手上 床上 桌上 车上 办公室 电梯 酒店 饭店 公寓
先輩 先生 学校 会社 部屋 時間 今日 明日 昨日 自分 彼女 彼氏 友達 家族 仕事 世界 本当 大丈夫 気持ち 毎日
'''.split())
_CJK_IN_KOREAN = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]')
_HANGUL = re.compile(r'[가-힣]')


def _top(counter: Counter, limit: int, min_count: int) -> list[tuple[str, int]]:
    return [(k, v) for k, v in counter.most_common() if v >= min_count][:limit]


_ZH_VERB_END = set('说道问笑喊叫骂答叹哭看想走')
_ZH_ACTION = ('点头', '摇头', '低声', '大声', '皱眉', '笑着', '看着', '看向', '冷冷', '淡淡', '轻轻', '忍不住')
_ZH_ACTION_HEAD = {a[0] for a in _ZH_ACTION} - set('大')   # 白颖低(声), 老王点(头)
_ZH_NAME_PREFIX = set('小大老阿')   # 小雪, 老王, 阿强: name prefixes that are otherwise stop characters
# A name right before a speech/action verb (林雨说, 白颖笑道) is a strong signal.
_ZH_SPEECH_AFTER = re.compile(r'([一-龥]{2,4})(?=(?:笑着|冷冷|淡淡|轻轻|低声|大声|忍不住)?(?:说|道|问|笑|喊|叫|骂|答|叹|哭|看着|看向|点头|摇头|皱眉))')


def candidates(text: str, lang: str, limit: int = 300) -> list[tuple[str, int]]:
    """Frequency-ranked candidate strings (no prose) for the given source language."""
    text = text or ''
    if lang == 'zh':
        counts: Counter = Counter()
        for run in re.findall(r'[一-龥]{2,}', text):
            # Characters belonging to an ordinary word (公司, 老师 …) never start a name candidate:
            # this drops 公司 itself and fragments around it (公司里, 在公司, 司里).
            covered = set()
            for n in (2, 3):
                for i in range(len(run) - n + 1):
                    if run[i:i + n] in COMMON_WORDS:
                        covered.update(range(i, i + n))
            for n in (2, 3, 4):
                for i in range(len(run) - n + 1):
                    gram = run[i:i + n]
                    first_ok = gram[0] not in _ZH_STOP or (gram[0] in _ZH_NAME_PREFIX and n <= 3)
                    if not first_ok or gram[-1] in _ZH_STOP or gram[-1] in _ZH_VERB_END or any(ch in _ZH_PARTICLE for ch in gram) \
                            or any(a in gram for a in _ZH_ACTION) or gram[-1] in _ZH_ACTION_HEAD:
                        continue
                    if covered.intersection(range(i, i + n)):
                        continue
                    counts[gram] += 1
        for m in _ZH_SPEECH_AFTER.finditer(text):
            g = m.group(1)
            for n in (2, 3, 4):   # the name is the tail of the run before the verb
                if len(g) >= n and g[-n:] in counts:
                    counts[g[-n:]] += 2
        top = _top(counts, limit * 4, 6)
        # Drop a shorter gram that is almost always part of one longer gram (keep the longer name).
        longer = {k: v for k, v in top if len(k) >= 3}
        out = []
        for k, v in top:
            if any(len(t) > len(k) and k in t and tv >= v * 0.8 for t, tv in longer.items()):
                continue
            out.append((k, v))
        return out[:limit]
    if lang == 'ja':
        counts = Counter()
        for m in _JA_HONORIFIC.finditer(text):
            counts[m.group(1)] += 3          # strong name signal
        for m in _KATAKANA.finditer(text):
            counts[m.group(0)] += 1
        for run in re.findall(r'[一-龥々]{2,4}', text):
            counts[run] += 1
        for w in COMMON_WORDS:
            counts.pop(w, None)
        return _top(counts, limit, 4)
    if lang == 'en':
        mid: Counter = Counter(); start: Counter = Counter()
        for m in _EN_NAME_SEQ.finditer(text):
            words = m.group(0).split()
            before = text[max(0, m.start() - 40):m.start()].rstrip()   # a short window: linear time on big files
            # A capitalized word that starts a sentence is a weak name signal ("Suddenly",
            # "Nothing"), unless a title stands before it ("Mr. Darcy", "Dr. Watson").
            at_start = (not before and m.start() < 40) or (bool(before) and before[-1] in '.!?"“”’\n')
            titled = bool(_EN_TITLE_BEFORE.search(before))
            while words and (words[0] in _EN_COMMON or words[0] in _EN_TITLES):
                words = words[1:]            # "Lord Voldemort" -> "Voldemort", "Miss Bennet" -> "Bennet"
            while words and words[-1] in _EN_COMMON:
                words = words[:-1]
            if not words or any(w in _EN_CALENDAR for w in words):
                continue
            name = ' '.join(words)
            (start if at_start and not titled and len(m.group(0).split()) == len(words) else mid)[name] += 1
        # Sentence-start occurrences count only for names also seen inside a sentence.
        counts = Counter({k: mid[k] + start.get(k, 0) for k in mid})
        return _top(counts, limit, 3)
    return []


def build_prompts(cands: list[tuple[str, int]], lang: str, name_policy: str = '', existing: dict | None = None,
                  earlier: dict | None = None) -> tuple[str, str]:
    lang_name = {'zh': 'Chinese', 'ja': 'Japanese', 'en': 'English'}.get(lang, lang)
    system = (
        'You build a translation glossary for a professional Korean translator. '
        'You receive only a frequency-ranked list of strings extracted from a ' + lang_name + ' novel, not the novel itself. '
        'Select only personal names, nicknames, place names, organizations/sects/guilds, titles used as names, and terms '
        'particular to this novel (its named skills, items, ranks). Be complete: include EVERY character name in the list, main and minor, '
        'even common-sounding ones, given-name-only forms and nicknames (小雪, 阿强) as separate entries; a full novel usually has dozens. '
        'Real sects and places that recur (少林, 武当, 华山) may be selected so their Korean stays fixed. '
        'Do not select generic genre vocabulary that any reader of the genre knows (内功 internal energy, 丹田, 长老 elder, 弟子 disciple, '
        '剑气 sword energy, 江湖, 武林), nor descriptive phrases, idioms, adjectives or verbs. '
        'If a candidate is a broken piece of a longer name (阳明 from 欧阳明月), return the full name as it appears instead. '
        'Ignore ordinary vocabulary, grammar fragments, and broken substrings. '
        'Never select ordinary words even if frequent (e.g. 公司 company, 学校 school, 老师 teacher, 手机 phone, 会社, 先輩). '
        'Do not select forms of address or kinship words, alone or with a surname, a birth-order or a familiar prefix '
        '(哥哥, 姐姐, 阿姨, 嫂子, 二哥, 二姐, 老二, 老李, 郝叔, 王总, 李老师, お姉さん): their Korean depends on who speaks. '
        'These are examples; treat every similar word the same way. '
        'Give one natural Korean rendering for each selected string, consistent across related names (same surname, same reading system). '
        'Write every rendering entirely in Hangul: no Chinese characters or kana may remain, and render every character of the name '
        '(郝江化 -> 학강화 or 하오장화, never 郝장화). '
        'Missing a real name is worse than including a doubtful one: when unsure whether a string is a character\'s '
        'name, include it (address words and ordinary words are filtered out afterwards anyway). '
        'Return ONLY a JSON object mapping each selected source string to its Korean rendering. No commentary.'
    )
    rows = '\n'.join(f'{k}\t{v}' for k, v in cands)
    keep = ''
    if existing:
        keep = '\n\nALREADY CONFIRMED (do not change; stay consistent with these):\n' + '\n'.join(f'{a} => {b}' for a, b in list(existing.items())[:80])
    seen = ''
    if earlier:
        # Shared-glossary entries found in this text: the model decides whether each is a
        # name/term HERE (another book's 慢慢 "Manman" is the word "slowly" in most novels).
        seen = ('\n\nSEEN AS NAMES/TERMS IN THE USER\'S EARLIER BOOKS (also in the candidate list above). '
                'Select one only if, in this novel too, it is a name or recurring term by the usual meaning of the string; '
                'a string that is an ordinary word here (慢慢 slowly, 微微 slightly, 老太太 old lady, 小妹 little sister) is NOT selected. '
                'If selected, use exactly this Korean:\n' + '\n'.join(f'{a} => {b}' for a, b in list(earlier.items())[:200]))
    if lang == 'zh':
        system += ' Never select a single-character string.'
    policy = f'\n\nNAME POLICY: {name_policy}' if name_policy else ''
    user = f'CANDIDATES (string<TAB>count):\n{rows}{keep}{seen}{policy}\n\nReturn the JSON object now.'
    return system, user


def valid_rendering(source: str, korean: str) -> bool:
    """A usable glossary rendering: Hangul (or a Latin name), no leftover Chinese/kana."""
    k = (korean or '').strip()
    if not k or k == (source or '').strip() or _CJK_IN_KOREAN.search(k):
        return False
    return bool(_HANGUL.search(k) or re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", k))


class GlossaryParseError(ValueError):
    """The model answered, but no JSON object could be read from the answer."""


_SRC_KEYS = ('source', 'original', 'term', 'name', 'src', 'chinese', 'japanese', 'english', '원문', '중국어', '일본어', '영어', '이름')
_KO_KEYS = ('korean', 'ko', 'translation', 'rendering', 'target', 'hangul', '한국어', '번역', '표기', '한글')
_PAREN = re.compile(r'\s*[(（\[［]([^)）\]］]*)[)）\]］]')


def _clean_rendering(v: str) -> str:
    """진란(秦岚) -> 진란 · Qin Lan (진란) -> 진란: a note in brackets is not part of the rendering."""
    v = str(v or '').strip()
    inner = [m.group(1).strip() for m in _PAREN.finditer(v)]
    outside = _PAREN.sub('', v).strip()
    if _HANGUL.search(outside) or not inner:
        return outside
    hangul = [x for x in inner if _HANGUL.search(x) and not _CJK_IN_KOREAN.search(x)]
    return hangul[0] if hangul else outside


def _answer_pairs(node, out: list):
    """Every (source, Korean) pair in a JSON answer, whatever its shape: a mapping, a list of
    {"source": .., "korean": ..} items, mappings grouped by kind, or {"林雨": {"korean": ..}}."""
    if isinstance(node, list):
        for x in node:
            _answer_pairs(x, out)
        return
    if not isinstance(node, dict):
        return
    low = {str(k).strip().lower(): v for k, v in node.items()}
    src = next((low[k] for k in _SRC_KEYS if isinstance(low.get(k), str)), None)
    ko = next((low[k] for k in _KO_KEYS if isinstance(low.get(k), str)), None)
    if src and ko:
        out.append((src, ko)); return
    for k, v in node.items():
        if isinstance(v, str):
            out.append((str(k), v))
        elif isinstance(v, dict):
            inner = {str(a).strip().lower(): b for a, b in v.items()}
            ko2 = next((inner[a] for a in _KO_KEYS if isinstance(inner.get(a), str)), None)
            if ko2 is not None:
                out.append((str(k), ko2))
            else:
                _answer_pairs(v, out)
        elif isinstance(v, list):
            _answer_pairs(v, out)


def _top_json(raw: str):
    """The outermost JSON values in the text (object or list)."""
    dec = json.JSONDecoder(); i = 0; found = []
    while i < len(raw):
        j = min([x for x in (raw.find('{', i), raw.find('[', i)) if x >= 0], default=-1)
        if j < 0:
            break
        for candidate in (raw[j:], re.sub(r',\s*([}\]])', r'\1', raw[j:])):
            try:
                obj, end = dec.raw_decode(candidate)
            except ValueError:
                continue
            found.append(obj); i = j + end; break
        else:
            i = j + 1
    return found


# Kinship addresses after a name depend on who speaks: 丹丹姐 is "단단 누나" from a man and
# "단단 언니" from a woman (哥 형/오빠 likewise). A glossary entry is a fixed rendering for
# every speaker, so such an entry keeps only the name; the address is left to the context.
_ZH_ADDRESS = ('姐姐', '哥哥', '妹妹', '弟弟', '姐', '哥', '妹', '弟')
_JA_ADDRESS = ('お姉ちゃん', 'お兄ちゃん', '姉ちゃん', '兄ちゃん', '姉さん', '兄さん', '姉様', '兄様', '姉', '兄')
_KO_ADDRESS_END = re.compile(r'^(?P<name>.*?[가-힣A-Za-z])\s*(?:언니|누나|누님|오빠|오라버니|형님|형|여동생|남동생|동생|아우)$')


# Address words alone (老姨, 姨姐, 李哥, 小妹, 阿姨, 二舅妈, お姉さん) are not names: who says them
# decides the Korean (이모 · 처제 · 언니 …), and as a fixed entry they become pseudo-names
# read out in hanja sounds (노이, 이저, 이가). The extraction leaves them to the context.
# Strong kinship characters make an address on their own; weak ones also end real names
# (杜十娘, 红娘, 周公, compound surnames 公孙 · 长孙) and count only next to a strong one (伯母, 叔父).
_ZH_KIN_STRONG = set('姐哥妹弟姨姑舅叔伯婶嫂爷奶妈爸爹兄')
_ZH_KIN_WEAK = set('娘公母父夫妻婆孙婿侄甥')
_ZH_KIN = _ZH_KIN_STRONG | _ZH_KIN_WEAK
_ZH_ADDRESS_WORDS = {'大娘', '婆婆', '公公', '岳父', '岳母', '娘亲', '老娘', '妈咪', '爹地', '媳妇', '闺女', '老幺'}
_ZH_KIN_PREFIX = set('老小大阿二三四五六七八九十表堂亲干后继')
_JA_KIN = {'姉', '兄', '妹', '弟', '母', '父', '叔母', '叔父', '伯母', '伯父', '祖母', '祖父', '婆', '爺', '義姉', '義兄', '義母', '義父'}
_JA_KIN_KANA = {'ママ', 'パパ', 'おばさん', 'おじさん', 'おばあさん', 'おじいさん', 'おばあちゃん', 'おじいちゃん', 'ねえさん', 'にいさん'}


def is_address_term(source: str) -> bool:
    """A source that is only a kinship/address word, optionally after an order or
    familiarity prefix (老 · 小 · 二 · 表 …) and/or a one-character surname (李哥, 王小妹)."""
    s = str(source or '').strip()
    if not s or len(s) > 6:
        return False
    if s in _JA_KIN_KANA or s in _ZH_ADDRESS_WORDS:
        return True
    if len(s) >= 2 and s.endswith('子') and s[-2] in _ZH_KIN and is_address_term(s[:-1]):
        return True       # 嫂子 · 婶子 · 舅子 · 小姑子: a kinship word with the familiar 子
    if re.fullmatch(r'老[一-龥]', s):
        return True       # 老二 · 老大 (birth order), 老李 · 老王 (surname): who calls decides 둘째 · 이 씨 · 여보
    core = s[1:] if s.startswith('お') else s
    for suffix in ('さん', 'ちゃん', 'さま', '様', 'くん'):
        if core.endswith(suffix) and len(core) > len(suffix):
            core = core[:-len(suffix)]
            break
    if core in _JA_KIN or core in _JA_KIN_KANA:
        return True
    if not re.fullmatch(r'[一-龥]+', s):
        return False
    i = len(s)
    while i > 0 and s[i - 1] in _ZH_KIN:
        i -= 1
    if i == len(s) or not any(c in _ZH_KIN_STRONG for c in s[i:]):
        return False                      # no kinship word at the end (杜十娘, 周公, 公孙 are names)
    rest = [c for c in s[:i] if c not in _ZH_KIN_PREFIX]
    return len(rest) <= 1                 # nothing, a prefix, or a bare surname before it


def split_address(source: str, korean: str, bases=None) -> tuple[str, str] | None:
    """(name, Korean name) when an entry is a name plus a kinship address that the
    Korean also renders (丹丹姐 = 단단 언니 -> 丹丹 = 단단); None otherwise.
    A transliterated name that only ends in such a character (李小妹 = 이소매) is kept."""
    src = str(source or '').strip(); ko = str(korean or '').strip()
    suffix = next((x for x in _ZH_ADDRESS + _JA_ADDRESS if src.endswith(x)), '')
    if not suffix:
        return None
    base = src[:-len(suffix)].rstrip('・ ')
    if len(base) < 2:
        return None          # 姐姐 itself, or a bare surname (林姐): nothing safe to keep
    m = _KO_ADDRESS_END.match(ko)
    if m:
        name = m.group('name').strip()
        spaced = ko[len(m.group('name')):][:1].isspace()
        if not spaced and len(re.findall(r'[가-힣]', name)) < 2:
            return None          # 이형: one syllable left, more likely a name than name+address
        return base, name
    # The address read out instead of translated: 丹丹姐 = 단단저 (hanja) or 단단지에 (Chinese
    # sound). Either the Korean has one syllable per character, or it ends in a known reading.
    flat = ko.replace(' ', '')
    # 李小妹 / 王小妹: the suffix belongs to 小妹 (a kinship word), not to a name before it.
    # 妹 / 弟 also end real given names (招弟, 春妹): read out, they are split only when the
    # bare name is known on its own (`bases`, e.g. the candidate list). 姐 / 哥 almost never are.
    if bases is not None and suffix[0] in '妹弟' and base not in bases:
        return None
    if re.fullmatch(r'[가-힣]+', flat) and base[-1] not in _ZH_KIN_PREFIX and base[-1] not in _ZH_KIN:
        # One Hangul syllable per Chinese character (hanja reading); Japanese is not 1:1.
        if re.fullmatch(r'[一-龥]+', src) and len(flat) == len(src):
            name = flat[:len(flat) - len(suffix)]
            return (base, name) if len(name) >= 1 else None
        for reading in _ADDRESS_READINGS.get(suffix, ()):
            if flat.endswith(reading) and len(flat) - len(reading) >= 2:
                return base, ko.rstrip()[:len(ko.rstrip()) - len(reading)].rstrip()
    return None


# How an address suffix comes out when it is read rather than translated (hanja / Chinese / Japanese sound).
_ADDRESS_READINGS = {
    '姐': ('지에', '저', '자', '제', '쩨'), '姐姐': ('지에지에', '저저', '제제'),
    '哥': ('거', '가', '꺼'), '哥哥': ('거거', '가가'),
    '妹': ('메이', '매', '메'), '妹妹': ('메이메이', '매매'),
    '弟': ('디', '제', '띠'), '弟弟': ('디디', '제제'),
    '姉さん': ('네상', '네에상', '자상'), '兄さん': ('니상', '니이상', '형상'),
    '姉': ('네', '자'), '兄': ('니', '형'),
}


def parse_glossary(text: str, cands: list[tuple[str, int]] | None = None, strict: bool = False,
                   known: dict[str, str] | None = None, report: dict | None = None, lang: str = '') -> dict[str, str]:
    """Parse the model's JSON answer; keep only candidates that really exist.
    strict=True raises GlossaryParseError when the answer holds no JSON at all.
    `report` (optional) receives how many entries the answer held and why some were left out."""
    raw = re.sub(r'<(think|thinking|reasoning)>.*?</\1>', '', (text or ''), flags=re.S | re.I)
    raw = re.split(r'</(?:think|thinking|reasoning)>', raw, flags=re.I)[-1].strip()
    pairs: list[tuple[str, str]] = []
    for obj in _top_json(raw):
        _answer_pairs(obj, pairs)
    if not pairs:
        # A cut-off answer ({"林雨": "린위", "苏晴": "쑤…): keep its complete "key": "value" pairs.
        for k, v in re.findall(r'"((?:[^"\\]|\\.){1,40})"\s*:\s*"((?:[^"\\]|\\.){1,60})"', raw):
            try:
                pairs.append((json.loads(f'"{k}"'), json.loads(f'"{v}"')))
            except ValueError:
                continue
    if not pairs:
        if strict and not re.search(r'[{\[]\s*[}\]]', raw):
            raise GlossaryParseError('모델 답에서 용어 목록(JSON)을 찾지 못했습니다')
        if report is not None:
            report.update(answer=0)
        return {}
    allowed = {k for k, _ in (cands or [])}
    bases = allowed | set(known or {})
    why = {'not_candidate': 0, 'bad_rendering': 0, 'common_word': 0, 'phrase': 0, 'address': 0, 'address_only': 0, 'single': 0}
    out: dict[str, str] = {}
    keys = set()
    for k, v in pairs:
        k = str(k).strip(); v = _clean_rendering(v)
        if not k or k.lower() in _SRC_KEYS or k.lower() in _KO_KEYS or k in keys:
            continue
        keys.add(k)
        if not v or len(k) > 40 or len(v) > 40:
            why['bad_rendering'] += 1; continue
        # The full name may be returned for a broken candidate (欧阳明月 for 阳明月).
        if allowed and k not in allowed and not (len(k) <= 8 and any(len(c) >= 2 and c in k for c in allowed)):
            why['not_candidate'] += 1; continue
        # A Korean rendering must be Korean (or a deliberate Latin name) with no Chinese/kana
        # left in it (郝장화), and never an ordinary word (公司 -> 회사).
        if k in COMMON_WORDS:
            why['common_word'] += 1; continue
        # One Chinese character alone is a surname or a word (李, 剑), almost never a name used
        # by itself. Japanese is left alone: one-kanji given names (誠, 翼, 薫) are common.
        if lang == 'zh' and len(k) == 1:
            why['single'] += 1; continue
        if split_address(k, v, bases) is None and is_address_term(k):
            why['address_only'] += 1; continue      # name + address (阿强哥) is split below instead
        if not valid_rendering(k, v):
            why['bad_rendering'] += 1; continue
        out[k] = v
    for k, v in list(out.items()):
        split = split_address(k, v, bases)
        if split:
            del out[k]
            why['address'] += 1
            name, name_ko = split
            if name not in out and name not in (known or {}):
                out[name] = name_ko
    kept = drop_name_phrases(out, known)
    why['phrase'] = len(out) - len(kept)
    if report is not None:
        report.update(answer=len(keys), kept=len(kept), **why)
    return kept


# Korean particles that follow a name inside a phrase (진서가 놓다, 심소영의 몸, 진서를 보다).
_KO_PARTICLES = ('가', '이', '의', '는', '은', '을', '를', '에', '와', '과', '도', '만', '께', '한테', '에게')


def drop_name_phrases(terms: dict[str, str], known: dict[str, str] | None = None) -> dict[str, str]:
    """Remove "name + following word" entries a model returned as if they were terms
    (陈屿放 -> 진서가 놓다, 沈疏影身 -> 심소영의 몸).

    Only the Korean decides: an entry is dropped when its source starts with another
    name's source and its Korean is that name's Korean followed by a space or by a
    particle and a space. A real name that merely begins with the same characters is
    written as one word (陈屿明 -> 진서명, 陈家村 -> 진가촌) and is always kept.
    """
    bases = dict(known or {})
    bases.update(terms)
    out = {}
    for src, ko in terms.items():
        phrase = False
        for base_src, base_ko in bases.items():
            base_ko = (base_ko or '').strip()
            if not base_ko or base_src == src or len(base_src) >= len(src) or not src.startswith(base_src):
                continue
            if not ko.startswith(base_ko):
                continue
            rest = ko[len(base_ko):]
            if rest.startswith(' ') or any(rest.startswith(p + ' ') for p in _KO_PARTICLES):
                phrase = True
                break
        if not phrase:
            out[src] = ko
    return out


def korean_collisions(terms: dict[str, str], focus=None) -> list[tuple[str, list[str]]]:
    """Different originals that got the same Korean (Susan · Suzanne -> 스잔): the
    translation would merge two characters into one. [(korean, [sources])], only groups
    that include an entry of `focus` (the entries this extraction added or changed) when
    it is given, so an earlier "keep both" choice is not asked again. Spellings of one
    word (Susan / SUSAN, full- and half-width) are not a collision."""
    import unicodedata
    groups: dict[str, list[str]] = {}
    for src, ko in (terms or {}).items():
        s = (src or '').strip(); k = re.sub(r'\s+', '', (ko or '').strip())
        if s and k:
            groups.setdefault(k, []).append(s)
    wanted = {str(x).strip() for x in focus} if focus is not None else None
    out = []
    for k, sources in groups.items():
        if len(sources) < 2:
            continue
        if len({unicodedata.normalize('NFKC', s).casefold() for s in sources}) < 2:
            continue
        if wanted is not None and not wanted.intersection(sources):
            continue
        shown = next((ko.strip() for s, ko in terms.items() if s.strip() == sources[0]), k)
        out.append((shown, sources))
    return out
