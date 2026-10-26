from __future__ import annotations
import requests
from .base import *


class OpenAIProvider(TranslationProvider):
    """Official OpenAI Responses API fallback provider.

    The endpoint and model remain external configuration so a model/API update does
    not require rebuilding the Windows executable.
    """
    name = 'openai'
    DEFAULT_ENDPOINT = 'https://api.openai.com/v1/responses'

    def __init__(self, endpoint: str | None = None):
        self.endpoint = (endpoint or self.DEFAULT_ENDPOINT).strip()
        self.last_usage: dict | None = None

    @staticmethod
    def _usage(data) -> dict | None:
        u=data.get('usage') if isinstance(data,dict) else None
        if not isinstance(u,dict):
            return None
        details=u.get('output_tokens_details') or {}
        return {'input':int(u.get('input_tokens') or 0),'output':int(u.get('output_tokens') or 0),
                'thinking':int(details.get('reasoning_tokens') or 0) if isinstance(details,dict) else 0}

    @staticmethod
    def _extract_text(data: dict) -> str:
        direct=data.get('output_text')
        if isinstance(direct,str) and direct.strip():
            return direct.strip()
        pieces=[]
        for item in data.get('output') or []:
            if not isinstance(item,dict):
                continue
            for part in item.get('content') or []:
                if not isinstance(part,dict):
                    continue
                if part.get('type') == 'refusal' and part.get('refusal'):
                    raise ProviderError(ErrorKind.REFUSAL, str(part.get('refusal'))[:500])
                text=part.get('text')
                if isinstance(text,str): pieces.append(text)
        return ''.join(pieces).strip()

    @staticmethod
    def _retry_after(resp) -> float | None:
        raw=resp.headers.get('Retry-After') if getattr(resp,'headers',None) else None
        if raw:
            try:return max(0.0,float(raw))
            except Exception:return None
        return None

    def translate(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        self.last_usage=None
        payload={
            'model': request.model,
            'instructions': request.system_prompt,
            'input': request.user_prompt,
        }
        try:
            r=requests.post(self.endpoint,headers={
                'Authorization':f'Bearer {api_key}',
                'Content-Type':'application/json',
                'User-Agent':'NovelTranslator/2.5.5',
            },json=payload,timeout=request.timeout)
        except requests.Timeout as e:
            raise ProviderError(ErrorKind.TRANSIENT,'OpenAI 요청 시간 초과') from e
        except requests.RequestException as e:
            raise ProviderError(ErrorKind.TRANSIENT,f'OpenAI 네트워크 오류: {e}') from e
        try:data=r.json()
        except Exception:data={}
        self.last_usage=self._usage(data)
        if r.status_code != 200:
            err=data.get('error') if isinstance(data,dict) else None
            msg=(err or {}).get('message') if isinstance(err,dict) else None
            msg=msg or r.text[:500] or f'HTTP {r.status_code}'
            low=msg.lower(); code=str((err or {}).get('code') or '').lower() if isinstance(err,dict) else ''
            etype=str((err or {}).get('type') or '').lower() if isinstance(err,dict) else ''
            retry_after=self._retry_after(r)
            if r.status_code == 429:
                quota_terms=('insufficient_quota','quota','billing','credits','usage limit')
                kind=ErrorKind.QUOTA if any(x in (low+' '+code+' '+etype) for x in quota_terms) else ErrorKind.RATE_LIMIT
            elif r.status_code == 401:
                kind=ErrorKind.AUTH
            elif r.status_code == 403:
                # Project/organization/access restrictions should not permanently
                # mark a key as bad without an unambiguous authentication signal.
                kind=ErrorKind.FATAL
            elif r.status_code in (408,500,502,503,504):
                kind=ErrorKind.TRANSIENT
            elif ('content' in code and ('filter' in code or 'policy' in code)) or code=='invalid_prompt' \
                    or 'usage policy' in low or 'content_policy' in code:
                # Reasoning models answer a flagged prompt with 400 invalid_prompt: a content
                # outcome (split / isolate / backup engine), not a settings error.
                kind=ErrorKind.POLICY
            else:
                kind=ErrorKind.FATAL
            raise ProviderError(kind,msg,r.status_code,retry_after)
        try:
            text=self._extract_text(data)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(ErrorKind.FORMAT,'OpenAI Responses 응답 형식을 해석하지 못했습니다.') from e
        status=str(data.get('status') or '')
        incomplete=data.get('incomplete_details') or {}
        reason=str(incomplete.get('reason') or '') if isinstance(incomplete,dict) else ''
        if status == 'incomplete' or reason:
            # A cut-off response must never be accepted as a translation, even when
            # some text came back: the tail of the chunk would be silently lost.
            if reason == 'content_filter':
                raise ProviderError(ErrorKind.POLICY,'OpenAI 출력 차단: content_filter')
            raise ProviderError(ErrorKind.FORMAT,f'OpenAI 응답 미완료: {reason or status}')
        if not text:
            raise ProviderError(ErrorKind.FORMAT,f'OpenAI 빈 번역 응답 (status={status})')
        return TranslationResponse(text=text,finish_reason=str(data.get('status') or ''),raw=data)
