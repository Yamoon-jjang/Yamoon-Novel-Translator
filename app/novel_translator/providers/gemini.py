from __future__ import annotations
import requests, re
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from .base import *
from ..config import CONFIG_DIR, load_json

class GeminiProvider(TranslationProvider):
    name = 'gemini'
    DEFAULT_BASE = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    DEFAULT_SAFETY = [
        {'category':'HARM_CATEGORY_HATE_SPEECH','threshold':'OFF'},
        {'category':'HARM_CATEGORY_HARASSMENT','threshold':'OFF'},
        {'category':'HARM_CATEGORY_SEXUALLY_EXPLICIT','threshold':'OFF'},
        {'category':'HARM_CATEGORY_DANGEROUS_CONTENT','threshold':'OFF'},
    ]

    def __init__(self):
        self.base = self.DEFAULT_BASE
        self.safety = [dict(x) for x in self.DEFAULT_SAFETY]
        # model id -> supported thinking levels ([] = never send thinkingConfig).
        # Models missing from models.json keep the legacy gemini-3 behaviour.
        self.thinking_support: dict[str, list[str]] = {}
        # Models that rejected thinkingConfig at runtime (HTTP 400) in this process.
        self._no_thinking: set[str] = set()
        self.last_usage: dict | None = None
        try:
            for row in load_json(CONFIG_DIR/'models.json').get('models',[]):
                if row.get('provider')=='gemini' and isinstance(row.get('thinking_levels'),list):
                    self.thinking_support[str(row.get('id'))]=[str(x).lower() for x in row['thinking_levels']]
        except Exception:
            pass
        try:
            cfg=load_json(CONFIG_DIR/'providers.json')
            row=next((x for x in cfg.get('providers',[]) if x.get('id')=='gemini'),None)
            if row:
                endpoint=str(row.get('endpoint') or '').strip()
                if endpoint and '{model}' in endpoint: self.base=endpoint
                safety=row.get('safety_settings')
                if isinstance(safety,list) and safety: self.safety=safety
        except Exception:
            # Runtime resource audit reports malformed config. Defaults keep direct unit tests deterministic.
            pass

    @staticmethod
    def _retry_after(resp, data: dict | None = None) -> float | None:
        raw = resp.headers.get('Retry-After') if getattr(resp, 'headers', None) else None
        if raw:
            try:return max(0.0,float(raw))
            except ValueError:
                try:return max(0.0,(parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
                except Exception:pass
        data=data or {}
        for detail in (data.get('error') or {}).get('details') or []:
            if not isinstance(detail,dict):continue
            delay=detail.get('retryDelay') or detail.get('retry_delay')
            if isinstance(delay,str):
                m=re.match(r'([0-9.]+)s$',delay.strip())
                if m:return max(0.0,float(m.group(1)))
        msg=str((data.get('error') or {}).get('message') or '')
        m=re.search(r'(?:retry|try again)\s+(?:in|after)\s+([0-9.]+)\s*s',msg,re.I)
        return max(0.0,float(m.group(1))) if m else None

    def _thinking_for(self, model: str, level: str | None) -> str | None:
        thinking=(level or '').strip().lower()
        if thinking not in {'minimal','low','medium','high'} or model in self._no_thinking:
            return None
        if model in self.thinking_support:
            levels = self.thinking_support[model]
            if thinking == 'minimal' and levels and 'minimal' not in levels:
                return 'low' if 'low' in levels else None    # the lowest level the model has
            return thinking if thinking in levels else None
        # Unknown/typed model ID: Gemini 3.x exposes discrete thinking levels.
        return thinking if model.startswith('gemini-3') and thinking in {'minimal','low','medium','high'} else None

    @staticmethod
    def _usage(data: dict | None) -> dict | None:
        meta=(data or {}).get('usageMetadata') if isinstance(data,dict) else None
        if not isinstance(meta,dict):
            return None
        out=int(meta.get('candidatesTokenCount') or 0); thought=int(meta.get('thoughtsTokenCount') or 0)
        return {'input':int(meta.get('promptTokenCount') or 0),'output':out+thought,'thinking':thought}

    def translate(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        self.last_usage=None
        try:
            return self._translate_once(request, api_key)
        except ProviderError as e:
            # A model that does not accept the thinking setting must not stop the
            # whole job (HTTP 400 is FATAL). Retry once without it and remember.
            if (e.status_code == 400 and 'thinking' in (e.message or '').lower()
                    and request.model not in self._no_thinking and self._thinking_for(request.model, request.thinking_level)):
                if self._thinking_for(request.model, request.thinking_level) == 'minimal':
                    # A model without the minimal level: step down to its lowest other level
                    # (never to no setting, which would mean the model's default, often higher).
                    levels = self.thinking_support.get(request.model)
                    if levels is not None and 'minimal' in levels:
                        self.thinking_support[request.model] = [x for x in levels if x != 'minimal']
                    else:
                        self.thinking_support[request.model] = ['low', 'medium', 'high']
                    from dataclasses import replace as _replace
                    return self.translate(_replace(request, thinking_level='low'), api_key)
                self._no_thinking.add(request.model)
                return self._translate_once(request, api_key)
            raise

    def _translate_once(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        url = self.base.format(model=request.model)
        payload = {
            'systemInstruction': {'parts':[{'text':request.system_prompt}]},
            'contents':[{'role':'user','parts':[{'text':request.user_prompt}]}],
            # Gemini 3.8 migration guidance deprecates temperature/top_p/top_k/candidate_count.
            # Translation behavior is controlled by the prompt; do not send deprecated sampling fields.
            'safetySettings': self.safety,
        }
        thinking=self._thinking_for(request.model, request.thinking_level)
        if thinking:
            payload['generationConfig']={'thinkingConfig': {'thinkingLevel': thinking}}
        try:
            r = requests.post(url, headers={'x-goog-api-key':api_key,'Content-Type':'application/json','User-Agent':'NovelTranslator/2.1'}, json=payload, timeout=request.timeout)
        except requests.Timeout as e:
            raise ProviderError(ErrorKind.TRANSIENT, '요청 시간 초과') from e
        except requests.RequestException as e:
            raise ProviderError(ErrorKind.TRANSIENT, f'네트워크 오류: {e}') from e
        try: data = r.json()
        except Exception: data = {}
        self.last_usage = self._usage(data)
        if r.status_code != 200:
            msg = (data.get('error') or {}).get('message') or r.text[:500]
            low = msg.lower(); retry_after = self._retry_after(r,data)
            details = str((data.get('error') or {}).get('details') or '') if isinstance(data, dict) else ''
            invalid_terms=('api key not valid','invalid api key','api_key_invalid','invalid authentication credentials','api key expired','expired api key')
            if r.status_code == 429:
                # The free tier's per-MINUTE limit also says "You exceeded your current quota,
                # please check your plan and billing details" but carries a short retryDelay and
                # a PerMinute quotaId: that is a rate limit (wait seconds), not a used-up day.
                ids = details.lower()
                if 'perday' in ids or 'per_day' in ids or 'daily' in low or 'per day' in low or 'quota has been exhausted' in low:
                    kind = ErrorKind.QUOTA
                elif 'perminute' in ids or 'per_minute' in ids or (retry_after is not None and retry_after <= 120):
                    kind = ErrorKind.RATE_LIMIT
                elif any(x in low for x in ('quota exceeded','billing','insufficient quota')):
                    kind = ErrorKind.QUOTA
                else:
                    kind = ErrorKind.RATE_LIMIT
            elif r.status_code == 400 and (any(x in low for x in invalid_terms) or 'API_KEY_INVALID' in details):
                kind = ErrorKind.AUTH   # Gemini reports a bad/expired key as 400 INVALID_ARGUMENT: try the next key
            elif r.status_code in (401, 403):
                blocked_terms=('leak','blocked','reported as leaked','dormant')
                if any(x in low for x in blocked_terms):
                    kind = ErrorKind.KEY_BLOCKED
                elif r.status_code == 401 or any(x in low for x in invalid_terms):
                    kind = ErrorKind.AUTH
                else:
                    # A generic 403 can be project access, API restriction, region,
                    # billing or another configuration issue. Do not permanently
                    # disable an otherwise valid key based on that ambiguous signal.
                    kind = ErrorKind.FATAL
            elif r.status_code in (408, 500, 502, 503, 504): kind = ErrorKind.TRANSIENT
            else: kind = ErrorKind.FATAL
            raise ProviderError(kind, msg, r.status_code, retry_after)
        pf = (data.get('promptFeedback') or {}).get('blockReason')
        if pf:
            kind = ErrorKind.SAFETY if pf == 'SAFETY' else ErrorKind.POLICY
            raise ProviderError(kind, f'입력 차단: {pf}')
        candidates = data.get('candidates') or []
        if not candidates: raise ProviderError(ErrorKind.FORMAT, '응답 후보가 없습니다.')
        cand = candidates[0]; finish = cand.get('finishReason', '')
        if finish == 'SAFETY': raise ProviderError(ErrorKind.SAFETY, f'출력 차단: {finish}')
        if finish in {'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'RECITATION'}:
            raise ProviderError(ErrorKind.POLICY, f'출력 차단: {finish}')
        if finish in {'MAX_TOKENS'}: raise ProviderError(ErrorKind.FORMAT, f'출력 잘림: {finish}')
        parts = (cand.get('content') or {}).get('parts') or []
        text = ''.join(x.get('text','') for x in parts if isinstance(x,dict) and not x.get('thought')).strip()
        if not text: raise ProviderError(ErrorKind.FORMAT, f'빈 번역 응답 (finish={finish})')
        return TranslationResponse(text=text, finish_reason=finish, raw=data)
