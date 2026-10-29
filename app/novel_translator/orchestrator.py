from __future__ import annotations
import random
import re, time, threading
from dataclasses import dataclass, replace
from .providers.base import *
from .prompting import build_system_prompt, build_user_prompt, REPAIR_SYSTEM, build_repair_prompt, SKIP_MARKER, relevant_terms
from .validation import TAG_RE, validate_translation, normalize_translation_output, id_forms, skipped_paragraph_ids, parse_tagged_translation, leftover_script, _is_refusal, meta_leak, dialogue_shift_run, SHIFT_RUN_MIN, LOCAL_SAFE_PARAGRAPHS
from .protected_literals import protect_literals, restore_literals, bracket_prose
from .lexical_guidance import relevant_guidance


@dataclass
class KeyState:
    key_id: str
    secret: str
    project_group: str = 'default'
    enabled: bool = True
    cooldown_until: float = 0.0
    disabled_reason: str = ''
    failures: int = 0
    tier: str = 'unknown'  # Metadata only; existing scheduling priority is unchanged.

def format_failure_detail(message: str) -> str:
    """Classify FORMAT reasons for logs without exposing original/translated prose."""
    msg = str(message or '')
    if msg.startswith('문단 ID 불일치'):
        source = re.search(r'source=(\d+)', msg)
        output = re.search(r'output=(\d+)', msg)
        missing = re.search(r'누락 ([P\d, ]+|없음)', msg)
        extra = re.search(r'추가 ([P\d, ]+|없음)', msg)
        parts = ['문단 ID 불일치']
        if source and output:
            parts.append(f'원문 {source.group(1)}개 · 응답 {output.group(1)}개')
        if missing: parts.append('누락 ' + missing.group(1).strip())
        if extra: parts.append('추가 ' + extra.group(1).strip())
        return ' · '.join(parts)
    if msg.startswith('문단 밀림'): return '문단 밀림 의심 · 번역이 이웃 문단 자리로 한 칸씩 어긋남'
    if '출력 잘림' in msg: return '로컬 출력 잘림 · 출력 한도 또는 반복 의심'
    if '반복' in msg and '로컬 LLM' in msg: return '로컬 응답 반복'
    if '너무 적습니다' in msg: return '한국어 비율 부족'
    if '비정상적으로 짧습니다' in msg:
        pid = re.search(r'문단 (P\d+)', msg)
        ratio = re.search(r'\((\d+(?:\.\d+)?)%\)', msg)
        return '번역 길이 부족' + (f' · {pid.group(1)}' if pid else '') + (f' · 원문 대비 {ratio.group(1)}%' if ratio else '')
    if '생각·설명' in msg: return '모델의 설명·생각 혼입'
    if '문단 제외 표식' in msg: return '문단 제외 표식 혼입'
    if '빈 번역 문단' in msg: return '빈 번역 문단'
    if '비어 있습니다' in msg or '빈 번역 응답' in msg: return '빈 번역 응답'
    if '미번역 원문' in msg: return '원문 언어 잔존'
    if '형식을 해석하지 못했습니다' in msg: return '로컬 응답 형식 해석 실패'
    if 'literal' in msg.lower() or '보호' in msg: return '보호 문자열 복원 오류'
    return '기타 FORMAT 검증 실패'


def format_log_wait(seconds: float) -> str:
    """Human-friendly display only. The internal retry timer is unchanged."""
    if seconds >= 60:
        minutes = seconds / 60.0
        return f'{minutes:g}분' if minutes.is_integer() else f'{minutes:.1f}분'
    return f'{seconds:.1f}초' if seconds < 10 else f'{seconds:g}초'


