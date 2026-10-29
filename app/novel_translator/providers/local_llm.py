from __future__ import annotations
import json
import re
from urllib.parse import urlsplit
import requests
from .base import *
from ..validation import strip_reasoning, ID_LINE_RE


class _NativeUnavailable(Exception):
    """The server on this address does not speak the Ollama native API."""


def local_provider_args(settings: dict | None, timeout: int | None = None) -> tuple[tuple, dict]:
    """(args, keyword args) for LocalLLMProvider from the saved 로컬 LLM settings (address,
    response wait, thinking, context length). Translation, analysis and the connection test
    all read the settings here, so they always build the provider the same way."""
    s = dict(settings or {})
    return ((str(s.get('endpoint') or '').strip() or None, int(timeout or s.get('timeout_seconds') or 300)),
            {'think': bool(s.get('think', False)), 'num_ctx': (int(s['num_ctx']) if s.get('num_ctx') else None)})


class LocalLLMProvider(TranslationProvider):
    """Last-resort backup on the user's own machine (Ollama / LM Studio / llama.cpp).

    Uses the OpenAI-compatible Chat Completions shape that these local servers
    expose. No API key is required; the endpoint normally points to localhost.
    It only receives paragraphs the cloud providers could not translate, with the
    same system prompt (including the translation SCOPE) as every other provider.
    """
    name = 'local'
    OLLAMA_ENDPOINT = 'http://localhost:11434/v1/chat/completions'
    LMSTUDIO_ENDPOINT = 'http://localhost:1234/v1/chat/completions'
    DEFAULT_ENDPOINT = OLLAMA_ENDPOINT
    # Reasoning models (e.g. Qwen-style) may prepend their thinking to the answer.
    _THINK_RE = re.compile(r'<think>.*?</think>\s*', re.S | re.I)

    # Ollama's default context (4,096 tokens) is smaller than one translation request
    # (system rules ~3k + previous context + chunk + answer), so Ollama would silently
    # cut the prompt. A fixed value also avoids reloading the model between requests.
    DEFAULT_NUM_CTX = 12288

    def __init__(self, endpoint: str | None = None, timeout: int | None = None,
                 think: bool = False, num_ctx: int | None = None,
                 structured: bool = True, sampling: str = 'recommended'):
        self.endpoint = (endpoint or self.DEFAULT_ENDPOINT).strip()
        # Local generation is much slower than a cloud API; never use the cloud timeout.
        self.min_timeout = max(60, int(timeout or 300))
        self.think = bool(think)
        self.num_ctx = int(self.DEFAULT_NUM_CTX if num_ctx is None else num_ctx)
        self.native_url = self.ollama_native_url(self.endpoint)
        self._send_think = True
        self.last_usage: dict | None = None
        self.last_speed: float | None = None   # generated tokens per second (Ollama native)
        self.output_budget_last = 0
        self.loops = 0   # loops seen (each one stopped early)
        # Structured output (Ollama `format`): the reply must be a JSON object with every
        # sent paragraph ID as a required key, so the model cannot drop or merge a
        # paragraph. Switched off for this session if the server rejects it.
        self.structured = bool(structured)
        self.sampling = str(sampling or 'recommended')

    @staticmethod
    def ollama_native_url(endpoint: str) -> str | None:
        """Ollama's native /api/chat (supports think=false and num_ctx); None for other servers."""
        try:
            u = urlsplit(endpoint or '')
            if not u.scheme or not u.netloc:
                return None
            if u.port == 11434 or u.path.startswith('/api/'):
                return f'{u.scheme}://{u.netloc}/api/chat'
        except ValueError:
            return None
        return None

    @staticmethod
    def _usage(data) -> dict | None:
        u = data.get('usage') if isinstance(data, dict) else None
        if not isinstance(u, dict):
            return None
        return {'input': int(u.get('prompt_tokens') or 0), 'output': int(u.get('completion_tokens') or 0), 'thinking': 0}

    def translate(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        self.last_usage = None; self.last_speed = None
        if self.native_url:
            try:
                return self._translate_native(request, api_key)
            except _NativeUnavailable:
                self.native_url = None   # e.g. another server on port 11434: use the OpenAI-compatible path
        return self._translate_compat(request, api_key)

    def _headers(self, api_key: str) -> dict:
        headers = {'Content-Type': 'application/json', 'User-Agent': 'NovelTranslator/2.5.9'}
        if api_key and api_key not in {'local', 'none', '-'}:
            headers['Authorization'] = f'Bearer {api_key}'
        return headers

    def _idle_timeout(self, request: TranslationRequest) -> int:
        return max(int(request.timeout or 0), self.min_timeout)

    def _timeout_error(self, request: TranslationRequest, streaming: bool) -> ProviderError:
        idle = self._idle_timeout(request)
        what = f'{idle}초 동안 한 글자도 보내지 않았습니다' if streaming else f'{idle}초 안에 답을 끝내지 못했습니다'
        return ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM이 {what} (모델을 불러오는 중이거나, 그래픽카드 메모리가 모자라 일부가 CPU에서 돌고 있을 수 있습니다. '
                                                  '명령 프롬프트에서 ollama ps 로 GPU 사용률을 확인하거나, API 설정 > 로컬 LLM에서 응답 대기 한도를 늘리세요)')

    def _post(self, url: str, request: TranslationRequest, api_key: str, payload: dict, stream: bool = False):
        try:
            if stream:
                # Streaming: the timeout is the longest silence between pieces of the answer,
                # so a long chunk never times out while the model is still writing.
                return requests.post(url, headers=self._headers(api_key), json=payload, stream=True,
                                     timeout=(15, self._idle_timeout(request)))
            return requests.post(url, headers=self._headers(api_key), json=payload, timeout=self._idle_timeout(request))
        except requests.Timeout as e:
            raise self._timeout_error(request, stream) from e
        except requests.ConnectionError as e:
            raise ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM 서버에 연결할 수 없습니다. Ollama 또는 LM Studio가 실행 중인지, 주소가 맞는지 확인하세요: {self.endpoint}') from e
        except requests.RequestException as e:
            raise ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM 요청 오류: {e}') from e

    @staticmethod
    def _error_text(r, data) -> str:
        err = data.get('error') if isinstance(data, dict) else None
        return str((err.get('message') if isinstance(err, dict) else err) or r.text[:500] or f'HTTP {r.status_code}')

    def _raise_status(self, r, msg: str):
        low = msg.lower()
        if r.status_code == 404 or 'not found' in low:
            # Wrong model name or endpoint: repeats on every request.
            raise ProviderError(ErrorKind.FATAL, f'로컬 LLM 모델/주소를 찾을 수 없습니다: {msg}', r.status_code)
        if r.status_code in (408, 429, 500, 502, 503, 504):
            raise ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM 일시 오류: {msg}', r.status_code)
        raise ProviderError(ErrorKind.FATAL, f'로컬 LLM 오류: {msg}', r.status_code)

    def _finish(self, text: str, finish: str, data, partial_ok: bool = False) -> TranslationResponse:
        if finish == 'length' and partial_ok and (text or '').strip():
            # Analysis answer cut off (or stopped in a loop): hand over what arrived; the
            # strict parser keeps only the complete, valid lines/entries in it.
            clean = strip_reasoning(self._THINK_RE.sub('', text)).strip()
            if clean:
                return TranslationResponse(text=clean, finish_reason='length', raw=data)
        if finish == 'length':
            raise ProviderError(ErrorKind.FORMAT, f'로컬 LLM 출력 잘림 (답이 원문보다 지나치게 길거나 같은 말을 반복함 · 출력 한도 {self.output_budget_last:,}토큰 · 컨텍스트 {self.num_ctx:,})')
        if finish == 'content_filter':
            raise ProviderError(ErrorKind.POLICY, '로컬 LLM 출력 차단: content_filter')
        text = self._THINK_RE.sub('', text or '')
        # DeepSeek-R1 style: the reply opens inside the reasoning and only "</think>" shows.
        # Only text before the first paragraph ID is cut (a novel may contain "</think>").
        text = strip_reasoning(text).strip()
        if not text:
            raise ProviderError(ErrorKind.FORMAT, '로컬 LLM 빈 번역 응답')
        return TranslationResponse(text=text, finish_reason=finish, raw=data)

    @staticmethod
    def _get_list(url: str, base: str, parts, timeout: float):
        """GET a model list. For "localhost", ask 127.0.0.1 first: on Windows "localhost" is
        tried as IPv6 (::1) first, and Ollama listens on IPv4 only, so the refused IPv6
        attempt alone costs about a second before the list arrives. Falls back to the
        address as written when nothing answers on 127.0.0.1."""
        headers = {'User-Agent': 'NovelTranslator/2.5.9'}
        if (parts.hostname or '').lower() == 'localhost':
            fast = url.replace(base, base.replace(parts.hostname, '127.0.0.1', 1), 1)
            try:
                return requests.get(fast, timeout=timeout, headers=headers)
            except requests.ConnectionError:
                pass
        return requests.get(url, timeout=timeout, headers=headers)

    def list_models(self, timeout: float = 4.0) -> list[str]:
        """Models the local server has: Ollama /api/tags, otherwise OpenAI-style /v1/models."""
        u = urlsplit(self.endpoint)
        base = f'{u.scheme}://{u.netloc}'
        urls = []
        if self.native_url:
            urls.append(base + '/api/tags')
        path = u.path or ''
        compat = base + (path[:path.rfind('/chat/completions')] if '/chat/completions' in path else '/v1') + '/models'
        urls.append(compat)
        last = None
        for url in urls:
            try:
                r = self._get_list(url, base, u, timeout)
            except requests.ConnectionError as e:
                raise ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM 서버에 연결할 수 없습니다. Ollama 또는 LM Studio가 실행 중인지 확인하세요: {base}') from e
            except requests.RequestException as e:
                last = e; continue
            if r.status_code != 200:
                last = r.status_code; continue
            try:
                data = r.json()
            except Exception:
                continue
            names = [str(m.get('name') or m.get('model') or '') for m in (data.get('models') or [])] if isinstance(data, dict) and 'models' in data \
                else [str(m.get('id') or '') for m in (data.get('data') or [])] if isinstance(data, dict) else []
            names = sorted({n for n in names if n})
            if names or url == urls[-1]:
                return names
        raise ProviderError(ErrorKind.TRANSIENT, f'모델 목록을 가져오지 못했습니다 ({last})')

    MAX_AUTO_CTX = 32768
    _CJK_RE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]')

    @classmethod
    def estimate_tokens(cls, text: str) -> int:
        """Rough, deliberately generous token estimate (CJK/Korean ~1 per char, Latin ~1 per 3.5)."""
        text = text or ''
        cjk = len(cls._CJK_RE.findall(text))
        return int(cjk + (len(text) - cjk) / 3.5) + 1

    def context_for(self, request: TranslationRequest) -> int:
        """The set context length, raised for a request that would not fit
        (long 작품 메모 / quality mode), so Ollama never cuts the instructions."""
        if self.num_ctx <= 0:
            return 0
        source = request.user_prompt or ''
        need = self.estimate_tokens(request.system_prompt) + self.estimate_tokens(source)
        cap = int(getattr(request, 'max_output', 0) or 0)
        need += (cap if cap else int(self.estimate_tokens(source) * 0.6)) + 512      # room for the answer
        if need <= self.num_ctx:
            return self.num_ctx
        # Raise it for good (never back down): Ollama reloads the whole model whenever
        # num_ctx changes, so flipping between sizes would reload it on every request.
        self.num_ctx = max(self.num_ctx, min(self.MAX_AUTO_CTX, ((need + 2047) // 2048) * 2048))
        return self.num_ctx

    # A small model sometimes falls into a loop ("야야야야…", the same phrase over and
    # over). It keeps sending text, so the silence timeout never fires; without a limit it
    # writes until the context is full (10+ minutes). Two guards: an output-length budget
    # from the source length, and stopping the stream as soon as a loop is seen.
    _LOOP_RE = re.compile(r'(.{1,20}?)\1{49,}$', re.S)
    LOOP_MIN_CHARS = 200
    LOOP_CHECK_EVERY = 160

    def output_budget(self, request: TranslationRequest) -> int:
        """Most tokens a correct answer can need (Korean is longer than Chinese/Japanese)."""
        text = request.user_prompt or ''
        i = text.find('[SOURCE - ')
        src = text[i:] if i >= 0 else text
        self.output_budget_last = max(1024, self.estimate_tokens(src) * 3 + 512)
        return self.output_budget_last

    def _loop_in(self, tail: str, request: TranslationRequest) -> str:
        m = self._LOOP_RE.search(tail)
        if not m or len(m.group(0)) < self.LOOP_MIN_CHARS:
            return ''
        unit = m.group(1)
        if not unit.strip() or unit * 20 in (request.user_prompt or ''):
            return ''   # blank lines / a separator line that the source itself has
        return unit

    # Sampling for the one retry after a loop: the same settings would loop the same way.
    ANTI_LOOP_OPTIONS = {'temperature': 0.6, 'repeat_penalty': 1.3, 'repeat_last_n': 256}

    # Google's recommended sampling for Gemma 4 (also the defaults of Ollama's own gemma4
    # models). Other local models keep the app's long-standing setting.
    GEMMA_SAMPLING = {'temperature': 1.0, 'top_p': 0.95, 'top_k': 64}
    DEFAULT_SAMPLING = {'temperature': 0.2}

    ANALYSIS_PURPOSES = ('analysis', 'json')

    def sampling_for(self, model: str, purpose: str = 'translate') -> dict:
        # Analysis (glossary, speech table, refinement checks) keeps the steady setting it
        # was tuned with; only prose (translation, refinement) uses the model's own sampling.
        if purpose in self.ANALYSIS_PURPOSES:
            return dict(self.DEFAULT_SAMPLING)
        if self.sampling == 'recommended' and 'gemma' in (model or '').lower():
            return dict(self.GEMMA_SAMPLING)
        return dict(self.DEFAULT_SAMPLING)

    @classmethod
    def source_ids(cls, request: TranslationRequest) -> list[str]:
        """Paragraph IDs a reply must contain: given by a refinement ('edit') request,
        or read from the SOURCE block of a translation request."""
        purpose = getattr(request, 'purpose', 'translate')
        if purpose == 'edit':
            return [str(x) for x in (getattr(request, 'paragraph_ids', ()) or ())]
        if purpose != 'translate':
            return []
        text = request.user_prompt or ''
        i = text.find('[SOURCE - ')
        if i < 0:
            return []
        return [f'P{x}' for x in ID_LINE_RE.findall(text[i:])]

    @staticmethod
    def structured_schema(ids: list[str]) -> dict:
        return {'type': 'object',
                'properties': {pid: {'type': 'string', 'minLength': 1} for pid in ids},
                'required': list(ids), 'additionalProperties': False}

    @staticmethod
    def structured_note(ids: list[str], purpose: str = 'translate') -> str:
        if purpose == 'edit':
            return ('\n\n[출력 형식]\n'
                    'JSON 객체 하나만 출력하세요. 키는 정확히 다음 문단 ID이며 이 순서입니다: ' + ', '.join(ids) + '. '
                    '각 값은 그 문단 하나의 교정된 한국어 문장 전체입니다. 한 문단의 내용을 다른 키로 옮기거나 합치지 마세요.')
        return ('\n\n[OUTPUT FORMAT FOR THIS REQUEST]\n'
                'Return one JSON object and nothing else. Its keys are exactly these paragraph IDs, '
                'in this order: ' + ', '.join(ids) + '. Each value is the complete Korean translation of '
                'that one paragraph (or the SCOPE exclusion marker for that paragraph only). '
                'Never move a paragraph\'s text into another key.')

    @staticmethod
    def structured_to_tagged(text: str, ids: list[str]) -> str:
        """Turn the JSON reply back into the usual tagged text; the rest of the app is unchanged."""
        body = (text or '').strip()
        m = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', body, re.S | re.I)
        if m:
            body = m.group(1)
        try:
            data = json.loads(body)
        except Exception as e:
            raise ProviderError(ErrorKind.FORMAT, '로컬 LLM 구조화 응답(JSON)을 해석하지 못했습니다.') from e
        if not isinstance(data, dict):
            raise ProviderError(ErrorKind.FORMAT, '로컬 LLM 구조화 응답이 객체가 아닙니다.')
        rows = []
        for pid in ids:
            value = data.get(pid)
            if isinstance(value, str) and value.strip():
                rows.append(f'<{pid}>\n{value.strip()}')
        return '\n\n'.join(rows)

    def _translate_native(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        ids = self.source_ids(request) if self.structured else []
        if len(ids) < 2:
            ids = []
        if ids or (self.structured and getattr(request, 'purpose', '') == 'json'):
            try:
                return self._translate_native_loops(request, api_key, ids)
            except ProviderError as e:
                low = (e.message or '').lower()
                if e.kind in (ErrorKind.FATAL, ErrorKind.TRANSIENT) and any(w in low for w in ('format', 'grammar', 'schema', 'json')):
                    self.structured = False   # this server cannot do it: plain tagged text from now on
                else:
                    raise
        return self._translate_native_loops(request, api_key, [])

    def _translate_native_loops(self, request: TranslationRequest, api_key: str, ids: list[str]) -> TranslationResponse:
        try:
            return self._translate_native_once(request, api_key, {}, ids)
        except ProviderError as e:
            if not is_loop_error(e):
                raise
            self.loops += 1
        try:
            return self._translate_native_once(request, api_key, self.ANTI_LOOP_OPTIONS, ids)
        except ProviderError as e:
            if is_loop_error(e):
                self.loops += 1
                raise ProviderError(ErrorKind.FORMAT, e.message + ' · 반복 억제 설정으로 다시 요청해도 반복') from e
            raise

    def _translate_native_once(self, request: TranslationRequest, api_key: str, extra: dict, ids: list[str] | None = None) -> TranslationResponse:
        ids = list(ids or [])
        # With thinking on, the reasoning shares this limit: give it room on top of the answer.
        purpose = getattr(request, 'purpose', 'translate')
        options = self.sampling_for(request.model, purpose)
        budget = self.output_budget(request)
        cap = int(getattr(request, 'max_output', 0) or 0)
        if cap:
            budget = min(budget, cap)
        options['num_predict'] = budget + (8192 if self.think else 0) + (16 * len(ids))
        options.update(extra)
        ctx = self.context_for(request)
        if ctx > 0:
            options['num_ctx'] = ctx
        user = request.user_prompt + (self.structured_note(ids, purpose) if ids else '')
        payload = {
            'model': request.model,
            'messages': [
                {'role': 'system', 'content': request.system_prompt},
                {'role': 'user', 'content': user},
            ],
            'stream': True,
            'options': options,
            # No pre-warm: the first actual local fallback loads the model.
            # Ollama then keeps it for five idle minutes and automatically unloads it.
            # Applied only to the native endpoint, never to LM Studio / compatible APIs.
            'keep_alive': '5m',
        }
        if ids:
            payload['format'] = self.structured_schema(ids)
        elif purpose == 'json' and self.structured:
            payload['format'] = 'json'   # a glossary answer that is always readable JSON
        for _ in range(2):
            if self._send_think:
                payload['think'] = self.think
            else:
                payload.pop('think', None)
            r = self._post(self.native_url, request, api_key, payload, stream=True)
            if r.status_code == 200:
                data = self._read_stream(r, request)
            else:
                try:
                    data = r.json()
                except Exception:
                    data = {}
            if r.status_code == 400 and 'think' in payload and 'think' in self._error_text(r, data).lower():
                self._send_think = False   # model without a thinking switch: ask again without it
                continue
            break
        if r.status_code == 404 and not (isinstance(data, dict) and 'model' in str(data.get('error') or '').lower()):
            raise _NativeUnavailable()
        if isinstance(data, dict) and data.get('prompt_eval_count') is not None:
            self.last_usage = {'input': int(data.get('prompt_eval_count') or 0), 'output': int(data.get('eval_count') or 0), 'thinking': 0}
            try:
                dur = float(data.get('eval_duration') or 0) / 1e9
                self.last_speed = round(int(data.get('eval_count') or 0) / dur, 1) if dur > 0 else None
            except Exception:
                self.last_speed = None
        if r.status_code != 200:
            self._raise_status(r, self._error_text(r, data))
        if isinstance(data, dict) and data.get('error'):
            msg = str(data.get('error'))
            if 'not found' in msg.lower():
                raise ProviderError(ErrorKind.FATAL, f'로컬 LLM 모델/주소를 찾을 수 없습니다: {msg}')
            # Anything else inside a started answer (out of GPU memory, server restart …)
            # is temporary: retry later instead of stopping the job as a settings error.
            raise ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM 일시 오류: {msg}')
        msg = data.get('message') if isinstance(data, dict) else None
        if not isinstance(msg, dict):
            raise ProviderError(ErrorKind.FORMAT, '로컬 LLM 응답 형식을 해석하지 못했습니다.')
        response = self._finish(str(msg.get('content') or ''), str(data.get('done_reason') or ''), data,
                                partial_ok=bool(getattr(request, 'partial_ok', False)))
        if ids:
            response = TranslationResponse(text=self.structured_to_tagged(response.text, ids),
                                           finish_reason=response.finish_reason, raw=response.raw)
        return response

    def _read_stream(self, r, request: TranslationRequest) -> dict:
        """Collect an Ollama /api/chat stream (one JSON object per line) into one reply."""
        parts: list[str] = []; final: dict = {}; fresh = 0
        cancel = getattr(request, 'cancel', None)
        try:
            for line in r.iter_lines():
                if cancel is not None and cancel():
                    # Closing the stream (finally below) also stops Ollama generating.
                    raise TranslationCancelled('사용자 취소')
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get('error'):
                    return {'error': obj.get('error')}
                msg = obj.get('message') if isinstance(obj, dict) else None
                if isinstance(msg, dict) and msg.get('content'):
                    piece = str(msg['content']); parts.append(piece); fresh += len(piece)
                    if fresh >= self.LOOP_CHECK_EVERY:
                        fresh = 0
                        unit = self._loop_in(''.join(parts[-400:])[-1200:], request)
                        if unit and getattr(request, 'partial_ok', False):
                            final = {'done_reason': 'length'}; break   # keep what came before the loop
                        if unit:
                            raise ProviderError(ErrorKind.FORMAT, f'로컬 LLM {LOOP_MARK} · 같은 말을 끝없이 반복해 중단함 (반복: "{unit[:10]}")')
                if isinstance(obj, dict) and obj.get('done'):
                    final = obj; break
        except requests.Timeout as e:
            raise self._timeout_error(request, True) from e
        except requests.RequestException as e:
            if 'timed out' in str(e).lower():
                # requests reports a read timeout inside iter_lines as ConnectionError.
                raise self._timeout_error(request, True) from e
            raise ProviderError(ErrorKind.TRANSIENT, f'로컬 LLM 응답이 중간에 끊겼습니다: {e}') from e
        finally:
            try: r.close()
            except Exception: pass
        data = dict(final)
        data['message'] = {'role': 'assistant', 'content': ''.join(parts)}
        if not final:
            data['done_reason'] = 'length' if parts else ''
        return data

    def _translate_compat(self, request: TranslationRequest, api_key: str) -> TranslationResponse:
        payload = {
            'model': request.model,
            'messages': [
                {'role': 'system', 'content': request.system_prompt},
                {'role': 'user', 'content': request.user_prompt},
            ],
            'max_tokens': (min(self.output_budget(request), int(getattr(request, 'max_output', 0) or 0) or self.output_budget(request))
                           + 4096),   # room for a reasoning model's thinking (no stream: no loop check here)
            'stream': False,
        }
        payload.update(self.sampling_for(request.model, getattr(request, 'purpose', 'translate')))
        r = self._post(self.endpoint, request, api_key, payload)
        try:
            data = r.json()
        except Exception:
            data = {}
        self.last_usage = self._usage(data)
        if r.status_code != 200:
            self._raise_status(r, self._error_text(r, data))
        try:
            choice = (data.get('choices') or [])[0]
            text = str(((choice.get('message') or {}).get('content')) or '')
            finish = str(choice.get('finish_reason') or '')
        except Exception as e:
            raise ProviderError(ErrorKind.FORMAT, '로컬 LLM 응답 형식을 해석하지 못했습니다.') from e
        return self._finish(text, finish, data, partial_ok=bool(getattr(request, 'partial_ok', False)))
