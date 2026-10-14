from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib

MAX_KEYS_PER_PROVIDER = 10

@dataclass
class ApiKeyRecord:
    id: str
    secret: str
    label: str = ''
    project_group: str = 'default'
    enabled: bool = True
    disabled_reason: str = ''
    tier: str = 'unknown'  # User-declared project tier; never inferred from key prefix.

    def masked(self) -> str:
        s = self.secret or ''
        if len(s) <= 8:
            return '•' * max(4, len(s))
        return f'{s[:4]}••••••••{s[-4:]}'

    def display_label(self) -> str:
        return self.label.strip() or self.id

    def to_dict(self) -> dict:
        return asdict(self)


def stable_key_id(secret: str, prefix: str = 'GEM') -> str:
    digest = hashlib.sha256(secret.encode('utf-8')).hexdigest()[:10].upper()
    return f'{prefix}-{digest}'


def normalize_key_records(raw, prefix: str = 'GEM') -> list[ApiKeyRecord]:
    """Migrate legacy list[str] and validate current list[dict] without exposing secrets."""
    out: list[ApiKeyRecord] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            secret = item.strip()
            if not secret:
                continue
            rid = stable_key_id(secret, prefix)
            rec = ApiKeyRecord(rid, secret, '', 'default', True, '')
        elif isinstance(item, dict):
            secret = str(item.get('secret') or '').strip()
            if not secret:
                continue
            rid = str(item.get('id') or stable_key_id(secret, prefix)).strip()
            rec = ApiKeyRecord(
                id=rid,
                secret=secret,
                label=str(item.get('label') or '').strip(),
                project_group=str(item.get('project_group') or 'default').strip() or 'default',
                enabled=bool(item.get('enabled', True)),
                disabled_reason=str(item.get('disabled_reason') or '').strip(),
                tier=str(item.get('tier') or 'unknown').lower() if str(item.get('tier') or 'unknown').lower() in ('free','paid') else 'unknown',
            )
        else:
            continue
        # Same secret must not be scheduled twice even if pasted twice.
        fingerprint = hashlib.sha256(rec.secret.encode('utf-8')).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(rec)
    return out


def normalize_secret_payload(raw) -> dict:
    """Normalize legacy/partially malformed secret-store schemas safely.

    Older builds stored only Gemini settings. Future/partial files may also contain
    unexpected list/string values after a manual recovery. Preserve unknown top-level
    fields, but guarantee provider sections are dictionaries with normalized key lists.
    """
    root = dict(raw) if isinstance(raw, dict) else {}

    gem = root.get('gemini')
    if isinstance(gem, dict):
        gem_section = dict(gem)
        gem_raw = gem_section.get('keys', [])
    elif isinstance(gem, list):
        gem_section = {}
        gem_raw = gem
    else:
        # Very early/manual payloads may have put the Gemini list at top-level `keys`.
        gem_section = {}
        gem_raw = root.get('keys', []) if isinstance(root.get('keys'), list) else []
    gem_section['keys'] = [r.to_dict() for r in normalize_key_records(gem_raw, 'GEM')]
    root['gemini'] = gem_section

    oai = root.get('openai')
    if isinstance(oai, dict):
        oai_section = dict(oai)
        oai_raw = oai_section.get('keys', [])
    elif isinstance(oai, list):
        oai_section = {}
        oai_raw = oai
    else:
        oai_section = {}
        oai_raw = []
    oai_section['keys'] = [r.to_dict() for r in normalize_key_records(oai_raw, 'OAI')]
    oai_section['enabled'] = bool(oai_section.get('enabled', False))
    oai_section['model'] = str(oai_section.get('model') or '').strip()
    root['openai'] = oai_section

    # Do not retain the ambiguous legacy top-level key list after migration.
    root.pop('keys', None)
    return root
