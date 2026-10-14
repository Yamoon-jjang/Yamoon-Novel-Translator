from __future__ import annotations
from copy import deepcopy
from .config import CONFIG_DIR, load_json

CONTENT_LABELS={'novel':'소설 · 성인 소재 포함','document':'일반 문서'}

def canonical_kind(value):
    return value if value in CONTENT_LABELS else 'novel'

def default_settings():
    return deepcopy(load_json(CONFIG_DIR/'defaults.json'))

def apply_content_preset(cfg, kind):
    kind=canonical_kind(kind)
    out=deepcopy(cfg)
    defaults=default_settings()
    out['content_kind']=kind
    out['translation_mode']='balanced'
    out['rate_limiter']=deepcopy(defaults['rate_limiter'])
    out['modes']=deepcopy(defaults['modes'])
    if kind=='document':
        for mode,factor,context,retries,timeout in [
                ('quality',1.125,1500,3,120),
                ('balanced',1.5,1000,3,90),
                ('speed',1.875,500,2,75)]:
            out['modes'][mode].update(chunk_factor=factor,context_chars=context,
                                     max_retries=retries,timeout_seconds=timeout)
    return out
