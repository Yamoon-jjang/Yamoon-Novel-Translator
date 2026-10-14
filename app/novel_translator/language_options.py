from __future__ import annotations

OPTIONS = {
    'ja-ko': {
        'label': '호칭 처리',
        'choices': [
            ('문맥에 따라 자연스럽게 (권장)', 'contextual'),
            ('さん/くん/ちゃん 등 원문 호칭을 가능한 유지', 'preserve_honorifics'),
            ('한국어 관계 호칭으로 자연스럽게', 'koreanize_honorifics'),
        ],
    },
    'zh-ko': {
        'label': '중국 인명 표기',
        'choices': [
            ('한국 한자음으로 (左京 → 좌경) · 기본', 'sino_korean_names'),
            ('장르·시대 문맥에 따라 자동', 'contextual_names'),
            ('현대 중국어 발음으로 (左京 → 쭤징)', 'mandarin_names'),
        ],
    },
    'en-ko': {
        'label': '고유명사/직함',
        'choices': [
            ('문맥과 관용 표기에 따라 자동 (권장)', 'contextual_titles'),
            ('한국어 관용 직함 번역 우선', 'korean_titles'),
            ('고유명사 음역 보존 우선', 'transliterate_names'),
        ],
    },
}

INSTRUCTIONS = {
    'contextual': 'Japanese honorifics and forms of address: decide from relationship and scene context; confirmed terminology always wins.',
    'preserve_honorifics': 'Japanese honorific policy: preserve meaningful source suffixes such as さん/くん/ちゃん/様 when natural and do not silently erase relationship information.',
    'koreanize_honorifics': 'Japanese honorific policy: prefer natural Korean relationship/title expressions when meaning is clear; do not keep Japanese suffixes mechanically.',
    'contextual_names': 'Chinese name policy: choose Korean rendering from genre, era, established convention, and confirmed project terms; never switch systems mid-work.',
    'mandarin_names': 'Chinese name policy: prefer modern Mandarin-based Korean transcription for Chinese personal names unless a confirmed project term overrides it.',
    'sino_korean_names': 'Chinese name policy: write every Chinese personal name, nickname and place name with its Korean Sino-Korean (한자음) reading, character by character (左京 → 좌경, 李萱诗 → 이훤시, 白颖 → 백영, 北京 → 북경), never with Mandarin-based transcription (not 쭤징, 리쉬안스, 바이잉, 베이징); confirmed project terms override it.',
    'contextual_titles': 'English proper-name/title policy: use established Korean convention and context; distinguish a proper noun from an ordinary noun before translating.',
    'korean_titles': 'English title policy: prefer established Korean translations for ranks/titles when they function as titles, while preserving confirmed names.',
    'transliterate_names': 'English proper-name policy: prefer Korean transliteration for names and name-like terms rather than semantic translation unless context clearly requires translation.',
}


def option_spec(profile_id: str) -> dict:
    return OPTIONS[profile_id]


USER_GUIDELINES_KEY = '__user_guidelines__'
USER_GUIDELINES_MAX = 3000


def prompt_instructions(language_options: dict | None) -> list[str]:
    if not language_options:
        return []
    out=[]
    from .speech_levels import SPEECH_KEY, render_instruction
    for name, value in language_options.items():
        if name in (USER_GUIDELINES_KEY, SPEECH_KEY):
            continue
        key=str(value)
        if key in INSTRUCTIONS:
            out.append(INSTRUCTIONS[key])
    speech = render_instruction(language_options.get(SPEECH_KEY))
    if speech:
        out.append(speech)
    guide = str(language_options.get(USER_GUIDELINES_KEY) or '').strip()[:USER_GUIDELINES_MAX]
    if guide:
        out.append('USER TRANSLATION PREFERENCES (the reader\'s own style wishes, written in Korean; follow them unless they '
                   'conflict with the fixed rules: complete and faithful translation, the same paragraph IDs, Korean Hangul output, '
                   'confirmed terminology, and the exclusion of sexual content involving minors):\n' + guide)
    return out
