from __future__ import annotations
import hashlib, json, re, time
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from ..storage import Database
from ..config import DATA_DIR, CONFIG_DIR, load_json
from ..providers.gemini import GeminiProvider
from ..providers.openai import OpenAIProvider
from ..providers.local_llm import LocalLLMProvider, local_provider_args
from ..providers.base import TranslationCancelled, ProviderError, ErrorKind, is_loop_error
from ..orchestrator import TranslationOrchestrator, KeyState
from ..api_keys import normalize_key_records
from ..chunking import split_chunk_for_retry, Chunk, Paragraph, estimate_tokens as _estimate_tokens, escape_protocol_literals
from ..translation_settings import canonical_kind
from ..prompting import PROMPT_VERSION, build_system_prompt, build_user_prompt
from ..user_guidelines import load_user_guidelines
from ..validation import parse_tagged_translation, has_meta_leak, meta_leak, untranslated_residue, leftover_script, LOCAL_SAFE_PARAGRAPHS
from ..rendering import render_snapshot, html_line_breaks
from ..protected_literals import stale_bracket_prose

_TAG = re.compile(r'(?m)^<P\d{6}>\s*\n?')

def _extra_note(count: int) -> str:
    """Requests whose answer was not used as the final translation (a retry after a
    format problem, a check that found nothing to fix). Normal, so never called a failure."""
    return f'(재시도·확인 {count}회 포함)' if count else ''