class TranslationOrchestrator:
    """Provider/key orchestration with project-aware retry/cooldown handling.

    Multiple keys in one provider project are *not* assumed to have independent quota.
    `project_group` lets the scheduler avoid hammering sibling keys after a project-level 429/quota event.
    `sleep_fn` and `clock_fn` are injectable for deterministic stress tests.
    """
    def __init__(self, provider: TranslationProvider, keys: list[KeyState], model: str,
                 retries: int = 3, timeout: int = 90, sleep_fn=time.sleep, clock_fn=time.time,
                 target_rpm: float = 12.0, min_rpm: float = 2.0, max_rpm: float = 30.0,
                 thinking_level: str | None = None, successes_before_step_up: int = 20, event_fn=None, cancel_check=None, content_kind='novel', pause_fn=None, repair_script: bool = False,
                 usage_fn=None, provider_id: str = '', wait_fn=None, key_selected_fn=None):
        self.content_kind=content_kind
        self.sentence_style='normal'   # 문장 스타일: 'normal' (보통) or 'natural' (자연스럽게); set by the worker
        self.provider = provider
        self.keys = keys
        self.model = model
        self.retries = max(0, int(retries))
        self.timeout = int(timeout)
        self._raw_sleep = sleep_fn
        self.cancel_check = cancel_check
        # pause_fn() blocks while the user has paused the job (raises TranslationCancelled on stop).
        self.pause_fn = pause_fn
        # repair_script: one follow-up request that replaces Chinese characters/kana a model left
        # inside its Korean output (common with small local models). Never fails the chunk.
        self.repair_script = bool(repair_script)
        self.sleep_fn = self._interruptible_sleep
        self.clock_fn = clock_fn
        self._group_cooldown: dict[str, float] = {}
        self._adaptive_rpm=max(float(min_rpm),float(target_rpm)); self._min_rpm=max(0.5,float(min_rpm)); self._max_rpm=max(self._adaptive_rpm,float(max_rpm))
        self._last_request_at: float|None=None; self._success_streak=0
        self._successes_before_step_up=max(1,int(successes_before_step_up))
        self.thinking_level=(thinking_level or '').strip().lower() or None
        self.event_fn=event_fn
        # usage_fn(provider_id, model, usage_dict|None, ok: bool) after every HTTP call,
        # including calls whose answer is later rejected (refusal, short reply, ...).
        self.usage_fn=usage_fn
        # wait_fn(message) shows pacing waits in the status line (not the error log).
        self.wait_fn=wait_fn
        self.provider_id=provider_id or getattr(provider,'name','')
        self.key_selected_fn=key_selected_fn
        self._usage_key=None
        # (pid, source body) pairs the last successful call kept as source because the
        # model used the narrow scope-exclusion marker for them.
        self.last_excluded: list[tuple[int,str]] = []
        # Aggregate performance counters: never store text, prompts or key secrets.
        self.perf = {'requests': 0, 'provider_seconds': 0.0, 'pace_seconds': 0.0,
                     'cooldown_seconds': 0.0, 'backoff_seconds': 0.0, 'errors': {}}

    def _check_cancelled(self):
        if self.cancel_check and self.cancel_check():
            raise TranslationCancelled()

    def _interruptible_sleep(self, seconds):
        if not self.cancel_check:
            return self._raw_sleep(seconds)
        deadline=time.monotonic()+max(0,seconds)
        while True:
            self._check_cancelled()
            left=deadline-time.monotonic()
            if left<=0:return
            self._raw_sleep(min(0.1,left))

    def _call_provider(self, request, secret):
        # A local LLM raises its context length for a request that would not fit (and keeps
        # it, so Ollama does not reload the model each time). Say so once per raise: a larger
        # context uses more VRAM and can slow a 16GB card down (v2.5.7, log only).
        before = getattr(self.provider, 'num_ctx', None)
        try:
            return self._call_provider_once(request, secret)
        finally:
            after = getattr(self.provider, 'num_ctx', None)
            if isinstance(before, int) and isinstance(after, int) and after > before > 0:
                self._event(f'로컬 LLM 컨텍스트 {before:,} → {after:,}토큰으로 늘림 · 요청(지침·작품 메모·원문·답)이 설정한 길이에 들어가지 않아서 · '
                            'VRAM이 부족하면 느려질 수 있음(API 설정의 컨텍스트 길이를 이 값으로 맞추면 처음부터 이 크기로 씀)')

    def _call_provider_once(self, request, secret):
        if self.pause_fn:
            self.pause_fn()   # every HTTP request, including retries, waits while paused
        self._check_cancelled()
        if not self.cancel_check:
            started=time.monotonic()
            try:
                return self.provider.translate(request,secret)
            finally:
                self.perf['requests'] += 1
                self.perf['provider_seconds'] += max(0.0,time.monotonic()-started)
        # requests cannot interrupt a pending socket before it returns. Isolate
        # that wait so cancelling stops the job immediately; a late result never
        # touches Qt, SQLite, output files, retries or the next translation job.
        provider=self.provider
        # The stop check also travels with the request: a local LLM stream then closes the
        # moment the user stops, so Ollama stops generating instead of finishing a chunk
        # nobody waits for (and the next job does not queue behind it on the GPU).
        if getattr(request,'cancel',None) is None:
            request=replace(request,cancel=self.cancel_check)
        done=threading.Event(); result=[]
        def invoke():
            try:result.append((True,provider.translate(request,secret)))
            except Exception as exc:result.append((False,exc))
            finally:done.set()
        started=time.monotonic()
        threading.Thread(target=invoke,daemon=True,name='translation-http').start()
        while not done.wait(0.1):self._check_cancelled()
        self._check_cancelled()
        self.perf['requests'] += 1
        self.perf['provider_seconds'] += max(0.0,time.monotonic()-started)
        ok,value=result[0]
        if not ok:raise value
        return value

    def _record_usage(self, ok: bool) -> None:
        if not self.usage_fn:
            return
        try:
            self.usage_fn(self.provider_id, self.model, getattr(self.provider,'last_usage',None), ok)
        except Exception:
            pass

    def _event(self, message: str) -> None:
        if self.event_fn:
            try:self.event_fn(str(message))
            except Exception:pass

    def _enabled(self) -> list[KeyState]:
        return [k for k in self.keys if k.enabled and k.secret]

    def _effective_ready_at(self, k: KeyState) -> float:
        return max(k.cooldown_until, self._group_cooldown.get(k.project_group or 'default', 0.0))

    def _pick_key(self) -> KeyState:
        enabled = self._enabled()
        if not enabled:
            raise ProviderError(ErrorKind.AUTH, '사용 가능한 API 키가 없습니다.')
        now = self.clock_fn()
        ready = [k for k in enabled if self._effective_ready_at(k) <= now]
        if not ready:
            wait = min(self._effective_ready_at(k) for k in enabled) - now
            if wait > 0:
                self.perf['cooldown_seconds'] += wait
                self.sleep_fn(wait)
            now = self.clock_fn()
            ready = [k for k in enabled if self._effective_ready_at(k) <= now]
        if not ready:
            raise ProviderError(ErrorKind.RATE_LIMIT, '모든 API 키/프로젝트가 일시적인 대기 상태입니다.')
        # Preserve the user-defined priority order. A healthy primary key stays
        # active; spare keys are used only when earlier keys are disabled or their
        # whole project group is cooling down. This avoids consuming a pool of
        # reserve keys simply by round-robin scheduling.
        return ready[0]

    def _pace(self) -> None:
        now=self.clock_fn()
        if self._last_request_at is not None and self._adaptive_rpm > 0:
            min_interval=60.0/self._adaptive_rpm
            wait=min_interval-(now-self._last_request_at)
            if wait>0:
                self.perf['pace_seconds'] += wait
                if wait>=2 and self.wait_fn:
                    try:self.wait_fn(f'속도 조절 대기 {wait:.0f}초 · 현재 {self._adaptive_rpm:.1f}회/분')
                    except Exception:pass
                self.sleep_fn(wait)
        self._last_request_at=self.clock_fn()

    def _note_success(self) -> None:
        self._success_streak += 1
        if self._success_streak >= self._successes_before_step_up and self._adaptive_rpm < self._max_rpm:
            self._adaptive_rpm=min(self._max_rpm,self._adaptive_rpm+1.0); self._success_streak=0

    def _note_rate_limit(self) -> None:
        self._success_streak=0
        self._adaptive_rpm=max(self._min_rpm,self._adaptive_rpm*0.70)

    def _cool_project(self, key: KeyState, delay: float) -> None:
        group = key.project_group or 'default'
        until = self.clock_fn() + max(0.0, delay)
        self._group_cooldown[group] = max(self._group_cooldown.get(group, 0.0), until)
        # Also retain per-key state for diagnostics.
        key.cooldown_until = max(key.cooldown_until, until)

    def _repair_leftover(self, source_text: str, translated: str, terms: dict[str, str], key, skip: set) -> str:
        """Ask once to replace leftover Chinese characters/kana in Korean paragraphs.
        Returns the original text object unchanged when nothing was (or could be) improved."""
        allowed = ''.join(terms.values())
        src = parse_tagged_translation(source_text); out = parse_tagged_translation(translated)
        targets = [(pid, src.get(pid, ''), body) for pid, body in out.items()
                   if pid not in skip and leftover_script(body, allowed) and body.strip() != src.get(pid, '').strip()]
        if not targets:
            return translated
        try:
            self._pace()
            prompt_terms = relevant_terms(terms, '\n'.join(t[1] for t in targets))
            resp = self._call_provider(TranslationRequest(self.model, REPAIR_SYSTEM, build_repair_prompt(targets, prompt_terms), 0.2, self.timeout, self.thinking_level), key.secret)
        except ProviderError:
            self._record_usage(False)
            return translated
        fixed = parse_tagged_translation(normalize_translation_output(resp.text, translated))
        repaired = 0
        for pid, _src, old in targets:
            new = (fixed.get(pid) or '').strip()
            indent = re.match(r'[ \t\u3000]*', old).group(0)
            if new and indent and not new.startswith(indent):
                new = indent + new   # keep the paragraph's leading 　
            if not new or _is_refusal(new, _src) or len(new) < 0.6 * len(old.strip()) or meta_leak(_src, new):
                continue
            if len(leftover_script(new, allowed)) < len(leftover_script(old, allowed)):
                out[pid] = new; repaired += 1
        self._record_usage(repaired > 0)
        if not repaired:
            return translated
        self._event(f'남은 한자·가나 정리 · 문단 {repaired}개를 한글로 다시 고침')
        return '\n\n'.join(f'<P{k:06d}>\n{v}' for k, v in out.items())

    @staticmethod
    def _attach_salvage(error, protected, normalized: str, expected_tags, normalized_tags, missing_tags) -> None:
        """A local answer that lacks exactly ONE paragraph and is otherwise correct keeps
        its other paragraphs: they are validated as if that paragraph had not been sent,
        their literals are restored, and the result rides on the error as `salvage`
        (text, missing pid). The caller then asks again only for the missing paragraph
        and its neighbour. Anything less clean leaves the error as it is."""
        try:
            if len(missing_tags) != 1 or len(expected_tags) < 3:
                return
            rest = [t for t in expected_tags if t not in missing_tags]
            if normalized_tags != rest or SKIP_MARKER in (normalized or ''):
                return
            missing = int(missing_tags[0][2:8])
            src = {k: v for k, v in parse_tagged_translation(protected.text).items() if k != missing}
            src_text = '\n\n'.join(f'<P{k:06d}>\n{v}' for k, v in src.items())
            validate_translation(src_text, normalized)
            sub_map = {tok: val for tok, val in protected.mapping.items() if tok in src_text}
            restored = restore_literals(normalized, sub_map)
            error.salvage = (restored, missing)
        except (ProviderError, ValueError):
            return

    def translate_chunk(self, profile, chunk_text: str, terms: dict[str, str],
                        previous_context: str = '', recovery_mode: bool = False,
                        book_memory: str = '', language_options: dict | None = None,
                        recovery_reason: str = 'format') -> tuple[str, str, int]:
        self.last_excluded=[]
        system_prompt = build_system_prompt(profile,self.content_kind,getattr(self,'sentence_style','normal'))
        protected=protect_literals(chunk_text)
        guidance=relevant_guidance(profile,chunk_text) if self.content_kind=='novel' else {}
        reason = recovery_reason if recovery_mode else 'format'
        # Only the terms this source can use go into the prompt (whole glossary when small);
        # leftover checks and repairs below still allow every confirmed rendering.
        prompt_terms = relevant_terms(terms, chunk_text)
        user_prompt = build_user_prompt(protected.text, prompt_terms, previous_context, recovery_mode=recovery_mode, book_memory=book_memory, language_options=language_options, lexical_guidance=guidance, recovery_reason=reason)
        if bracket_prose(chunk_text):
            # Sound effects / system words in brackets are prose (v2.5.7); the user prompt only,
            # so the job identity and translation memory key stay the same.
            user_prompt += ('\nBracketed English sound effects and system-window words (<WHACK>, <Slurp, slurp>, '
                            '[System]) are prose: translate the words inside into Korean and keep the brackets '
                            '(<퍽>, <후릅, 후릅>, [시스템]).')
        attempt = 0
        while True:
            self._check_cancelled()
            key = self._pick_key()
            self._usage_key=key
            if self.key_selected_fn is not None:
                try:self.key_selected_fn(key)
                except Exception:pass
            usage_done = False
            try:
                self._pace()
                try:
                    resp = self._call_provider(
                        TranslationRequest(self.model, system_prompt, user_prompt, 0.2, self.timeout, self.thinking_level),
                        key.secret,
                    )
                except ProviderError:
                    self._record_usage(False)
                    raise
                normalized=normalize_translation_output(resp.text, protected.text)
                forms=id_forms(resp.text, protected.text)
                if forms['own_line'] != sum(forms.values()):
                    # Diagnostics only (counts, never text): how the model wrote the IDs.
                    self._event('ID 형태 · 제 줄 {own_line} · 같은 줄 본문 {text_after} · 줄 중간 {mid_line} · 변형 표기 {look_alike} · 없음 {absent}'.format(**forms))
                try:
                    validate_translation(protected.text, normalized)
                except ProviderError as verr:
                    if verr.kind == ErrorKind.FORMAT and verr.message.startswith('문단 ID 불일치'):
                        raw_tags = re.findall(r'<P[0-9]{6}>', resp.text or '')
                        normalized_tags = re.findall(r'<P[0-9]{6}>', normalized or '')
                        self._event(f'원본 응답 태그 {len(raw_tags)}개 · 정규화 태그 {len(normalized_tags)}개 · 원본 유일 ID {len(set(raw_tags))}개 · 정규화 유일 ID {len(set(normalized_tags))}개')
                        expected_tags = re.findall(r'<P[0-9]{6}>', protected.text)
                        missing_tags = [tag for tag in expected_tags if tag not in normalized_tags]
                        self._event('문단 위치 진단 · 누락 ID ' + (', '.join(missing_tags[:5]) if missing_tags else '없음') + (' 외 추가 누락' if len(missing_tags) > 5 else '') + ' · 마지막 문단 누락 ' + ('예' if expected_tags and expected_tags[-1] in missing_tags else '아니오'))
                        if self.provider_id == 'local':
                            self._attach_salvage(verr, protected, normalized, expected_tags, normalized_tags, missing_tags)
                    # A sentence left in Chinese/Japanese inside an otherwise Korean paragraph
                    # (typical of small local models) gets the leftover repair before rejecting.
                    residue = (self.repair_script and profile.source_language in ('zh','ja')
                               and '번역되지 않고 남아' in verr.message)
                    if not residue:
                        self._record_usage(False)
                        raise
                    fixed = self._repair_leftover(protected.text, normalized, terms, key, set(skipped_paragraph_ids(normalized)))
                    try:
                        if fixed is normalized:
                            raise verr
                        validate_translation(protected.text, fixed)
                    except ProviderError:
                        self._record_usage(False)   # this answer was not usable after all
                        raise verr
                    self._record_usage(True)
                    normalized = fixed; usage_done = True
                skipped=skipped_paragraph_ids(normalized)
                if skipped:
                    # Keep the excluded paragraph's own (protected) source so literal
                    # tokens inside it still restore exactly.
                    src_parsed=parse_tagged_translation(protected.text)
                    out_parsed=parse_tagged_translation(normalized)
                    for spid in skipped:
                        out_parsed[spid]=src_parsed.get(spid,'')
                    normalized='\n\n'.join(f'<P{k:06d}>\n{v}' for k,v in out_parsed.items())
                try:
                    restored=restore_literals(normalized,protected.mapping)
                except ValueError as exc:
                    self._record_usage(False)
                    raise ProviderError(ErrorKind.FORMAT,str(exc)) from exc
                try:
                    validate_translation(chunk_text,restored,ignore_pids=skipped,check_residue=False)
                except ProviderError:
                    self._record_usage(False)
                    raise
                if (self.provider_id == 'local' and not recovery_mode
                        and len(TAG_RE.findall(chunk_text)) > LOCAL_SAFE_PARAGRAPHS):
                    # Local LLM, first request with more paragraphs than the long-standing 12:
                    # translations shifted one line from some paragraph on (every ID present,
                    # so the checks above pass). The worker sends it again in halves. Requests
                    # of 12 or fewer paragraphs (long-paragraph books) and recovery requests are
                    # never checked: this can cost a request but never leaves a paragraph
                    # untranslated.
                    shift = dialogue_shift_run(chunk_text, restored, skipped)
                    if shift >= SHIFT_RUN_MIN:
                        if not usage_done:
                            self._record_usage(False)
                        raise ProviderError(ErrorKind.FORMAT, f'문단 밀림 의심 · 대사 시작 따옴표가 {shift}문단 연속 이웃 문단과 맞음 · 나눠서 다시 요청')
                if not usage_done:
                    self._record_usage(True)
                if self.repair_script and profile.source_language in ('zh','ja'):
                    fixed = self._repair_leftover(protected.text, normalized, terms, key, set(skipped))
                    if fixed is not normalized:
                        try:
                            candidate = restore_literals(fixed, protected.mapping)
                            validate_translation(chunk_text, candidate, ignore_pids=skipped, check_residue=False)
                            restored = candidate
                        except (ValueError, ProviderError):
                            pass   # keep the validated answer without the repair
                if skipped:
                    restored_parsed=parse_tagged_translation(restored)
                    self.last_excluded=[(spid,restored_parsed.get(spid,'')) for spid in skipped]
                    self._event('문단 '+', '.join(f'P{x:06d}' for x in skipped[:5])+' · 범위 제외(미성년자 성적 내용) 표식 · 원문 유지')
                key.failures = 0; self._note_success()
                return restored, key.key_id, attempt
            except ProviderError as e:
                if e.kind == ErrorKind.FORMAT:
                    self._event('FORMAT 상세 · ' + format_failure_detail(e.message))
                kind = str(getattr(e.kind, 'value', e.kind))
                self.perf['errors'][kind] = self.perf['errors'].get(kind, 0) + 1
                key.failures += 1
                if e.kind in {ErrorKind.AUTH, ErrorKind.KEY_BLOCKED}:
                    key.enabled = False
                    key.disabled_reason = e.kind.value
                    self._event(f'API 키 {key.key_id} · {e.kind.value} · 비활성 처리')
                    if self._enabled():
                        continue
                    raise

                # Refusals are content/policy outcomes, never a key-health failure.
                if e.kind in {ErrorKind.SAFETY, ErrorKind.POLICY, ErrorKind.REFUSAL}:
                    raise

                retryable = {ErrorKind.TRANSIENT, ErrorKind.RATE_LIMIT, ErrorKind.QUOTA, ErrorKind.FORMAT}
                if e.kind not in retryable or attempt >= self.retries or is_loop_error(e):
                    raise
                # A local model that cannot reproduce the paragraph IDs of a multi-paragraph
                # request fails the same way when asked again at the same size. Hand it back
                # at once: the worker keeps the good paragraphs (one missing) or splits it.
                if (e.kind == ErrorKind.FORMAT and self.provider_id == 'local'
                        and e.message.startswith(('문단 ID 불일치', '문단 밀림')) and len(TAG_RE.findall(chunk_text)) > 1):
                    raise

                if e.retry_after is not None and e.retry_after > 0:
                    delay = min(600.0, float(e.retry_after))
                elif e.kind == ErrorKind.QUOTA:
                    # Unknown quota reset: do not rapidly rotate sibling keys in the same project.
                    delay = min(300.0, 30.0 * (2 ** attempt))
                else:
                    delay = min(40.0, 2.0 * (2 ** attempt)) + random.uniform(0, 0.7)

                if e.kind in {ErrorKind.RATE_LIMIT, ErrorKind.QUOTA}:
                    self._cool_project(key, delay)
                    # A hard quota belongs to the cooled project, not necessarily
                    # the other independently configured projects. Do not globally
                    # slow healthy projects simply because one project's quota ran out.
                    now = self.clock_fn()
                    has_independent_ready=any(self._effective_ready_at(k) <= now for k in self._enabled())
                    if e.kind == ErrorKind.RATE_LIMIT or not has_independent_ready:
                        self._note_rate_limit()
                    self._event(f'프로젝트 {key.project_group or "default"} · {e.kind.value} · {format_log_wait(delay)}간 사용 보류'+(' · 다른 프로젝트 이용 가능' if has_independent_ready else ' · 즉시 사용 가능한 다른 프로젝트 없음')+f' · RPM {self._adaptive_rpm:g}')
                    # Do not rotate sibling keys that share the same project quota.
                    # For a hard quota with no independent project ready, bubble up
                    # immediately so the worker may use its configured fallback provider.
                    if e.kind == ErrorKind.QUOTA and not has_independent_ready:
                        raise
                    if not has_independent_ready:
                        self.perf['backoff_seconds'] += delay
                        self.sleep_fn(delay)
                else:
                    # A log label only: `reason` keeps the recovery note ('content'/'format').
                    label = {'TRANSIENT':'서버 일시적 오류','FORMAT':'응답 형식 오류'}.get(e.kind.value,e.kind.value)
                    self._event(f'{label} · {format_log_wait(delay)} 후 재시도 ({attempt+1}/{self.retries})')
                    self.perf['backoff_seconds'] += delay
                    self.sleep_fn(delay)
                if e.kind == ErrorKind.FORMAT:
                    user_prompt=build_user_prompt(protected.text,prompt_terms,previous_context,recovery_mode=True,book_memory=book_memory,language_options=language_options,lexical_guidance=guidance,recovery_reason=reason)
                attempt += 1
