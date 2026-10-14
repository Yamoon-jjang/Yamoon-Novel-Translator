from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .config import PROFILE_DIR, load_json

@dataclass(frozen=True)
class LanguageProfile:
    id: str
    display_name: str
    source_language: str
    system_prompt: str
    language_rules: list[str]
    style_rules: list[str]
    ambiguity_rules: list[str]
    term_rules: list[str]
    default_terms: dict[str, str]
    lexical_guidance: dict[str, str]
    ui: dict
    chunk: dict
    validation: dict

    @classmethod
    def from_file(cls, path: Path) -> 'LanguageProfile':
        d = load_json(path)
        return cls(
            id=d['id'], display_name=d['display_name'], source_language=d['source_language'],
            system_prompt=d['system_prompt'], language_rules=d.get('language_rules', []),
            style_rules=d.get('style_rules', []), ambiguity_rules=d.get('ambiguity_rules', []),
            term_rules=d.get('term_rules', []), default_terms=d.get('default_terms', {}),
            lexical_guidance=d.get('lexical_guidance', {}),
            ui=d.get('ui', {}), chunk=d.get('chunk', {}), validation=d.get('validation', {})
        )

class ProfileManager:
    def __init__(self) -> None:
        self._profiles = {}
        self.load_errors: list[str] = []
        for path in sorted(PROFILE_DIR.glob('*.json')):
            try:
                p = LanguageProfile.from_file(path)
            except Exception as e:   # a hand-edited file with a JSON mistake must not stop the program
                where = f' ({e.lineno}번째 줄 {e.colno}번째 글자)' if hasattr(e, 'lineno') else ''
                self.load_errors.append(f'{path.name}{where}: {type(e).__name__}: {str(e)[:160]}')
                continue
            self._profiles[p.id] = p
        if not self._profiles:
            raise RuntimeError('No language profiles found')
        # Chinese is the default source language; others are one click away.
        self._active_id = 'zh-ko' if 'zh-ko' in self._profiles else next(iter(self._profiles))

    @property
    def active(self) -> LanguageProfile:
        return self._profiles[self._active_id]

    def set_active(self, profile_id: str) -> LanguageProfile:
        if profile_id not in self._profiles:
            raise KeyError(profile_id)
        self._active_id = profile_id
        return self.active

    def all(self) -> list[LanguageProfile]:
        return list(self._profiles.values())