class TranslationWorker(QThread):
    progress = Signal(int, int, str)
    failed = Signal(str)
    paused = Signal(str, bool)  # reason, user_requested
    finished_ok = Signal(str)
    result_ready = Signal(str, str, int)  # status, output path, failed chunk count
    key_disabled = Signal(str, str, str)  # provider_id, key_id, reason
    issue = Signal(str)                  # live user-facing error/recovery event
    api_state = Signal(str)              # compact provider/request state
    chunk_translated = Signal(int, int, str)   # chunk position, total, readable Korean (live view)

    def __init__(self, source_path: Path, source_bytes: bytes, profile, chunks, model: str,
                 api_keys, terms: dict[str, str], mode_id: str = 'balanced', output_dir: Path | None = None,
                 book_memory: str = '', language_options: dict | None = None, initial_context: str = '', restart_existing: bool = False,
                 openai_settings: dict | None = None, all_paragraphs=None, parent=None, local_settings: dict | None = None,
                 engine_order: list[str] | None = None, speech_table=None, common_terms=None, sentence_style: str = 'normal'):
        super().__init__(parent)
        if common_terms:
            # Shared-glossary hints ride on a copy of the profile; the job identity reads only
            # profile.lexical_guidance, so they never restart a book.
            import copy as _copy
            profile=_copy.copy(profile); object.__setattr__(profile,'lexical_extra',dict(common_terms))
        from ..speech_levels import clean_rows
        self.speech_table = clean_rows(speech_table)   # per book; like the guidelines, NOT part of the job identity
        # 문장 스타일: like the guidelines, NOT part of the job identity (switching it does not restart a book),
        # but part of the translation-memory key so the two styles are never mixed by reuse.
        self.sentence_style = 'natural' if sentence_style == 'natural' else 'normal'
        self._pending_style: str | None = None   # set from the window while running (set_sentence_style)
        self.style_changes: list[tuple[int, str]] = []   # (chunk number, style) changed during the run
        self.source_path = source_path; self.source_bytes = source_bytes; self.profile = profile; self.chunks = chunks
        self.model = model; self.api_keys = api_keys; self.terms = terms; self.mode_id = mode_id; self.output_dir = Path(output_dir) if output_dir else None
        self.all_paragraphs=list(all_paragraphs) if all_paragraphs is not None else [p for c in chunks for p in c.paragraphs]
        self.book_memory=(book_memory or '').strip(); self.language_options=dict(language_options or {}); self.user_guidelines=load_user_guidelines(getattr(profile,'id',''), DATA_DIR); self.initial_context=(initial_context or '').strip(); self.restart_existing=bool(restart_existing); self.final_context=''; self._stop = False; self._paused = False; self._paused_seconds = 0.0; self._in_second_opinion = False; self._waiting_in_pause = False
        self.openai_settings=dict(openai_settings or {})
        self.local_settings=dict(local_settings or {})
        # engine -> chunks since it was marked unavailable for this file
        self._down: dict[str,int] = {}
        # Backups that failed for infrastructure reasons while taking ONE blocked part: not
        # asked for parts for a few chunks, but still available for whole-chunk failover.
        self._benched: dict[str,int] = {}
        self.pause_kind=''
        self._local_warned=False
        self._chunk_label=''; self._pending_traces=[]; self._chunk_prev=''
        self._timing={'chunks':0,'seconds':0.0,'recovered_chunks':0,'recovered_seconds':0.0,'started':time.monotonic()}
        self.eta_left=None; self.eta_samples=[]; self.eta_all=[]; self.eta_trouble_only=False   # remaining-time estimate
        # Observability only. These counters never influence translation decisions.
        self.recovery_metrics={'reframe_attempts':0,'reframe_successes':0,'split_attempts':0,'split_successes':0}
        self._para_text={p.pid:p.text for p in self.all_paragraphs}
        self._engine_order_setting=list(engine_order or [])
        # Accumulated API usage of this file, failed/rejected calls included.
        self.usage_totals: dict[str,dict] = {}
        self.gemini_project_usage: dict[tuple[str,str],dict] = {}
        self.provider_chunks: dict[str,int] = {}
        self._perf_engines = {}
        self._chunk_requests=0; self._chunk_loops=0
        self._prices: dict[str,tuple[float,float]] = {}
        try:
            for row in load_json(CONFIG_DIR/'models.json').get('models',[]):
                if 'price_input_per_m' in row and 'price_output_per_m' in row:
                    self._prices[str(row.get('id'))]=(float(row['price_input_per_m']),float(row['price_output_per_m']))
        except Exception:
            pass
        try:
            app_cfg=load_json(CONFIG_DIR/'app.json')
            self.content_kind=canonical_kind(app_cfg.get('content_kind'))
            self._mode_cfg=dict((app_cfg.get('modes') or {}).get(self.mode_id) or {})
            self._refusal_cfg=dict(app_cfg.get('refusal_recovery') or {})
        except Exception:
            self.content_kind='novel'
            self._mode_cfg={}
            self._refusal_cfg={}
        self.allow_reframe=bool(self._refusal_cfg.get('reframe_once',True))
        self.allow_split_recovery=bool(self._refusal_cfg.get('split_chunk',True))
        self.allow_provider_fallback=bool(self._refusal_cfg.get('fallback_provider',True))
        # Gemini PROHIBITED_CONTENT/BLOCKLIST is a non-adjustable input/output filter.
        # It is never reframed into a different request. After the block has been
        # isolated to one paragraph, that paragraph may go to the user-configured
        # backup provider, which applies its own policy independently.
        self.allow_policy_fallback=bool(self._refusal_cfg.get('fallback_on_policy',True))
        self._chunk_exclusions=[]   # (pids, kind, message, source bodies) kept as source inside a chunk
        self._chunk_fallback_used=False
        self._fallback_chain=[]     # [(provider_id, orchestrator)] tried in order for an isolated part
        # Hard cap on provider requests spent on one chunk (all retries/splits/backups).
        self.max_requests_per_chunk=max(4,int(self._refusal_cfg.get('max_requests_per_chunk',16) or 16))
        # While a provider-level Gemini failure keeps the file on OpenAI, retry Gemini
        # every N chunks so a temporary outage does not move a whole book to OpenAI.
        self.gemini_probe_every=max(0,int(self._refusal_cfg.get('gemini_probe_every_chunks',10) or 0))
        # Local LLM only (cloud engines are unaffected): paragraph IDs per request
        # (0 = no limit), structured output (JSON with every paragraph ID required) and
        # the model's own recommended sampling.
        try:
            local_cfg=dict(app_cfg.get('local_llm') or {})
        except Exception:
            local_cfg={}
        try:
            self.local_max_paragraphs=max(0,int(local_cfg.get('max_paragraphs_per_request',self.LOCAL_DEFAULT_PARAGRAPHS)))
        except (TypeError, ValueError):
            self.local_max_paragraphs=self.LOCAL_DEFAULT_PARAGRAPHS
        # Requests larger than the long-standing 12 paragraphs: counted per file, and
        # if the model keeps misplacing paragraph IDs in them, the rest of the file goes back
        # to 12 (see _note_wide_request).
        self._wide_requests=0; self._wide_misfits=0; self._wide_stepped_down=False
        self.local_structured=bool(local_cfg.get('structured_output', True))
        self.local_sampling=str(local_cfg.get('sampling') or 'recommended')
        self.thinking_level=str(self._mode_cfg.get('thinking_level') or '').strip().lower() or None
        self.context_chars=max(500,int(self._mode_cfg.get('context_chars', {'quality':5000,'balanced':3000,'speed':1800}.get(self.mode_id,3000))))
        factor=float(self._mode_cfg.get('chunk_factor', {'quality':0.78,'balanced':1.0,'speed':1.35}.get(self.mode_id,1.0)))
        self.max_chunk_tokens=max(500,int(self.profile.chunk.get('max_tokens',3500)*max(0.25,min(3.0,factor))))

    def stop(self):
        self._stop = True
        self._paused = False

    # Pause: the request already in flight finishes and is saved; no new request is
    # sent until resume(). Stop while paused ends the job normally (completed parts saved).
    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def set_sentence_style(self, style: str) -> None:
        """문장 스타일 changed in the window while this job runs: used from the next chunk
        (the one in flight finishes with the style it started with)."""
        self._pending_style = 'natural' if style == 'natural' else 'normal'

    def _wait_if_paused(self, raise_on_stop: bool = True):
        if not self._paused or self._stop:
            return
        started = time.monotonic(); self._waiting_in_pause = True
        self.api_state.emit('⏸ 일시정지됨 · 진행 중이던 요청은 마무리해 저장했습니다 · [다시 진행]을 누르면 이어서 번역합니다')
        try:
            while self._paused and not self._stop:
                time.sleep(0.1)
        finally:
            self._waiting_in_pause = False
        self._paused_seconds += time.monotonic() - started
        if self._stop:
            if raise_on_stop:
                raise TranslationCancelled()
            return
        self.api_state.emit('▶ 다시 진행 · 번역을 이어 갑니다')

    def _model_for(self, provider_id: str) -> str:
        pid=provider_id.lower()
        if pid=='gemini': return self.model
        if pid=='local': return str(self.local_settings.get('model') or '')
        return str(self.openai_settings.get('model') or '')

    def _provider_event(self, provider_id: str, message: str):
        # Diagnostics belong to development, not the user-facing job log.
        # Do not alter provider error handling, retries, recovery, or accounting.
        if message.startswith(('원본 응답 태그 ', '문단 위치 진단 · ', 'FORMAT 상세 · ', 'ID 형태 · ')):
            self._write_diagnostic(provider_id, message)
            return
        model=self._model_for(provider_id)
        self.issue.emit(f'{provider_id} · {model} · {message}')
        self.api_state.emit(f'{provider_id} · {model} · {message}')

    DIAGNOSTIC_FILE = 'format_diagnostics.log'
    DIAGNOSTIC_MAX_BYTES = 1_000_000

    def _write_diagnostic(self, provider_id: str, message: str) -> None:
        """Keep FORMAT diagnostics (ID counts and positions only, never prose or keys) in
        a separate file under the data folder's logs, out of the user-facing job log.
        Writing never affects translation; one older file is kept when it grows large."""
        try:
            log_dir=Path(DATA_DIR)/'logs'; log_dir.mkdir(parents=True, exist_ok=True)
            path=log_dir/self.DIAGNOSTIC_FILE
            if path.exists() and path.stat().st_size > self.DIAGNOSTIC_MAX_BYTES:
                path.replace(path.with_name(path.name+'.1'))
            stamp=time.strftime('%Y-%m-%d %H:%M:%S')
            line=f'{stamp}\t{self.source_path.name}\t{getattr(self,"_chunk_label","")}\t{provider_id}\t{self._model_for(provider_id.lower())}\t{message}\n'
            with path.open('a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            pass

    def _emit_new_disabled_keys(self, provider_id: str, keys, already: set[str]):
        for k in keys:
            token=f'{provider_id}:{k.key_id}'
            if not k.enabled and token not in already and k.disabled_reason:
                already.add(token)
                self.key_disabled.emit(provider_id, k.key_id, k.disabled_reason)

    def _request_language_options(self) -> dict:
        """Options sent with every request: the job's language options plus the reader's own
        guidelines. The guidelines are NOT part of the job identity, so editing them never
        restarts a half-translated book; they apply from the next request on."""
        from ..language_options import USER_GUIDELINES_KEY
        from ..user_guidelines import GUIDELINES_FILE
        try:   # edits made while the job runs apply from the next request
            m=(DATA_DIR/GUIDELINES_FILE).stat().st_mtime
            if m!=getattr(self,'_guide_mtime',None):
                self._guide_mtime=m; self.user_guidelines=load_user_guidelines(getattr(self.profile,'id',''), DATA_DIR)
        except OSError:
            pass
        from ..speech_levels import SPEECH_KEY
        extra={}
        if self.user_guidelines: extra[USER_GUIDELINES_KEY]=self.user_guidelines
        if getattr(self,'speech_table',None): extra[SPEECH_KEY]=self.speech_table
        if not extra:
            return self.language_options
        return dict(self.language_options, **extra)

    def _settings_key(self) -> str:
        """What makes two translations of the same chunk interchangeable."""
        payload = {
            'profile': self.profile.id, 'content_kind': self.content_kind,
            'system': build_system_prompt(self.profile,self.content_kind,self.sentence_style),
            'lexical_guidance': self.profile.lexical_guidance,
            'terms': sorted(self.terms.items()), 'book_memory': self.book_memory,
            'language_options': self.language_options, 'prompt_version': PROMPT_VERSION,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def _fingerprint(self) -> str:
        # Model, thinking level and backup-provider settings are deliberately NOT part
        # of the job identity: switching the model and pressing "계속" retries only the
        # unfinished/partial chunks instead of re-translating the whole book.
        raw = json.dumps(self._fingerprint_payload(), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def identity_parts(self) -> dict[str, str]:
        """A short hash of each part of the job identity, stored with the job so a later
        run can tell the user which setting changed."""
        out = {}
        for k, v in self._fingerprint_payload().items():
            raw = json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
            out[k] = hashlib.sha256(raw).hexdigest()[:16]
        return out

    def _fingerprint_payload(self) -> dict:
        prompt_contract=build_user_prompt('<P000001>\n__SOURCE__',{'__TERM__':'__TARGET__'},'__CONTEXT__',False,'__BOOK_MEMORY__',self.language_options)
        payload = {
            'profile': self.profile.id,
            'source_language': self.profile.source_language,
            'content_kind':self.content_kind,
            'system': build_system_prompt(self.profile,self.content_kind),
            'lexical_guidance': self.profile.lexical_guidance,
            'chunk': self.profile.chunk,
            'terms': sorted(self.terms.items()),
            'prompt_version': PROMPT_VERSION,
            'user_prompt_contract_sha256': hashlib.sha256(prompt_contract.encode('utf-8')).hexdigest(),
            'mode_id': self.mode_id,
            'book_memory': self.book_memory,
            'language_options': self.language_options,
            'context_chars': self.context_chars,
            'max_chunk_tokens': self.max_chunk_tokens,
            'initial_context_sha256': hashlib.sha256(self.initial_context.encode('utf-8')).hexdigest() if self.initial_context else '',
            'chunks': [hashlib.sha256(c.source_text.encode('utf-8')).hexdigest() for c in self.chunks],
        }
        return payload

    _CONTENT_KINDS = frozenset({ErrorKind.SAFETY, ErrorKind.REFUSAL, ErrorKind.POLICY})
    # Failures that may leave one isolated part as source while its siblings keep
    # their successful translations. Infrastructure errors still fail the chunk
    # so the whole chunk is retried later instead of half-written.
    _KEEPABLE_PART_KINDS = frozenset({ErrorKind.SAFETY, ErrorKind.REFUSAL, ErrorKind.POLICY, ErrorKind.FORMAT})

    def _policy_floor_chars(self) -> int:
        return 800 if self.profile.source_language == 'en' else 300

    def _on_usage(self, provider_id: str, model: str, usage, ok: bool, key=None):
        if provider_id!='local':
            self._chunk_requests += 1   # the per-chunk cap protects paid APIs; the local LLM is free
        row=self.usage_totals.setdefault(provider_id,{'model':model,'requests':0,'failed':0,'input':0,'output':0,'failed_input':0,'failed_output':0})
        row['model']=model; row['requests']+=1
        u=usage or {}
        row['input']+=int(u.get('input') or 0); row['output']+=int(u.get('output') or 0)
        if not ok:
            row['failed']+=1; row['failed_input']+=int(u.get('input') or 0); row['failed_output']+=int(u.get('output') or 0)
        if provider_id=='gemini' and key is not None:
            project=(key.project_group or 'default', key.tier if key.tier in ('free','paid') else 'unknown')
            pro=self.gemini_project_usage.setdefault(project,{'model':model,'requests':0,'failed':0,'input':0,'output':0,'failed_input':0,'failed_output':0})
            pro['model']=model; pro['requests']+=1
            pro['input']+=int(u.get('input') or 0); pro['output']+=int(u.get('output') or 0)
            if not ok:
                pro['failed']+=1; pro['failed_input']+=int(u.get('input') or 0); pro['failed_output']+=int(u.get('output') or 0)

    @staticmethod
    def _key_tier_text(key):
        tier=getattr(key,'tier','unknown')
        return '무료' if tier=='free' else ('유료' if tier=='paid' else '등급 미지정')

    def _gemini_key_description(self, key_id: str) -> str:
        key=next((k for k in getattr(self,'_gemini_live_keys',[]) if k.key_id==key_id),None)
        if key is None: return '등급 확인 불가'
        return f'{self._key_tier_text(key)} · 프로젝트 {key.project_group or "default"}'

    def _cost(self, row, failed_only=False):
        if row.get('provider')=='local':
            return 0.0
        price=self._prices.get(str(row.get('model') or ''))
        if not price:
            return None
        i=row['failed_input'] if failed_only else row['input']; o=row['failed_output'] if failed_only else row['output']
        return (i*price[0]+o*price[1])/1_000_000

    def usage_summary(self) -> str:
        if not self.usage_totals:
            return ''
        parts=[]; total=0.0; wasted=0.0; known=True
        for pid,row in self.usage_totals.items():
            row=dict(row,provider=pid); cost=self._cost(row); fail=self._cost(row,True)
            if pid=='gemini' and self.gemini_project_usage and any(tier!='unknown' for _,tier in self.gemini_project_usage):
                known_cost=0.0; known_fail=0.0; unknown=False; descriptions=[]
                for (group,tier),project_row in self.gemini_project_usage.items():
                    project_cost=self._cost(dict(project_row,provider='gemini'))
                    project_fail=self._cost(dict(project_row,provider='gemini'),True)
                    label=('무료' if tier=='free' else ('유료' if tier=='paid' else '등급 미지정'))
                    if tier=='free':
                        descriptions.append(f'{group}({label}) {project_row["requests"]}회 · $0')
                    elif tier=='paid' and project_cost is not None:
                        known_cost+=project_cost; known_fail+=(project_fail or 0.0)
                        descriptions.append(f'{group}({label}) {project_row["requests"]}회 · 약 ${project_cost:.3f}')
                    else:
                        unknown=True
                        descriptions.append(f'{group}({label}) {project_row["requests"]}회 · 청구액 미확정')
                total+=known_cost; wasted+=known_fail
                if unknown: known=False
                parts.append('Gemini 프로젝트별 '+', '.join(descriptions))
                continue
            label={'gemini':'Gemini','openai':'OpenAI','local':'로컬 LLM'}.get(pid,pid)
            tok=f"입력 {row['input']:,} · 출력 {row['output']:,} 토큰"
            if pid=='local':
                parts.append(f"{label} 요청 {row['requests']}회{_extra_note(row['failed'])} · {tok} · 무료")
            elif cost is None:
                known=False; parts.append(f"{label} 요청 {row['requests']}회{_extra_note(row['failed'])} · {tok} · 요금표 없음")
            else:
                total+=cost; wasted+=fail or 0.0
                parts.append(f"{label} 요청 {row['requests']}회{_extra_note(row['failed'])} · {tok} · 약 ${cost:.3f}")
        head=f'추정 비용 약 ${total:.3f}' + (f' (그중 재시도·확인 요청 ${wasted:.3f})' if wasted else '') + ('' if known else ' + 등급 미지정/요금표 미확인 사용량 별도')
        return head+' · '+' / '.join(parts)

    @staticmethod
    def _roll_context(prev: str, new: str, limit: int) -> str:
        """Previous Korean context = the tail of the last several chunks, not only the
        last one, so names introduced a few chunks ago stay visible to the model."""
        joined=(prev.rstrip()+'\n\n'+new.lstrip()) if prev else new
        return joined[-limit:]

    def _note_chunk_time(self, started: float):
        paused=self._paused_seconds-getattr(self,'_chunk_paused_base',self._paused_seconds)
        took=max(0.0,time.monotonic()-started-paused); t=self._timing
        t['chunks']+=1; t['seconds']+=took
        if self._chunk_requests>1:
            t['recovered_chunks']+=1; t['recovered_seconds']+=took
        return took

    def _eta_note(self, index: int, seconds: float, ordinary: bool):
        """One chunk of this run is done: it leaves the remaining set, and an ordinary new
        chunk's time becomes a speed sample for the remaining-time estimate."""
        left=getattr(self,'eta_left',None)
        if left is None:
            return
        left.discard(index)
        self.eta_all.append(float(seconds))
        if ordinary:
            self.eta_samples.append(float(seconds))

    def timing_summary(self) -> str:
        t=self._timing
        if not t['chunks']:
            return ''
        total=time.monotonic()-t['started']; avg=t['seconds']/t['chunks']
        line=f"소요 시간 {total/60:.1f}분 · 청크당 평균 {avg:.0f}초"
        if t['recovered_chunks']:
            share=t['recovered_seconds']/max(1e-9,t['seconds'])
            line+=f" · 재시도·복구가 필요했던 청크 {t['recovered_chunks']}개가 {t['recovered_seconds']/60:.1f}분({share:.0%}) 사용"
        return line

    def run_summary(self) -> str:
        names={'gemini':'Gemini','openai':'OpenAI','local':'로컬 LLM'}
        chunks=' · '.join(f'{names.get(k,k)} {v}청크' for k,v in self.provider_chunks.items() if not k.endswith('_paragraphs') and v)
        paras=' · '.join(f'{names.get(k[:-11],k)} {v}문단' for k,v in self.provider_chunks.items() if k.endswith('_paragraphs') and v)
        lines=[]
        if chunks: lines.append('처리 공급자: '+chunks+(f' (보조로 처리한 막힌 문단: {paras})' if paras else ''))
        timing=self.timing_summary()
        if timing: lines.append(timing)
        rm=getattr(self,'recovery_metrics',None)
        if rm and (rm['reframe_attempts'] or rm['split_attempts']):
            lines.append('복구 단계 효율 · 문맥 제외 재시도 '+f'{rm["reframe_successes"]}/{rm["reframe_attempts"]}회 성공'
                         +' · 분할 복구 '+f'{rm["split_successes"]}/{rm["split_attempts"]}회 성공'
                         +' (분할 성공은 로컬 LLM 보조 문단을 포함할 수 있음)')
        for name, orch in getattr(self, '_perf_engines', {}).items():
            metrics=getattr(orch, 'perf', {})
            if not metrics or not metrics.get('requests'): continue
            reasons=', '.join(f'{k} {v}건' for k,v in sorted(metrics.get('errors', {}).items())) or '없음'
            lines.append(f'성능 {self._label(name)} · 실제 요청 {metrics["requests"]}회 · 응답 대기 {metrics["provider_seconds"]:.1f}초'
                         f' · RPM 조절 {metrics["pace_seconds"]:.1f}초 · 프로젝트 냉각 대기 {metrics["cooldown_seconds"]:.1f}초'
                         f' · 재시도 대기 {metrics["backoff_seconds"]:.1f}초 · 오류 {reasons}')
        usage=self.usage_summary()
        if usage: lines.append(usage)
        return '\n'.join(lines)

    _KIND_KO = {'POLICY':'정책 관련 응답 실패','SAFETY':'안전 차단','REFUSAL':'거부','FORMAT':'응답 이상','TRANSIENT':'연결 실패',
                'FATAL':'설정 오류','QUOTA':'한도 소진','RATE_LIMIT':'속도 제한','AUTH':'키 오류','KEY_BLOCKED':'키 차단'}

    def _label(self, name: str) -> str:
        return self.ENGINE_LABELS.get(name, name or '?')

    def _announce(self, name: str, what: str):
        """Show in the status line which engine is working right now."""
        self.api_state.emit(f'{self._label(name)} · {self._model_for(name)} · {getattr(self,"_chunk_label","")} · {what}')

    # A rejected answer is not always a refusal: say what actually happened.
    _REFUSAL_REASONS = (('청크당 요청 상한','요청 상한 도달(보내지 않음)'),('제외 표식','범위 제외 표시'),
                        ('생략/검열 표기','생략 표기 삽입'),('번역되지 않고 남아','원문 일부 미번역'))

    def _kind_ko(self, error) -> str:
        kind=getattr(getattr(error,'kind',None),'value',str(error))
        if is_loop_error(error):
            return '반복 루프'
        if kind=='REFUSAL':
            msg=str(getattr(error,'message','') or '')
            for needle,label in self._REFUSAL_REASONS:
                if needle in msg:
                    return label
        return self._KIND_KO.get(kind, kind)

    def _part_label(self, part) -> str:
        pids=sorted({p.pid for p in part.paragraphs})
        label=', '.join(f'P{x:06d}' for x in pids[:3])+(' …' if len(pids)>3 else '')
        if len(part.paragraphs)==1 and self._para_text.get(part.paragraphs[0].pid,part.paragraphs[0].text)!=part.paragraphs[0].text:
            full=self._para_text.get(part.paragraphs[0].pid,'')
            pos=full.find(part.paragraphs[0].text.strip()[:20])
            nth=len(re.findall(r'[。！？!?.]+(?=\s*\S)',full[:pos]))+1 if pos>0 else 1
            label+=f' (일부 문장 · {nth}번째 문장부터)'
        return label

    def _trace(self, part, trail, ok: bool):
        self.issue.emit(f'{self._chunk_label} · {self._part_label(part)} · '+' → '.join(trail)+(' ✓' if ok else ' ✗'))

    # A local model that loops on a passage tends to loop on its pieces too. After this
    # many loops in one chunk the local engine is not asked again for that chunk: the
    # rest goes to the next engine, or stays as source (retried by a later 번역 계속).
    MAX_LOOPS_PER_CHUNK = 3
    # Paragraph IDs per local request (config local_llm.max_paragraphs_per_request). 12 until
    # v2.5.8; 20 sends short-line novels in about a third fewer requests. Groups are evenly
    # sized, so a 27~40 paragraph chunk becomes two requests of 14~20 instead of three or four.
    LOCAL_DEFAULT_PARAGRAPHS = 20
    # Step back to LOCAL_SAFE_PARAGRAPHS for the rest of a file once requests above it have
    # had at least this many ID/shift failures and they are at least this share of them.
    WIDE_MISFIT_MIN = 3
    WIDE_MISFIT_SHARE = 0.05

    def _local_cap(self) -> int:
        cap=self.local_max_paragraphs
        if getattr(self,'_wide_stepped_down',False) and cap > LOCAL_SAFE_PARAGRAPHS:
            return LOCAL_SAFE_PARAGRAPHS
        return cap

    def _note_wide_request(self, chunk, error=None) -> None:
        """A first local request of more than LOCAL_SAFE_PARAGRAPHS paragraphs finished (error
        None) or failed. Paragraph-ID failures (missing/extra/duplicated IDs, or translations
        shifted one line) count against the larger size; once they are frequent, the rest of
        this file is sent in groups of LOCAL_SAFE_PARAGRAPHS, as before."""
        if getattr(self,'local_max_paragraphs',0) <= LOCAL_SAFE_PARAGRAPHS or len(chunk.paragraphs) <= LOCAL_SAFE_PARAGRAPHS:
            return
        if not hasattr(self,'_wide_requests'):
            self._wide_requests=0; self._wide_misfits=0; self._wide_stepped_down=False
        self._wide_requests+=1
        if error is None or getattr(error,'kind',None)!=ErrorKind.FORMAT or \
                not str(getattr(error,'message','') or '').startswith(('문단 ID 불일치','문단 밀림')):
            return
        self._wide_misfits+=1
        if (not self._wide_stepped_down and self._wide_misfits >= self.WIDE_MISFIT_MIN
                and self._wide_misfits >= self._wide_requests*self.WIDE_MISFIT_SHARE):
            self._wide_stepped_down=True
            self.issue.emit(f'로컬 LLM 문단 묶음 · {LOCAL_SAFE_PARAGRAPHS}문단 넘는 요청에서 문단 위치 오류 '
                            f'{self._wide_misfits}회/{self._wide_requests}회 · 이 파일의 나머지는 '
                            f'{LOCAL_SAFE_PARAGRAPHS}문단씩 나눠 요청 (번역 결과는 그대로 유지)')

    def _call_orch(self, orch, chunk, prev: str, recovery: bool, reason: str = 'format'):
        self._wait_if_paused()
        local=getattr(orch,'provider_id','')=='local'
        if local and self._chunk_loops >= self.MAX_LOOPS_PER_CHUNK:
            raise ProviderError(ErrorKind.FORMAT, f'로컬 LLM 반복 루프 {self._chunk_loops}회 · 이 청크에는 더 요청하지 않음')
        wide=local and not recovery
        try:
            result=self._call_orch_once(orch, chunk, prev, recovery, reason)
        except ProviderError as e:
            if wide:
                self._note_wide_request(chunk, e)
            if local and is_loop_error(e):
                self._chunk_loops += 1
                self.issue.emit(f'{self._chunk_label} · {self._part_label(chunk)} · 로컬 LLM이 같은 말을 반복해 끊음 '
                                f'({self._chunk_loops}/{self.MAX_LOOPS_PER_CHUNK}회 · 넘으면 이 청크는 다음 엔진 또는 원문 유지)')
            raise
        if wide:
            self._note_wide_request(chunk)
        return result

    def _call_orch_once(self, orch, chunk, prev: str, recovery: bool, reason: str = 'format'):
        if self._chunk_requests >= self.max_requests_per_chunk and getattr(orch,'provider_id','')!='local':
            raise ProviderError(ErrorKind.REFUSAL, f'청크당 요청 상한({self.max_requests_per_chunk}회) 도달 · 비용 보호를 위해 원문 유지')
        result=orch.translate_chunk(self.profile, chunk.source_text, self.terms, prev, recovery_mode=recovery,
                                    book_memory=self.book_memory, language_options=self._request_language_options(),
                                    recovery_reason=reason)
        excluded=list(getattr(orch,'last_excluded',[]) or [])
        if excluded and not self._in_second_opinion:
            result,excluded=self._second_opinion(orch, chunk, prev, result, excluded)
        if excluded:
            self._chunk_exclusions.append(([pid for pid,_ in excluded],'EXCLUDED',
                                           '범위 제외 표식(미성년자 성적 내용)',[body for _,body in excluded]))
        return result

    def _second_opinion(self, orch, chunk, prev: str, result, excluded):
        """A paragraph the engine marked with the narrow scope-exclusion marker gets one
        check by the next engine (same instructions, same exclusion rule). A wrong
        exclusion of ordinary adult content is then translated instead of being left
        as source; if the next engine also excludes it, the exclusion stands."""
        backups=[(n,o) for n,o in self._fallback_chain if o is not orch and n not in self._down]
        if not backups:
            return result, excluded
        text,key_id,retries=result
        parsed=parse_tagged_translation(text)
        remaining=[]
        self._in_second_opinion=True
        try:
            for pid,body in excluded:
                para=next((x for x in chunk.paragraphs if x.pid==pid),None)
                if para is None:
                    remaining.append((pid,body)); continue
                sub=Chunk(chunk.index,[para],_estimate_tokens(para.text,self.profile.source_language),len(para.text))
                trail=[f'{self._label(getattr(orch,"provider_id",""))} 범위 제외 표시']; fixed=False
                for fb_id,fb in backups:
                    self._announce(fb_id, f'범위 제외 표시 문단 재확인 (P{pid:06d})')
                    try:
                        t,_,_=self._call_orch(fb, sub, prev, True, 'content')
                    except ProviderError as e:
                        if e.kind==ErrorKind.REFUSAL and '제외 표식' in e.message:
                            trail.append(f'{self._label(fb_id)}도 범위 제외'); break   # a one-paragraph request came back fully excluded
                        trail.append(f'{self._label(fb_id)} {self._kind_ko(e)}'); continue
                    if getattr(fb,'last_excluded',None):
                        trail.append(f'{self._label(fb_id)}도 범위 제외'); break
                    body2=parse_tagged_translation(t).get(pid)
                    if body2:
                        parsed[pid]=body2; fixed=True; self._chunk_fallback_used=True
                        self.provider_chunks[fb_id+'_paragraphs']=self.provider_chunks.get(fb_id+'_paragraphs',0)+1
                        trail.append(f'{self._label(fb_id)} 재확인 후 번역 성공'); break
                self.issue.emit(f'{self._chunk_label} · P{pid:06d} · '+' → '.join(trail)+(' ✓' if fixed else ' → 원문 유지(범위 제외)'))
                if not fixed:
                    remaining.append((pid,body))
        finally:
            self._in_second_opinion=False
        if len(remaining)==len(excluded):
            return result, excluded
        order=[x.pid for x in chunk.paragraphs]
        rebuilt='\n\n'.join(f'<P{k:06d}>\n{parsed[k]}' for k in order if k in parsed)
        return (rebuilt,key_id,retries), remaining

    def _leftover_pids(self, chunk, text: str) -> set[int]:
        """Translated paragraphs that still hold 2+ Chinese characters / kana (characters of the
        confirmed glossary renderings are allowed)."""
        allowed=''.join(self.terms.values())
        out=parse_tagged_translation(text or '')
        bad=set()
        for para in chunk.paragraphs:
            body=out.get(para.pid)
            if body and re.sub(r'\s+','',body)!=re.sub(r'\s+','',para.text) and len(leftover_script(body, allowed))>=2:
                bad.add(para.pid)
        return bad

    @staticmethod
    def _bracket_pids(chunk, text: str) -> set[int]:
        """Paragraphs whose saved Korean still holds <WHACK> / [System] in English (saved by a
        build before v2.5.6, which kept those words untranslated): translated again (v2.5.7)."""
        out=parse_tagged_translation(text or '')
        return {p.pid for p in chunk.paragraphs if stale_bracket_prose(p.text, out.get(p.pid, ''))}

    def _partial_retry_chunk(self, chunk, stored: str, kept=(), handled=()):
        """A PARTIAL chunk from an earlier run: keep its translated paragraphs and
        return a chunk of only the paragraphs still left as source (or None)."""
        parsed=parse_tagged_translation(stored or '')
        if not parsed:
            return chunk
        norm=lambda t: re.sub(r'\s+','',t or '')
        left=[]
        for para in chunk.paragraphs:
            body=parsed.get(para.pid)
            if not para.translatable or para.pid in handled:
                continue
            # Kept paragraphs are re-sent even when half Korean (one refused sentence kept
            # as source inside a translated paragraph), as are paragraphs with chatter or
            # a long verbatim source run (rows saved before kept paragraphs were recorded).
            if body is None or para.pid in kept or norm(body) in {norm(para.text), norm(escape_protocol_literals(para.text))} \
                    or meta_leak(para.text, body) or untranslated_residue(para.text, body):
                left.append(para)
        if not left:
            return None
        if len(left)==len(chunk.paragraphs):
            return chunk
        lang=self.profile.source_language
        return Chunk(chunk.index,left,sum(_estimate_tokens(x.text,lang) for x in left),sum(len(x.text) for x in left))

    _SENT_CJK = re.compile(r'[^。！？!?…\n]*(?:[。！？!?…]+[」』”’"\')）]*|$)')
    _SENT_EN = re.compile(r'[^.!?\n]*(?:[.!?]+["”’\')]*|$)')

    def _untranslated_runs(self, src: str, body: str) -> list[str]:
        """Source sentences still present verbatim inside a mostly Korean paragraph
        (a piece kept as source when one sentence was refused), grouped into runs of
        neighbouring sentences."""
        rx=self._SENT_EN if self.profile.source_language=='en' else self._SENT_CJK
        allowed=set(''.join(self.terms.values()))
        def foreign(t):
            if self.profile.source_language=='en':
                return len(re.findall(r'[A-Za-z]+', t))>=3
            # characters the user put in confirmed terms (a name kept in Han) do not count
            return any(ch not in allowed for ch in re.findall(r'[一-龥ぁ-ゟ゠-ヿ]', t))
        flagged=[m for m in rx.finditer(src) if m.group(0).strip() and len(m.group(0).strip())>=2
                 and foreign(m.group(0)) and m.group(0).strip() in body]
        runs=[]
        for m in flagged:
            if runs:
                start,end=runs[-1]
                joined=src[start:m.end()].strip()
                if not src[end:m.start()].strip() and joined in body:
                    runs[-1]=(start,m.end()); continue   # neighbouring sentence, kept together in the body
            runs.append((m.start(),m.end()))
        return [src[x:y].strip() for x,y in runs]

    def _targeted_fixes(self, chunk, base: str, kept, engines, prev: str):
        """Fix a stored chunk without re-translating whole (possibly huge) paragraphs:
        - sentences still in the source language inside a translated paragraph: only those
          sentences are sent, all of the chunk's in ONE request (the Korean around them is kept);
        - a few leftover Chinese characters / kana: one repair request per engine, in order.
        Returns (base, kept for full re-translation, handled pids, pids still partly source)."""
        out=parse_tagged_translation(base or ''); kept=set(kept or ()); handled=set(); still=set()
        leftover=self._leftover_pids(chunk, base)
        norm=lambda t: re.sub(r'\s+','',t or '')
        pieces=[]   # (pid, run)
        for para in chunk.paragraphs:
            body=out.get(para.pid)
            if not body or not para.translatable or not re.search(r'[가-힣]', body):
                continue
            src=escape_protocol_literals(para.text)
            if norm(body)==norm(src):
                continue
            runs=self._untranslated_runs(src, body) if (para.pid in kept or untranslated_residue(para.text, body)) else []
            if runs:
                handled.add(para.pid); kept.discard(para.pid)
                pieces.extend((para.pid, r) for r in runs)
        if pieces:
            temp=[Paragraph(k+1, run, '\n\n', '', True) for k,(_,run) in enumerate(pieces)]
            work=Chunk(chunk.index, temp, sum(_estimate_tokens(x.text,self.profile.source_language) for x in temp), sum(len(x.text) for x in temp))
            saved=(self._chunk_exclusions,self._pending_traces,self._para_text)
            down_before=dict(self._down); bench_before=dict(self._benched)
            self._chunk_exclusions=[]; self._pending_traces=[]; self._para_text={**self._para_text, **{p.pid:p.text for p in temp}}
            failed_ids=set(); got={}
            try:
                text,*_=self._translate_with_engines(engines, work, prev)
                got=parse_tagged_translation(text)
                failed_ids={x for ids,_,_,_ in self._chunk_exclusions for x in ids}
            except ProviderError as e:
                failed_ids={p.pid for p in temp}
                self.issue.emit(f'{self._chunk_label} · 원문으로 남은 문장 {len(temp)}곳 다시 번역 실패 ({self._kind_ko(e)}) · 원문 유지')
            finally:
                self._chunk_exclusions,self._pending_traces,self._para_text=saved
                # the pieces belong to this chunk: engine re-check counters advance once per chunk
                for n,v in down_before.items():
                    if n in self._down: self._down[n]=v
                for n,v in bench_before.items():
                    if n in self._benched: self._benched[n]=v
            done={}
            for k,(pid_,run) in enumerate(pieces, start=1):
                ko=(got.get(k) or '').strip()
                if k in failed_ids or not ko:
                    still.add(pid_); continue
                if norm(ko)!=norm(run) and run in out[pid_]:
                    out[pid_]=out[pid_].replace(run, ko, 1)
                done[pid_]=done.get(pid_,0)+1   # same text back = it is fine as it is (a name, a title)
            if done:
                self.issue.emit(f'{self._chunk_label} · 원문으로 남았던 문장 {sum(done.values())}곳만 다시 번역 ✓ (번역된 부분은 유지 · '
                                +', '.join(f'P{x:06d}' for x in sorted(done)[:6])+')')
        for para in chunk.paragraphs:
            if para.pid in handled or para.pid not in leftover:
                continue
            body=out.get(para.pid,''); src=escape_protocol_literals(para.text)
            allowed=''.join(self.terms.values())
            before=len(leftover_script(body, allowed)); tried=False
            for name,orch in (engines if getattr(self,'_repair_enabled',True) else []):
                if name in self._down: continue
                try:
                    self._wait_if_paused()
                    key=orch._pick_key()
                    fixed=orch._repair_leftover(f'<P{para.pid:06d}>\n{src}', f'<P{para.pid:06d}>\n{body}', self.terms, key, set())
                    tried=True
                except TranslationCancelled:
                    raise
                except Exception:
                    continue
                new=parse_tagged_translation(fixed).get(para.pid, body)
                if len(leftover_script(new, allowed)) < before:
                    body=new; before=len(leftover_script(body, allowed))
                if before < 2:
                    break
            out[para.pid]=body
            if before < 2 or (tried and len(para.text) > 400):
                handled.add(para.pid); kept.discard(para.pid)   # fixed, or too long to re-translate for a few characters
            else:
                kept.add(para.pid)   # short paragraph, or repair switched off / unavailable: translate it again
        new_base='\n\n'.join(f'<P{p.pid:06d}>\n{out[p.pid]}' for p in chunk.paragraphs if p.pid in out)
        return new_base, kept, handled, still

    @staticmethod
    def _norm_para(text: str) -> str:
        return re.sub(r'\s+','',text or '')

    def _paragraph_memory(self, rows) -> dict[str,str]:
        memory={}
        for r in rows:
            if r['status'] not in ('COMPLETED','PARTIAL') or not r['translation']:
                continue
            src=parse_tagged_translation(r['source_text'] or ''); out=parse_tagged_translation(r['translation'])
            kept=Database.kept_pid_set(r)
            for k,body in src.items():
                t=out.get(k)
                if k in kept:
                    continue
                if t and self._norm_para(t)!=self._norm_para(body) and not meta_leak(body,t) and not untranslated_residue(body,t) \
                        and len(leftover_script(t, ''.join(self.terms.values())))<2 and not stale_bracket_prose(body,t):
                    # Keyed by the paragraph's place AND its text: the same book keeps its paragraph
                    # numbers (they come from the source text alone), so a resumed or re-chunked job
                    # finds every paragraph it translated. A short line repeated elsewhere in the book
                    # (“好。”, “……”) is not taken from another place: there the speaker and the speech
                    # level may differ, and the line is translated with its own context (v2.5.3).
                    memory[(int(k), self._norm_para(body))]=t
        return memory

    def _base_from_memory(self, chunk, memory: dict) -> str | None:
        """Tagged text for the chunk: remembered translations, source for the rest."""
        known=0; rows=[]
        for p in chunk.paragraphs:
            src=escape_protocol_literals(p.text); t=memory.get((int(p.pid), self._norm_para(src)))
            if t: known+=1
            rows.append(f'<P{p.pid:06d}>\n{t if t else src}')
        return '\n\n'.join(rows) if known else None

    @staticmethod
    def _merge_partial(chunk, stored: str, new_text: str) -> str:
        merged=parse_tagged_translation(stored or ''); merged.update(parse_tagged_translation(new_text or ''))
        return '\n\n'.join(f'<P{p.pid:06d}>\n{merged[p.pid]}' for p in chunk.paragraphs if p.pid in merged)

    def _translate_parts(self, orch, parts, prev: str, depth: int):
        results=[]; total_retry=0; key_id=''; child_prev=prev
        same_pid = len({p.pid for part in parts for p in part.paragraphs}) == 1
        mark=len(self._chunk_exclusions); tmark=len(self._pending_traces); failures=0; last_error=None
        for part in parts:
            try:
                text,key_id,retries=self._translate_recover(orch,part,child_prev,depth+1)
                total_retry += retries; child_prev=text[-3000:]
            except ProviderError as e:
                if e.kind not in self._KEEPABLE_PART_KINDS:
                    raise
                text=None
                trail=[f'{self._label(getattr(orch,"provider_id",""))} {self._kind_ko(e)}']
                for fb_id,fb in self._fallback_chain:
                    if fb is orch or not self._fallback_allowed(e, self.allow_policy_fallback):
                        continue
                    if fb_id in self._down or fb_id in self._benched:
                        # Say why the backup was not asked, so "→ 원문 유지" is never unexplained.
                        trail.append(f'{self._label(fb_id)} 쉬는 중(앞서 사용 불가)'); continue
                    before=len(self._chunk_exclusions)
                    # The main engine's split retries ran without the previous chunk's
                    # context (to stop input-filter cascades). A backup engine gets that
                    # context back so names, setting and flow stay continuous.
                    fb_prev=(self._chunk_prev+'\n'+child_prev).strip()[-self.context_chars:] if self._chunk_prev else child_prev
                    self._announce(fb_id, f'보조 번역 중 ({self._part_label(part)} · 앞 엔진 {trail[-1]})')
                    try:
                        text,key_id,retries=self._translate_recover(fb,part,fb_prev,depth+1)
                        total_retry += retries; child_prev=text[-3000:]; self._chunk_fallback_used=True
                        self.provider_chunks[fb_id+'_paragraphs']=self.provider_chunks.get(fb_id+'_paragraphs',0)+len(part.paragraphs)
                        partial=len(self._chunk_exclusions)>before
                        trail.append(f'{self._label(fb_id)} {"부분 성공(일부 문장 원문 유지)" if partial else "성공"}')
                        self._trace(part, trail, ok=not partial)
                        break
                    except ProviderError as e2:
                        backup_down = e2.kind in self._INFRA_KINDS or e2.kind in {ErrorKind.RATE_LIMIT, ErrorKind.KEY_BLOCKED, ErrorKind.FATAL}
                        if e2.kind not in self._KEEPABLE_PART_KINDS and not backup_down:
                            raise
                        trail.append(f'{self._label(fb_id)} {self._kind_ko(e2)}')
                        if fb_id!='local' and backup_down:
                            # The BACKUP failed (quota, key, outage). That says nothing about the
                            # main engine or this chunk: keep the main engine's translations and
                            # keep this part as source if no other backup can take it. A lasting
                            # failure benches the backup for blocked parts for a few chunks; it
                            # stays available if the main engine itself goes down.
                            if e2.kind in {ErrorKind.QUOTA, ErrorKind.KEY_BLOCKED, ErrorKind.AUTH, ErrorKind.FATAL} and fb_id not in self._benched:
                                self._benched[fb_id]=max(1,int(self.gemini_probe_every or 10))
                                self.issue.emit(f'⚠ 보조 엔진 {self._label(fb_id)} 사용 불가 ({self._kind_ko(e2)}: {e2.message[:120]}) · 막힌 문단은 다른 보조 엔진이 맡거나 원문으로 남습니다')
                            text=None
                            continue
                        if fb_id=='local' and e2.kind in {ErrorKind.TRANSIENT, ErrorKind.FATAL}:
                            # Local server off / wrong model: stop using it for this file
                            # (re-checked every N chunks) instead of waiting on every part.
                            self._down.setdefault('local',0)
                            if not self._local_warned:
                                self._local_warned=True
                                self.issue.emit(f'⚠ 로컬 LLM 사용 불가 · {e2.message[:200]}')
                            e=ProviderError(e.kind, e.message, e.status_code)
                        elif e2.kind in {ErrorKind.TRANSIENT, ErrorKind.FATAL}:
                            e=ProviderError(e.kind, e2.message, e2.status_code)
                        else:
                            e=e2
                        text=None
                if text is None:
                    # Keep this isolated part as source; siblings keep their translations.
                    failures += 1; last_error=e
                    trail.append('원문 유지')
                    bodies=list(parse_tagged_translation(part.source_text).values())
                    self._chunk_exclusions.append(([p.pid for p in part.paragraphs],e.kind.value,' → '.join(trail)[:200],bodies))
                    self._pending_traces.append((part,trail))
                    text=part.source_text
            results.append(text)
        if failures == len(parts):
            # Nothing in this group translated: report it as one failure upward
            # instead of a partial made only of source text.
            del self._chunk_exclusions[mark:]
            del self._pending_traces[tmark:]
            raise last_error
        if same_pid:
            pid=parts[0].paragraphs[0].pid
            bodies=[_TAG.sub('',x).strip() for x in results]
            joiner='' if self.profile.source_language in {'ja','zh'} else ' '
            combined=f'<P{pid:06d}>\n'+joiner.join(bodies)
        else:
            combined='\n\n'.join(x.strip() for x in results)
        return combined,key_id,total_retry

    def _paragraph_groups(self, chunk, cap: int) -> list:
        """Consecutive, evenly sized groups of at most `cap` paragraphs."""
        paras=list(chunk.paragraphs); n=len(paras)
        groups=-(-n//cap); size=-(-n//groups)
        lang=self.profile.source_language
        out=[]
        for i in range(0,n,size):
            part=paras[i:i+size]
            out.append(Chunk(chunk.index,part,sum(_estimate_tokens(p.text,lang) for p in part),sum(len(p.text) for p in part)))
        return out

    def _salvage_missing_paragraph(self, orch, chunk, prev: str, depth: int, error):
        """A local answer that dropped exactly one paragraph (often merged into the one
        before it) keeps every other paragraph. Only the missing paragraph and its
        neighbour are asked again, each as its own request so they cannot be merged.
        Returns None when this does not apply; a failure here falls back to the normal
        recovery of the whole chunk."""
        salvage=getattr(error,'salvage',None)
        if not salvage or getattr(orch,'provider_id','')!='local':
            return None
        text,missing=salvage
        order=[p.pid for p in chunk.paragraphs]
        if missing not in order:
            return None
        i=order.index(missing)
        neighbour=order[i-1] if i>0 else order[i+1]   # merged text sits under the paragraph before it
        kept=parse_tagged_translation(text)
        kept.pop(neighbour,None)
        lang=self.profile.source_language
        redo=[]
        for pid in sorted({neighbour,missing}, key=order.index):
            para=chunk.paragraphs[order.index(pid)]
            redo.append(Chunk(chunk.index,[para],_estimate_tokens(para.text,lang),len(para.text)))
        self._announce('local', f'문단 1개 누락 · 나머지 {len(kept)}문단은 유지하고 {len(redo)}문단만 한 문단씩 다시 요청')
        mark=len(self._chunk_exclusions); tmark=len(self._pending_traces)
        try:
            part_text,key_id,retries=self._translate_parts(orch,redo,prev,depth)
        except ProviderError:
            del self._chunk_exclusions[mark:]; del self._pending_traces[tmark:]
            return None
        kept.update(parse_tagged_translation(part_text))
        if any(pid not in kept for pid in order):
            return None
        rebuilt='\n\n'.join(f'<P{pid:06d}>\n{kept[pid]}' for pid in order)
        return rebuilt,key_id,retries

    _FINE_RETRY_KINDS = frozenset({'POLICY','SAFETY','REFUSAL','FORMAT'})

    @classmethod
    def _fine_retry_reason(cls, error_type) -> str:
        """'content' / 'format' when an earlier run of this chunk failed for that reason, else ''."""
        kinds={k.strip() for k in str(error_type or '').split('/') if k.strip()} & cls._FINE_RETRY_KINDS
        if not kinds: return ''
        return 'format' if kinds=={'FORMAT'} else 'content'

    def _translate_recover(self, orch, chunk, prev: str, depth: int = 0):
        # Pathological single paragraphs can exceed the configured maximum all by
        # themselves. Split them before any HTTP call so an avoidable 400/413 does
        # not trip the fatal configuration circuit breaker.
        if chunk.estimated_tokens > self.max_chunk_tokens and depth < 4:
            parts=split_chunk_for_retry(chunk,self.profile.source_language)
            if len(parts)>1:
                if depth == 0:
                    self.issue.emit(f'{self._chunk_label} · 긴 청크 {len(parts)}부분 분할 · 원문 변경 없이 번역')
                return self._translate_parts(orch,parts,prev,depth)
        # Retry of a chunk that already went down the whole ladder last time for a
        # content/format reason: the same whole-chunk request would very likely fail
        # the same way. Start at the fine end instead: no earlier scene as context,
        # the stricter recovery instructions, and paragraph-sized pieces. The source
        # text itself is never altered.
        fine = depth == 0 and getattr(self,'_retry_pass','') in ('content','format')
        if fine:
            prev=''
            if len(chunk.paragraphs) > 1:
                parts=split_chunk_for_retry(chunk,self.profile.source_language)
                if len(parts) > 1:
                    self.issue.emit(f'{self._chunk_label} · 이전 실패 문단 {len(parts)}부분 분할 재시도 · 앞 문맥 제외')
                    return self._translate_parts(orch,parts,'',depth)
        # A local model copies every paragraph ID of a request exactly; with short-line
        # novels one chunk holds 50+ IDs and the model drops or merges some. Send it in
        # consecutive groups of at most N paragraphs from the start instead of finding
        # the limit by failing. Chunk boundaries (and so resume/memory) are unchanged, and the
        # groups are ordinary requests (same depth), not recovery retries.
        cap=self._local_cap()
        if getattr(orch,'provider_id','')=='local' and cap and len(chunk.paragraphs) > cap and depth < 4:
            parts=self._paragraph_groups(chunk, cap)
            self._announce('local', f'문단 {len(chunk.paragraphs)}개를 {len(parts)}묶음(묶음당 최대 {cap}문단)으로 나눠 요청')
            return self._translate_parts(orch,parts,prev,depth-1)   # same depth: not a recovery request
        try:
            return self._call_orch(orch, chunk, prev, depth > 0 or fine, self._retry_pass if fine else 'format')
        except ProviderError as first:
            salvaged=self._salvage_missing_paragraph(orch, chunk, prev, depth, first)
            if salvaged is not None:
                return salvaged
            # An explicit provider content refusal is definitive for this request.
            # Do not pay for reframe/split calls to the SAME provider: the caller
            # can move the intact chunk to the next configured engine instead.
            # FORMAT errors (including missing paragraph IDs) retain recovery.
            direct_backup = (self.allow_provider_fallback
                             and self._fallback_allowed(first, self.allow_policy_fallback)
                             and any(fb is not orch and name not in self._down
                                     and name not in self._benched
                                     for name,fb in self._fallback_chain))
            if first.kind in self._CONTENT_KINDS and direct_backup:
                # One exception: an input filter judges the whole prompt, so a heated
                # previous scene in the context can block an ordinary chunk. When there
                # was such context, one request without it comes first (one request, the
                # cheapest step); only a block that remains moves the chunk to the backup.
                if self.allow_reframe and depth == 0 and not fine and prev.strip():
                    self.issue.emit(f'{self._chunk_label} · {self._label(getattr(orch,"provider_id",""))} {self._kind_ko(first)} · 앞 문맥 없이 1회 재시도')
                    self.recovery_metrics['reframe_attempts']+=1
                    try:
                        result=self._call_orch(orch, chunk, '', True, 'content')
                        self.recovery_metrics['reframe_successes']+=1
                        return result
                    except ProviderError as second:
                        if second.kind not in self._CONTENT_KINDS:
                            raise
                        first = second
                self.issue.emit(f'{self._chunk_label} · {self._label(getattr(orch,"provider_id",""))} '
                                f'{self._kind_ko(first)} · 동일 엔진 재시도 생략 · 다음 엔진으로 전환')
                raise first
            content_failure = first.kind in self._CONTENT_KINDS
            # Provider input filters judge the whole prompt, including the previous
            # Korean context. A heated earlier scene can therefore block an innocent
            # next chunk. Retry once with the context removed and clearer translation
            # framing; the source text itself is never altered.
            if self.allow_reframe and depth == 0 and content_failure and not fine:
                self.issue.emit(f'{self._chunk_label} · {self._label(getattr(orch,"provider_id",""))} {self._kind_ko(first)} · 앞 문맥 없이 1회 재시도')
                self.recovery_metrics['reframe_attempts']+=1
                try:
                    result=self._call_orch(orch, chunk, '', True, 'content')
                    self.recovery_metrics['reframe_successes']+=1
                    return result
                except ProviderError as second:
                    first = second
            if first.kind not in (self._CONTENT_KINDS | {ErrorKind.FORMAT}) or depth >= 3 or not self.allow_split_recovery:
                raise first
            # Core policy blocks are isolated only down to an ordinary paragraph-sized
            # window. A huge "paragraph" (a chapter with no line breaks) may be narrowed
            # to that size, but never cut into single sentences: each request keeps
            # enough surrounding context for the provider to judge it as written.
            if first.kind == ErrorKind.POLICY and len(chunk.paragraphs) <= 1 and \
                    sum(len(p.text) for p in chunk.paragraphs) <= 2*self._policy_floor_chars():
                raise first
            parts = split_chunk_for_retry(chunk, self.profile.source_language)
            if len(parts) <= 1 or (len(parts) == 1 and parts[0].source_text == chunk.source_text):
                raise first
            if depth == 0:
                self.issue.emit(f'{self._chunk_label} · {self._label(getattr(orch,"provider_id",""))} {self._kind_ko(first)} · {len(parts)}부분 분할 재시도 (문단별 복구)')
            self.recovery_metrics['split_attempts']+=1
            result=self._translate_parts(orch,parts,'' if content_failure else prev,depth)
            self.recovery_metrics['split_successes']+=1
            return result

    @staticmethod
    def _fallback_allowed(error: ProviderError, policy_ok: bool = False) -> bool:
        if error.kind == ErrorKind.POLICY:
            # Confirmed policy blocks can move intact to an explicitly enabled backup.
            # policy_ok is the user's fallback_on_policy option, not a size check.
            return bool(policy_ok)
        if error.kind == ErrorKind.FATAL:
            # A generic provider/project 403 can make Gemini unavailable, while a
            # bad model/request 400 should be corrected instead of silently hidden.
            return error.status_code == 403
        return error.kind in {ErrorKind.AUTH, ErrorKind.KEY_BLOCKED, ErrorKind.QUOTA, ErrorKind.RATE_LIMIT,
                              ErrorKind.TRANSIENT, ErrorKind.FORMAT, ErrorKind.SAFETY, ErrorKind.REFUSAL}

    _INFRA_KINDS = frozenset({ErrorKind.AUTH, ErrorKind.KEY_BLOCKED, ErrorKind.QUOTA, ErrorKind.TRANSIENT})

    ENGINE_LABELS = {'gemini':'Gemini','openai':'OpenAI','local':'로컬 LLM'}

    def engine_order(self) -> list[str]:
        """User-chosen engine order; the first one is the main translator.

        Older settings (no explicit order) keep their meaning: Gemini first, then the
        enabled backups in the chosen backup order.
        """
        raw=self._engine_order_setting
        if raw:
            out=[]
            for name in raw:
                if name in self.ENGINE_LABELS and name not in out: out.append(name)
            if out: return out
        backups=['openai','local'] if str(self.local_settings.get('order') or 'local_first')=='openai_first' else ['local','openai']
        return ['gemini']+[b for b in backups if (self.openai_settings if b=='openai' else self.local_settings).get('enabled')]

    def _engines(self, orchs: dict) -> list[tuple[str,object]]:
        return [(n,orchs[n]) for n in self.engine_order() if orchs.get(n) is not None]

    def _ready_now(self, orch) -> bool:
        now=orch.clock_fn()
        return any(orch._effective_ready_at(k) <= now for k in orch._enabled())

    def _mark_down(self, name: str, error: ProviderError, next_name: str | None):
        if name in self._down:
            return
        self._down[name]=0
        every=f' · {self.gemini_probe_every}청크마다 복구 여부를 다시 확인합니다' if self.gemini_probe_every else ''
        target=f'{self.ENGINE_LABELS.get(next_name,next_name)}로 계속합니다' if next_name else '다른 엔진이 없습니다'
        self.issue.emit(f'⚠ {self.ENGINE_LABELS.get(name,name)} 사용 불가 ({self._kind_ko(error)}: {error.message[:100]}) → 이 파일의 남은 청크는 {target}{every}')

    def _translate_with_engines(self, engines, chunk, prev: str):
        """Translate one chunk with the ordered engines.

        engines[0] is the main translator. Later engines receive (a) isolated parts
        the earlier engine could not translate for content reasons and (b) whole
        chunks when an earlier engine is unavailable (quota, keys, outage). An
        unavailable engine is re-tried every N chunks and resumes when healthy.
        """
        if not engines:
            raise ProviderError(ErrorKind.AUTH,'사용 가능한 번역 엔진이 없습니다. API 설정에서 엔진과 키를 확인하세요.')
        for name in list(self._benched):
            self._benched[name]-=1
            if self._benched[name]<=0: del self._benched[name]
        # Probe engines that went down, earliest first, every N chunks.
        for name in list(self._down):
            self._down[name]+=1
        active=[(n,o) for n,o in engines if n not in self._down]
        first_active=engines.index(active[0]) if active else len(engines)
        for rank,(name,orch) in enumerate(engines):
            if rank > first_active and name in self._down and self.gemini_probe_every and self._down[name] >= self.gemini_probe_every:
                # A backup that was unavailable is simply allowed again after N chunks;
                # it is never handed a whole chunk while a higher engine is healthy.
                del self._down[name]
        for rank,(name,orch) in enumerate(engines):
            if rank > first_active:
                break
            if name not in self._down or not self.gemini_probe_every:
                continue
            if self._down[name] % self.gemini_probe_every == 0 and self._ready_now(orch):
                rest=[(n,o) for n,o in active if n!=name]
                self._fallback_chain=rest if self.allow_provider_fallback else []
                self._announce(name, '복구 확인 요청 중')
                try:
                    text,key_id,retries=self._translate_recover(orch,chunk,prev)
                    del self._down[name]
                    self.issue.emit(f'↩ {self.ENGINE_LABELS.get(name,name)} 복귀 · 정상 응답 확인, 다시 이 엔진으로 번역합니다')
                    return text,key_id,retries,name,self._model_for(name),(1 if (self._chunk_fallback_used or name!=engines[0][0]) else 0)
                except ProviderError:
                    self._chunk_exclusions=[]; self._chunk_requests=0; self._chunk_loops=0
            break   # probe only the highest-priority down engine
        if not active:
            # Everything is marked down: try again in order rather than giving up.
            self._down.clear(); active=list(engines)
        primary_index=engines.index(active[0])
        last=None; trail=[]
        for pos,(name,orch) in enumerate(active):
            later=[(n,o) for n,o in active[pos+1:]]
            self._fallback_chain=later if self.allow_provider_fallback else []
            if pos>0:
                # A failed earlier attempt may have recorded exclusions; start clean.
                self._chunk_exclusions=[]
                self._announce(name, f'청크 전체 보조 번역 중 (앞 엔진: {trail[-1] if trail else "사용 불가"})')
            try:
                text,key_id,retries=self._translate_recover(orch,chunk,prev)
                is_backup = pos>0 or primary_index>0 or self._chunk_fallback_used
                if pos>0:
                    trail.append(f'{self._label(name)} 성공')
                    self.issue.emit(f'{self._chunk_label} · 청크 전체 · '+' → '.join(trail)+' ✓')
                return text,key_id,retries,name,self._model_for(name),(1 if is_backup else 0)
            except ProviderError as e:
                setattr(e,'provider_id',name)
                trail.append(f'{self._label(name)} {self._kind_ko(e)}'); setattr(e,'trail',list(trail))
                if last is not None:
                    setattr(e,'primary_kind',getattr(last,'primary_kind',last.kind))
                    setattr(e,'primary_status_code',getattr(last,'primary_status_code',last.status_code))
                infra = e.kind in self._INFRA_KINDS or (e.kind==ErrorKind.FATAL and e.status_code==403)
                if name=='local' and e.kind in {ErrorKind.TRANSIENT, ErrorKind.FATAL}:
                    # Local server off / wrong model name: report and move on.
                    self.issue.emit(f'로컬 LLM 사용 불가 · {e.message[:200]}')
                    infra=True
                nxt=later[0][0] if later else None
                if infra:
                    if nxt is None or not self.allow_provider_fallback:
                        # A content-blocked main engine may still translate the
                        # unaffected paragraphs. If the backup is out of quota,
                        # restore the former isolated recovery instead of losing
                        # the whole chapter as FAILED.
                        if (pos > 0 and last is not None
                                and last.kind in self._CONTENT_KINDS
                                and active[0][0] != name):
                            primary_name,primary_orch=active[0]
                            previous_chain=self._fallback_chain
                            try:
                                self._fallback_chain=[]
                                self._chunk_exclusions=[]
                                self.issue.emit(f'{self._chunk_label} · 보조 엔진 {self._label(name)} 사용 불가 '
                                                f'({self._kind_ko(e)}) · 기존 엔진에서 문단별 복구 시도')
                                text,key_id,retries=self._translate_recover(primary_orch,chunk,prev)
                                return text,key_id,retries,primary_name,self._model_for(primary_name),1
                            finally:
                                self._fallback_chain=previous_chain
                        raise (last if (last is not None and name=='local') else e)
                    self._mark_down(name,e,nxt)
                    last=last or e
                    continue
                # Content-type failure of this engine for this chunk.
                if last is None: last=e
                if nxt is None or not self.allow_provider_fallback or \
                        not self._fallback_allowed(e, self.allow_policy_fallback):
                    raise e
                continue
        if last is not None:
            setattr(last,'trail',list(trail))
            raise last
        raise ProviderError(ErrorKind.AUTH,'사용 가능한 번역 엔진이 없습니다.')

    def _write_snapshot(self, db, project_id: int):
        rows=db.all_chunks(project_id)
        preferred_dir=self.output_dir or self.source_path.parent
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); punct=bool(cfg.get('korean_punctuation',True)); quotes=str(cfg.get('quote_style') or 'straight')
        except Exception:
            punct=True; quotes='straight'
        out,failed,report,output_dir=render_snapshot(self.chunks,rows,self.source_path,preferred_dir,DATA_DIR/'exports',self.all_paragraphs,korean_punct=punct,quote_style=quotes)
        self.pending_report=report or ''   # shown in the window's 미완료 목록 tab
        from ..rendering import not_started
        unstarted=sum(1 for r in failed if not_started(r))
        self.pending_breakdown=(unstarted, len(failed)-unstarted)   # (not reached yet, failed/partial)
        return out,failed,report,output_dir,preferred_dir

    def _pause_with_snapshot(self, db, project_id: int, reason: str, user_requested: bool=False, kind: str='STOPPED'):
        # Why the job paused, for unattended auto-resume in the main window:
        # 'STOPPED' (user), 'RATE_LIMIT' / 'QUOTA' / 'TRANSIENT' (waiting helps),
        # 'FATAL' / 'AUTH' (settings problem; waiting does not help).
        self.pause_kind = 'STOPPED' if user_requested else kind
        out,failed,report,_,_=self._write_snapshot(db,project_id)
        self.result_ready.emit('stopped' if user_requested else 'paused',str(out),len(failed))
        extra=f'\n현재 부분 결과: {out}' + (f'\n{self.pending_text(len(failed))} · 자세한 목록은 미완료 목록 탭' if failed else '')
        summary=self.run_summary()
        if summary: extra += '\n'+summary
        self.paused.emit(reason+extra,user_requested)

    def pending_text(self, count: int) -> str:
        """'미완료 N청크', split into chunks not reached yet and chunks that were tried."""
        unstarted, tried = getattr(self, 'pending_breakdown', (0, count))
        if unstarted and tried:
            return f'미완료 {count:,}청크 (아직 번역 전 {unstarted:,} · 실패·부분 완료 {tried:,})'
        if unstarted:
            return f'미완료 {count:,}청크 (아직 번역 전)'
        return f'미완료 {count:,}청크'

    def _carry_renamed_terms(self, db, sha: str, fingerprint: str, settings_key: str) -> None:
        """Only the Korean side of confirmed terms changed since this book was translated
        (좌경 → 쭤징): rename it in the stored translations and keep the book, instead of
        translating it all again. Any other change keeps the normal behaviour."""
        try:
            if db.find_project(str(self.source_path), self.profile.source_language, self.profile.id, sha, fingerprint):
                return
            row = db.latest_project_for_source(sha, self.profile.source_language, self.profile.id)
            if not row:
                return
            from ..name_rename import renames_between
            old_terms = db.project_terms(int(row['id']))
            renames = renames_between(old_terms, self.terms)
            if not renames:
                return
            current = self.terms
            try:
                self.terms = old_terms; old_fp = self._fingerprint()
            finally:
                self.terms = current
            if old_fp != row['job_fingerprint']:
                return            # other settings changed too: not a pure rename
            n = db.rename_in_translations(int(row['id']), renames)
            db.rekey_project(int(row['id']), fingerprint, settings_key)
            pairs = ', '.join(f'{a}→{b}' for a, b in list(renames.items())[:6]) + (' …' if len(renames) > 6 else '')
            skipped = int(getattr(db, 'last_rename_skipped', 0) or 0)
            kept = f' · 다른 낱말의 일부로 보이는 {skipped:,}곳(예: 하늘색)은 그대로 둠' if skipped else ''
            self.issue.emit(f'확정 용어 이름 변경 · 다시 번역하지 않고 저장된 번역에 반영: {pairs} ({n:,}곳){kept} · 이전 결과 파일은 백업됩니다')
            self._rename_common_terms(old_terms)
        except Exception as e:
            self.issue.emit(f'이름 변경 반영 생략 · {type(e).__name__}: {e}')

    # Parts of the job identity, in the words the user knows (several parts share a label).
    IDENTITY_LABELS = (
        ('terms', '확정 용어집'), ('book_memory', '작품 메모'), ('language_options', '이름 표기·언어 옵션'),
        ('mode_id', '번역 모드(빠르게·균형·품질)'), ('context_chars', '번역 모드(빠르게·균형·품질)'),
        ('max_chunk_tokens', '번역 모드(빠르게·균형·품질)'),
        ('chunks', '원문 나누기(인코딩·번역 모드·원문 내용)'), ('chunk', '원문 나누기(인코딩·번역 모드·원문 내용)'),
        ('initial_context_sha256', '앞 파일에서 이어지는 문맥'), ('content_kind', '소설·문서 종류'),
        ('system', '프로그램의 번역 지침(새 버전)'), ('user_prompt_contract_sha256', '프로그램의 번역 지침(새 버전)'),
        ('prompt_version', '프로그램의 번역 지침(새 버전)'), ('lexical_guidance', '프로그램의 번역 지침(새 버전)'),
    )

    def identity_change(self, db, sha: str) -> dict | None:
        """Before a single-file start: an earlier job on this exact source holds saved
        translation, but the current settings make a different job (glossary, 작품 메모,
        mode, encoding, a new program version ...). Without asking, everything would be
        translated again from the first chunk. Returns what to tell the user, or None when
        nothing would be lost (normal resume, new book, or a pure rename of terms that the
        job applies to the saved translation by itself)."""
        lang = self.profile.source_language; prof = self.profile.id
        fingerprint = self._fingerprint()
        current = db.find_project(str(self.source_path), lang, prof, sha, fingerprint)
        if current is not None:
            done_now, _ = db.progress_counts(int(current['id']))
            best = db.best_project_for_source(sha, lang, prof, exclude_project=int(current['id']))
            # A run with the new settings already started over (an earlier build did this
            # without asking) while an older job holds clearly more: offer that one.
            if best is None or int(best['done'] or 0) <= done_now:
                return None
            row = best
        else:
            row = db.best_project_for_source(sha, lang, prof)
            if row is None:
                return None
            done_now = 0
        old_terms = db.project_terms(int(row['id']))
        from ..name_rename import renames_between
        if current is None:
            renames = renames_between(old_terms, self.terms)
            if renames:
                held = self.terms
                try:
                    self.terms = old_terms; old_fp = self._fingerprint()
                finally:
                    self.terms = held
                if old_fp == row['job_fingerprint'] and row['id'] == (db.latest_project_for_source(sha, lang, prof) or {'id': None})['id']:
                    return None   # only the Korean side of terms changed: renamed in place, as before
        new_parts = self.identity_parts()
        try:
            old_parts = json.loads(row['identity_json'] or '{}') if 'identity_json' in row.keys() else {}
        except Exception:
            old_parts = {}
        labels: list[str] = []
        def add(label):
            if label not in labels: labels.append(label)
        if old_parts:
            for key, label in self.IDENTITY_LABELS:
                if key in old_parts and old_parts.get(key) != new_parts.get(key):
                    add(label)
            unknown = not labels
        else:
            held = self.terms
            try:
                self.terms = old_terms; only_terms = self._fingerprint() == row['job_fingerprint']
            finally:
                self.terms = held
            if old_terms != self.terms: add('확정 용어집')
            unknown = not only_terms
        added = [k for k in self.terms if k not in old_terms]
        removed = [k for k in old_terms if k not in self.terms]
        changed = [k for k in self.terms if k in old_terms and old_terms[k] != self.terms[k]]
        common = {k for k in self.terms if k in old_terms}
        renames = renames_between({k: old_terms[k] for k in common}, {k: self.terms[k] for k in common}) if changed else {}
        done_old = int(row['done'] or 0)
        _, total = db.progress_counts(int(row['id']))
        return {'project_id': int(row['id']), 'current_project_id': int(current['id']) if current is not None else None,
                'done': done_old, 'done_now': done_now, 'total': max(total, len(self.chunks)), 'labels': labels,
                'unknown_other': bool(unknown), 'added': added, 'removed': removed, 'changed': changed, 'renames': renames}

    def adopt_previous(self, db, info: dict) -> None:
        """Keep the earlier job's translation: it becomes this job (new settings apply to the
        chunks not translated yet). Name renderings that only changed on the Korean side are
        renamed in the saved translation, as a pure rename always was."""
        old_id = int(info['project_id'])
        if info.get('renames'):
            db.rename_in_translations(old_id, dict(info['renames']))
        cur = info.get('current_project_id')
        if cur is not None and int(cur) != old_id:
            # The run that started over steps aside; its chunks stay reusable as translation memory.
            with db.connect() as con:
                con.execute("UPDATE projects SET job_fingerprint=job_fingerprint||? WHERE id=?", (f':superseded:{old_id}', int(cur)))
        db.rekey_project(old_id, self._fingerprint(), self._settings_key())

    def _rename_common_terms(self, old_terms: dict) -> None:
        """The shared glossary follows a rename the user made, where it still holds the old rendering."""
        if getattr(self.profile, 'lexical_extra', None) is None:
            return            # the shared glossary is switched off for this job
        try:
            from .. import common_terms
            lang = self.profile.source_language
            shared = common_terms.load(lang, DATA_DIR)
            changed = {src: self.terms[src] for src, old in old_terms.items()
                       if src in self.terms and shared.get(src) == old and self.terms[src] != old}
            if changed:
                shared.update(changed); common_terms.replace(lang, shared, DATA_DIR)
        except Exception:
            pass

    def _make_engines(self, keys, openai_keys, mode_retry, mode_timeout, rate_cfg, repair):
        """(Gemini, OpenAI, local LLM) engines of this job; None for an engine that is off
        or has no key/model. All three share the same stop/pause/repair/usage hooks."""
        shared = dict(cancel_check=lambda: self._stop, pause_fn=self._wait_if_paused,
                      repair_script=repair, content_kind=self.content_kind)
        cloud = dict(retries=mode_retry, timeout=mode_timeout,
                     target_rpm=float(rate_cfg.get('start_rpm', 12)), min_rpm=float(rate_cfg.get('min_rpm', 2)),
                     max_rpm=float(rate_cfg.get('max_rpm', 30)),
                     successes_before_step_up=int(rate_cfg.get('successes_before_step_up', 5)))
        orch = None
        if keys:
            orch = TranslationOrchestrator(GeminiProvider(), keys, self.model, thinking_level=self.thinking_level,
                wait_fn=lambda msg: self.api_state.emit(f'{self._chunk_label} · Gemini · {msg}'),
                event_fn=lambda msg: self._provider_event('Gemini', msg),
                usage_fn=lambda p, m, u, ok: self._on_usage(p, m, u, ok, getattr(orch, '_usage_key', None)), provider_id='gemini',
                key_selected_fn=lambda key: self.api_state.emit(f'{self._chunk_label} · Gemini · {self.model} · {self._key_tier_text(key)} · 프로젝트 {key.project_group or "default"} · 요청 중'),
                **cloud, **shared)
        openai_orch = None
        openai_model = str(self.openai_settings.get('model') or '').strip()
        if bool(self.openai_settings.get('enabled')) and openai_keys and openai_model:
            openai_orch = TranslationOrchestrator(OpenAIProvider(str(self.openai_settings.get('endpoint') or '').strip() or None),
                openai_keys, openai_model,
                wait_fn=lambda msg: self.api_state.emit(f'{self._chunk_label} · OpenAI · {msg}'),
                event_fn=lambda msg: self._provider_event('OpenAI', msg),
                usage_fn=self._on_usage, provider_id='openai', **cloud, **shared)
        local_orch = None
        local_model = str(self.local_settings.get('model') or '').strip()
        if bool(self.local_settings.get('enabled')) and local_model:
            local_timeout = int(self.local_settings.get('timeout_seconds') or 300)
            local_args, local_kw = local_provider_args(self.local_settings, timeout=local_timeout)
            local_orch = TranslationOrchestrator(
                LocalLLMProvider(*local_args, **local_kw, structured=self.local_structured, sampling=self.local_sampling),
                [KeyState('LOCAL', 'local', 'local', True)], local_model,
                retries=min(mode_retry, 1), timeout=local_timeout, target_rpm=600.0, min_rpm=60.0, max_rpm=600.0,
                event_fn=lambda msg: self._provider_event('Local', msg),
                usage_fn=self._on_usage, provider_id='local', **shared)
        return orch, openai_orch, local_orch

    def run(self):
        try:
            self.api_state.emit('번역 준비 중 · 저장된 번역 확인')
            db = Database(DATA_DIR/'translations.db')
            sha = hashlib.sha256(self.source_bytes).hexdigest()
            fingerprint = self._fingerprint()
            settings_key = self._settings_key()
            if not self.restart_existing:
                self._carry_renamed_terms(db, sha, fingerprint, settings_key)
            pid = db.get_or_create_project(str(self.source_path), self.profile.source_language, self.profile.id, sha, fingerprint, settings_key)
            db.replace_terms(pid, self.terms)
            try:
                db.set_identity(pid, json.dumps(self.identity_parts(), sort_keys=True))
            except Exception:
                pass
            # Paragraph memory: when chunk boundaries change (e.g. lines that used to be
            # kept as source are now translated), earlier translations are reused paragraph
            # by paragraph so only the new/untranslated lines are requested.
            para_rows = [] if self.restart_existing else db.translated_rows_for_source(sha, self.profile.id, settings_key)
            if not para_rows and not self.restart_existing:
                para_rows = db.translated_rows_for_source(sha, self.profile.id, '')   # jobs from builds before settings were recorded
            para_memory = self._paragraph_memory(para_rows)
            # One transaction for all chunks (a long book has thousands).
            db.upsert_chunks(pid, self.chunks)
            # Rows of an older chunk layout (a continued job whose chunking changed) go;
            # their paragraphs were read into the paragraph memory above.
            db.prune_chunks(pid, [c.index for c in self.chunks])
            if self.restart_existing and self.chunks:
                db.reset_chunks_from(pid,0)
            # Keep every successfully translated chunk, even if an earlier chunk failed.
            # The product goal is lossless forward progress: retry only incomplete chunks.
            # Context is rebuilt from the nearest completed translations as the loop advances.
            done_map = {int(r['chunk_index']): r['translation'] for r in db.completed_translations(pid)}
            # Results saved before the reasoning-leak check (</think>, "The user provided…")
            # are translated again instead of being kept.
            by_index = {c.index: c for c in self.chunks}
            leaked = {i: t for i, t in done_map.items() if i in by_index and has_meta_leak(by_index[i].source_text, t)}
            for i in leaked:
                done_map.pop(i, None)
            if leaked:
                self.issue.emit(f'모델의 생각·설명이 섞인 이전 번역 {len(leaked)}개 청크 · 해당 문단만 다시 번역합니다')
            # Paragraphs a model translated but left Chinese characters / kana in (the one
            # repair request did not fix them): a new run translates just those again.
            leftover_retry = {} if self.restart_existing else \
                {i: (t, bad) for i, t in done_map.items() if i in by_index and (bad := self._leftover_pids(by_index[i], t))}
            for i in leftover_retry:
                done_map.pop(i, None)
            if leftover_retry:
                n = sum(len(b) for _, b in leftover_retry.values())
                self.issue.emit(f'이어 번역 · 한자·가나가 남은 문단 {n}개(청크 {len(leftover_retry)}개)를 다시 번역합니다')
            bracket_retry = {} if self.restart_existing else \
                {i: (t, bad) for i, t in done_map.items() if i in by_index and (bad := self._bracket_pids(by_index[i], t))}
            for i, (t, bad) in bracket_retry.items():
                done_map.pop(i, None)
                old_t, old_bad = leftover_retry.get(i, (t, set()))
                leftover_retry[i] = (old_t, set(old_bad) | bad)
            if bracket_retry:
                n = sum(len(b) for _, b in bracket_retry.values())
                self.issue.emit(f'이어 번역 · <WHACK>·[System] 같은 영어 효과음이 그대로 남은 문단 {n}개(청크 {len(bracket_retry)}개)를 다시 번역합니다')
            # Translation memory: an identical chunk already translated with the same
            # settings in another job (e.g. the opened file before text was appended
            # or pasted) is reused instead of being paid for again. "처음부터 다시"
            # (restart_existing) deliberately skips this.
            if not self.restart_existing:
                reused=0
                todo=[c for c in self.chunks if c.index not in done_map]
                found=db.cached_translations(self.profile.id, settings_key, todo, pid) if todo else {}
                updates=[]
                for c in todo:
                    cached=found.get(c.index)
                    if cached and not has_meta_leak(c.source_text, cached) and not self._bracket_pids(c, cached):
                        updates.append((c.index, 'COMPLETED', dict(translation=cached, provider='memory', model='',
                                        error_type=None, error_message=None, retry_count=0, fallback_used=0, prompt_version=PROMPT_VERSION)))
                        done_map[c.index]=cached; reused+=1
                if updates:
                    db.set_chunk_statuses(pid, updates)
                if reused:
                    self.issue.emit(f'번역 기억 · 이전에 같은 설정으로 번역한 청크 {reused}개를 재사용합니다 (새로 요청하지 않음)')
            records = [r for r in normalize_key_records(self.api_keys,'GEM') if r.enabled]
            keys = [KeyState(r.id, r.secret, r.project_group, True, tier=r.tier) for r in records]
            self._gemini_live_keys=keys
            openai_records=[r for r in normalize_key_records(self.openai_settings.get('keys',[]),'OAI') if r.enabled]
            openai_keys=[KeyState(r.id,r.secret,r.project_group,True) for r in openai_records]
            mode_retry = max(0,int(self._mode_cfg.get('max_retries', {'quality':4,'balanced':3,'speed':2}.get(self.mode_id,3))))
            mode_timeout = max(15,int(self._mode_cfg.get('timeout_seconds', {'quality':120,'balanced':90,'speed':75}.get(self.mode_id,90))))
            context_chars = self.context_chars
            try:
                app_cfg=load_json(CONFIG_DIR/'app.json'); rate_cfg=(app_cfg.get('rate_limiter') or {})
            except Exception:
                app_cfg={}; rate_cfg={}
            repair=bool(app_cfg.get('repair_leftover_script', True))
            self._repair_enabled=repair
            orch, openai_orch, local_orch = self._make_engines(keys, openai_keys, mode_retry, mode_timeout, rate_cfg, repair)
            for _o in (orch,openai_orch,local_orch):
                if _o is not None: _o.sentence_style=self.sentence_style
            engines=self._engines({'gemini':orch,'openai':openai_orch,'local':local_orch})
            self._perf_engines={name:engine for name,engine in (('gemini',orch),('openai',openai_orch),('local',local_orch)) if engine is not None}
            if not engines:
                raise ProviderError(ErrorKind.AUTH,'사용 가능한 번역 엔진이 없습니다. API 설정에서 엔진 순서와 키를 확인하세요.')
            if engines[0][0]!='gemini' or len(engines)>1:
                self.issue.emit('번역 엔진 순서 · '+' → '.join(self.ENGINE_LABELS.get(n,n) for n,_ in engines)+' (첫 번째가 주 엔진)')
            self.issue.emit('실제 번역 모델 · '+' / '.join(f'{self._label(n)}: {self._model_for(n)}' for n,_ in engines)+' · 현재 작업에 적용됨')
            disabled_emitted: set[str] = set()
            translations = dict(done_map)
            # Earlier PARTIAL chunks: only their untranslated paragraphs are sent again.
            partial_rows = [] if self.restart_existing else db.partial_translations(pid)
            partial_map = {int(r['chunk_index']): r['translation'] for r in partial_rows}
            kept_map = {int(r['chunk_index']): Database.kept_pid_set(r) for r in partial_rows}
            if not self.restart_existing:
                partial_map.update(leaked)   # only the paragraphs with chatter are sent again
                for i, (t, bad) in leftover_retry.items():
                    partial_map[i] = t; kept_map[i] = set(kept_map.get(i, set())) | bad
            if para_memory:
                self.issue.emit(f'이어 번역 · 이전에 번역한 문단 {len(para_memory):,}개를 문단 단위로 재사용합니다 (청크 구성이 바뀌어도 새 문단만 요청)')
            if partial_map:
                self.issue.emit(f'이어 번역 · 부분 완료 청크 {len(partial_map)}개는 원문으로 남은 문단만 다시 요청합니다 (이미 번역된 문단은 유지)')
            earlier_trouble = [] if self.restart_existing else \
                [r for r in db.pending_chunks(pid) if r['status'] in ('FAILED','PARTIAL')]
            prior_fail = {int(r['chunk_index']): self._fine_retry_reason(r['error_type']) for r in earlier_trouble}
            prior_fail = {i: k for i, k in prior_fail.items() if k}
            # Chunks an earlier run already had trouble with (failed, partial, or sent again for
            # leftover source text). A FORMAT failure there again is about that text, not the
            # server, so it must not count toward the consecutive-failure pause: on resume these
            # chunks come back to back, and three of them kept pausing the whole book before any
            # new chunk was reached.
            known_trouble = {int(r['chunk_index']) for r in earlier_trouble} | set(partial_map)
            if prior_fail:
                self.issue.emit(f'이어 번역 · 지난번 막힌 청크 {len(prior_fail)}개는 앞 문맥 없이, 문단 단위로 나눠 더 세밀하게 다시 요청합니다')
            prev = self.initial_context[-context_chars:] if self.initial_context else ''
            consecutive_infra_failures = 0
            total = len(self.chunks)
            # Remaining-time estimate (read by the window): only the chunks this run still has
            # to translate count, and only ordinary new chunks give the speed - saved chunks,
            # ones filled from translation memory and earlier trouble chunks would skew it.
            self.eta_left = {c.index for c in self.chunks if c.index not in translations}
            self.eta_trouble_only = bool(self.eta_left) and self.eta_left <= known_trouble
            self.eta_samples = []; self.eta_all = []
            saved_view: list[str] = []
            for i, c in enumerate(self.chunks):
                if c.index not in translations:
                    self._wait_if_paused(raise_on_stop=False)
                if self._stop:
                    self._pause_with_snapshot(db,pid,'사용자가 번역을 중지했습니다. 완료된 청크는 저장되어 있습니다.',True)
                    return
                if c.index in translations:
                    prev = self._roll_context(prev, translations[c.index], context_chars); self.final_context=prev
                    self.progress.emit(i+1, total, '저장된 번역 사용')
                    try:   # resumed job: the live view shows the saved part too (sent in one block below)
                        _parsed=parse_tagged_translation(translations[c.index])
                        saved_view.append('\n'.join(html_line_breaks(_parsed[p.pid].strip()) for p in c.paragraphs if p.pid in _parsed))
                    except Exception:
                        pass
                    continue
                if saved_view:
                    self.chunk_translated.emit(i, total, '\n\n'.join(x for x in saved_view if x.strip()))
                    saved_view=[]
                pending_style=self._pending_style
                if pending_style and pending_style!=self.sentence_style:
                    # 문장 스타일 changed during the run: from this chunk on (a chunk is never
                    # mixed). It is not part of the job identity, so nothing is translated again.
                    self._pending_style=None
                    self.sentence_style=pending_style
                    for _o in self._perf_engines.values():
                        _o.sentence_style=pending_style
                    try:   # the translation memory label follows the style now in use
                        db.get_or_create_project(str(self.source_path), self.profile.source_language, self.profile.id, sha, fingerprint, self._settings_key())
                    except Exception:
                        pass
                    self.style_changes.append((i+1, pending_style))
                    self.issue.emit(f'문장 스타일 변경 · 청크 {i+1}/{total}부터 '+('자연스럽게' if pending_style=='natural' else '보통')
                                    +' 적용 (이미 번역한 앞부분은 다시 번역하지 않음)')
                partial_base=partial_map.get(c.index); work=c
                if partial_base is None and para_memory:
                    partial_base=self._base_from_memory(c, para_memory)
                    if partial_base is not None:
                        self._memory_chunks=getattr(self,'_memory_chunks',0)+1
                self._extra_kept=set(); self._retry_pass=''
                if partial_base is not None:
                    self._chunk_label=f'청크 {i+1}/{total}'; self._chunk_prev=prev
                    self._chunk_requests=0; self._chunk_loops=0; self._fallback_chain=[]
                    kept_now=kept_map.get(c.index, ())
                    if kept_now or self._leftover_pids(c, partial_base) or any(untranslated_residue(p.text, b) for p in c.paragraphs
                                                                              for b in [parse_tagged_translation(partial_base).get(p.pid,'')] if b):
                        try:
                            partial_base, kept_now, handled, self._extra_kept = self._targeted_fixes(c, partial_base, kept_now, engines, prev)
                        except TranslationCancelled:
                            self._pause_with_snapshot(db,pid,'사용자가 번역을 중지했습니다. 완료된 번역을 저장했습니다. 진행 중이던 API 요청의 늦은 응답은 사용하지 않습니다.',True)
                            return
                    else:
                        handled=set()
                    work=self._partial_retry_chunk(c, partial_base, kept_now, handled)
                    if work is None:
                        if self._extra_kept:
                            still=sorted(self._extra_kept)
                            db.set_chunk_status(pid, c.index, 'PARTIAL', translation=partial_base, kept_pids=','.join(map(str,still)), error_type='REFUSAL',
                                                error_message=f'일부 문장 원문 유지 ({len(still)}개 문단: '+', '.join(f'P{x:06d}' for x in still[:6])+')', prompt_version=PROMPT_VERSION)
                        else:
                            db.set_chunk_status(pid, c.index, 'COMPLETED', translation=partial_base, error_type=None, error_message=None, prompt_version=PROMPT_VERSION)
                        translations[c.index]=partial_base; prev=self._roll_context(prev, partial_base, context_chars); self.final_context=prev
                        try:   # the live view shows this saved chunk too
                            _parsed=parse_tagged_translation(partial_base)
                            saved_view.append('\n'.join(html_line_breaks(_parsed[p.pid].strip()) for p in c.paragraphs if p.pid in _parsed))
                        except Exception:
                            pass
                        self.eta_left.discard(c.index)
                        self.progress.emit(i+1, total, '저장된 번역 사용'); continue
                self._chunk_exclusions=[]; self._chunk_fallback_used=False; self._fallback_chain=[]; self._chunk_requests=0; self._chunk_loops=0
                self._pending_traces=[]; self._chunk_label=f'청크 {i+1}/{total}'; self._chunk_prev=prev
                chunk_started=time.monotonic(); self._chunk_paused_base=self._paused_seconds
                # Label the engine that will actually be asked first (engine order, skipping one that is down).
                request_provider=next((n for n,o in engines if n not in getattr(self,'_down',{}) and self._ready_now(o)), engines[0][0]); request_model=self._model_for(request_provider)
                if partial_base is None:   # a partial chunk stays PARTIAL (its translated paragraphs survive a crash)
                    db.set_chunk_status(pid, c.index, 'REQUESTING', provider=request_provider, model=request_model, prompt_version=PROMPT_VERSION)
                self.api_state.emit(f'{self._label(request_provider)} · {request_model} · 청크 {i+1}/{total} 요청 중')
                self._retry_pass = prior_fail.get(c.index, '')
                try:
                    text, key_id, retries, used_provider, used_model, fallback_used = self._translate_with_engines(engines, work, prev)
                    if partial_base is not None and work is not c:
                        text=self._merge_partial(c, partial_base, text)
                        self.issue.emit(f'청크 {i+1}/{total} · 이전 번역은 그대로 쓰고 아직 번역되지 않은 {len(work.paragraphs)}문단만 요청')
                except TranslationCancelled:
                    if partial_base is None:
                        db.set_chunk_status(pid,c.index,'PENDING',error_type=None,error_message=None)
                    self._pause_with_snapshot(db,pid,'사용자가 번역을 중지했습니다. 완료된 번역을 저장했습니다. 진행 중이던 API 요청의 늦은 응답은 사용하지 않습니다.',True)
                    return
                except ProviderError as e:
                    self._emit_new_disabled_keys('gemini',keys,disabled_emitted); self._emit_new_disabled_keys('openai',openai_keys,disabled_emitted)
                    failed_provider=getattr(e,'provider_id','gemini'); failed_model=self._model_for(failed_provider)
                    if partial_base is not None:
                        # Keep the earlier partial result; never throw away translated paragraphs.
                        still=sorted(set(kept_map.get(c.index, ())) | {p.pid for p in work.paragraphs})
                        db.set_chunk_status(pid, c.index, 'PARTIAL', translation=partial_base, kept_pids=','.join(map(str,still)),
                                            error_type=e.kind.value, error_message=str(e)[:900], prompt_version=PROMPT_VERSION)
                    else:
                        db.set_chunk_status(pid, c.index, 'FAILED', provider=failed_provider, model=failed_model,
                                            error_type=e.kind.value, error_message=str(e), prompt_version=PROMPT_VERSION)
                    detail=str(e).replace('\n',' ')[:500]
                    elapsed=self._note_chunk_time(chunk_started)
                    self._eta_note(c.index, elapsed, partial_base is None and c.index not in known_trouble)
                    self.issue.emit(f'청크 {i+1}/{total} · 실패 · {elapsed:.1f}초')
                    trail=getattr(e,'trail',None)
                    if trail and len(trail)>1:
                        self.issue.emit(f'청크 {i+1}/{total} · 청크 전체 · '+' → '.join(trail)+' → 원문 유지 ✗ · '+detail[:200])
                    else:
                        self.issue.emit(f'청크 {i+1}/{total} · {self._label(failed_provider)} {self._kind_ko(e)} · {failed_model} · {detail} · 원문 유지 후 계속 ✗')
                    self.api_state.emit(f'{failed_provider} · {failed_model} · {e.kind.value} · {detail[:120]}')
                    self.progress.emit(i+1, total, f'실패: {e.kind.value} · 원문 유지')
                    if e.kind in {ErrorKind.AUTH, ErrorKind.KEY_BLOCKED}:
                        failed_provider=getattr(e,'provider_id','gemini')
                        if failed_provider=='gemini' and not any(k.enabled for k in keys) and openai_orch is None:
                            self._pause_with_snapshot(db,pid,'사용 가능한 Gemini API 키가 모두 인증 실패/차단 상태입니다. 키를 수정하거나 OpenAI fallback을 설정한 뒤 이어서 번역할 수 있습니다.',kind='AUTH')
                            return
                        if failed_provider=='openai' and not any(k.enabled for k in openai_keys):
                            primary_kind=getattr(e,'primary_kind',None)
                            if orch is None or primary_kind in {ErrorKind.AUTH,ErrorKind.KEY_BLOCKED,ErrorKind.QUOTA,ErrorKind.RATE_LIMIT,ErrorKind.TRANSIENT}:
                                self._pause_with_snapshot(db,pid,'OpenAI 사용 가능 키가 모두 인증 실패/차단되었습니다. API 설정을 확인한 뒤 이어서 번역하세요.',kind='AUTH')
                                return
                    if e.kind in {ErrorKind.QUOTA, ErrorKind.RATE_LIMIT}:
                        self._pause_with_snapshot(db,pid,f'{e.kind.value} 상태가 재시도 후에도 계속되어 작업을 안전 일시정지했습니다. 제한이 풀린 뒤 이어서 번역할 수 있습니다.',kind=e.kind.value)
                        return
                    if e.kind == ErrorKind.FATAL:
                        self._pause_with_snapshot(db,pid,'모델 ID·API 요청 형식·프로젝트 접근 권한처럼 다음 청크에서도 반복될 가능성이 높은 오류가 발생해 작업을 즉시 안전 일시정지했습니다. API/모델/프로젝트 설정을 확인한 뒤 이어서 번역하세요.',kind='FATAL')
                        return
                    if e.kind == ErrorKind.FORMAT and c.index in known_trouble:
                        # Same text failed before: content-specific. Neither counts nor resets.
                        pass
                    elif e.kind in {ErrorKind.TRANSIENT, ErrorKind.FORMAT}:
                        consecutive_infra_failures += 1
                        if consecutive_infra_failures >= 3:
                            self._pause_with_snapshot(db,pid,'연속적인 네트워크/응답 형식 오류가 발생해 대량 실패를 막기 위해 작업을 안전 일시정지했습니다.',kind='TRANSIENT')
                            return
                    else:
                        # Content-specific refusal does not trip the infrastructure circuit breaker.
                        consecutive_infra_failures = 0
                    continue
                self._emit_new_disabled_keys('gemini',keys,disabled_emitted); self._emit_new_disabled_keys('openai',openai_keys,disabled_emitted)
                exclusions=list(self._chunk_exclusions)
                if getattr(self,'_extra_kept',None) and not exclusions:
                    exclusions=[(sorted(self._extra_kept),'REFUSAL','일부 문장 원문 유지',[])]
                elif getattr(self,'_extra_kept',None):
                    exclusions.append((sorted(self._extra_kept),'REFUSAL','일부 문장 원문 유지',[]))
                context_text=text
                if exclusions:
                    # Partial chunk: translated paragraphs are kept, isolated paragraphs
                    # stay as source. Status PARTIAL keeps it out of the completed set so
                    # the next run retries only this chunk (e.g. with another model).
                    kinds=sorted({k for _,k,_,_ in exclusions})
                    pids=[x for ids,_,_,_ in exclusions for x in ids]
                    upids=sorted(set(pids))
                    summary=f'일부 문단 원문 유지 ({len(upids)}개: '+', '.join(f'P{x:06d}' for x in upids[:6])+(' …' if len(upids)>6 else '')+') · '+' / '.join(m for _,_,m,_ in exclusions[:3])
                    db.set_chunk_status(pid, c.index, 'PARTIAL', translation=text, provider=used_provider, model=used_model, kept_pids=','.join(map(str,upids)),
                                        error_type='/'.join(kinds), error_message=summary[:900], retry_count=retries, fallback_used=fallback_used, prompt_version=PROMPT_VERSION)
                    for part,trail in self._pending_traces:
                        self._trace(part, trail, ok=False)
                    uniq=sorted(set(pids))
                    self.issue.emit(f'청크 {i+1}/{total} · 부분 완료 · 원문 유지 {len(uniq)}문단('+', '.join(f'P{x:06d}' for x in uniq[:6])+(' …' if len(uniq)>6 else '')+') · 나머지 번역 완료')
                    # Never carry untranslated/blocked source into the next request's
                    # "previous Korean context"; it would re-trigger the same block.
                    for _,_,_,bodies in exclusions:
                        for body in bodies:
                            if body: context_text=context_text.replace(body,'')
                else:
                    db.set_chunk_status(pid, c.index, 'COMPLETED', translation=text, provider=used_provider, model=used_model,
                                        error_type=None, error_message=None, retry_count=retries, fallback_used=fallback_used, prompt_version=PROMPT_VERSION)
                translations[c.index] = text; prev = self._roll_context(prev, context_text, context_chars); self.final_context=prev; consecutive_infra_failures = 0
                try:   # live view: just this chunk's Korean, in paragraph order
                    _parsed=parse_tagged_translation(text)
                    self.chunk_translated.emit(i+1, total, '\n'.join(html_line_breaks(_parsed[p.pid].strip()) for p in c.paragraphs if p.pid in _parsed))
                except Exception:
                    pass
                self.provider_chunks[used_provider]=self.provider_chunks.get(used_provider,0)+1
                elapsed=self._note_chunk_time(chunk_started)
                self._eta_note(c.index, elapsed, partial_base is None and c.index not in known_trouble)
                self.issue.emit(f'청크 {i+1}/{total} · '+('부분 완료' if exclusions else '완료')+f' · {elapsed:.1f}초' )
                usage=self.usage_summary()
                backup_note=' · 막힌 문단은 보조 엔진이 처리' if self._chunk_fallback_used else ''
                self.api_state.emit(f'{self._label(used_provider)} · {used_model} · {(self._gemini_key_description(key_id) if used_provider=="gemini" else key_id)} · 정상{backup_note}' + (f' · {usage.split(" · ")[0]}' if usage else ''))
                self.progress.emit(i+1, total, (f'부분 완료 · {used_provider} · {key_id}' if exclusions else f'완료 · {used_provider} · {key_id}'))

            if self._stop:
                self._pause_with_snapshot(db,pid,'사용자가 번역을 중지했습니다. 완료된 번역을 저장했습니다.',True)
                return
            if saved_view:   # saved chunks after the last new one: shown before the full result replaces the view
                self.chunk_translated.emit(total, total, '\n\n'.join(x for x in saved_view if x.strip()))
                saved_view=[]
            out,failed,report,output_dir,preferred_dir=self._write_snapshot(db,pid)
            fallback_note='' if output_dir==preferred_dir else f'\n원본 폴더에 쓸 수 없어 안전 저장 폴더를 사용했습니다: {output_dir}'
            summary=self.run_summary()
            if summary:
                self.issue.emit('작업 요약 · '+summary.replace('\n',' · '))
            left=[p for c in self.chunks if c.index in translations for p in sorted(self._leftover_pids(c, translations[c.index]))]
            self.leftover_count=len(left)   # read by the window to offer / run one retry of the untranslated parts
            if left:
                self.issue.emit(f'한자·가나가 남은 문단 {len(left)}개 ('+', '.join(f'P{x:06d}' for x in left[:8])+(' …' if len(left)>8 else '')+
                                ') · 그 문단만 다시 번역할 수 있습니다')
                fallback_note += '\n'+summary
            if failed:
                self.result_ready.emit('partial',str(out),len(failed))
                self.finished_ok.emit(f'{out}\n미완료 청크: {len(failed)} · 자세한 목록은 미완료 목록 탭{fallback_note}')
            else:
                self.result_ready.emit('completed',str(out),0)
                self.finished_ok.emit(f'{out}\n모든 청크 번역 완료{fallback_note}')
        except Exception as e:
            self.failed.emit(str(e))
