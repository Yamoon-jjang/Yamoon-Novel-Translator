from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class TranslationCancelled(Exception):
    """User cancellation; never an API/key/content failure."""

class ErrorKind(str, Enum):
    TRANSIENT='TRANSIENT'
    RATE_LIMIT='RATE_LIMIT'
    QUOTA='QUOTA'
    AUTH='AUTH'
    KEY_BLOCKED='KEY_BLOCKED'
    SAFETY='SAFETY'
    POLICY='POLICY'
    REFUSAL='REFUSAL'
    FORMAT='FORMAT'
    FATAL='FATAL'

LOOP_MARK = '반복 루프'


def is_loop_error(error) -> bool:
    """A local model kept repeating the same text (see LocalLLMProvider)."""
    return getattr(getattr(error, 'kind', None), 'value', '') == 'FORMAT' and LOOP_MARK in str(getattr(error, 'message', '') or '')


@dataclass
class ProviderError(Exception):
    kind: ErrorKind
    message: str
    status_code: int|None=None
    retry_after: float|None=None
    def __str__(self): return self.message

@dataclass
class TranslationRequest:
    model: str
    system_prompt: str
    user_prompt: str
    temperature: float = 0.2
    timeout: int = 90
    thinking_level: str | None = None
    # What the request is for. Only the local LLM uses it: 'translate' / 'edit' (prose,
    # model-recommended sampling), 'analysis' (steady 0.2), 'json' (steady 0.2, JSON only).
    purpose: str = 'translate'
    # Paragraph IDs of a tagged 'edit' request (translation requests are read from SOURCE).
    paragraph_ids: tuple = ()

@dataclass
class TranslationResponse:
    text: str
    finish_reason: str=''
    raw: dict|None=None

class TranslationProvider:
    name='base'
    def translate(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        raise NotImplementedError
