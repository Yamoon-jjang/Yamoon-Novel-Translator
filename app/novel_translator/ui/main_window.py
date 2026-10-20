from __future__ import annotations
from pathlib import Path
import hashlib, os, re, subprocess, sys, html, time
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, QEvent
from PySide6.QtWidgets import (
    QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QComboBox,QFileDialog,
    QPlainTextEdit,QProgressBar,QFrame,QMessageBox,QTabWidget,QTableWidget,QTableWidgetItem,
    QHeaderView,QCheckBox,QFormLayout,QAbstractItemView,QGridLayout,QSizePolicy,
    QAbstractButton,QTabBar,QDialog
)
from ..profiles import ProfileManager
from ..textio import decode_txt_bytes, atomic_write_text, directory_writable, read_txt_sample, read_source_file
from ..source_formats import document_kind, SourceTextError, NoTextError, document_sample_text
from ..rendering import output_stem
from ..chunking import split_paragraphs,make_chunks,mark_translatable
from ..secure_store import SecretStore
from ..config import DATA_DIR, CONFIG_DIR, load_json, save_json
from ..storage import Database
from ..api_keys import normalize_key_records, normalize_secret_payload
from .api_dialog import ApiDialog, initial_engine_order, ENGINE_LABELS, LocalModelsThread, ClickModelCombo
from .advanced_dialog import AdvancedSettingsDialog
from ..translation_settings import canonical_kind, CONTENT_LABELS, default_settings
from .worker import TranslationWorker
from ..batch import discover_txt_files, output_dir_for, should_continue_after_result, SOURCE_SUFFIXES
from ..workspace import WorkspaceStore
from ..language_options import option_spec
from ..language_detection import detect_language, detect_language_bytes
from ..power import set_translation_awake
from ..protected_literals import count_protectable_literals
from ..term_extraction import candidates as term_candidates, build_prompts as term_prompts, parse_glossary
from ..language_options import prompt_instructions


def _make_provider(provider_id, endpoint='', local_options=None):
    local_options=dict(local_options or {})
    if provider_id=='gemini':
        from ..providers.gemini import GeminiProvider; return GeminiProvider()
    if provider_id=='local':
        from ..providers.local_llm import LocalLLMProvider
        # Same local_llm settings as the translation worker (structured output, sampling).
        try:
            cfg=dict(load_json(CONFIG_DIR/'app.json').get('local_llm') or {})
        except Exception:
            cfg={}
        return LocalLLMProvider(endpoint or None, int(local_options.get('timeout_seconds') or 300),
                                think=bool(local_options.get('think',False)), num_ctx=local_options.get('num_ctx'),
                                structured=bool(cfg.get('structured_output', True)),
                                sampling=str(cfg.get('sampling') or 'recommended'))
    from ..providers.openai import OpenAIProvider; return OpenAIProvider(endpoint or None)


def _call_until_cancelled(provider, request, secret, cancelled):
    """Run one analysis request in a helper thread so a cancel (or "skip") takes effect at
    once instead of after the request ends. The request carries the same check, so a local
    LLM stream closes and Ollama stops generating; a cloud reply that arrives later is
    simply dropped. Raises TranslationCancelled when cancelled."""
    import threading
    from ..providers.base import TranslationCancelled
    if cancelled():
        raise TranslationCancelled('사용자 취소')
    box={}
    def run():
        try: box['resp']=provider.translate(request,secret)
        except BaseException as e: box['err']=e
    t=threading.Thread(target=run,name='analysis-request',daemon=True); t.start()
    while t.is_alive():
        t.join(0.1)
        if t.is_alive() and cancelled():
            raise TranslationCancelled('사용자 취소')
    if 'err' in box:
        raise box['err']
    return box['resp']


class SpeechExtractThread(QThread):
    """Prefer one full-context analysis; use split excerpts only if it fails.

    Conflicting proposals for the same directed pair are withheld for review,
    never silently resolved by whichever batch happened to run first.
    """
    done = Signal(bool, object)
    stage = Signal(str)   # what is being asked right now (shown with the elapsed time)
    def __init__(self, engines, prompts, parse, parent=None, fallback_prompts=None):
        super().__init__(parent)
        self.engines=list(engines)
        self.prompts=list(prompts)
        self.fallback_prompts=list(fallback_prompts or [])
        self.parse=parse

    def requestInterruption(self):
        self._cancel_requested=True
        super().requestInterruption()

    def _cancelled(self):
        return getattr(self,'_cancel_requested',False) or self.isInterruptionRequested()

    def run(self):
        from ..providers.base import TranslationRequest
        from ..speech_levels import MAX_ROWS
        errors=[]; used=set(); requests=0

        def analyse(system, user, what):
            nonlocal requests
            for provider_id,secret,model,endpoint,opts in self.engines:
                if self._cancelled():
                    return None
                try:
                    requests+=1
                    label=ENGINE_LABELS.get(provider_id,provider_id)
                    self.stage.emit(f'{what} · {label} 요청 중 (AI 요청 {requests}회째)'
                                    +(' · 앞 엔진: '+errors[-1] if errors else ''))
                    resp=_call_until_cancelled(_make_provider(provider_id,endpoint,opts),
                        TranslationRequest(model,system,user,timeout=40,purpose='analysis',cancel=self._cancelled),
                        secret,self._cancelled)
                    if self._cancelled():
                        return None
                    parsed=self.parse(resp.text)
                except Exception as e:
                    if self._cancelled():
                        return None
                    kind=getattr(getattr(e,'kind',None),'value','') or type(e).__name__
                    errors.append(f'{ENGINE_LABELS.get(provider_id,provider_id)} {kind}')
                    continue
                if parsed:
                    used.add(provider_id)
                    return parsed
                errors.append(f'{ENGINE_LABELS.get(provider_id,provider_id)} 유효한 인물 관계 0쌍')
            return []

        # One 6,000-character sample supplies a coherent view of all the evidence.
        for system,user in self.prompts:
            result=analyse(system,user,'전체 대사 분석')
            if result is None:
                self.done.emit(False,'사용자 취소'); return
            if result:
                self.done.emit(True,{'rows':result[:MAX_ROWS], 'parts':1,'failed':0,
                                     'errors':errors,'used':sorted(used),
                                     'requests':requests,'conflicts':0})
                return

        # A failed/empty full sample alone is not sufficient evidence; split only now.
        if not self.fallback_prompts:
            self.done.emit(False,' · '.join(dict.fromkeys(errors)) or '유효한 인물 관계 없음')
            return
        grouped={}; conflicts=set(); failed=0
        for part_no,(system,user) in enumerate(self.fallback_prompts,1):
            if self._cancelled():
                self.done.emit(False,'사용자 취소'); return
            result=analyse(system,user,f'나눠서 분석 {part_no}/{len(self.fallback_prompts)}묶음')
            if result is None:
                self.done.emit(False,'사용자 취소'); return
            if not result:
                failed+=1
                continue
            for row in result:
                key=(row['speaker'],row['listener'])
                if key not in grouped:
                    grouped[key]=row
                elif (grouped[key]['level'] != row['level']
                      or grouped[key].get('note','') != row.get('note','')):
                    # A relation note can differ in wording even when the level agrees.
                    # Only conflicting speech levels are uncertain.
                    if grouped[key]['level'] != row['level']:
                        conflicts.add(key)
        rows=[row for key,row in grouped.items() if key not in conflicts][:MAX_ROWS]
        if not rows:
            self.done.emit(False,' · '.join(dict.fromkeys(errors)) or '확실한 인물 관계 없음')
            return
        self.done.emit(True,{'rows':rows,'parts':len(self.fallback_prompts),
                             'failed':failed,'errors':errors,'used':sorted(used),
                             'requests':requests,'conflicts':len(conflicts)})


class TermExtractThread(QThread):
    """Try configured analysis models in order without changing translation settings."""
    done = Signal(bool, object)
    stage = Signal(str)
    def __init__(self, provider_id, secret, model, endpoint, system, user, cands, parent=None, local_options=None, parse=None, engines=None):
        super().__init__(parent)
        self.engines=list(engines or [(provider_id,secret,model,endpoint,local_options)])
        self.system=system; self.user=user; self.cands=cands; self.parse=parse
        self.known={}   # current glossary (set by the window): its names are bases for the name-phrase guard
    def requestInterruption(self):
        self._cancel_requested=True
        super().requestInterruption()

    def _cancelled(self):
        return getattr(self,'_cancel_requested',False) or self.isInterruptionRequested()

    def run(self):
        from ..providers.base import TranslationRequest
        errors=[]
        for provider_id,secret,model,endpoint,options in self.engines:
            if self._cancelled():
                self.done.emit(False,'사용자 취소'); return
            try:
                provider=_make_provider(provider_id,endpoint,options)
                self.stage.emit(f'{ENGINE_LABELS.get(provider_id,provider_id)} 요청 중'+(' · 앞 엔진: '+errors[-1][:60] if errors else ''))
                resp=_call_until_cancelled(provider,
                    TranslationRequest(model,self.system,self.user,timeout=60,purpose='json',cancel=self._cancelled),
                    secret,self._cancelled)
                if self._cancelled():
                    self.done.emit(False,'사용자 취소'); return
                result=self.parse(resp.text) if self.parse else parse_glossary(resp.text,self.cands,strict=True,known=self.known)
                if result:
                    self.done.emit(True,result); return
                errors.append(f'{ENGINE_LABELS.get(provider_id,provider_id)} 유효한 용어 0개')
            except Exception as e:
                if self._cancelled():
                    self.done.emit(False,'사용자 취소'); return
                kind=getattr(getattr(e,'kind',None),'value','')
                errors.append(f'{ENGINE_LABELS.get(provider_id,provider_id)} '+(kind+' · ' if kind else '')+str(e)[:120])
        self.done.emit(False,'사용자 취소' if self._cancelled() else (' · '.join(errors) or '응답 없음'))


class ElidedLabel(QLabel):
    def __init__(self,text='',parent=None):
        super().__init__('',parent)
        self._full_text=text
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.setText(text)
    def setText(self,text):
        self._full_text=str(text);self.setToolTip(self._full_text)
        super().setText(self.fontMetrics().elidedText(self._full_text,Qt.ElideMiddle,max(1,self.width())))
    def text(self):return self._full_text
    def resizeEvent(self,event):
        super().resizeEvent(event);self.setText(self._full_text)

class NoChainTextEdit(QPlainTextEdit):
    """A text box inside a scrolling tab: the mouse wheel scrolls only the box, never the
    whole tab when the box reaches its end (which hid the options above it)."""
    def wheelEvent(self, event):
        super().wheelEvent(event); event.accept()


class WheelGuard(QObject):
    """A combo box in a scrolling tab ignores the wheel unless it has focus, so scrolling
    the tab never changes a setting by accident (the wheel goes on to scroll the tab)."""
    def eventFilter(self, obj, event):
        if event.type()==QEvent.Wheel and isinstance(obj, QComboBox) and not obj.hasFocus():
            event.ignore(); return True
        return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        from .. import APP_NAME, VERSION_LABEL
        from .about_dialog import app_icon
        self.setWindowTitle(f'{APP_NAME} {VERSION_LABEL}'); self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)   # drop a TXT file or a folder anywhere on the window
        self.resize(1120,780)
        self.pm=ProfileManager()
        if getattr(self.pm,'load_errors',None):
            QTimer.singleShot(0,lambda: QMessageBox.warning(self,'언어 프로필 읽기 실패',
                'profiles 폴더의 다음 파일을 읽지 못해 그 언어는 이번에 쓸 수 없습니다. 고친 곳의 쉼표·따옴표·괄호를 확인하거나, '
                '내려받은 압축 파일의 원래 파일로 되돌리세요.\n\n'+'\n'.join(self.pm.load_errors)+
                '\n\n번역 방식을 바꾸고 싶다면 파일을 고치는 대신 언어별 설정 탭의 "내 번역 지침"을 쓰세요.'))
        self.content_kind=canonical_kind(load_json(CONFIG_DIR/'app.json').get('content_kind'))
        self.source_path: Path|None=None
        self.source_text=''; self.source_bytes=b''; self.paragraphs=[]; self.chunks=[]
        self.input_mode='none'              # none | single | folder
        self.batch_root: Path|None=None
        self.batch_files: list[Path]=[]
        self.batch_index=-1
        self.batch_results: dict[str,tuple[str,str,int]]={}
        self.batch_context=''
        self.batch_excluded: set[str]=set()
        self.batch_selection: list[Path]|None=None   # several TXT files dropped together (instead of a whole folder)
        # One output folder for every result of the app (not per book): saved app-wide.
        self.custom_output_dir: Path|None=self._load_output_setting()
        self._output_fallback: Path|None=None   # safe folder for one run when the output folder is not writable
        self.last_result_path: Path|None=None
        self.workspace_store=WorkspaceStore(DATA_DIR/'workspaces.json')
        self.secret_store=SecretStore(DATA_DIR/'secrets.bin')
        _loaded_secrets=self.secret_store.load()
        self.secrets=normalize_secret_payload(_loaded_secrets)
        if self.secrets != _loaded_secrets and not self.secret_store.last_error:
            try:
                self.secret_store.save(self.secrets)
            except Exception:
                pass
        self.worker=None; self._close_after_worker=False; self._stop_batch=False; self._completion_message=''; self._last_worker_status=''; self._force_restart=False
        # Unattended run: temporary pauses are resumed automatically, hopeless files are skipped.
        self._pending_pause=''; self._auto_resumes={}; self._resume_left=0; self._resume_reason=''
        self._batch_retried=set(); self._batch_retry_pending=False; self._last_leftover=0; self._last_failed=0; self._retry_rounds=0
        self._resume_timer=QTimer(self); self._resume_timer.setInterval(1000); self._resume_timer.timeout.connect(self._resume_tick)
        self._term_cache={}
        self._build_ui(); self._apply_interactive_cursors(); self._apply_profile(self.pm.active.id); self._update_ready_state()
        if self.secret_store.last_error:
            backup=f'\n보존 사본: {self.secret_store.recovered_corrupt_path}' if self.secret_store.recovered_corrupt_path else ''
            QMessageBox.warning(self,'API 키 저장소 읽기 실패','기존 API 키 저장소를 읽지 못했습니다. 파일은 삭제하지 않았습니다.\nAPI 설정에서 키를 다시 확인해 주세요.'+backup)

    def _apply_interactive_cursors(self):
        """Only actual UI controls (never labels/text) receive the hand pointer.

        Qt restores the disabled appearance and suppresses clicking itself; do
        not give disabled controls a misleading clickable cursor.
        """
        for control in self.findChildren(QAbstractButton):
            control.setCursor(Qt.PointingHandCursor)
        for control in self.findChildren(QComboBox):
            control.setCursor(Qt.PointingHandCursor)
        for tabs in self.findChildren(QTabBar):
            tabs.setCursor(Qt.PointingHandCursor)

    def _build_ui(self):
        root=QWidget(); self.setCentralWidget(root)
        layout=QVBoxLayout(root); layout.setContentsMargins(12,10,12,10); layout.setSpacing(7)
        top=QHBoxLayout(); title_box=QVBoxLayout()
        title=QLabel('야문 Novel Translator'); title.setStyleSheet('font-size:18px;font-weight:700;')
        sub=QLabel('중국어 · 일본어 · 영어 TXT·PDF·EPUB 소설을 한국어로 순차 번역'); sub.setStyleSheet('color:#6b7280;')
        title.setToolTip(sub.text()); title_box.addWidget(title); top.addLayout(title_box); top.addStretch(1)
        self.health_btn=QPushButton('환경 점검'); self.health_btn.setToolTip('설정·저장 폴더·SQLite와 API 키 저장소(DPAPI)를 실제 API 호출 없이 검사합니다.')
        self.settings_toggle=QPushButton('설정 접기'); self.settings_toggle.setCheckable(True); self.settings_toggle.setChecked(True); self.settings_toggle.setStyleSheet('QPushButton,QPushButton:checked{background:#f8fafc;color:#334155;border:1px solid #d1d5db;border-radius:5px;} QPushButton:hover{background:#eef2f7;}'); self.settings_toggle.toggled.connect(self._toggle_settings); top.addWidget(self.settings_toggle)
        self.health_btn.clicked.connect(self._run_environment_check); top.addWidget(self.health_btn)
        self.advanced_btn=QPushButton('고급 설정'); self.advanced_btn.clicked.connect(self.open_advanced); top.addWidget(self.advanced_btn)
        self.api_btn=QPushButton('API 설정'); self.api_btn.clicked.connect(self.open_api); top.addWidget(self.api_btn)
        self.refinement_run_btn=QPushButton('한국어 소설 재정비'); self.refinement_run_btn.setEnabled(True); self.refinement_run_btn.setToolTip('한국어 TXT 파일을 선택해 문학적 윤문을 실행합니다. 번역 원문 선택이나 번역 엔진 준비 상태와 무관하게 열 수 있습니다.'); self.refinement_run_btn.clicked.connect(self.open_refinement_run); top.addWidget(self.refinement_run_btn)
        self.manual_btn=QPushButton('사용 설명서'); self.manual_btn.setToolTip('사용 설명서를 웹 브라우저로 엽니다.'); self.manual_btn.clicked.connect(self.open_manual); top.addWidget(self.manual_btn)
        self.about_btn=QPushButton('정보'); self.about_btn.clicked.connect(self.open_about); top.addWidget(self.about_btn)
        layout.addLayout(top)

        # No modal wizard: show a small, actionable guide only until an engine is ready.
        self.first_use_panel=QFrame()
        self.first_use_panel.setObjectName('firstUsePanel')
        self.first_use_panel.setStyleSheet('QFrame#firstUsePanel{background:#eff6ff;border:1px solid #bfdbfe;border-radius:7px;}')
        first_layout=QVBoxLayout(self.first_use_panel)
        first_label=QLabel('처음 사용하시나요? AI 서비스 하나만 연결하면 바로 번역할 수 있습니다. 나머지는 필요할 때 설정하세요.')
        first_label.setWordWrap(True)
        first_layout.addWidget(first_label)
        first_row=QHBoxLayout()
        for title, provider in (('Gemini 키 연결','gemini'),('OpenAI 키 연결','openai'),('로컬 LLM 연결','local')):
            btn=QPushButton(title)
            btn.clicked.connect(lambda _checked=False, kind=provider: self.open_api(kind))
            first_row.addWidget(btn)
        first_row.addStretch(1)
        first_layout.addLayout(first_row)
        first_note=QLabel('성인 소설 등 일부 내용은 온라인 AI가 번역을 거부할 수 있습니다. Ollama 설치·모델 등록은 사용 설명서 11-1절에 순서대로 안내되어 있습니다. 로컬 LLM 설치는 선택 사항입니다.')
        first_note.setWordWrap(True)
        first_layout.addWidget(first_note)
        first_manual=QPushButton('Ollama 설치 설명서 보기')
        first_manual.clicked.connect(self.open_local_setup_manual)
        first_row.addWidget(first_manual)
        layout.addWidget(self.first_use_panel)

        self.settings_area=QWidget(); self.settings_area.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        settings_layout=QVBoxLayout(self.settings_area); settings_layout.setContentsMargins(0,0,0,0); settings_layout.setSpacing(6); layout.addWidget(self.settings_area)
        input_box=QFrame(); input_box.setFrameShape(QFrame.StyledPanel); ib=QGridLayout(input_box)
        ib.setHorizontalSpacing(8); ib.setVerticalSpacing(7); ib.setColumnStretch(1,1)
        def _cap(text):
            lab=QLabel(text); lab.setStyleSheet('color:#475569;font-weight:600;'); lab.setMinimumWidth(64); return lab
        card_css='padding:6px 10px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;color:#1f2937;'
        # Row 0: where the source comes from
        row=QHBoxLayout(); row.setSpacing(6)
        self.single_btn=QPushButton('TXT·PDF·EPUB 파일 선택'); self.single_btn.clicked.connect(self.open_txt)
        self.folder_btn=QPushButton('폴더 선택'); self.folder_btn.setToolTip('폴더 안의 TXT·PDF·EPUB 파일을 한 파일씩 차례로 번역합니다.'); self.folder_btn.clicked.connect(self.open_folder)
        self.paste_hint=QLabel('또는 TXT·PDF·EPUB 파일·폴더 끌어다 놓기 · 글 붙여넣기 (Ctrl+V)'); self.paste_hint.setStyleSheet('color:#6b7280;')
        row.addWidget(self.single_btn); row.addWidget(self.folder_btn); row.addSpacing(6); row.addWidget(self.paste_hint); row.addStretch(1)
        self.reset_settings_btn=QPushButton('설정 초기화'); self.reset_settings_btn.setToolTip('API 설정(엔진·키·모델)과 원문·번역 파일을 보존하고, 앱 옵션 및 현재 작업의 메모·확정 용어를 기본값으로 되돌립니다.'); self.reset_settings_btn.clicked.connect(self._reset_settings); row.addWidget(self.reset_settings_btn)
        ib.addWidget(_cap('번역할 원문'),0,0); ib.addLayout(row,0,1)
        # Row 1: what is selected now
        self.file_label=ElidedLabel('아직 원문을 선택하지 않았습니다.'); self.file_label.setStyleSheet(card_css)
        ib.addWidget(_cap('선택한 원문'),1,0); ib.addWidget(self.file_label,1,1)
        # Row 2: folder options (only shown for a folder job)
        self.folder_opts_widget=QWidget(); folder_opts=QHBoxLayout(self.folder_opts_widget); folder_opts.setContentsMargins(0,0,0,0)
        self.include_sub=QCheckBox('하위 폴더 포함'); self.include_sub.stateChanged.connect(self._rescan_folder_if_needed); self.include_sub.setEnabled(False); folder_opts.addWidget(self.include_sub)
        folder_opts.addSpacing(12); folder_opts.addWidget(QLabel('파일 관계')); self.batch_relation=QComboBox(); self.batch_relation.addItem('각 파일 독립 작품 (기본)','independent'); self.batch_relation.addItem('한 작품의 연속 파일','continuous'); self.batch_relation.currentIndexChanged.connect(lambda _=None: (self._update_batch_memory_mode(), self._save_workspace())); self.batch_relation.setEnabled(False); folder_opts.addWidget(self.batch_relation); folder_opts.addStretch(1)
        self.folder_opts_caption=_cap('폴더 옵션')
        ib.addWidget(self.folder_opts_caption,2,0); ib.addWidget(self.folder_opts_widget,2,1)
        # Row 3: where results go
        outrow=QHBoxLayout(); outrow.setSpacing(6)
        self.output_label=ElidedLabel('원문 선택 후 자동 결정됩니다.'); self.output_label.setStyleSheet(card_css); outrow.addWidget(self.output_label,1)
        self.out_btn=QPushButton('출력 폴더 지정'); self.out_btn.clicked.connect(self.choose_output_dir)
        self.out_reset=QPushButton('기본값'); self.out_reset.clicked.connect(self.reset_output_dir)
        outrow.addWidget(self.out_btn); outrow.addWidget(self.out_reset)
        ib.addWidget(_cap('결과 저장'),3,0); ib.addLayout(outrow,3,1)
        self._update_folder_opts_visibility()
        settings_layout.addWidget(input_box)

        controls=QFrame(); controls.setFrameShape(QFrame.StyledPanel); c=QGridLayout(controls)
        c.addWidget(QLabel('원문 언어'),0,0)
        self.lang=QComboBox()
        # Per-option help is visible in the dropdown and when the box is closed.
        language_help = {
            'zh-ko': '중국어 → 한국어\n인명·호칭·성어를 고려한 소설 번역. 확정 용어집과 말투표를 적용합니다.\n용어·말투 검출 엔진은 본문 번역 엔진과 독립적으로 지정할 수 있습니다.',
            'ja-ko': '일본어 → 한국어\n이름 읽기, 존칭·경어, 등장인물 관계를 고려해 번역합니다.\n인명 표기와 말투를 먼저 확정하면 장편의 일관성 유지에 도움이 됩니다.',
            'en-ko': '영어 → 한국어\n인명, 대명사의 지시 대상, 관용 표현을 자연스러운 한국어로 번역합니다.\n검출 엔진과 본문 번역 엔진은 별도로 선택할 수 있습니다.',
            'ko-refine': '한국어 소설 재정비\n이미 번역된 소설의 이름·용어·말투 불일치와 어색한 문장을 교정합니다.\n윤문과 용어·말투 분석은 로컬 LLM으로 실행합니다(검열 해제 모델 권장).',
        }
        for p in self.pm.all():
            self.lang.addItem(p.display_name,p.id)
            hint=language_help.get(p.id,'')
            if hint: self.lang.setItemData(self.lang.count()-1,hint,Qt.ToolTipRole)
        self.lang.setToolTip(self.lang.currentData(Qt.ToolTipRole) or '')
        def _on_lang_selected():
            self.lang.setToolTip(self.lang.currentData(Qt.ToolTipRole) or '')
            single=self.input_mode=='single' and bool(self.source_bytes)
            if single: self._lang_chosen=False   # the language left behind is no longer this book's choice
            self._apply_profile(self.lang.currentData())
            if self.input_mode=='single' and self.source_bytes:
                # Only a language the user picks here locks this book against automatic detection.
                self._lang_chosen=True; self._save_workspace()
        self.lang.currentIndexChanged.connect(_on_lang_selected)
        c.addWidget(self.lang,0,1); self.model_caption=QLabel('Gemini 모델'); c.addWidget(self.model_caption,0,2)
        self.model=ClickModelCombo(); self.model.setEditable(True)
        try:
            model_cfg=load_json(CONFIG_DIR/'models.json')
            for m in model_cfg.get('models',[]):
                if m.get('provider')=='gemini' and m.get('enabled',True): self.model.addItem(m.get('display_name') or m['id'],m['id'])
        except Exception: pass
        if self.model.count()==0: self.model.addItem('Gemini 3.1 Flash Lite','gemini-3.1-flash-lite')
        # The model box follows the main (first checked) engine: Gemini uses self.model,
        # OpenAI / local LLM use self.alt_model, whose choice is saved to that engine's settings.
        self.alt_model=ClickModelCombo(); self.alt_model.setEditable(True); self.alt_model.setInsertPolicy(QComboBox.NoInsert); self.alt_model.hide()
        self._alt_engine=''; self._local_model_names={}; self._local_models_thread=None
        self.alt_model.activated.connect(lambda _i: self._alt_model_changed())
        self.alt_model.lineEdit().editingFinished.connect(self._alt_model_changed)
        model_box=QWidget(); mbl=QHBoxLayout(model_box); mbl.setContentsMargins(0,0,0,0); mbl.setSpacing(0)
        mbl.addWidget(self.model,1); mbl.addWidget(self.alt_model,1)
        c.addWidget(model_box,0,3); c.addWidget(QLabel('번역 방식'),1,0)
        self.mode=QComboBox(); self.mode.addItem('균형 (권장)','balanced'); self.mode.addItem('품질 우선','quality'); self.mode.addItem('속도 우선','speed')
        self.mode.currentIndexChanged.connect(self._mode_changed); c.addWidget(self.mode,1,1); c.addWidget(QLabel('TXT 인코딩'),1,2)
        self.encoding=QComboBox(); self.encoding.addItem('자동 감지','auto'); self.encoding.addItem('UTF-8','utf-8'); self.encoding.addItem('일본어 CP932 / Shift-JIS','cp932'); self.encoding.addItem('일본어 EUC-JP','euc_jp'); self.encoding.addItem('중국어 GB18030 / GBK','gb18030'); self.encoding.addItem('중국어 번체 Big5 (CP950)','cp950'); self.encoding.addItem('영어 Windows-1252','cp1252'); self.encoding.addItem('UTF-16 LE','utf-16-le'); self.encoding.addItem('UTF-16 BE','utf-16-be')
        self.encoding.currentIndexChanged.connect(self._encoding_changed); c.addWidget(self.encoding,1,3)
        self.profile_hint=QLabel(''); self.profile_hint.setStyleSheet('color:#2563eb;font-weight:600;'); c.addWidget(self.profile_hint,0,4,2,1); c.setColumnStretch(1,1); c.setColumnStretch(3,2)
        settings_layout.addWidget(controls)

        self.policy=QLabel('원문 충실 번역 · 합법적인 성인 표현/욕설/폭력 표현은 원문의 수위와 의미를 유지하며 임의로 순화·생략하지 않음')
        self.policy.setWordWrap(True); self.policy.setStyleSheet('padding:10px;background:#f3f4f6;border-radius:7px;'); self.policy.hide()

        self.tabs=QTabWidget(); self.tabs.setMinimumHeight(260); self.tabs.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding); layout.addWidget(self.tabs,1)
        # The selected tab must stand out (white, bold, dark underline) without the blue button colour.
        self.tabs.setStyleSheet('QTabBar::tab{background:#e9ebef;color:#4b5563;border:1px solid #d1d5db;border-bottom:none;'
                                'padding:5px 12px;margin-right:2px;border-top-left-radius:6px;border-top-right-radius:6px;}'
                                'QTabBar::tab:selected{background:#ffffff;color:#111827;font-weight:bold;border-bottom:2px solid #374151;}'
                                'QTabBar::tab:hover:!selected{background:#dfe2e7;}')
        preview=QWidget(); pv=QVBoxLayout(preview); self.preview_tab=preview
        vrow=QHBoxLayout()
        self.view_source_btn=QPushButton('원문'); self.view_result_btn=QPushButton('번역 결과')
        toggle_style=('QPushButton{background:#ffffff;color:#334155;border:1px solid #d1d5db;border-radius:5px;padding:4px 12px;}'
                      'QPushButton:hover{background:#f1f5f9;}'
                      'QPushButton:checked{background:#e2e8f0;color:#0f172a;border:1px solid #94a3b8;font-weight:bold;}'
                      'QPushButton:disabled{background:#f8fafc;color:#9ca3af;border:1px solid #e5e7eb;}')
        for b in (self.view_source_btn,self.view_result_btn): b.setCheckable(True); b.setStyleSheet(toggle_style)
        self.view_source_btn.setChecked(True); self.view_result_btn.setEnabled(False)
        self.view_source_btn.clicked.connect(lambda:self._show_preview('source'))
        self.view_result_btn.clicked.connect(lambda:self._show_preview('result'))
        self.result_copy_btn=QPushButton('결과 복사'); self.result_copy_btn.clicked.connect(self._copy_result); self.result_copy_btn.setEnabled(False)
        self.result_save_btn=QPushButton('결과 저장'); self.result_save_btn.clicked.connect(self._save_result); self.result_save_btn.setEnabled(False)
        self.preview_hint=QLabel('TXT·PDF·EPUB 파일을 열거나, 이 창에 글을 직접 붙여넣거나 고친 뒤 [번역 시작 / 계속]을 누르면 창의 내용 그대로 번역합니다.')
        self.preview_hint.setStyleSheet('color:#6b7280;')
        for w_ in (self.view_source_btn,self.view_result_btn,self.result_copy_btn,self.result_save_btn): vrow.addWidget(w_)
        vrow.addWidget(self.preview_hint,1); pv.addLayout(vrow)
        self.preview=QPlainTextEdit(); self.preview.setPlaceholderText('TXT·PDF·EPUB 파일을 선택하거나 이 창에 파일·폴더를 끌어다 놓으면 원문이 표시됩니다 (PDF·EPUB는 글자만 꺼내 표시). 파일 없이 번역할 글을 여기에 붙여넣어도 됩니다 (Ctrl+V).')
        self.preview.viewport().installEventFilter(self); self.preview.installEventFilter(self)
        self.preview.textChanged.connect(self._preview_changed)
        pv.addWidget(self.preview); self.tabs.addTab(preview,'원문')
        self._preview_mode='source'; self._preview_baseline=''; self._preview_loading=False
        self._result_text=''; self._paste_active=False

        jobs=QWidget(); jv=QVBoxLayout(jobs)
        help_lbl=QLabel('폴더 작업은 여러 파일을 동시에 번역하지 않습니다. 목록 순서대로 한 파일을 완전히 처리한 뒤 다음 파일로 넘어갑니다.')
        help_lbl.setWordWrap(True); help_lbl.setStyleSheet('color:#4b5563;'); jv.addWidget(help_lbl)
        self.job_table=QTableWidget(0,4); self.job_table.setHorizontalHeaderLabels(['파일','크기','상태','결과'])
        self.job_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.job_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.job_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.job_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.job_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.job_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Stretch); self.job_table.verticalHeader().setVisible(False)
        jv.addWidget(self.job_table)
        job_actions=QHBoxLayout()
        self.batch_exclude_btn=QPushButton('선택 파일 제외'); self.batch_exclude_btn.setToolTip('선택한 파일을 현재 폴더 번역 큐에서만 제외합니다.')
        self.batch_exclude_btn.clicked.connect(self._exclude_selected_batch_files)
        self.batch_rescan_btn=QPushButton('폴더 다시 스캔'); self.batch_rescan_btn.setToolTip('제외 목록을 초기화하고 폴더의 TXT·PDF·EPUB 목록을 다시 만듭니다.')
        self.batch_rescan_btn.clicked.connect(self._rescan_all_batch_files)
        self.batch_exclude_btn.setEnabled(False); self.batch_rescan_btn.setEnabled(False)
        job_actions.addWidget(self.batch_exclude_btn); job_actions.addWidget(self.batch_rescan_btn); job_actions.addStretch(1); jv.addLayout(job_actions)
        self.tabs.addTab(jobs,'작업 목록'); self.jobs_tab=jobs

        errors=QWidget(); ev=QVBoxLayout(errors)
        err_help=QLabel('번역 중 발생한 API 키 거부, Provider 전환, 정책/안전 차단, 재시도 및 미완료 구간을 실시간으로 표시합니다. 실패 구간은 결과 TXT에서 누락하지 않고 원문 그대로 유지됩니다.')
        err_help.setWordWrap(True); err_help.setToolTip(err_help.text()); err_help.setText('API 응답과 검사 실패 원인을 표시합니다. 미완료 구간은 원문으로 보존됩니다.'); ev.addWidget(err_help)
        self.error_view=QPlainTextEdit(); self.error_view.setReadOnly(True); self.error_view.setMaximumBlockCount(500); self.error_view.setPlaceholderText('아직 상태 / 오류 기록이 없습니다.'); ev.addWidget(self.error_view)
        self.tabs.addTab(errors,'상태 / 오류 기록')
        pending=QWidget(); pending_layout=QVBoxLayout(pending); pending_layout.addWidget(QLabel('PENDING: 미처리 · FAILED: 실제 실패. 상태 / 오류 기록은 별도 탭에 유지됩니다.'))
        self.pending_view=QPlainTextEdit(); self.pending_view.setReadOnly(True); self.pending_view.setPlaceholderText('미완료 목록이 없습니다.'); pending_layout.addWidget(self.pending_view); self.tabs.addTab(pending,'미완료 목록')

        language=QWidget(); lv=QVBoxLayout(language)
        self.lang_info=QLabel(); self.lang_info.setWordWrap(True); self.lang_info.setAlignment(Qt.AlignTop); lv.addWidget(self.lang_info)
        form=QFormLayout(); self.lang_option_label=QLabel('언어 옵션'); self.lang_option=QComboBox(); self.lang_option.currentIndexChanged.connect(self._language_option_changed); form.addRow(self.lang_option_label,self.lang_option)
        self.sentence_style=QComboBox()
        self.sentence_style.addItem('보통 (지금까지와 같음)','normal')
        self.sentence_style.addItem('자연스럽게 (한국 소설 문장 우선)','natural')
        self.sentence_style.setToolTip('자연스럽게: 원문의 어순·문장 수를 따르지 않고 한국 소설처럼 읽히게 번역합니다(그녀는…, ~것이었다 같은 번역투를 피함).\n'
                                       '내용·수위·누가 무엇을 했는지는 그대로 옮기며, 추가 요청 없이 같은 번역 요청 안에서 적용됩니다.\n'
                                       '언어별로 따로 저장됩니다. 바꾸면 다음 요청부터 적용되고, 번역 중인 책을 처음부터 다시 번역하지는 않습니다.\n'
                                       '번역이 끝나면 작업 기록에 번역투 표현 수가 나오니 두 설정을 같은 장으로 비교해 보세요.')
        self.sentence_style.currentIndexChanged.connect(self._sentence_style_changed)
        form.addRow('문장 스타일',self.sentence_style)
        self.korean_punct=QCheckBox('중국·일본식 문장부호를 한국식으로 바꾸기')
        self.korean_punct.setToolTip('。→.  ，、→,  ！？：；→!?:;  ・→·  〜→~  전각 숫자·기호→3 %\n'
                                     '결과 파일을 쓸 때 적용합니다. 번역 요청에는 영향이 없어 이미 번역한 책도 다시 번역하지 않고, 다음에 결과를 저장할 때 정리됩니다. 원문으로 남은 문단은 바꾸지 않습니다.')
        try: self.korean_punct.setChecked(bool(load_json(CONFIG_DIR/'app.json').get('korean_punctuation',True)))
        except Exception: self.korean_punct.setChecked(True)
        self.korean_punct.toggled.connect(self._korean_punct_toggled)
        form.addRow('문장부호',self.korean_punct)
        self.quote_style=QComboBox()
        self.quote_style.addItem('곧은 따옴표  " "  \' \'  (기본)','straight')
        self.quote_style.addItem('둥근 따옴표  “ ”  ‘ ’','curly')
        self.quote_style.addItem('바꾸지 않음 (모델이 쓴 그대로)','keep')
        self.quote_style.setToolTip('결과 파일의 따옴표 모양입니다. 「」는 곧은/둥근 따옴표로 바뀌고, 책 제목에 쓰는 『』는 그대로 둡니다. 결과 파일을 쓸 때 적용되어 다시 번역하지 않습니다.')
        try: qs=str(load_json(CONFIG_DIR/'app.json').get('quote_style') or 'straight')
        except Exception: qs='straight'
        self.quote_style.setCurrentIndex(max(0,self.quote_style.findData(qs)))
        self.quote_style.currentIndexChanged.connect(self._quote_style_changed)
        form.addRow('따옴표',self.quote_style); lv.addLayout(form)
        guide_title=QLabel('<b>내 번역 지침</b> · 이 언어(원문)의 모든 책에 적용되는 나만의 지침입니다. 한국어로 편하게 적으세요.')
        grow=QHBoxLayout(); grow.addWidget(guide_title,1)
        self.guide_big=QPushButton('크게 편집'); self.guide_big.setToolTip('큰 창에서 지침을 편집합니다.'); self.guide_big.clicked.connect(self._edit_guide_big)
        grow.addWidget(self.guide_big); lv.addLayout(grow)
        self.user_guide=NoChainTextEdit()
        self.user_guide.setPlaceholderText('예)\n- 등장인물 이름은 한자 독음으로 (左京 → 좌경)\n- 대사는 자연스러운 구어체, 지문은 과거형\n'
                                           '- 남자 주인공은 반말, 여자 주인공은 존댓말\n- 의성어는 한국식으로 바꾸기')
        self.user_guide.setToolTip('매 번역 요청에 "독자의 지침"으로 함께 보냅니다. 원문을 빠짐없이 그대로 옮기기, 문단 번호, 미성년자 관련 성적 내용 제외 같은 기본 규칙이 우선합니다.\n'
                                   '바꾸면 다음 요청부터 적용되고, 번역 중인 책을 처음부터 다시 번역하지는 않습니다(이미 번역된 부분까지 바꾸려면 "처음부터 다시").')
        self.user_guide.setMinimumHeight(170)   # ~9 lines even on a small screen; grows with the window
        self.user_guide.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        lv.addWidget(self.user_guide,1)
        self.user_guide_note=QLabel('자동 저장 · 다음 번역 요청부터 적용 (번역 중인 책을 처음부터 다시 번역하지 않음) · 최대 3,000자')
        self.user_guide_note.setStyleSheet('color:#6b7280;'); lv.addWidget(self.user_guide_note)
        self._guide_timer=QTimer(self); self._guide_timer.setSingleShot(True); self._guide_timer.setInterval(600)
        self._guide_timer.timeout.connect(self._save_user_guide)
        self.user_guide.textChanged.connect(lambda: (not getattr(self,'_guide_loading',False)) and self._guide_timer.start())
        lv.addStretch(1)
        from PySide6.QtWidgets import QScrollArea
        lang_scroll=QScrollArea(); lang_scroll.setWidgetResizable(True); lang_scroll.setFrameShape(QFrame.NoFrame); lang_scroll.setWidget(language)
        self.tabs.addTab(lang_scroll,'언어별 설정'); self.lang_scroll=lang_scroll
        self._wheel_guard=WheelGuard(self)
        for combo in (self.lang_option,self.sentence_style,self.quote_style):
            combo.setFocusPolicy(Qt.StrongFocus); combo.installEventFilter(self._wheel_guard)
        # Opening the tab always shows it from the top, where the options are.
        self.tabs.currentChanged.connect(lambda i: self.tabs.widget(i) is self.lang_scroll and self.lang_scroll.verticalScrollBar().setValue(0))

        memory=QWidget(); mv=QVBoxLayout(memory)
        mem_help=QLabel('작품 전체에서 유지해야 할 인물 관계·말투·세계관 정보를 간단히 적으세요. 예: “A는 B에게 반말, B는 A에게 존댓말.” 너무 긴 줄거리 전체를 넣기보다는 번역 일관성에 필요한 사실만 적는 것이 좋습니다.')
        mem_help.setWordWrap(True); mv.addWidget(mem_help)
        self.book_memory=QPlainTextEdit(); self.book_memory.setPlaceholderText('예) 하야토 → 미사키: 반말\n미사키 → 하야토: 존댓말\n두 사람은 연인 관계\n왕레이는 천현문의 장로')
        mv.addWidget(self.book_memory)
        self.batch_memory_common=QCheckBox('폴더의 “각 파일 독립 작품”에도 이 메모를 공통 적용')
        self.batch_memory_common.setChecked(False); self.batch_memory_common.setEnabled(False); self.batch_memory_common.stateChanged.connect(lambda _=None:self._save_workspace())
        mv.addWidget(self.batch_memory_common); self.tabs.addTab(memory,'작품 메모')

        terms=QWidget(); tv=QVBoxLayout(terms)
        term_help=QLabel('언어 프로필에는 자주 쓰는 장르·호칭·관계 용어의 자동 언어팩이 내장되어 있으며, 현재 청크에 실제로 등장한 항목만 참고 힌트로 전달됩니다. 아래 확정 용어는 사용자가 지정한 최우선 규칙입니다.')
        term_help.setWordWrap(True); term_help.setStyleSheet('color:#4b5563;'); term_help.setToolTip(term_help.text()); term_help.setText('인명·지명 등 책 전체에서 같게 유지할 표기입니다. 자동 추출(번역 시작 시 자동 가능)·용어 추가·TSV 가져오기로 채우고, 틀린 항목은 고치거나 지우세요. 확정 용어는 자동 언어팩보다 우선합니다.'); tv.addWidget(term_help)
        nrow=QHBoxLayout(); self.name_option_label=QLabel('이름 표기'); self.name_option=QComboBox()
        self.name_option.setModel(self.lang_option.model())   # the same choices as 언어별 설정
        self.name_option.setToolTip('자동 추출과 번역 모두 이 방식을 따릅니다. 바꾸면 이미 확정 용어집에 있는 이름을 새 방식으로 다시 뽑을지 묻습니다.')
        self.name_option.currentIndexChanged.connect(self._name_option_changed)
        nrow.addWidget(self.name_option_label); nrow.addWidget(self.name_option,1); nrow.addStretch(1); tv.addLayout(nrow)
        self.term_table=QTableWidget(0,2); self.term_table.setHorizontalHeaderLabels(['원문 용어','확정 한국어'])
        self.term_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); tv.addWidget(self.term_table,1); self.term_table.setMinimumHeight(150); self.term_table.verticalHeader().setVisible(False)
        self._marking_terms=False; self.term_table.itemChanged.connect(lambda _i: self._mark_suspicious_terms())
        self.term_table.itemChanged.connect(self._term_manual_edit)
        self.term_table.itemChanged.connect(lambda _i: self._update_terms_tab_title())
        self.term_table.model().rowsInserted.connect(lambda *_: self._mark_suspicious_terms())
        ta=QHBoxLayout(); self.term_add=QPushButton('용어 추가'); self.term_add.clicked.connect(self._add_term_row); self.term_delete=QPushButton('선택 삭제'); self.term_delete.clicked.connect(self._delete_term_rows)
        self.term_import=QPushButton('TSV 가져오기'); self.term_import.clicked.connect(self._import_terms); self.term_export=QPushButton('TSV 내보내기'); self.term_export.clicked.connect(self._export_terms)
        self.term_extract=QPushButton('인명·용어 자동 추출'); self.term_extract.setToolTip('원문에서 자주 나오는 단어 후보만(본문 문장 없이) 모델에 보내 인명·지명·고유 용어와 한국어 표기를 제안받습니다. 결과를 확인·수정한 뒤 번역을 시작하세요. 요청 1회.')
        self.term_extract.clicked.connect(self._extract_terms)
        self.auto_terms=QCheckBox('번역 시작 시 먼저 자동 추출')
        self.auto_terms.setToolTip('파일·붙여넣은 글을 처음 번역할 때(원문이 바뀌었을 때만) 인명·용어 자동 추출을 먼저 한 번 실행하고, 새 항목만 확정 용어집에 더한 뒤 번역을 시작합니다. 이미 있는 항목은 바꾸지 않습니다. 요청 1회 · 보통 몇 초. 폴더 작업에는 적용하지 않습니다.')
        try: self.auto_terms.setChecked(bool(load_json(CONFIG_DIR/'app.json').get('auto_extract_terms',True)))
        except Exception: self.auto_terms.setChecked(True)
        self.auto_terms.toggled.connect(self._auto_terms_toggled)
        ta.addWidget(self.term_add); ta.addWidget(self.term_delete); ta.addWidget(self.term_import); ta.addWidget(self.term_export); ta.addWidget(self.term_extract); ta.addWidget(self.auto_terms); ta.addStretch(1); tv.addLayout(ta)
        cr=QHBoxLayout(); self.use_common=QCheckBox('공용 용어집 사용 (번역을 시작할 때 이 용어집을 쌓아 다음 책에도 적용)')
        self.use_common.setToolTip('번역이 끝날 때마다 이 책의 확정 용어 중 공용 용어집에 없는 것을 언어별로 쌓아 둡니다(이미 있는 항목은 바꾸지 않음).\n'
                                   '새 책은 인명·용어를 추출할 때 본문에 나오는 공용 항목을 먼저 가져오고(속편의 이름 유지), 나머지는 번역 중 참고 힌트로 씁니다.\n'
                                   '한자가 남은 표기·일반 단어·한 글자 원문은 쌓지 않습니다.')
        try: self.use_common.setChecked(bool(load_json(CONFIG_DIR/'app.json').get('common_terms',True)))
        except Exception: self.use_common.setChecked(True)
        self.common_btn=QPushButton('공용 용어집'); self.common_btn.clicked.connect(self._open_common_terms)
        cr.addWidget(self.use_common); cr.addWidget(self.common_btn); cr.addStretch(1); tv.addLayout(cr)
        self.tabs.addTab(terms,'확정 용어집'); self.terms_tab=terms; self._common_pulled=[]
        for sig in (self.term_table.model().rowsInserted, self.term_table.model().rowsRemoved, self.term_table.model().modelReset):
            sig.connect(lambda *_: self._update_terms_tab_title())
        self._auto_terms_sha=''; self._auto_terms_passed=False; self._auto_terms_force=False
        self._auto_terms_dialog_force=False; self._auto_terms_declined=set(); self._close_after_extract=False; self._term_source_sha=''
        self._term_locks={}; self._term_internal_edit=False
        self._term_replace=False; self._loading_workspace_option=False
        self._term_thread=None
        self._build_speech_tab()

        guide=QLabel('사용 순서: ① TXT·PDF·EPUB 파일/폴더 선택 → ② 원문 언어·인코딩 확인 → ③ API 키 확인 → ④ 필요하면 작품 메모/확정 용어 추가 → ⑤ 번역 시작. 완료분은 청크마다 저장되어 중단 후 이어서 진행할 수 있습니다.')
        guide.setWordWrap(True); guide.setStyleSheet('color:#374151;padding:7px 2px;'); self.settings_toggle.setToolTip(guide.text()); guide.hide()
        self.ready_label=QLabel('준비 상태 확인 중…'); self.ready_label.setWordWrap(True); self.ready_label.setStyleSheet('padding:8px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;'); layout.addWidget(self.ready_label)
        self.api_runtime_label=QLabel('API 상태 · 아직 요청 없음'); self.api_runtime_label.setWordWrap(True); self.api_runtime_label.setStyleSheet('padding:7px;background:#f9fafb;color:#374151;border-radius:6px;'); layout.addWidget(self.api_runtime_label)
        bottom=QHBoxLayout(); self.progress=QProgressBar(); self.progress.setValue(0); self.status=ElidedLabel('대기 중'); self.status.setMinimumWidth(220)
        self.open_result=QPushButton('결과 폴더 열기'); self.open_result.setEnabled(False); self.open_result.clicked.connect(self._open_result_folder)
        self.cleanreader_btn=QPushButton('클린리더로 열기'); self.cleanreader_btn.setEnabled(False); self.cleanreader_btn.clicked.connect(self._open_in_cleanreader)
        self.cleanreader_btn.setToolTip('번역 결과를 야문 클린리더(문단 정리기)로 엽니다. 폴더 번역이면 결과 파일 전체를 일괄 변환 창으로 엽니다.\n'
                                        '처음 한 번 YamoonCleanReader.exe 위치를 고르면 기억합니다. 위치를 바꾸려면 Shift를 누른 채 클릭하세요.')
        self.restart_btn=QPushButton('처음부터 다시'); self.restart_btn.setToolTip('저장된 완료 청크를 무시하고 현재 원문/설정으로 처음부터 다시 번역합니다.')
        self.restart_btn.clicked.connect(self._restart_translation)
        self.start=QPushButton('번역 시작 / 계속'); self.start.setMinimumHeight(40); self.start.setMinimumWidth(170); self.start.clicked.connect(lambda _=False: self.start_translation(False))
        self.pause_btn=QPushButton('일시정지'); self.pause_btn.setMinimumHeight(40); self.pause_btn.setEnabled(False); self._user_paused=False
        self.pause_btn.setToolTip('진행 중이던 요청 하나를 마무리한 뒤 멈춥니다. [다시 진행]을 누르면 그 자리에서 이어서 번역합니다. (폴더 작업이면 다음 파일도 멈춘 상태로 기다립니다)')
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.time_label=QLabel(''); self.time_label.setStyleSheet('color:#475569;'); self.time_label.setMinimumWidth(150)
        self.time_label.setToolTip('번역에 걸린 시간 (사용자 일시정지 시간 제외)')
        self._job_t0=None; self._job_paused_total=0.0; self._job_paused_at=None; self._last_job_seconds=None; self._prog=(0,0); self._prog_start=None
        self._clock=QTimer(self); self._clock.setInterval(1000); self._clock.timeout.connect(self._tick_clock)
        bottom.addWidget(self.status,2); bottom.addWidget(self.progress,3); bottom.addWidget(self.time_label); bottom.addWidget(self.open_result); bottom.addWidget(self.cleanreader_btn); bottom.addWidget(self.restart_btn); bottom.addWidget(self.pause_btn); bottom.addWidget(self.start); layout.addLayout(bottom)
        self.setStyleSheet('QWidget{font-size:13px;} QMainWindow{background:#fff;} QPushButton{padding:5px 9px;} QComboBox{padding:4px;min-width:90px;} QPushButton[analysisBusy="true"]:disabled{color:#1745b5;background:#dbeafe;border:1px solid #4f83d8;font-weight:700;} QTabWidget::pane{border:1px solid #d1d5db;}')

    def _set_analysis_busy(self, busy: bool, caption: str = '', label: str = ''):
        """An analysis button stays visible, high-contrast, and non-clickable.
        While it runs, the status line and the API state show what is being asked and for
        how long (one-second ticker), so a slow engine never looks like a frozen program."""
        self.start.setProperty('analysisBusy', bool(busy))
        if not hasattr(self,'_analysis_timer'):
            self._analysis_timer=QTimer(self); self._analysis_timer.setInterval(1000)
            self._analysis_timer.timeout.connect(self._refresh_analysis_status)
        if busy:
            self._analysis_label=label or caption; self._analysis_stage=''; self._analysis_t0=time.monotonic()
            self._analysis_timer.start()
        else:
            self._analysis_timer.stop(); self._analysis_t0=None
        if busy:
            self.start.setText(caption)
            self.start.setEnabled(False)
            # A completed result may remain on disk, but opening it during an
            # analysis is confusing and risks mixing old/new job states.
            self.open_result.setEnabled(False)
            self.cleanreader_btn.setEnabled(False)
        else:
            self.start.setText('번역 시작 / 계속')
            self.open_result.setEnabled(bool(self.last_result_path))
            self.cleanreader_btn.setEnabled(bool(self.last_result_path))
        self.start.style().unpolish(self.start)
        self.start.style().polish(self.start)
        self.start.update()

    def _connect_stage(self, thread):
        stage=getattr(thread,'stage',None)
        if stage is not None: stage.connect(self._analysis_stage_changed)

    def _analysis_stage_changed(self, text: str):
        self._analysis_stage=str(text or ''); self._refresh_analysis_status()

    def _refresh_analysis_status(self):
        t0=getattr(self,'_analysis_t0',None)
        if t0 is None or not self._extracting():
            return
        sec=int(time.monotonic()-t0)
        took=f'{sec//60}분 {sec%60:02d}초' if sec>=60 else f'{sec}초'
        stage=getattr(self,'_analysis_stage','')
        # Short line beside the progress bar; the engine/stage detail goes to the API state.
        label=getattr(self,'_analysis_label','')
        short='말투표 만드는 중' if '말투표' in label else ('인명·용어 추출 중' if '용어' in label else label)
        line=f'{short} · 경과 {took}'
        if getattr(self,'_speech_skip',False):
            line+=' · 건너뛰는 중…'
        self.status.setText(line)
        if hasattr(self,'api_runtime_label'):
            self.api_runtime_label.setText('API 상태 · '+' · '.join(x for x in (getattr(self,'_analysis_label',''),stage,f'경과 {took}') if x))

    def _speech_skippable(self) -> bool:
        """An automatic speech-table run before a translation can be skipped from the main
        button: the translation then starts at once without the table."""
        return (self._extracting() and isinstance(self._term_thread, SpeechExtractThread)
                and bool(getattr(self,'_speech_auto',False)) and not getattr(self,'_speech_skip',False))

    def _skip_auto_speech(self):
        if not self._speech_skippable():
            return
        self._speech_skip=True
        self._term_thread.requestInterruption()
        self.start.setEnabled(False)
        self._refresh_analysis_status()

    def _refresh_content_ui(self):
        kind=self.content_kind
        name=self.pm.active.display_name.split(' → ')[0]
        self.profile_hint.setText(name+(' 소설' if kind=='novel' else ' 문서'))
        if kind=='document':
            self.lang_info.setText('일반 문서: 사실·수치·단위·용어·목록을 보존하고, 소설 문체로 윤문하거나 감정·장면을 추가하지 않습니다. 언어별 표기 옵션과 확정 용어는 계속 적용됩니다.'); self.lang_info.setToolTip('')
        else:
            p=self.pm.active
            description=p.ui.get('description') or f'{p.display_name} 번역 규칙이 자동 적용됩니다.'
            self.lang_info.setText(description)
            self.lang_info.setToolTip(f'내장 규칙: 언어 {len(p.language_rules)}개 · 문체 {len(p.style_rules)}개 · 문맥 {len(p.ambiguity_rules)}개 · 용어 힌트 {len(p.lexical_guidance)}개.\n원문에 해당하는 힌트만 전달하며 확정 용어집에 자동 등록하지 않습니다. 아래 옵션은 기본값으로 준비되어 있습니다.')
        if hasattr(self,'sentence_style'):
            self.sentence_style.setEnabled(kind=='novel')   # documents always use the precise document prompt
        if hasattr(self,'book_memory'):
            self.tabs.setTabText(self.tabs.indexOf(self.book_memory.parentWidget()),'작품 메모' if kind=='novel' else '문서 메모')
            self.book_memory.setPlaceholderText('인물 관계·말투·세계관 정보' if kind=='novel' else '문서 목적·공식 명칭·약어·단위 표기 규칙')

    def _reset_settings(self):
        if self.worker and self.worker.isRunning():return
        cfg=default_settings()
        # The API setup (engines, their order, models, keys) belongs to the API 설정 window
        # and is kept as it is. Earlier builds switched OpenAI/local LLM off here while the
        # engine list still showed them checked, so they silently became "미준비".
        try:
            save_json(CONFIG_DIR/'app.json',cfg)
        except Exception as exc:
            QMessageBox.critical(self,'초기화 실패',str(exc));return
        self.content_kind='novel'
        self._term_cache={}
        # Reset options for the current source in every language. Keep other
        # books, the original TXT, persisted translations and secrets intact.
        state=self.workspace_store.load_all()
        target=self.source_path if self.input_mode=='single' else self.batch_root
        sha=hashlib.sha256(self.source_bytes).hexdigest() if self.input_mode=='single' and self.source_bytes else ''
        for profile in self.pm.all():
            key=self.workspace_store.key(self.input_mode,target,profile.id,sha)
            state[key]={'book_memory':'','language_option':option_spec(profile.id)['choices'][0][1], 'terms':{},'encoding':'auto','mode':'balanced','model':str(self.model.itemData(0) or self.model.itemText(0)),'include_subfolders':False,'batch_relation':'independent','custom_output_dir':'','batch_memory_common':False}
        self.workspace_store.save_all(state)
        for widget in [self.lang,self.model,self.mode,self.encoding,self.lang_option,self.include_sub,self.batch_relation,self.batch_memory_common]:widget.blockSignals(True)
        try:
            detected,_,_=detect_language_bytes(self.source_bytes) if self.source_bytes else (None,0,None)
            initial_profile=(detected+'-ko') if detected in {'ja','zh','en'} else 'zh-ko'
            self.lang.setCurrentIndex(self.lang.findData(initial_profile))
            self.pm.set_active(initial_profile);self._configure_language_option(initial_profile)
            self.model.setCurrentIndex(0);self.mode.setCurrentIndex(self.mode.findData('balanced'));self.encoding.setCurrentIndex(0)
            self.lang_option.setCurrentIndex(0);self.include_sub.setChecked(False);self.batch_relation.setCurrentIndex(0);self.batch_memory_common.setChecked(False)
            self.book_memory.clear();self.term_table.setRowCount(0);self._set_speech_rows([])
            for box,key,default in ((self.korean_punct,'korean_punctuation',True),(self.auto_terms,'auto_extract_terms',True)):
                box.blockSignals(True); box.setChecked(bool(cfg.get(key,default))); box.blockSignals(False)
            self.quote_style.blockSignals(True)
            qi=self.quote_style.findData(str(cfg.get('quote_style','straight'))); self.quote_style.setCurrentIndex(qi if qi>=0 else 0)
            self.quote_style.blockSignals(False)
        finally:
            for widget in [self.lang,self.model,self.mode,self.encoding,self.lang_option,self.include_sub,self.batch_relation,self.batch_memory_common]:widget.blockSignals(False)
        self._refresh_content_ui();self.settings_toggle.setChecked(True)
        if self.input_mode=='single' and self.source_bytes:self._encoding_changed()
        elif self.input_mode=='folder':self._rescan_all_batch_files()
        self._update_output_label();self._save_workspace();self._update_ready_state()
        self.status.setText('설정 초기화 완료 · API 설정(엔진·키·모델) 보존')

    def _toggle_settings(self, expanded):
        self.settings_area.setVisible(expanded)
        self.settings_toggle.setText('설정 접기' if expanded else '설정 펼치기')

    def _apply_profile(self, profile_id):
        if not profile_id: return
        if hasattr(self,'term_table') and self.pm.active.id!=profile_id:
            self._save_workspace(); self._term_cache[self.pm.active.id]=self._terms()
        p=self.pm.set_active(profile_id); self._configure_language_option(profile_id)
        if self.input_mode=='single' and self.source_bytes:
            try:
                self.source_text,enc=self._decode_source(p.source_language,self.encoding.currentData() if hasattr(self,'encoding') else 'auto')
                self.file_label.setText(self._single_label(self.source_path.name,enc,self.source_text,self.source_bytes))
                self._set_preview_source(self.source_text)
            except Exception as e: self.status.setText(f'인코딩 재해석 실패: {e}')
        idx=self.lang.findData(profile_id)
        if idx>=0 and self.lang.currentIndex()!=idx:
            self.lang.blockSignals(True); self.lang.setCurrentIndex(idx); self.lang.blockSignals(False)
        self.profile_hint.setText(p.ui.get('badge','')); self.lang_info.setText(p.ui.get('description','')); self._refresh_content_ui()
        self._term_internal_edit=True
        self._term_locks={}
        self.term_table.setRowCount(0); terms=dict(p.default_terms); terms.update(self._term_cache.get(profile_id,{}))
        if self.input_mode=='single' and self.source_bytes:
            try:
                sha=hashlib.sha256(self.source_bytes).hexdigest(); saved=Database(DATA_DIR/'translations.db').latest_terms_for_source(sha,profile_id)
                if saved: terms.update(saved)
            except Exception: pass
        for a,b in terms.items():
            r=self.term_table.rowCount(); self.term_table.insertRow(r); self.term_table.setItem(r,0,QTableWidgetItem(a)); self.term_table.setItem(r,1,QTableWidgetItem(b))
        self._term_internal_edit=False
        self._term_cache[profile_id]=dict(terms)
        if self.input_mode in {'single','folder'}:
            self._load_workspace()
        if self.input_mode=='single' and self.source_bytes:
            try:
                self.source_text,enc=self._decode_source(p.source_language,self.encoding.currentData() or 'auto')
                self.file_label.setText(self._single_label(self.source_path.name,enc,self.source_text,self.source_bytes)); self._set_preview_source(self.source_text); self._rechunk()
            except Exception as e:
                self.source_text=''; self.paragraphs=[]; self.chunks=[]; self.status.setText(f'인코딩 재해석 실패: {e}')
        self._update_ready_state()


        if hasattr(self,'common_btn'): self._update_common_btn()
    def _run_environment_check(self):
        try:
            from ..runtime_audit import self_test_report, secret_store_self_test
            ok1, report1=self_test_report(); ok2, report2=secret_store_self_test()
            ok=ok1 and ok2
            body=report1+'\n\n'+report2+'\n\n※ 실제 인터넷/API 연결은 “API 설정 → 선택 키 연결 테스트”에서 확인합니다.'
            if ok:
                QMessageBox.information(self,'환경 점검 정상',body)
                self.status.setText('환경 점검 정상 · 실제 API 연결은 API 설정에서 테스트 가능')
            else:
                QMessageBox.warning(self,'환경 점검 문제 발견',body)
                self.status.setText('환경 점검에서 문제 발견 · 표시된 항목을 확인하세요.')
        except Exception as e:
            QMessageBox.critical(self,'환경 점검 실패',f'{type(e).__name__}: {e}')


    def open_advanced(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self,'번역 중','번역 작업이 끝나거나 중지된 뒤 고급 설정을 변경하세요.'); return
        try:
            dlg=AdvancedSettingsDialog(self)
        except Exception as e:
            QMessageBox.critical(self,'고급 설정 열기 실패',f'{type(e).__name__}: {e}'); return
        if dlg.exec():
            new_kind=canonical_kind(dlg.cfg.get('content_kind'))
            if new_kind!=self.content_kind:
                self.content_kind=new_kind; self.mode.setCurrentIndex(self.mode.findData('balanced'))
                choice='mandarin_names' if new_kind=='document' and self.pm.active.id=='zh-ko' else option_spec(self.pm.active.id)['choices'][0][1]
                self.lang_option.setCurrentIndex(self.lang_option.findData(choice))
            self._refresh_content_ui()
            if self.input_mode=='single' and self.source_text:
                self._rechunk()
            self.status.setText('고급 설정 저장 완료 · 다음 번역 요청부터 적용됩니다.')
            self._update_ready_state()

    def open_refinement_run(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, '번역 중', '기존 번역 작업이 완료된 뒤 한국어 재정비를 시작하세요.')
            return
        from .refinement_run_dialog import RefinementRunDialog
        RefinementRunDialog(self).exec()

    def open_refinement_engines(self):
        # Compatibility entry point for callers using the old separate settings action.
        self.open_api()

    def open_api(self, first_provider=None):
        raw=(self.secrets.get('gemini') or {}).get('keys',[]); records=normalize_key_records(raw)
        oai=dict(self.secrets.get('openai') or {})
        try:
            pcfg=load_json(CONFIG_DIR/'providers.json')
            orow=next((x for x in pcfg.get('providers',[]) if x.get('id')=='openai'),{})
            oendpoint=str(orow.get('endpoint') or 'https://api.openai.com/v1/responses')
            if not oai.get('model'): oai['model']=str(orow.get('default_model') or 'gpt-6.1-sol')
        except Exception:
            oendpoint='https://api.openai.com/v1/responses'
        editing=dict(self.secrets.get('refinement_editing') or {})
        if not editing:
            try:
                from ..refinement_engines import load_routes
                editing=dict(load_routes()['editing'])
            except (OSError, ValueError):
                editing={'provider':'local','model':''}
        analysis_settings=dict(self.secrets.get('extraction_engines') or {})
        if not analysis_settings:
            try:
                from ..refinement_engines import load_routes
                legacy_analysis=load_routes()['analysis']
                if legacy_analysis.get('model'):
                    analysis_settings={'terms':legacy_analysis['provider'],'speech':legacy_analysis['provider'],
                                       'model':legacy_analysis['model']}
            except (OSError, ValueError):
                pass
        dlg=ApiDialog([r.to_dict() for r in records],self._selected_model_id(),oai,oendpoint,self,local_settings=dict(self.secrets.get('local') or {}),engines=self.secrets.get('engines'),extraction_engines=analysis_settings,refinement_editing=editing)
        if first_provider in ('gemini', 'openai', 'local') and not self._ready_engines():
            # First-use shortcut: prepare only the selected engine in this unsaved dialog.
            for i in range(dlg.engine_list.count()):
                item=dlg.engine_list.item(i)
                item.setCheckState(Qt.Checked if item.data(Qt.UserRole)==first_provider else Qt.Unchecked)
            dlg.tabs.setCurrentIndex(dlg._tab_of[first_provider])
        if dlg.exec():
            try:
                names=[dlg.local_model.itemText(i) for i in range(dlg.local_model.count())]
                ep=dlg.local_endpoint.text().strip()
                if ep and names: self._local_model_names[ep]=names; self._alt_engine=''
            except Exception: pass
            payload=dlg.settings_payload(); gmodel=str(payload.pop('gemini_model','') or '').strip()
            candidate=dict(self.secrets); candidate.update(payload)
            try: self.secret_store.save(candidate)
            except Exception as e:
                QMessageBox.critical(self,'API 설정 저장 실패',f'API 설정을 안전하게 저장하지 못했습니다. 기존 저장 내용은 유지됩니다.\n{type(e).__name__}: {e}'); return
            self.secrets=candidate
            if gmodel and gmodel!=self._selected_model_id():   # the Gemini model chosen in the dialog = the main-window box
                idx=self.model.findData(gmodel)
                if idx>=0: self.model.setCurrentIndex(idx)
                else: self.model.setCurrentText(gmodel)
                self._save_workspace()
            self.status.setText('API 설정 저장 완료 · 번역 엔진: '+(' → '.join(ENGINE_LABELS[n] for n in self._engine_order()) or '없음')); self._update_ready_state()


    def _prepare_new_input(self):
        self._retry_rounds=0
        """Persist the current book state, then clear all book-scoped UI/cache state."""
        self._save_workspace()
        self._term_cache = {}
        if hasattr(self, 'term_table'):
            self.term_table.setRowCount(0)
        if hasattr(self, 'speech_table'):
            self._set_speech_rows([])
        if hasattr(self, 'book_memory'):
            self.book_memory.clear()
        if hasattr(self, 'error_view'):
            self.error_view.clear();self.pending_view.clear()
        self.last_result_path = None
        if hasattr(self, 'open_result'):
            self.open_result.setEnabled(False); self.cleanreader_btn.setEnabled(False)
        self.batch_context = ''
        self.batch_excluded = set()
        self._output_fallback = None
        if hasattr(self, 'include_sub'):
            self.include_sub.blockSignals(True); self.include_sub.setChecked(False); self.include_sub.blockSignals(False)
        if hasattr(self, 'batch_relation'):
            self.batch_relation.blockSignals(True); self.batch_relation.setCurrentIndex(0); self.batch_relation.blockSignals(False)
        if hasattr(self,'batch_memory_common'):
            self.batch_memory_common.blockSignals(True); self.batch_memory_common.setChecked(False); self.batch_memory_common.blockSignals(False)
        self._completion_message = ''
        self._last_worker_status = ''
        self._paste_active = False
        self._origin_kind = ''
        self._auto_terms_sha = ''
        self._lang_chosen = False
        if hasattr(self,'view_result_btn'): self._set_result('')

    def open_txt(self):
        if self._extracting(): return   # the extraction finishes first (a few seconds)
        fn,_=QFileDialog.getOpenFileName(self,'번역할 TXT·PDF·EPUB 파일 선택','','TXT·PDF·EPUB (*.txt *.pdf *.epub);;Text files (*.txt);;PDF (*.pdf);;EPUB (*.epub)')
        if not fn:return
        self._open_txt_path(fn)

    def _source_doc_kind(self) -> str:
        """'PDF' / 'EPUB' when the open single source is such a book (its text was taken out), else ''."""
        if not self.source_path or getattr(self,'_paste_active',False):
            return ''
        return document_kind(self.source_path)

    def _decode_source(self, language, encoding):
        """Text of the open single source; a PDF/EPUB's text is kept as taken out (no TXT encoding)."""
        kind=self._source_doc_kind()
        if kind:
            return self.source_bytes.decode('utf-8'), getattr(self,'_doc_label','') or f'{kind} · 글자 추출'
        return decode_txt_bytes(self.source_bytes,language,encoding)

    @staticmethod
    def _no_text_reason(p: Path) -> str:
        return '문자 인식(OCR)이 필요한 PDF (글자 없는 스캔 PDF)' if document_kind(p)=='PDF' else '글자가 없는 EPUB (그림으로만 된 책)'

    def _read_source(self, p: Path):
        """(text, label, bytes) of a TXT, PDF or EPUB; a PDF/EPUB shows a wait cursor while its text is taken out."""
        kind=document_kind(p)
        if not kind:
            return read_source_file(p,self.pm.active.source_language,self.encoding.currentData() or 'auto')
        from PySide6.QtWidgets import QApplication
        self.status.setText(f'{kind}에서 글자를 꺼내는 중 · {p.name}')
        QApplication.setOverrideCursor(Qt.WaitCursor)
        def progress(done, total):
            if done==total or done%10==0:
                self.status.setText(f'{kind}에서 글자를 꺼내는 중 · {p.name} · {done:,}/{total:,}쪽'); QApplication.processEvents()
        try:
            QApplication.processEvents()
            return read_source_file(p,self.pm.active.source_language,progress=progress)
        finally:
            QApplication.restoreOverrideCursor()

    def _open_txt_path(self, fn):
        p=Path(fn); kind=document_kind(p)
        try:
            text,enc,raw=self._read_source(p)
        except NoTextError as e:
            reason=self._no_text_reason(p)
            QMessageBox.information(self,reason.split(' (')[0],str(e)); self.status.setText(f'{p.name} · {reason}라 열지 않았습니다'); return
        except SourceTextError as e:
            QMessageBox.warning(self,f'{kind}를 열 수 없음',str(e)); self.status.setText(f'{p.name} · {kind}를 열지 못했습니다'); return
        except Exception as e: QMessageBox.critical(self,'파일을 열 수 없음',f'{kind or "TXT"}를 읽지 못했습니다.\n{e}'); return
        self._prepare_new_input()
        self.input_mode='single'; self.batch_relation.setEnabled(False); self.include_sub.setEnabled(False); self._update_batch_memory_mode(); self.batch_root=None; self.batch_files=[]; self.batch_index=-1; self.batch_results={}; self.batch_selection=None
        self.source_path=p; self.source_bytes=raw; self.source_text=text; self._doc_label=enc if kind else ''; self._origin_kind=kind
        self.file_label.setText(self._single_label(p.name,enc,text,raw)); self._set_preview_source(text); self._refresh_job_table()
        target,note=self._auto_source_profile(p,raw)
        if target!=self.pm.active.id:
            # Switch before applying so no empty settings are saved for this book under the old language.
            self.pm.set_active(target)
        self._apply_profile(target); self._update_output_label(); self._update_ready_state(); self.tabs.setCurrentWidget(self.preview_tab)
        if note: self.status.setText(note)

    def _auto_source_profile(self, path: Path, raw: bytes) -> tuple[str, str]:
        """The language profile to open a TXT/PDF/EPUB with: the detected source language when
        the detection is confident, unless the user picked the current language for this book
        in the language box before. Settings saved for other reasons (opening it under that
        language in an older build, settings reset) do not block the detection."""
        current=self.pm.active.id
        try:
            detected,confidence,_enc=detect_language_bytes(raw[:262144])   # the bytes already read; a cut character is tolerated
        except Exception:
            return current,''
        want=f'{detected}-ko' if detected in {'zh','ja','en'} and confidence>=0.70 else ''
        if not want or want==current or self.lang.findData(want)<0:
            return current,''
        try:
            sha=hashlib.sha256(raw).hexdigest()
            if self.workspace_store.get(self.workspace_store.key('single',path,current,sha)).get('language_chosen'):
                return current,''
        except Exception:
            pass
        names={'zh':'중국어','ja':'일본어','en':'영어'}
        return want,f'원문 언어를 {names[detected]}로 자동 설정했습니다 (원문 문자 분석) · 다르면 원문 언어 칸에서 바꾸세요'

    def open_folder(self):
        if self._extracting(): return   # the extraction finishes first (a few seconds)
        fn=QFileDialog.getExistingDirectory(self,'번역할 TXT·PDF·EPUB 파일이 든 폴더 선택')
        if not fn:return
        self._open_folder_path(fn)

    # ---------------------------------------------------------------- drag & drop
    @staticmethod
    def _dropped_paths(mime) -> list[Path]:
        if not mime or not mime.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def _drop_busy(self) -> bool:
        return bool((self.worker and self.worker.isRunning()) or self._extracting())

    def _handle_drop(self, paths: list[Path]) -> bool:
        """One TXT/PDF/EPUB file -> open it; one folder -> folder translation. Returns True when handled."""
        if not paths:
            return False
        if self._drop_busy():
            self.status.setText('번역(또는 용어 추출) 중에는 다른 파일을 열 수 없습니다 · 끝나거나 중지한 뒤 끌어다 놓으세요')
            return True
        if len(paths)==1 and paths[0].is_dir():
            self._open_folder_path(str(paths[0])); return True
        if len(paths)==1 and paths[0].is_file() and paths[0].suffix.lower() in SOURCE_SUFFIXES:
            self._open_txt_path(str(paths[0])); return True
        if len(paths)>1 and all(x.is_file() and x.suffix.lower() in SOURCE_SUFFIXES for x in paths):
            # Several files: translated one after another like a folder, but only these files.
            import os
            try:
                root=Path(os.path.commonpath([str(x.parent) for x in paths]))
            except ValueError:
                QMessageBox.information(self,'같은 드라이브의 파일만','서로 다른 드라이브에 있는 파일은 함께 열 수 없습니다. 한 드라이브의 파일만 끌어다 놓으세요.')
                return True
            self._open_folder_path(str(root), selection=paths)
            self.status.setText(f'끌어다 놓은 파일 {len(paths):,}개를 차례로 번역합니다 · 결과: {self._default_output_dir()}')
            return True
        if len(paths)>1 and any(x.is_dir() for x in paths):
            QMessageBox.information(self,'폴더는 하나씩','폴더는 한 번에 하나만 끌어다 놓을 수 있습니다. 여러 파일은 파일만 골라서 끌어다 놓으세요.')
            return True
        QMessageBox.information(self,'TXT·PDF·EPUB 파일만 열 수 있음','TXT·PDF·EPUB 파일 또는 그런 파일이 든 폴더 하나를 끌어다 놓으세요.')
        return True

    def dragEnterEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self._handle_drop(self._dropped_paths(event.mimeData())):
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def eventFilter(self, obj, event):
        # Files dropped on the source pane open the file instead of pasting its path as text;
        # dragging plain text into the pane still works as before.
        from PySide6.QtCore import QEvent
        if event.type() in (QEvent.DragEnter, QEvent.DragMove) and self._dropped_paths(event.mimeData()):
            event.acceptProposedAction(); return True
        if event.type()==QEvent.Drop and self._dropped_paths(event.mimeData()):
            self._handle_drop(self._dropped_paths(event.mimeData())); event.acceptProposedAction(); return True
        return super().eventFilter(obj, event)

    def _open_folder_path(self, fn, selection=None):
        self._prepare_new_input()
        self.batch_selection=[Path(x) for x in selection] if selection else None
        self.input_mode='folder'; self.batch_relation.setEnabled(True); self.include_sub.setEnabled(True); self._update_batch_memory_mode(); self.batch_root=Path(fn); self.source_path=None; self.source_text=''; self.source_bytes=b''; self.paragraphs=[]; self.chunks=[]; self.batch_results={}; self.batch_index=-1
        if self.batch_selection:
            self.include_sub.setChecked(False); self.include_sub.setEnabled(False)   # only the dropped files
        self._apply_profile(self.pm.active.id); self._scan_batch_files(); self._set_preview_source(''); self._update_output_label(); self._update_ready_state(); self.tabs.setCurrentWidget(self.jobs_tab)

    def _rescan_folder_if_needed(self):
        if self.input_mode=='folder' and self.batch_root and not (self.worker and self.worker.isRunning()): self._scan_batch_files(); self._save_workspace(); self._update_ready_state()

    def _scan_batch_files(self):
        root=self.batch_root
        if not root:return
        default_out=(root/'한국어_번역').resolve()
        excluded=default_out
        if self.custom_output_dir:
            try:
                candidate=self.custom_output_dir.resolve()
                if candidate != root.resolve() and root.resolve() in candidate.parents:
                    excluded=candidate
            except Exception:
                pass
        if self.batch_selection:
            discovered=sorted((p for p in self.batch_selection if p.is_file()), key=lambda p: str(p).lower())
        else:
            discovered=discover_txt_files(root,self.include_sub.isChecked(),excluded)
        self.batch_files=[p for p in discovered if str(p.resolve()) not in self.batch_excluded]
        if hasattr(self,'batch_exclude_btn'):
            self.batch_exclude_btn.setEnabled(bool(self.batch_files) and not (self.worker and self.worker.isRunning()))
            self.batch_rescan_btn.setEnabled(not (self.worker and self.worker.isRunning()))
        total_bytes=0
        for f in self.batch_files:
            try: total_bytes+=f.stat().st_size
            except OSError: pass
        kind='끌어다 놓은 파일' if self.batch_selection else '폴더 작업'
        self.file_label.setText(f'{kind} · {root} · {self._count_kinds(self.batch_files)} · 합계 {self._size_text(total_bytes)} · 순차 처리' + (f' · 제외 {len(self.batch_excluded):,}개' if self.batch_excluded else ''))
        self._refresh_job_table()

    def _refresh_job_table(self):
        self.job_table.setRowCount(0)
        if self.input_mode=='single' and self.source_path:
            files=[self.source_path]; root=self.source_path.parent
        elif self.input_mode=='folder' and self.batch_root:
            files=self.batch_files; root=self.batch_root
        else:return
        for p in files:
            r=self.job_table.rowCount(); self.job_table.insertRow(r)
            try: rel=str(p.relative_to(root))
            except Exception: rel=p.name
            name_item=QTableWidgetItem(rel); name_item.setData(Qt.UserRole,str(p.resolve())); self.job_table.setItem(r,0,name_item); self.job_table.setItem(r,1,QTableWidgetItem(self._safe_size(p)))
            status,result,fail=self.batch_results.get(str(p),('대기','','0'))
            self.job_table.setItem(r,2,QTableWidgetItem(status)); self.job_table.setItem(r,3,QTableWidgetItem(result))

    @staticmethod
    def _count_kinds(files) -> str:
        """'TXT 3개' or 'TXT 3개 · PDF 2개 · EPUB 1개' (PDF/EPUB shown only when there are any)."""
        kinds=[document_kind(f) or 'TXT' for f in files]
        parts=[f'{k} {kinds.count(k):,}개' for k in ('TXT','PDF','EPUB') if kinds.count(k)]
        return ' · '.join(parts) or 'TXT 0개'

    @staticmethod
    def _size_text(n:int)->str:
        if n<1024: return f'{n:,} B'
        if n<1024**2: return f'{n/1024:.2f} KB'
        return f'{n/1024**2:.2f} MB'

    def _single_label(self, name, enc, text, raw) -> str:
        size=len(raw or b'')
        if self._source_doc_kind():
            try: size=self.source_path.stat().st_size   # the PDF/EPUB file, not the text taken out of it
            except OSError: pass
        return f'단일 파일 · {name} · {self._size_text(size)} · {enc} · {len(text or ""):,}자'

    @classmethod
    def _safe_size(cls,p:Path)->str:
        # A file deleted/renamed during a long folder run must not break the table refresh.
        try: return cls._format_size(p.stat().st_size)
        except OSError: return '없음'

    @staticmethod
    def _format_size(n:int)->str:
        if n<1024:return f'{n} B'
        if n<1024**2:return f'{n/1024:.1f} KB'
        return f'{n/1024**2:.1f} MB'

    def _exclude_selected_batch_files(self):
        if self.input_mode!='folder' or (self.worker and self.worker.isRunning()):
            return
        rows=sorted({i.row() for i in self.job_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self,'파일 선택','작업 목록에서 제외할 파일 행을 먼저 선택하세요.')
            return
        paths=[]
        for row in rows:
            item=self.job_table.item(row,0)
            raw=item.data(Qt.UserRole) if item else None
            if raw: paths.append(str(raw))
        if not paths:
            return
        for raw in paths: self.batch_excluded.add(str(Path(raw).resolve()))
        self._scan_batch_files(); self._update_ready_state()
        self.status.setText(f'파일 {len(paths):,}개를 현재 번역 큐에서 제외했습니다.')

    def _rescan_all_batch_files(self):
        if self.input_mode!='folder' or (self.worker and self.worker.isRunning()):
            return
        self.batch_excluded.clear(); self._scan_batch_files(); self._update_ready_state()
        self.status.setText('끌어다 놓은 파일 목록을 다시 불러왔습니다 (제외했던 파일 복원).' if self.batch_selection else '폴더의 TXT·PDF·EPUB 목록을 다시 스캔했습니다.')

    OUTPUT_SETTING_FILE='output_folder.json'

    @staticmethod
    def _load_output_setting() -> Path|None:
        try:
            raw=str(load_json(DATA_DIR/MainWindow.OUTPUT_SETTING_FILE).get('output_dir') or '').strip()
            return Path(raw) if raw else None
        except Exception:
            return None

    def _save_output_setting(self):
        try:
            DATA_DIR.mkdir(parents=True,exist_ok=True)
            save_json(DATA_DIR/self.OUTPUT_SETTING_FILE,{'output_dir':str(self.custom_output_dir) if self.custom_output_dir else ''})
        except Exception:
            pass

    def choose_output_dir(self):
        start=str(self.custom_output_dir) if self.custom_output_dir else ''
        fn=QFileDialog.getExistingDirectory(self,'결과 저장 폴더 지정 (모든 결과 파일에 적용)',start)
        if fn:
            self.custom_output_dir=Path(fn); self._output_fallback=None
            self._save_output_setting(); self._update_output_label()
            if self.input_mode=='folder': self._scan_batch_files(); self._update_ready_state()

    def reset_output_dir(self):
        self.custom_output_dir=None; self._output_fallback=None
        self._save_output_setting(); self._update_output_label()
        if self.input_mode=='folder': self._scan_batch_files(); self._update_ready_state()

    @staticmethod
    def _documents_output_dir() -> Path:
        """Where pasted-text results go when no output folder is chosen (never a hidden folder)."""
        return Path.home()/'Documents'/'야문 Novel Translator'

    def _default_output_dir(self)->Path|None:
        if self._output_fallback:return self._output_fallback
        if self.custom_output_dir:return self.custom_output_dir
        if self.input_mode=='single' and self.source_path:return self.source_path.parent
        if self.input_mode=='folder' and self.batch_root:return self.batch_root/'한국어_번역'
        return None

    def _output_dir_for(self,p:Path)->Path|None:
        if getattr(self,'_paste_active',False) and self.source_path and Path(p)==self.source_path:
            # Pasted text: the output folder if chosen, otherwise Documents (the temp source's
            # own folder is hidden in AppData and hard to find).
            return self._output_fallback or self.custom_output_dir or self._documents_output_dir()
        base=self._default_output_dir()
        if not base:return None
        if self.input_mode=='folder' and self.batch_root:
            return output_dir_for(p,self.batch_root,base)
        return base

    def _update_output_label(self):
        d=self._default_output_dir()
        if not d:self.output_label.setText('원문 선택 후 자동 결정됩니다.')
        elif self.input_mode=='folder': self.output_label.setText(f'{d}  · 파일별 .ko.txt · 원래 하위 폴더 구조 보존'+self._output_scope_note())
        else:
            name=(output_stem(self.source_path) if document_kind(self.source_path) else self.source_path.stem)+'.ko.txt' if self.source_path else '결과.ko.txt'
            base=self._output_dir_for(self.source_path) if self.source_path else d
            self.output_label.setText(f'{(base or d) / name}  · 미완료 시 .ko.partial.txt'+self._output_scope_note())

    def _output_scope_note(self) -> str:
        if self._output_fallback: return ' · 이번 작업만 안전 저장 폴더 사용'
        return ' · 지정 폴더(모든 결과에 적용)' if self.custom_output_dir else ''

    def _workspace_key(self):
        path=self.source_path if self.input_mode=='single' else self.batch_root if self.input_mode=='folder' else None
        if not path:
            return None
        source_sha=''
        if self.input_mode=='single' and self.source_bytes:
            source_sha=hashlib.sha256(self.source_bytes).hexdigest()
        return self.workspace_store.key(self.input_mode,path,self.pm.active.id,source_sha)

    def _book_memory_text(self) -> str:
        return self.book_memory.toPlainText().strip()[:8000] if hasattr(self,'book_memory') else ''

    def _effective_book_memory(self) -> str:
        text=self._book_memory_text()
        if self.input_mode=='folder' and self.batch_relation.currentData()=='independent' and not self.batch_memory_common.isChecked():
            return ''
        return text

    def _update_batch_memory_mode(self):
        if not hasattr(self,'batch_memory_common'):
            return
        independent=self.input_mode=='folder' and self.batch_relation.currentData()=='independent'
        running=bool(self.worker and self.worker.isRunning()) if hasattr(self,'worker') else False
        self.batch_memory_common.setEnabled(independent and not running)
        if independent:
            self.batch_memory_common.setToolTip('기본 OFF: 서로 다른 작품 사이에 인물 관계/설정이 섞이지 않게 합니다.')
        else:
            self.batch_memory_common.setToolTip('단일 파일 또는 연속 작품 모드에서는 작품 메모가 현재 작품에 적용됩니다.')

    def _language_options(self) -> dict:
        if not hasattr(self,'lang_option') or self.lang_option.count()==0:
            return {}
        return {str(self.lang_option_label.text()): str(self.lang_option.currentData() or self.lang_option.currentText())}

    def _configure_language_option(self, profile_id: str):
        spec=option_spec(profile_id)
        self.lang_option.blockSignals(True); self.lang_option.clear(); self.lang_option_label.setText(spec['label'])
        for label,value in spec['choices']: self.lang_option.addItem(label,value)
        if self.content_kind=='document' and profile_id=='zh-ko':self.lang_option.setCurrentIndex(self.lang_option.findData('mandarin_names'))
        self.lang_option.blockSignals(False)
        self._last_language_option=self.lang_option.currentData(); self._sync_name_option()
        self._load_sentence_style(profile_id)
        if getattr(self,'_guide_timer',None) and self._guide_timer.isActive():
            self._guide_timer.stop(); self._save_user_guide()   # keep what was typed for the previous language
        self._load_user_guide(profile_id)

    def _edit_guide_big(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        d=QDialog(self); d.setWindowTitle(f'내 번역 지침 · {self.pm.active.display_name}'); d.resize(760,560); v=QVBoxLayout(d)
        ed=QPlainTextEdit(); ed.setPlainText(self.user_guide.toPlainText()); ed.setPlaceholderText(self.user_guide.placeholderText())
        ed.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn); v.addWidget(ed,1)
        count=QLabel(''); count.setStyleSheet('color:#6b7280;'); v.addWidget(count)
        def upd(): n=len(ed.toPlainText()); count.setText(f'{n:,} / 3,000자'+(' · 3,000자를 넘는 부분은 보내지 않습니다' if n>3000 else ''))
        ed.textChanged.connect(upd); upd()
        bb=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); bb.button(QDialogButtonBox.Save).setText('저장'); bb.button(QDialogButtonBox.Cancel).setText('취소')
        bb.accepted.connect(d.accept); bb.rejected.connect(d.reject); v.addWidget(bb)
        self._guide_dialog=d
        if d.exec():
            self.user_guide.setPlainText(ed.toPlainText()); self._guide_timer.stop(); self._save_user_guide()

    def _load_user_guide(self, profile_id: str):
        from ..user_guidelines import load_user_guidelines
        if not hasattr(self,'user_guide'): return
        self._guide_loading=True
        try: self.user_guide.setPlainText(load_user_guidelines(profile_id, DATA_DIR))
        finally: self._guide_loading=False
        self._guide_profile=profile_id

    def _save_user_guide(self):
        from ..user_guidelines import save_user_guidelines, MAX_CHARS
        text=self.user_guide.toPlainText()
        try:
            save_user_guidelines(getattr(self,'_guide_profile',self.pm.active.id), text, DATA_DIR)
            self.user_guide_note.setText(f'저장됨 · {len(text.strip()):,}/{MAX_CHARS:,}자 · 다음 번역 요청부터 적용 (번역 중인 책을 처음부터 다시 번역하지 않음)'
                                         + (' · 3,000자를 넘는 부분은 쓰지 않습니다' if len(text.strip())>MAX_CHARS else ''))
        except Exception as e:
            self.user_guide_note.setText(f'저장 실패: {e}')

    def _sync_name_option(self):
        if not hasattr(self,'name_option'): return
        self.name_option_label.setText(self.lang_option_label.text())
        if self.name_option.currentIndex()!=self.lang_option.currentIndex():
            self.name_option.blockSignals(True); self.name_option.setCurrentIndex(self.lang_option.currentIndex()); self.name_option.blockSignals(False)

    def _name_option_changed(self, index: int):
        if index>=0 and index!=self.lang_option.currentIndex():
            self.lang_option.setCurrentIndex(index)   # -> _language_option_changed

    def _language_option_changed(self):
        prev=getattr(self,'_last_language_option',None); now=self.lang_option.currentData()
        self._last_language_option=now
        self._sync_name_option(); self._save_workspace(); self._update_ready_state()
        if prev is not None and prev!=now and self.pm.active.id=='zh-ko' and self._terms() and not self._loading_workspace_option:
            self._offer_reextract_names()

    def _offer_reextract_names(self):
        """The name reading changed but the glossary still holds names in the old reading
        (confirmed terms always win), so offer to extract them again in the new reading."""
        if (self.worker and self.worker.isRunning()) or self._extracting():
            return
        source=self.preview.toPlainText() if self._preview_edited() else (self.source_text if self.input_mode=='single' else '')
        if not source.strip():
            self.status.setText('이름 표기를 바꿨습니다 · 확정 용어집에 이미 있는 이름은 그대로이니 필요하면 고치거나 지우세요'); return
        box=QMessageBox(self); box.setIcon(QMessageBox.Question); box.setWindowTitle('이름 표기 변경')
        box.setText(f'확정 용어집의 {len(self._terms())}개 항목은 이전 표기 방식으로 만들어졌을 수 있습니다.\n'
                    '확정 용어집이 이름 표기 설정보다 우선하므로, 그대로 두면 이전 방식의 이름이 계속 쓰입니다.\n\n'
                    '새 방식으로 이름을 다시 추출할까요? (같은 원문 용어의 한국어 표기를 새로 뽑은 표기로 바꿉니다)')
        redo=box.addButton('새 방식으로 다시 추출',QMessageBox.AcceptRole); box.addButton('그대로 두기',QMessageBox.RejectRole)
        box.setDefaultButton(redo); box.exec()
        if box.clickedButton() is redo:
            self._term_replace=True
            if not self._begin_term_extraction(source, auto=False):
                self._term_replace=False

    def _encoding_changed(self):
        if self.input_mode=='single' and self.source_bytes:
            try:
                self.source_text,enc=self._decode_source(self.pm.active.source_language,self.encoding.currentData() or 'auto')
                self.file_label.setText(self._single_label(self.source_path.name,enc,self.source_text,self.source_bytes))
                self._set_preview_source(self.source_text); self._rechunk()
                if self._source_doc_kind():
                    self.status.setText(f'{self._source_doc_kind()}에는 TXT 인코딩 설정이 적용되지 않습니다 (꺼낸 글자를 그대로 사용).')
            except Exception as e:
                self.source_text=''; self.paragraphs=[]; self.chunks=[]; self.status.setText(f'인코딩 해석 실패: {e}')
        elif self.input_mode=='folder':
            self.status.setText('인코딩 설정이 변경되었습니다. 폴더의 각 TXT는 처리 차례에 이 설정으로 읽습니다 (PDF·EPUB에는 적용되지 않음).')
        self._save_workspace(); self._update_ready_state()

    def _save_workspace(self):
        if not hasattr(self,'workspace_store'): return
        key=self._workspace_key()
        if not key:return
        try:
            self.workspace_store.put(key,{
                'book_memory':self._book_memory_text(),
                'language_option': self.lang_option.currentData() if hasattr(self,'lang_option') else None,
                'terms':self._terms() if hasattr(self,'term_table') else {},
                'term_locks':{a:b for a,b in getattr(self,'_term_locks',{}).items() if self._terms().get(a)==b} if hasattr(self,'term_table') else {},
                'encoding':self.encoding.currentData() if hasattr(self,'encoding') else 'auto',
                'mode':self.mode.currentData() if hasattr(self,'mode') else 'balanced',
                'model':self._selected_model_id() if hasattr(self,'model') else '',
                'include_subfolders': bool(self.include_sub.isChecked()) if hasattr(self,'include_sub') else False,
                'batch_relation': self.batch_relation.currentData() if hasattr(self,'batch_relation') else 'independent',
                'batch_memory_common': bool(self.batch_memory_common.isChecked()) if hasattr(self,'batch_memory_common') else False,
                'auto_terms_sha': getattr(self,'_auto_terms_sha',''),
                'language_chosen': bool(getattr(self,'_lang_chosen',False)),
                'speech': self._speech_rows() if hasattr(self,'speech_table') else [],
            })
        except Exception:
            pass

    def _load_workspace(self):
        key=self._workspace_key()
        if not key:return
        state=self.workspace_store.get(key)
        self._lang_chosen=bool(state.get('language_chosen')) if state else False
        if not state:return
        if hasattr(self,'book_memory'): self.book_memory.setPlainText(str(state.get('book_memory') or ''))
        self._auto_terms_sha=str(state.get('auto_terms_sha') or '')
        enc=state.get('encoding'); enc='cp950' if enc=='big5' else enc; idx=self.encoding.findData(enc) if enc else -1
        if idx>=0:
            self.encoding.blockSignals(True); self.encoding.setCurrentIndex(idx); self.encoding.blockSignals(False)
        mode=state.get('mode'); idx=self.mode.findData(mode) if mode else -1
        if idx>=0:
            self.mode.blockSignals(True); self.mode.setCurrentIndex(idx); self.mode.blockSignals(False)
        opt=state.get('language_option'); idx=self.lang_option.findData(opt) if opt else -1
        if idx>=0:
            self.lang_option.blockSignals(True); self.lang_option.setCurrentIndex(idx); self.lang_option.blockSignals(False)
            self._last_language_option=self.lang_option.currentData(); self._sync_name_option()
        model=str(state.get('model') or '').strip()
        if model:
            index=self.model.findData(model.removeprefix('models/'))
            if index>=0:self.model.setCurrentIndex(index)
            else:self.model.setCurrentText(model)
        if self.input_mode=='folder':
            self.include_sub.blockSignals(True); self.include_sub.setChecked(bool(state.get('include_subfolders',False))); self.include_sub.blockSignals(False)
            rel=state.get('batch_relation'); ridx=self.batch_relation.findData(rel) if rel else -1
            if ridx>=0:self.batch_relation.setCurrentIndex(ridx)
            self.batch_memory_common.setChecked(bool(state.get('batch_memory_common',False)))
            self._update_batch_memory_mode()
        if 'speech' in state and isinstance(state.get('speech'),list):
            self._set_speech_rows(state.get('speech'))
        saved_terms=state.get('terms') or {}
        saved_locks=state.get('term_locks') or {}
        self._term_locks={str(a):str(b) for a,b in saved_locks.items()} if isinstance(saved_locks,dict) else {}
        if 'terms' in state and isinstance(saved_terms,dict):
            current={str(a):str(b) for a,b in saved_terms.items() if str(a).strip() and str(b).strip()}
            self._term_internal_edit=True
            self.term_table.setRowCount(0)
            for a,b in current.items():
                r=self.term_table.rowCount(); self.term_table.insertRow(r); self.term_table.setItem(r,0,QTableWidgetItem(a)); self.term_table.setItem(r,1,QTableWidgetItem(b))
            self._term_internal_edit=False
            self._term_locks={a:b for a,b in self._term_locks.items() if current.get(a)==b}

    def _import_terms(self):
        fn,_=QFileDialog.getOpenFileName(self,'확정 용어 TSV 가져오기','','TSV/Text (*.tsv *.txt);;All files (*)')
        if not fn:return
        try:
            text=Path(fn).read_text(encoding='utf-8-sig'); imported=0; terms=self._terms()
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith('#'):continue
                parts=line.split('\t',1)
                if len(parts)!=2:continue
                a,b=parts[0].strip(),parts[1].strip()
                if a and b: terms[a]=b; imported+=1
            self._term_internal_edit=True
            self.term_table.setRowCount(0)
            for a,b in terms.items():
                r=self.term_table.rowCount(); self.term_table.insertRow(r); self.term_table.setItem(r,0,QTableWidgetItem(a)); self.term_table.setItem(r,1,QTableWidgetItem(b))
            self._term_internal_edit=False
            self._term_locks.update({a:b for a,b in terms.items()})
            self._save_workspace(); self.status.setText(f'확정 용어 {imported}개 가져옴')
        except Exception as e: QMessageBox.critical(self,'용어 가져오기 실패',str(e))

    def open_about(self):
        from .about_dialog import AboutDialog
        AboutDialog(self).exec()

    def open_local_setup_manual(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        from ..config import manual_path
        path=manual_path()
        if path:
            url=QUrl.fromLocalFile(str(path))
            url.setFragment('local-setup')
            if not QDesktopServices.openUrl(url):
                self.open_manual()
        else:
            self.open_manual()

    def open_manual(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        from ..config import manual_path
        path=manual_path()
        if not path:
            QMessageBox.information(self,'사용 설명서','사용 설명서 파일(사용설명서.html)을 찾지 못했습니다. 프로그램 폴더를 확인하세요.'); return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self,'사용 설명서',f'브라우저를 열지 못했습니다. 이 파일을 직접 여세요:\n{path}')

    def _load_sentence_style(self, profile_id: str):
        if not hasattr(self,'sentence_style'): return
        try: style=str((load_json(CONFIG_DIR/'app.json').get('sentence_style') or {}).get(profile_id) or 'natural')
        except Exception: style='natural'
        self.sentence_style.blockSignals(True)
        self.sentence_style.setCurrentIndex(max(0,self.sentence_style.findData(style)))
        self.sentence_style.blockSignals(False)

    def _sentence_style_value(self) -> str:
        if not hasattr(self,'sentence_style') or self.content_kind=='document': return 'normal'
        return 'natural' if self.sentence_style.currentData()=='natural' else 'normal'

    def _sentence_style_changed(self, _i=None):
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); styles=dict(cfg.get('sentence_style') or {})
            styles[self.pm.active.id]=self.sentence_style.currentData() or 'normal'
            cfg['sentence_style']=styles; save_json(CONFIG_DIR/'app.json',cfg)
        except Exception:
            pass
        label='자연스럽게' if self.sentence_style.currentData()=='natural' else '보통'
        if self.worker and self.worker.isRunning() and hasattr(self.worker,'set_sentence_style'):
            self.worker.set_sentence_style(self._sentence_style_value())   # the running job switches from its next chunk
            self.status.setText(f'문장 스타일: {label} · 진행 중인 번역의 다음 청크부터 적용 (이미 번역한 부분은 다시 번역하지 않음)')
        else:
            self.status.setText(f'문장 스타일: {label} · 다음 번역부터 적용 (번역 중인 책을 처음부터 다시 번역하지 않음)')

    def _korean_punct_toggled(self, on: bool):
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); cfg['korean_punctuation']=bool(on); save_json(CONFIG_DIR/'app.json',cfg)
        except Exception:
            pass

    def _quote_style_changed(self, _i=None):
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); cfg['quote_style']=str(self.quote_style.currentData() or 'straight'); save_json(CONFIG_DIR/'app.json',cfg)
        except Exception:
            pass

    def _auto_terms_toggled(self, on: bool):
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); cfg['auto_extract_terms']=bool(on); save_json(CONFIG_DIR/'app.json',cfg)
        except Exception:
            pass

    def _extraction_engines(self, purpose: str = 'speech') -> list:
        """Independent analysis choices; translation checkboxes are left unchanged."""
        choices=dict(self.secrets.get('extraction_engines') or {})
        preferred=choices.get(purpose,'auto')
        explicit_model=str(choices.get('model') or '').strip()
        order=self._engine_order()+[n for n in ('gemini','openai','local') if n not in self._engine_order()]
        if preferred in ('gemini','openai','local'):
            order=[preferred]+[n for n in order if n!=preferred]
        out=[]
        for name in order:
            if name=='gemini' and self._enabled_keys() and self._selected_model_id():
                out.append(('gemini',self._enabled_keys()[0].secret,self._selected_model_id(),'',None))
            elif name=='openai' and self._enabled_openai_keys() and str((self.secrets.get('openai') or {}).get('model') or '').strip():
                row=self._openai_worker_settings()
                out.append(('openai',self._enabled_openai_keys()[0].secret,str(row.get('model') or ''),str(row.get('endpoint') or ''),None))
            elif name=='local':
                opts=self._local_worker_settings()
                if str(opts.get('model') or '').strip() and str(opts.get('endpoint') or '').strip():
                    out.append(('local','local',str(opts['model']),str(opts['endpoint']),opts))
        if explicit_model and preferred in ('gemini','openai','local'):
            for i,record in enumerate(out):
                if record[0]==preferred:
                    out[i]=(record[0],record[1],explicit_model,record[3],record[4])
                    break
        return out

    def _extraction_engine(self):
        engines=self._extraction_engines('terms')
        return engines[0] if engines else None

    def _extract_terms(self):
        if self._extracting():
            if isinstance(self._term_thread, TermExtractThread):
                self._term_thread.requestInterruption()
                self.term_extract.setEnabled(False)
                self.status.setText('인명·용어 추출 취소 요청 · 중단하는 중…')
            return
        if self._term_thread and self._term_thread.isRunning():
            return
        source=self.preview.toPlainText() if self._preview_edited() else (self.source_text if self.input_mode=='single' else '')
        if not source.strip():
            QMessageBox.information(self,'원문 필요','TXT·PDF·EPUB 한 파일을 선택하거나 원문 창에 글을 붙여넣은 뒤 실행하세요. (폴더 작업은 첫 파일을 따로 열어 추출하면 같은 작품 전체에 활용할 수 있습니다)'); return
        self._begin_term_extraction(source, auto=False)

    def _begin_term_extraction(self, source: str, auto: bool) -> bool:
        """Start one extraction request. auto=True: no dialogs; translation starts when it ends."""
        self._auto_terms_fail=''
        cands=term_candidates(source,self.pm.active.source_language)
        if not cands:
            if not auto: QMessageBox.information(self,'후보 없음','원문에서 반복되는 이름/용어 후보를 찾지 못했습니다.')
            self._auto_terms_fail='인명·용어 자동 추출 · 원문에서 여러 번 나오는 이름·용어 후보를 찾지 못해 용어집 없이 번역합니다'
            return False
        analysis_engines=self._extraction_engines('terms')
        engine=analysis_engines[0] if analysis_engines else None
        if engine is None:
            self._auto_terms_fail='인명·용어 자동 추출 · 추출에 쓸 엔진이 준비되지 않아 용어집 없이 번역합니다'
            if not auto: QMessageBox.warning(self,'번역 엔진 필요','자동 추출에는 API 설정의 번역 엔진(Gemini·OpenAI·로컬 LLM 중 하나)이 준비되어 있어야 합니다.')
            return False
        provider_id,secret,model,endpoint,local_opts=engine
        from ..language_options import USER_GUIDELINES_KEY
        opts=dict(self._language_options())
        if hasattr(self,'user_guide') and self.user_guide.toPlainText().strip():
            opts[USER_GUIDELINES_KEY]=self.user_guide.toPlainText().strip()
        policy=' '.join(prompt_instructions(opts))
        self._common_pulled=[]
        if not getattr(self,'_term_replace',False):
            self._common_pulled=self._pull_common_terms(source)
        system,user=term_prompts(cands,self.pm.active.source_language,policy,{} if (getattr(self,'_term_replace',False) or not auto) else self._terms())
        self._term_auto=auto; self._term_source_sha=self._source_sha()
        self._lock_inputs(True); self.start.setEnabled(False)
        self.term_extract.setEnabled(True); self.term_extract.setText('용어 추출 취소')
        self.status.setText(('번역 전 ' if auto else '')+f'인명·용어 후보 {len(cands)}개 분석 중 · {provider_id}')
        self._set_analysis_busy(True, '용어 검출 중…', ('번역 전 ' if auto else '')+f'인명·용어 후보 {len(cands)}개 분석')
        if auto:
            self.on_api_state(f'{ {"gemini":"Gemini","openai":"OpenAI","local":"로컬 LLM"}.get(provider_id,provider_id)} · {model} · 번역 전 인명·용어 자동 추출 (요청 1회)')
        self._term_thread=TermExtractThread(provider_id,secret,model,endpoint,system,user,cands,self,local_options=local_opts)
        self._term_thread.known=self._terms()
        self._term_thread.engines=analysis_engines
        self._connect_stage(self._term_thread)
        self._term_result=(False,'추출이 종료되었습니다.')
        self._term_thread.done.connect(lambda ok,res:setattr(self,'_term_result',(ok,res)))
        self._term_thread.finished.connect(self._extract_terms_finished); self._term_thread.start()
        self._update_ready_state()
        return True

    def _merge_terms(self, res, replace: bool=False) -> list[tuple[str,str]]:
        current=self._terms(); added=[]
        self._term_internal_edit=True
        for a,b in (res or {}).items():
            if a in current:
                if not replace and self._term_locks.get(a)==current[a]:
                    continue
                if replace and current[a]!=b:
                    for r in range(self.term_table.rowCount()):
                        it=self.term_table.item(r,0)
                        if it and it.text().strip()==a:
                            self.term_table.setItem(r,1,QTableWidgetItem(b))
                    self._term_locks[a]=b
                    added.append((a,b))
                continue
            r=self.term_table.rowCount(); self.term_table.insertRow(r); self.term_table.setItem(r,0,QTableWidgetItem(a)); self.term_table.setItem(r,1,QTableWidgetItem(b)); added.append((a,b))
        self._term_internal_edit=False
        self._save_workspace()
        return added

    def _extract_terms_finished(self):
        ok,res=self._term_result; thread=self._term_thread; self._term_thread=None
        if thread is not None: thread.deleteLater()
        auto=getattr(self,'_term_auto',False); self._term_auto=False
        self._lock_inputs(False)
        self.term_extract.setText('인명·용어 자동 추출'); self.term_extract.setEnabled(not (self.worker and self.worker.isRunning()))
        self._set_analysis_busy(False)
        if not ok and res=='사용자 취소':
            self._term_replace=False
            self._auto_terms_sha=''
            self._update_ready_state()
            self.status.setText('인명·용어 추출 취소됨 · 결과를 적용하지 않았으며 번역도 시작하지 않았습니다')
            return
        if getattr(self,'_close_after_extract',False):
            self.close(); return
        if ok and getattr(self,'_term_source_sha','')!=self._source_sha():
            # The source changed meanwhile: these names belong to the previous text.
            self.status.setText('원문이 바뀌어 인명·용어 추출 결과를 쓰지 않았습니다'); self._update_ready_state(); return
        if auto:
            # Never block the translation on the extraction: log the outcome and start.
            added=[]
            if ok:
                added=self._merge_terms(res)
                note=self._common_note()+'인명·용어 자동 추출 · '+(f'새 항목 {len(added)}개를 확정 용어집에 추가: '+', '.join(f'{a}→{b}' for a,b in added[:12])+(' …' if len(added)>12 else '') if added else '새로 추가할 항목 없음')+' (확정 용어집 탭에서 고칠 수 있음)'
                self._auto_terms_sha=self._source_sha(); self._save_workspace()
            else:
                note=f'인명·용어 자동 추출 실패 · 용어집 없이 번역을 계속합니다 · {str(res)[:200]}'
            force=self._auto_terms_force
            if getattr(self,'_auto_terms_dialog_force',False) and not added:
                # "추출 후 처음부터" was chosen only to use new terms: with none, resume instead
                # of paying for the whole book again.
                force=False; note+=' · 추가된 용어가 없어 처음부터 다시 하지 않고 이어서 번역합니다'
            self._auto_terms_dialog_force=False
            if self._speech_auto_due():
                self._auto_terms_force=force; self._auto_notes=[note]
                if self._begin_speech_extraction(auto=True):
                    return
                note=' · '.join(x for x in (note,getattr(self,'_speech_fail','')) if x)
            self._auto_start(force,[note])
            return
        self._update_ready_state()
        if not ok:
            self._term_replace=False
            QMessageBox.warning(self,'자동 추출 실패',str(res)); self.status.setText('인명·용어 자동 추출 실패'); return
        replace=getattr(self,'_term_replace',False); self._term_replace=False
        # Never ask about a source whose current rendering has already been locked.
        current=self._terms()
        differing=[(a,current[a],b) for a,b in (res or {}).items()
                   if a in current and current[a]!=b and self._term_locks.get(a)!=current[a]]
        decisions={}
        if differing and not replace:
            from .term_conflict_dialog import TermConflictDialog
            dlg=TermConflictDialog(differing,self)
            if dlg.exec()==QDialog.DialogCode.Accepted:   # an instance has no .Accepted in PySide6 6.12
                decisions=dlg.decisions()
        # Apply independent per-term decisions without silently changing locked rows.
        added=self._merge_terms({a:b for a,b in (res or {}).items() if a not in current},
                                replace=False)
        changed=[]
        if decisions:
            self._term_internal_edit=True
            try:
                for source,target in decisions.items():
                    if source not in current: continue
                    if target!=current[source]:
                        for row in range(self.term_table.rowCount()):
                            item=self.term_table.item(row,0)
                            if item and item.text().strip()==source:
                                self.term_table.setItem(row,1,QTableWidgetItem(target))
                        changed.append((source,target))
                    self._term_locks[source]=target
            finally:
                self._term_internal_edit=False
            self._save_workspace()
        elif replace:
            # Explicit replacement does not override manual or reviewed locks.
            changed=self._merge_terms(res,replace=True)
        added+=changed
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.term_table.parentWidget()))
        msg=(f'확정 용어 {len(added)}개를 새로 추가하거나 변경했습니다.' if added
             else '새로 추가하거나 바꿀 인명·용어가 없습니다.')
        self.status.setText(msg)
        if added: QMessageBox.information(self,'자동 추출 완료',msg+'\n\n자동 제안이므로 표기를 확인하고, 틀린 항목은 고치거나 삭제한 뒤 번역을 시작하세요.')

    # ------------------------------------------------------------ 내 공용 용어집
    def _log_style_check(self, path, status):
        """번역투 점검 (local, no request): lets the user compare 보통 / 자연스럽게 on the same chapter."""
        if self.content_kind=='document' or not path: return
        try:
            from .. import style_metrics
            text=Path(path).read_text(encoding='utf-8-sig',errors='replace')
            total,rate,found=style_metrics.summary(text)
        except Exception:
            return
        style='자연스럽게' if getattr(self.worker,'sentence_style','normal')=='natural' else '보통'
        changes=list(getattr(self.worker,'style_changes',[]) or [])
        if changes:
            style+=' (번역 중 변경: '+', '.join(f"청크 {n}부터 {'자연스럽게' if v=='natural' else '보통'}" for n,v in changes)+')'
        top=', '.join(f'{k} {v}' for k,v in sorted(found.items(),key=lambda kv:-kv[1])[:4])
        left=int(getattr(self.worker,'leftover_count',0) or 0)
        self.on_worker_issue(f'문장 점검 · 문장 스타일 {style} · 번역투 표현 {total:,}개 (한글 1,000자당 {rate:.1f})'+(f' · {top}' if top else '')
                             +(' · 미번역 문단 있음' if status=='partial' or left else ' · 미번역 없음'))

    def _common_enabled(self) -> bool:
        return hasattr(self,'use_common') and self.use_common.isChecked()

    def _common_terms_for_job(self) -> dict:
        if not self._common_enabled(): return {}
        from .. import common_terms
        try: return common_terms.load(self.pm.active.source_language, DATA_DIR)
        except Exception: return {}

    def _pull_common_terms(self, source: str) -> list[tuple[str,str]]:
        """Shared entries that occur in this text (2+ times) and are not in the book's glossary yet."""
        if not self._common_enabled() or not source: return []
        from .. import common_terms
        current=self._terms()
        found={a:b for a,b in common_terms.present_in(source,self._common_terms_for_job()).items() if a not in current}
        return self._merge_terms(found) if found else []

    def _common_note(self) -> str:
        got=getattr(self,'_common_pulled',[]) or []
        if not got: return ''
        return f'공용 용어집에서 {len(got)}개 가져옴: '+', '.join(f'{a}→{b}' for a,b in got[:8])+(' …' if len(got)>8 else '')+' · '

    def _grow_common_terms(self):
        if not self._common_enabled(): return
        from .. import common_terms
        lang=self.pm.active.source_language
        try:
            wanted=common_terms.new_candidates(lang,self._terms(),DATA_DIR)
            added=common_terms.add(lang,wanted,DATA_DIR)
        except Exception as e:
            self.on_worker_issue(f'공용 용어집 저장 실패 · {type(e).__name__}: {e}'); return
        if added:
            self.on_worker_issue(f'공용 용어집 · 새 항목 {len(added)}개를 쌓음 (다음 책에도 적용): '+', '.join(f'{a}→{b}' for a,b in added[:8])+(' …' if len(added)>8 else ''))
        left=len(wanted)-len(added)
        if left>0:
            self.on_worker_issue(f'공용 용어집이 가득 찼습니다({common_terms.MAX_PER_LANG:,}개) · 새 항목 {left}개를 쌓지 못함. '
                                 '[공용 용어집]을 열어 틀렸거나 더 쓰지 않을 항목을 지워 정리하세요.')
        self._update_common_btn()

    def _update_common_btn(self):
        try:
            from .. import common_terms
            n=len(common_terms.load(self.pm.active.source_language, DATA_DIR))
            full=n>=common_terms.MAX_PER_LANG
            self.common_btn.setText((f'공용 용어집 ({n:,} · 가득 참)' if full else f'공용 용어집 ({n:,})') if n else '공용 용어집')
            self.common_btn.setToolTip('공용 용어집이 가득 찼습니다. 눌러서 틀렸거나 더 쓰지 않을 항목을 지워 정리하세요.' if full else '')
        except Exception:
            pass

    def _open_common_terms(self):
        from .. import common_terms
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit
        lang=self.pm.active.source_language
        entries=common_terms.load(lang, DATA_DIR)
        d=QDialog(self); d.setWindowTitle(f'공용 용어집 · {self.pm.active.display_name}'); d.resize(620,560); v=QVBoxLayout(d)
        info=QLabel('책을 번역할 때마다 확정 용어 중 새것이 여기에 쌓이고, 새 책에 자동으로 쓰입니다. 틀린 항목은 고치거나 지우세요.'); info.setWordWrap(True); v.addWidget(info)
        cap=common_terms.MAX_PER_LANG
        if len(entries)>=cap:
            full_note=QLabel(f'<b>가득 찼습니다 ({len(entries):,} / {cap:,}개).</b> 더는 새 항목이 쌓이지 않습니다. 틀렸거나 더 쓰지 않을 항목을 골라 [선택&nbsp;삭제]한 뒤 [저장]하세요.')
            full_note.setWordWrap(True); full_note.setObjectName('commonFullNote')
            full_note.setStyleSheet('QLabel#commonFullNote{background:#fff4d6;border:1px solid #d9a400;color:#5a4300;padding:6px;}')
            v.addWidget(full_note)
        pending={a:b for a,b in self._terms().items() if a not in entries and common_terms.acceptable(a,b)}
        room=max(0,cap-len(entries))
        if len(pending)>room: pending=dict(list(pending.items())[:room])
        prow=QHBoxLayout()
        plabel=QLabel(f'지금 확정 용어집에서 새로 쌓일 항목: <b>{len(pending)}개</b>'+(' (자리가 없어 더는 쌓이지 않음)' if not room else '')+(' — '+', '.join(f'{a}→{b}' for a,b in list(pending.items())[:6])+(' …' if len(pending)>6 else '') if pending else ('' if not room else ' (번역을 시작할 때도 자동으로 쌓입니다)')))
        plabel.setWordWrap(True); prow.addWidget(plabel,1)
        add_now=QPushButton('지금 추가'); add_now.setEnabled(bool(pending)); prow.addWidget(add_now); v.addLayout(prow)
        def do_add():
            for a,b in pending.items():
                r=table.rowCount(); table.insertRow(r); table.setItem(r,0,QTableWidgetItem(a)); table.setItem(r,1,QTableWidgetItem(b))
            plabel.setText(f'{len(pending)}개를 목록에 넣었습니다. [저장]을 누르면 쌓입니다.'); add_now.setEnabled(False)
        add_now.clicked.connect(do_add)
        find=QLineEdit(); find.setPlaceholderText('찾기 (원문 또는 한국어)'); v.addWidget(find)
        table=QTableWidget(0,2); table.setHorizontalHeaderLabels(['원문','한국어']); table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); table.verticalHeader().setVisible(False)
        for a,b in sorted(entries.items()):
            r=table.rowCount(); table.insertRow(r); table.setItem(r,0,QTableWidgetItem(a)); table.setItem(r,1,QTableWidgetItem(b))
        v.addWidget(table,1)
        def apply_filter(t):
            t=t.strip()
            for r in range(table.rowCount()):
                a=table.item(r,0).text() if table.item(r,0) else ''; b=table.item(r,1).text() if table.item(r,1) else ''
                table.setRowHidden(r, bool(t) and t not in a and t not in b)
        find.textChanged.connect(apply_filter)
        row=QHBoxLayout(); dele=QPushButton('선택 삭제'); row.addWidget(dele); row.addStretch(1)
        dele.clicked.connect(lambda: [table.removeRow(r) for r in sorted({i.row() for i in table.selectedIndexes()},reverse=True)])
        bb=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); bb.button(QDialogButtonBox.Save).setText('저장'); bb.button(QDialogButtonBox.Cancel).setText('취소')
        bb.accepted.connect(d.accept); bb.rejected.connect(d.reject); row.addWidget(bb); v.addLayout(row)
        self._common_dialog=d
        if d.exec():
            out={}
            for r in range(table.rowCount()):
                a=table.item(r,0).text().strip() if table.item(r,0) else ''; b=table.item(r,1).text().strip() if table.item(r,1) else ''
                if a and b: out[a]=b
            common_terms.replace(lang,out,DATA_DIR); self._update_common_btn()
            self.status.setText(f'공용 용어집 저장 · {len(out):,}개'+(' · 가득 참 — 지워서 정리하세요' if len(out)>=cap else ''))

    def _auto_start(self, force: bool, notes):
        """Start the translation after the automatic extraction(s); their notes go to the fresh log."""
        self._auto_terms_passed=True
        self.start_translation(force)
        for n in notes or []:
            if n: self.on_worker_issue(n)   # after the start, which clears the log for the new job
        if not (self.worker and self.worker.isRunning()):
            self._auto_terms_passed=False; self._update_ready_state()   # a check after the extraction stopped the start

    # ------------------------------------------------------------ 인물 말투표
    def _build_speech_tab(self):
        from ..speech_levels import LEVELS, LEVEL_HINTS
        tab=QWidget(); v=QVBoxLayout(tab)
        help=QLabel('인물끼리 서로 어떤 말투(반말·존댓말)를 쓰는지 정해 두면, 책 전체에서 같은 두 사람의 말투가 흔들리지 않습니다. '
                    '번역을 시작할 때 확정 용어집의 인물 이름과 원문 대사 일부로 자동으로 만들어지므로, 대부분 손댈 필요가 없습니다. '
                    '틀린 줄만 고치거나 지우세요. 고쳐도 처음부터 다시 번역하지 않고 다음 요청부터 적용됩니다.')
        help.setWordWrap(True); help.setStyleSheet('color:#4b5563;'); v.addWidget(help)
        self.speech_table=QTableWidget(0,4); self.speech_table.setHorizontalHeaderLabels(['화자','상대','말투','관계·메모'])
        hh=self.speech_table.horizontalHeader(); hh.setSectionResizeMode(QHeaderView.Stretch)
        self.speech_table.verticalHeader().setVisible(False); self.speech_table.setMinimumHeight(150)
        self.speech_table.setToolTip('\n'.join(f'{lv}: {LEVEL_HINTS[lv]}' for lv in LEVELS))
        v.addWidget(self.speech_table,1)
        row=QHBoxLayout()
        self.speech_add=QPushButton('말투 추가'); self.speech_add.clicked.connect(lambda: self._add_speech_row({}))
        self.speech_delete=QPushButton('말투 삭제'); self.speech_delete.clicked.connect(self._delete_speech_rows)
        self.speech_generate=QPushButton('말투표 자동 생성'); self.speech_generate.clicked.connect(self._generate_speech)
        self.speech_generate.setToolTip('확정 용어집의 인물 이름과 원문 대사 일부(책 전체에서 고르게 약 6천 자)를 엔진에 보내, 누가 누구에게 어떤 말투를 쓰는지 표로 받습니다. 요청 1회.')
        self.auto_speech=QCheckBox('번역 시작 시 자동 생성')
        self.auto_speech.setToolTip('말투표가 비어 있으면 번역을 시작할 때 한 번 자동으로 만듭니다(파일·붙여넣은 글). 실패해도 번역은 그대로 시작합니다.')
        try: self.auto_speech.setChecked(bool(load_json(CONFIG_DIR/'app.json').get('auto_speech_table',True)))
        except Exception: self.auto_speech.setChecked(True)
        for w in (self.speech_add,self.speech_delete,self.speech_generate,self.auto_speech): row.addWidget(w)
        row.addStretch(1); v.addLayout(row)
        self.tabs.addTab(tab,'인물 말투'); self.speech_tab=tab
        for sig in (self.speech_table.model().rowsInserted, self.speech_table.model().rowsRemoved, self.speech_table.model().modelReset):
            sig.connect(lambda *_: self._update_speech_tab_title())
        self._speech_tried=set(); self._speech_fail=''; self._auto_notes=[]
        self.speech_table.itemChanged.connect(lambda _i: self._save_workspace() if not getattr(self,'_filling_speech',False) else None)

    def _update_speech_tab_title(self):
        try:
            n=len(self._speech_rows()) if hasattr(self,'speech_table') else 0
            self.tabs.setTabText(self.tabs.indexOf(self.speech_tab),f'인물 말투 ({n})' if n else '인물 말투')
        except RuntimeError:
            pass

    def _add_speech_row(self, r: dict):
        from ..speech_levels import LEVELS, normalize_level
        self._filling_speech=True
        try:
            i=self.speech_table.rowCount(); self.speech_table.insertRow(i)
            self.speech_table.setItem(i,0,QTableWidgetItem(str(r.get('speaker',''))))
            self.speech_table.setItem(i,1,QTableWidgetItem(str(r.get('listener',''))))
            from .table_combo import TableComboBox
            combo=TableComboBox(); combo.addItems(LEVELS)
            lv=normalize_level(r.get('level','')) or '반말'; combo.setCurrentIndex(max(0,combo.findText(lv)))
            combo.currentIndexChanged.connect(lambda _i: (self._save_workspace(), self._update_speech_tab_title()))
            self.speech_table.setCellWidget(i,2,combo)
            self.speech_table.setItem(i,3,QTableWidgetItem(str(r.get('note',''))))
        finally:
            self._filling_speech=False
        if not r: self.speech_table.setCurrentCell(i,0); self.speech_table.editItem(self.speech_table.item(i,0))
        self._update_speech_tab_title()

    def _delete_speech_rows(self):
        for r in sorted({i.row() for i in self.speech_table.selectedIndexes()} | ({self.speech_table.currentRow()} if self.speech_table.currentRow()>=0 else set()), reverse=True):
            self.speech_table.removeRow(r)
        self._save_workspace()

    def _speech_rows(self) -> list[dict]:
        from ..speech_levels import clean_rows
        rows=[]
        for i in range(self.speech_table.rowCount()):
            cell=lambda c: (self.speech_table.item(i,c).text().strip() if self.speech_table.item(i,c) else '')
            combo=self.speech_table.cellWidget(i,2)
            rows.append({'speaker':cell(0),'listener':cell(1),'level':combo.currentText() if combo else '','note':cell(3)})
        return clean_rows(rows)

    def _set_speech_rows(self, rows):
        from ..speech_levels import clean_rows
        if not hasattr(self,'speech_table'): return
        self.speech_table.setRowCount(0)
        for r in clean_rows(rows): self._add_speech_row(r)
        self._update_speech_tab_title()

    def _speech_names(self) -> dict[str,str]:
        """Book glossary first; shared entries are considered only if they occur in this book."""
        from ..term_extraction import valid_rendering, COMMON_WORDS
        out={}
        merged=dict(self._terms())
        if self._common_enabled():
            from ..common_terms import present_in
            for src, ko in present_in(self._speech_source(), self._common_terms_for_job()).items():
                merged.setdefault(src, ko)
        from ..term_extraction import drop_name_phrases
        merged=drop_name_phrases(merged)   # 陈屿放 -> 진서가 놓다 is not a character
        for src,ko in merged.items():
            if src in COMMON_WORDS or not valid_rendering(src,ko): continue
            if not re.fullmatch(r'[가-힣 ]{1,10}',ko.strip()): continue
            out[src]=ko.strip()
        return out

    def _speech_source(self) -> str:
        return self.preview.toPlainText() if self._preview_edited() else (self.source_text if self.input_mode=='single' else '')

    def _speech_auto_due(self) -> bool:
        if not (hasattr(self,'auto_speech') and self.auto_speech.isChecked()) or self.input_mode!='single': return False
        if self._speech_rows() or self.pm.active.source_language not in ('zh','en','ja'): return False
        key=self._source_sha() or hashlib.sha256(self._speech_source().encode('utf-8')).hexdigest()
        return key not in self._speech_tried and len(self._speech_names())>=2

    def _generate_speech(self):
        if self._extracting():
            if isinstance(self._term_thread, SpeechExtractThread):
                self._term_thread.requestInterruption()
                self.speech_generate.setEnabled(False)
                self.status.setText('말투표 생성 취소 요청 · 중단하는 중…')
            return
        if self.worker and self.worker.isRunning(): return
        if len(self._speech_names())<2:
            QMessageBox.information(self,'인물 이름 필요','말투표는 확정 용어집의 인물 이름을 바탕으로 만듭니다. 먼저 확정 용어집 탭에서 [인명·용어 자동 추출]로 이름을 채워 주세요.'); return
        if not self._speech_source().strip():
            QMessageBox.information(self,'원문 필요','TXT·PDF·EPUB 한 파일을 선택하거나 원문 창에 글을 붙여넣은 뒤 실행하세요.'); return
        if self._speech_rows():
            if QMessageBox.question(self,'말투표 다시 만들기','지금 말투표를 새로 만든 것으로 바꿀까요?')!=QMessageBox.Yes: return
        if not self._begin_speech_extraction(auto=False) and self._speech_fail:
            QMessageBox.warning(self,'말투표 자동 생성',self._speech_fail)

    def _begin_speech_extraction(self, auto: bool) -> bool:
        from ..speech_levels import sample_dialogue, build_prompts, parse_table, split_samples
        from ..lexical_guidance import extra_pack
        self._speech_fail=''
        names=self._speech_names(); source=self._speech_source(); lang=self.pm.active.source_language
        key=self._source_sha() or hashlib.sha256(source.encode('utf-8')).hexdigest(); self._speech_tried.add(key); self._speech_key=key
        cues=[c for c in list(getattr(self.pm.active,'lexical_guidance',{}) or {})+list(extra_pack(lang)) if 0<len(c)<=(12 if lang=='en' else 4)]
        samples=sample_dialogue(source,names,cues=cues)
        if not samples:
            self._speech_fail='인물 말투표 · 원문에서 인물 이름이 나오는 대사를 찾지 못해 말투표 없이 번역합니다'; return False
        engines=self._extraction_engines()
        if not engines:
            self._speech_fail='인물 말투표 · 쓸 수 있는 엔진이 없어 말투표 없이 번역합니다'; return False
        provider_id=engines[0][0]
        prompts=[build_prompts(samples,names,lang)]
        fallback_prompts=[build_prompts(part,names,lang) for part in split_samples(samples)]
        self._speech_auto=auto; self._term_source_sha=self._source_sha()
        self._lock_inputs(True); self.start.setEnabled(False)
        self.speech_generate.setEnabled(True); self.speech_generate.setText('말투표 생성 취소'); self.status.setText(('번역 전 ' if auto else '')+f'인물 말투표 만드는 중 · 인물 {len(names)}명 · {provider_id}')
        self._speech_skip=False
        self._set_analysis_busy(True, '말투표 건너뛰고 번역 시작' if auto else '말투표 검출 중…',
                                ('번역 전 ' if auto else '')+f'인물 말투표 만드는 중 · 인물 {len(names)}명')
        self._term_thread=SpeechExtractThread(engines,prompts,lambda text,n=dict(names): parse_table(text,n),self,
                                              fallback_prompts=fallback_prompts)
        self._connect_stage(self._term_thread)
        self._speech_result=(False,'말투표 만들기가 종료되었습니다.')
        self._term_thread.done.connect(lambda ok,res:setattr(self,'_speech_result',(ok,res)))
        self._term_thread.finished.connect(self._extract_speech_finished); self._term_thread.start()
        self._update_ready_state()
        return True

    def _extract_speech_finished(self):
        ok,res=self._speech_result; thread=self._term_thread; self._term_thread=None
        if thread is not None: thread.deleteLater()
        auto=getattr(self,'_speech_auto',False); self._speech_auto=False
        skipped=bool(getattr(self,'_speech_skip',False)); self._speech_skip=False
        self._lock_inputs(False); self.speech_generate.setText('말투표 자동 생성'); self._set_analysis_busy(False)
        if getattr(self,'_close_after_extract',False):
            self.close(); return
        self.speech_generate.setEnabled(True)
        if not ok and res=='사용자 취소' and auto and skipped:
            # [말투표 건너뛰고 번역 시작]: this book's automatic attempt is over (it is not
            # asked again on resume); the button on the 인물 말투 tab can still build it later.
            self._auto_start(getattr(self,'_auto_terms_force',False), list(getattr(self,'_auto_notes',[]))+[
                '인물 말투표 · 건너뜀 (사용자 선택) · 말투표 없이 번역합니다 · 필요하면 인물 말투 탭의 [말투표 자동 생성]으로 나중에 만들 수 있습니다'])
            self._auto_notes=[]
            return
        if not ok and res=='사용자 취소':
            # A cancelled attempt is not a finished one: the next start tries again.
            self._speech_tried.discard(getattr(self,'_speech_key',''))
            self._update_ready_state()
            self.status.setText('말투표 자동 생성 취소됨 · 번역은 시작하지 않았습니다')
            return
        stale=ok and getattr(self,'_term_source_sha','')!=self._source_sha()
        info=res if ok and isinstance(res,dict) else {'rows':list(res) if ok else []}
        rows=list(info.get('rows') or []) if ok and not stale else []
        if rows: self._set_speech_rows(rows); self._save_workspace()
        if ok and not stale:
            parts=info.get('parts',1); failed=info.get('failed',0); used=info.get('used') or []
            requests=info.get('requests',0); conflicts=info.get('conflicts',0)
            how=(f' · 분할 분석 {parts}묶음 중 {parts-failed}묶음 성공' if parts>1 else ' · 전체 문맥 1회 분석')+f' · AI 요청 {requests}회'+(f' · 말투 충돌 {conflicts}쌍 제외' if conflicts else '')+(' · '+'·'.join(ENGINE_LABELS.get(u,u) for u in used) if used else '')
            note=(f'인물 말투표 자동 생성 · {len(rows)}쌍{how} (인물 말투 탭에서 확인·수정할 수 있음): '+', '.join(f"{r['speaker']}→{r['listener']} {r['level']}" for r in rows[:6])+(' …' if len(rows)>6 else '')
                  if rows else '인물 말투표 · 대사에서 확실한 관계를 찾지 못해 말투표 없이 번역합니다'+how)
        else:
            hint=(' · 엔진이 대사 내용을 거부했습니다. API 설정에서 로컬 LLM이나 OpenAI를 보조 엔진으로 켜 두면 그 엔진이 만듭니다. [말투 추가]로 직접 넣어도 됩니다.'
                  if any(k in str(res) for k in ('POLICY','SAFETY','REFUSAL')) else '')
            note=('인물 말투표 만들기 실패 · 말투표 없이 번역합니다 · '+str(res)[:160]+hint) if not stale else '원문이 바뀌어 말투표 결과를 쓰지 않았습니다'
        if auto:
            self._auto_start(getattr(self,'_auto_terms_force',False), list(getattr(self,'_auto_notes',[]))+[note]); self._auto_notes=[]
            return
        self._update_ready_state(); self.status.setText(note[:120])
        if ok and rows:
            self.tabs.setCurrentWidget(self.speech_tab)
            QMessageBox.information(self,'말투표 자동 생성',f'{len(rows)}쌍의 말투를 정했습니다. 틀린 줄이 있으면 고치거나 지우세요.')
        else:
            QMessageBox.warning(self,'말투표 자동 생성',note)

    def _source_sha(self) -> str:
        return hashlib.sha256((self.source_text or '').encode('utf-8')).hexdigest() if self.source_text else ''

    def _auto_terms_due(self) -> bool:
        return self._auto_terms_plan()[0]=='extract'

    def _auto_terms_plan(self) -> tuple[str,str]:
        """('extract'|'ask'|'skip', note for the log). Every skip says why in the log,
        so a glossary that stays empty is never a silent surprise."""
        if not (hasattr(self,'auto_terms') and self.auto_terms.isChecked()):
            return 'skip',''
        if self.input_mode!='single':
            return 'skip','인명·용어 자동 추출 · 폴더 번역에서는 하지 않습니다 (첫 파일을 따로 열어 추출하면 같은 작품 전체에 쓸 수 있음)'
        if not self.source_text.strip():
            return 'skip',''
        have=len(self._terms())
        if have and self._auto_terms_sha==self._source_sha():
            return 'skip',f'인명·용어 자동 추출 · 이 원문은 이미 추출함 · 확정 용어집 {have}개로 번역'
        # A file that already has translated chunks is a resume: adding glossary terms now
        # changes the job identity, so everything would be translated again. Ask when the
        # glossary is empty (the user expects one); otherwise keep the current glossary.
        if not self._force_restart:
            try:
                sha=hashlib.sha256(self.source_bytes).hexdigest() if self.source_bytes else ''
                resume=bool(sha) and Database(DATA_DIR/'translations.db').has_progress_for_source(sha,self.pm.active.id)
            except Exception:
                resume=False
            if resume:
                if not have and self._source_sha() in self._auto_terms_declined:
                    return 'skip','인명·용어 자동 추출 생략 · 이 파일은 용어집 없이 이어서 번역하기로 선택함'
                if not have:
                    return 'ask',''
                return 'skip',f'인명·용어 자동 추출 생략 · 이어 번역 중인 파일이라 현재 확정 용어집 {have}개를 그대로 씁니다 (새로 추출하려면 처음부터 다시)'
        return 'extract',''

    def _ask_extract_on_resume(self) -> str:
        """'extract' (then restart from the beginning), 'resume' (no glossary) or 'cancel'."""
        box=QMessageBox(self); box.setIcon(QMessageBox.Question); box.setWindowTitle('인명·용어 자동 추출')
        box.setText('이 파일은 이전에 번역하던 기록이 있고, 확정 용어집이 비어 있습니다.\n\n'
                    '지금 인명·용어를 추출하면 용어집이 바뀌므로 이전 번역을 이어 쓰지 않고 처음부터 다시 번역합니다.')
        extract=box.addButton('추출 후 처음부터 번역',QMessageBox.AcceptRole)
        resume=box.addButton('용어집 없이 이어서 번역',QMessageBox.ActionRole)
        box.addButton('취소',QMessageBox.RejectRole)
        box.setDefaultButton(extract); box.exec()
        clicked=box.clickedButton()
        return 'extract' if clicked is extract else 'resume' if clicked is resume else 'cancel'

    # ---------------------------------------------------------------- 원문 창
    def _set_preview_source(self, text: str):
        """Fill the 원문 window programmatically (not an edit by the user)."""
        self._preview_loading=True
        try:
            self._preview_mode='source'; self.view_source_btn.setChecked(True); self.view_result_btn.setChecked(False)
            self.preview.setReadOnly(self.input_mode=='folder')
            self.preview.setPlainText(text or '')
            self._preview_baseline=self.preview.toPlainText()
        finally:
            self._preview_loading=False

    def _preview_edited(self) -> bool:
        """True when the 원문 window holds pasted/edited text that differs from the loaded file."""
        if not hasattr(self,'preview') or self.input_mode=='folder' or self._preview_mode!='source':
            return False
        current=self.preview.toPlainText()
        return bool(current.strip()) and current!=self._preview_baseline

    def _preview_changed(self):
        if self._preview_loading or self._preview_mode!='source':
            return
        if self._preview_edited():
            n=len(self.preview.toPlainText())
            base=f' (열린 파일 {self.source_path.name} 수정본)' if (self.input_mode=='single' and self.source_path and not self._paste_active) else ''
            self.file_label.setText(f'원문 창의 글 · {n:,}자{base} · [번역 시작 / 계속]을 누르면 이 내용을 번역합니다')
        self._update_ready_state()

    def _use_preview_as_source(self) -> bool:
        """Make the 원문 window's text the job source (temporary TXT), keeping terms/memory."""
        from datetime import datetime
        text=self.preview.toPlainText()
        folder=DATA_DIR/'paste'; folder.mkdir(parents=True,exist_ok=True)
        from_file=self.input_mode=='single' and self.source_path and not self._paste_active
        stem=(self.source_path.stem+'_수정') if from_file else '붙여넣기'
        if not from_file:
            self._origin_kind=''   # new text, not an edit of an opened PDF/EPUB
        path=folder/f'{stem}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
        raw=text.encode('utf-8')
        try: path.write_bytes(raw)
        except Exception as e:
            QMessageBox.critical(self,'임시 파일 저장 실패',str(e)); return False
        self._save_workspace()
        self.input_mode='single'; self.batch_root=None; self.batch_files=[]; self.batch_index=-1; self.batch_results={}; self.batch_selection=None
        self.batch_relation.setEnabled(False); self.include_sub.setEnabled(False); self._update_batch_memory_mode()
        self.source_path=path; self.source_bytes=raw; self.source_text=text; self._paste_active=True
        self._preview_baseline=self.preview.toPlainText()
        self._rechunk(); self._refresh_job_table(); self._update_output_label()
        self.file_label.setText(f'원문 창의 글 · {len(text):,}자 · 임시 원문 {path.name} (이미 번역한 부분은 번역 기억으로 재사용)')
        if not self.paragraphs:
            QMessageBox.information(self,'번역할 내용 없음','원문 창에서 번역할 문장을 찾지 못했습니다.'); return False
        return True

    def _set_result(self, text: str):
        self._result_text=text or ''
        has=bool(self._result_text.strip())
        self.view_result_btn.setEnabled(has); self.result_copy_btn.setEnabled(has); self.result_save_btn.setEnabled(has)
        if not has and self._preview_mode=='result':
            self._show_preview('source')

    def _show_preview(self, mode: str):
        if mode=='result' and not self._result_text.strip():
            mode='source'
        if mode==self._preview_mode:
            self.view_source_btn.setChecked(mode=='source'); self.view_result_btn.setChecked(mode=='result'); return
        self._preview_loading=True
        try:
            if mode=='result':
                self._source_draft=self.preview.toPlainText()   # keep unsaved edits/pastes
                self.preview.setPlainText(self._result_text); self.preview.setReadOnly(True)
            else:
                self.preview.setPlainText(getattr(self,'_source_draft',self._preview_baseline))
                self.preview.setReadOnly(self.input_mode=='folder' or bool(self.worker and self.worker.isRunning()))
            self._preview_mode=mode
        finally:
            self._preview_loading=False
        self.view_source_btn.setChecked(mode=='source'); self.view_result_btn.setChecked(mode=='result')

    def _copy_result(self):
        if self._result_text.strip():
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self._result_text); self.status.setText('번역 결과를 클립보드에 복사했습니다.')

    TXT_FILTER='Text (*.txt)'
    PDF_FILTER='PDF · 읽기용 (*.pdf)'
    EPUB_FILTER='EPUB · 읽기용 (*.epub)'

    def _save_result(self):
        if not self._result_text.strip(): return
        # A book opened from a PDF/EPUB can also be saved in that format; TXT stays the default.
        # The 원문 window's edit of an opened PDF/EPUB is still that book (its origin format is kept).
        kind=(document_kind(self.source_path) or (getattr(self,'_origin_kind','') if self._paste_active else '')) if self.source_path else ''
        stem=(output_stem(self.source_path) if document_kind(self.source_path) else self.source_path.stem) if self.source_path else '번역결과'
        name=stem+'.ko.txt'
        base=(self._output_dir_for(self.source_path) if self.source_path else None) or self.custom_output_dir
        extra={'PDF':self.PDF_FILTER,'EPUB':self.EPUB_FILTER}.get(kind,'')
        filters=f'{self.TXT_FILTER};;{extra}' if extra else self.TXT_FILTER
        fn,chosen=QFileDialog.getSaveFileName(self,'번역 결과 저장',str(Path(base)/name) if base else name,filters,self.TXT_FILTER)
        if not fn: return
        out=Path(fn)
        ext='.'+kind.lower() if kind else ''
        as_doc=bool(extra) and (chosen==extra or out.suffix.lower()==ext)
        try:
            if as_doc:
                if out.suffix.lower()!=ext:
                    out=out.with_name((out.name[:-len('.ko.txt')] if out.name.lower().endswith('.ko.txt') else out.stem)+'.ko'+ext)
                self._save_result_document(out,stem,kind)
            else:
                atomic_write_text(out,self._result_text); self.status.setText(f'저장했습니다: {out}')
        except Exception as e: QMessageBox.critical(self,'저장 실패',str(e))

    def _save_result_document(self, out: Path, title: str, kind: str):
        """The translation as a reading PDF/EPUB (Korean font, paragraph spacing, chapter titles; page numbers in the PDF)."""
        from PySide6.QtWidgets import QApplication
        self.status.setText(f'{kind} 만드는 중 · {out.name}')
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            QApplication.processEvents()
            if kind=='PDF':
                from ..pdf_export import write_reading_pdf
                n=write_reading_pdf(out,self._result_text,title); note=f'{n:,}쪽'
            else:
                from ..epub_export import write_reading_epub
                n=write_reading_epub(out,self._result_text,title); note=f'{n:,}개 장'
        finally:
            QApplication.restoreOverrideCursor()
        self.status.setText(f'{kind}로 저장했습니다 ({note}): {out}')

    def _export_terms(self):
        base=self.custom_output_dir or (self.source_path.parent if self.source_path and not getattr(self,'_paste_active',False) else None)
        fn,_=QFileDialog.getSaveFileName(self,'확정 용어 TSV 내보내기',str(Path(base)/'terms.tsv') if base else 'terms.tsv','TSV (*.tsv);;Text (*.txt)')
        if not fn:return
        try:
            rows=[f'{a}\t{b}' for a,b in self._terms().items()]; atomic_write_text(Path(fn),'\n'.join(rows)+'\n',encoding='utf-8-sig',newline='\n'); self.status.setText(f'용어집 저장: {fn}')
        except Exception as e: QMessageBox.critical(self,'용어 내보내기 실패',str(e))

    # ------------------------------------------------------------ 야문 클린리더 (문단 정리기)
    CLEANREADER_EXE='YamoonCleanReader.exe'
    TOOLS_FILE='external_tools.json'

    def _tools_cfg(self) -> dict:
        try: return dict(load_json(DATA_DIR/self.TOOLS_FILE))
        except Exception: return {}

    def _find_cleanreader(self) -> Path | None:
        saved=str(self._tools_cfg().get('cleanreader') or '').strip()
        if saved and Path(saved).is_file(): return Path(saved)
        base=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[2]
        cands=[base/self.CLEANREADER_EXE, base/'YamoonCleanReader'/self.CLEANREADER_EXE]
        try: cands+=sorted(base.parent.glob('*/'+self.CLEANREADER_EXE))+sorted(base.parent.glob('*/*/'+self.CLEANREADER_EXE))
        except Exception: pass
        return next((c for c in cands if c.is_file()), None)

    def _pick_cleanreader(self) -> Path | None:
        fn,_=QFileDialog.getOpenFileName(self,'야문 클린리더 실행 파일 선택 (YamoonCleanReader.exe)','','YamoonCleanReader (YamoonCleanReader.exe);;실행 파일 (*.exe)')
        if not fn: return None
        cfg=self._tools_cfg(); cfg['cleanreader']=fn
        try: (DATA_DIR).mkdir(parents=True,exist_ok=True); save_json(DATA_DIR/self.TOOLS_FILE,cfg)
        except Exception: pass
        return Path(fn)

    def _cleanreader_targets(self) -> list[Path]:
        if self.input_mode=='folder':
            files=[Path(v[1]) for v in self.batch_results.values() if v and v[1]]
        else:
            files=[self.last_result_path] if self.last_result_path else []
        out=[]
        for f in files:
            try:
                if f and f.suffix.lower()=='.txt' and f.name!='번역_요약.txt' and not f.name.endswith('.ko.failed.txt') and f.is_file() and f not in out:
                    out.append(f)
            except Exception: pass
        return out

    def _open_in_cleanreader(self):
        from PySide6.QtWidgets import QApplication
        exe=None if (QApplication.keyboardModifiers() & Qt.ShiftModifier) else self._find_cleanreader()
        if exe is None:
            exe=self._pick_cleanreader()
            if exe is None: return
        files=self._cleanreader_targets()
        if not files:
            QMessageBox.information(self,'클린리더','열 번역 결과 파일이 없습니다. 번역이 끝난 뒤 다시 눌러 주세요.'); return
        # Windows command lines are limited (~32K characters): pass as many files as fit.
        args=[]; room=30000-len(str(exe))
        for f in files:
            room-=len(str(f))+3
            if room<0: break
            args.append(str(f))
        try:
            flags=(getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)) if os.name=='nt' else 0
            subprocess.Popen([str(exe)]+args,close_fds=True,creationflags=flags)
        except Exception as e:
            QMessageBox.warning(self,'클린리더 실행 실패',f'{exe}\n{type(e).__name__}: {e}\n\nShift를 누른 채 버튼을 눌러 위치를 다시 지정할 수 있습니다.'); return
        more=f' (명령줄 길이 제한으로 {len(files):,}개 중 {len(args):,}개)' if len(args)<len(files) else ''
        self.status.setText(f'클린리더로 열기 · 결과 파일 {len(args):,}개{more}')

    def _open_result_folder(self):
        p=self.last_result_path
        if not p:return
        folder=p if p.is_dir() else p.parent
        try:
            if os.name=='nt': os.startfile(str(folder))
            elif sys.platform=='darwin': subprocess.Popen(['open',str(folder)])
            else: subprocess.Popen(['xdg-open',str(folder)])
        except Exception as e: QMessageBox.warning(self,'폴더 열기 실패',str(e))

    def _mode_changed(self):
        if self.input_mode=='single' and self.source_text:self._rechunk()

    def _mode_chunk_settings(self):
        base=self.pm.active.chunk; mode=self.mode.currentData() or 'balanced'
        factor={'quality':0.75,'balanced':1.0,'speed':1.25}.get(mode,1.0)
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); factor=float(((cfg.get('modes') or {}).get(mode) or {}).get('chunk_factor',factor))
        except Exception:
            pass
        factor=max(0.25,min(3.0,factor))
        target=max(300,int(base.get('target_tokens',1200)*factor)); minimum=max(100,int(base.get('min_tokens',250)*factor)); maximum=max(target,int(base.get('max_tokens',2200)*factor))
        target_chars=max(250,int(base.get('target_chars',800)*factor)); min_chars=max(100,int(base.get('min_chars',280)*factor)); max_chars=max(target_chars,int(base.get('max_chars',1150)*factor))
        return target,minimum,maximum,target_chars,min_chars,max_chars

    def _selected_model_id(self)->str:
        idx=self.model.currentIndex(); typed=self.model.currentText().strip()
        if idx>=0 and typed==self.model.itemText(idx):return str(self.model.itemData(idx) or typed).strip()
        return typed.removeprefix('models/')

    def _rechunk(self):
        paras=mark_translatable(split_paragraphs(self.source_text),self.pm.active.source_language); self.paragraphs=paras
        target,minimum,maximum,target_chars,min_chars,max_chars=self._mode_chunk_settings()
        self.chunks=make_chunks(paras,self.pm.active.source_language,target,minimum,maximum,target_chars,min_chars,max_chars)
        passed=sum(1 for p in paras if not p.translatable); protected=count_protectable_literals(self.source_text)
        self.status.setText(f'{len(paras):,} 문단 · 번역 {sum(1 for p in paras if p.translatable):,} · 한국어 통과 {passed:,} · {len(self.chunks):,} 청크 · 기준 약 {target_chars:,}자 · 보호 리터럴 {protected:,}')
        self.progress.setRange(0,max(1,len(self.chunks))); self.progress.setValue(0)

    def _term_manual_edit(self,item):
        """Manual edits always supersede an automatic lock, including locked rows."""
        if getattr(self,'_term_internal_edit',False): return
        row=item.row()
        a=self.term_table.item(row,0); b=self.term_table.item(row,1)
        src=a.text().strip() if a else ''
        dst=b.text().strip() if b else ''
        if src and dst:
            self._term_locks[src]=dst
        current=self._terms()
        self._term_locks={k:v for k,v in self._term_locks.items() if current.get(k)==v}
        self._save_workspace()

    def _add_term_row(self):
        r=self.term_table.rowCount(); self.term_table.insertRow(r); self.term_table.setItem(r,0,QTableWidgetItem('')); self.term_table.setItem(r,1,QTableWidgetItem('')); self.term_table.setCurrentCell(r,0); self.term_table.editItem(self.term_table.item(r,0))
    def _delete_term_rows(self):
        for r in sorted({i.row() for i in self.term_table.selectedIndexes()},reverse=True):
            source=self.term_table.item(r,0)
            if source:self._term_locks.pop(source.text().strip(),None)
            self.term_table.removeRow(r)
        self._save_workspace()
    def _suspicious_terms(self) -> list[tuple[int,str,str]]:
        from ..term_extraction import valid_rendering, COMMON_WORDS
        bad=[]
        for r in range(self.term_table.rowCount()):
            a=self.term_table.item(r,0); b=self.term_table.item(r,1)
            src=(a.text().strip() if a else ''); ko=(b.text().strip() if b else '')
            if src and ko and (not valid_rendering(src,ko) or src in COMMON_WORDS):
                bad.append((r,src,ko))
        return bad

    def _update_terms_tab_title(self):
        try:
            self._set_terms_tab_title()
        except RuntimeError:
            pass   # the window is being destroyed

    def _set_terms_tab_title(self):
        if not hasattr(self,'terms_tab'): return
        n=sum(1 for r in range(self.term_table.rowCount()) if self.term_table.item(r,0) and self.term_table.item(r,0).text().strip())
        n=n or self.term_table.rowCount()
        self.tabs.setTabText(self.tabs.indexOf(self.terms_tab), f'확정 용어집 ({n})' if n else '확정 용어집')

    def _mark_suspicious_terms(self):
        """Light-red rows: a Korean rendering with Chinese/kana left in it (郝장화) or equal to the source."""
        if getattr(self,'_marking_terms',False): return
        from PySide6.QtGui import QBrush, QColor
        self._marking_terms=True
        try:
            bad={r for r,_,_ in self._suspicious_terms()}
            for r in range(self.term_table.rowCount()):
                for c in (0,1):
                    it=self.term_table.item(r,c)
                    if not it: continue
                    if r in bad:
                        src_item=self.term_table.item(r,0)
                        from ..term_extraction import COMMON_WORDS
                        common=bool(src_item and src_item.text().strip() in COMMON_WORDS)
                        it.setBackground(QBrush(QColor('#fde2e2')))
                        it.setToolTip('확인 필요: 이름이 아닌 일반 단어입니다. 용어집에 두면 책 전체에서 항상 같은 말로만 번역되니 지우는 것을 권합니다.' if common
                                      else '확인 필요: 한국어 표기에 한자·가나가 남아 있거나 원문과 같습니다. 고치거나 지우세요.')
                    elif it.toolTip():
                        it.setBackground(QBrush()); it.setToolTip('')
        finally:
            self._marking_terms=False

    def _terms(self):
        out={}
        for r in range(self.term_table.rowCount()):
            a=self.term_table.item(r,0); b=self.term_table.item(r,1)
            if a and b and a.text().strip() and b.text().strip():out[a.text().strip()]=b.text().strip()
        return out

    def _term_conflicts(self) -> list[tuple[str,str,str]]:
        seen={}; conflicts=[]
        for r in range(self.term_table.rowCount()):
            a=self.term_table.item(r,0); b=self.term_table.item(r,1)
            src=(a.text() if a else '').strip(); dst=(b.text() if b else '').strip()
            if not src or not dst: continue
            if src in seen and seen[src] != dst:
                conflicts.append((src,seen[src],dst))
            else:
                seen[src]=dst
        return conflicts

    def _enabled_keys(self):
        raw=(self.secrets.get('gemini') or {}).get('keys',[]); return [r for r in normalize_key_records(raw,'GEM') if r.enabled]

    def _enabled_openai_keys(self):
        raw=(self.secrets.get('openai') or {}).get('keys',[]); return [r for r in normalize_key_records(raw,'OAI') if r.enabled]

    def _engine_checked(self, name: str) -> bool:
        """Whether the engine is checked in API 설정's engine list - the one place that says
        an engine is in use (older saved settings without that list fall back to their own flag)."""
        return name in self._engine_order()

    def _openai_fallback_enabled(self):
        row=self.secrets.get('openai') or {}
        return self._engine_checked('openai') and bool(str(row.get('model') or '').strip()) and bool(self._enabled_openai_keys())

    def _engine_order(self) -> list[str]:
        """Checked engines in the user's order (first = main translator)."""
        rows=initial_engine_order(self.secrets.get('engines'),self.secrets.get('openai'),self.secrets.get('local'))
        return [name for name,on in rows if on]

    def _engine_ready(self, name: str) -> bool:
        if name=='gemini': return bool(self._enabled_keys()) and bool(self._selected_model_id())
        if name=='openai': return self._openai_fallback_enabled()
        if name=='local': return self._local_enabled()
        return False

    def _ready_engines(self) -> list[str]:
        return [n for n in self._engine_order() if self._engine_ready(n)]

    def _local_enabled(self):
        row=self.secrets.get('local') or {}
        return self._engine_checked('local') and bool(str(row.get('model') or '').strip()) and bool(str(row.get('endpoint') or '').strip())

    def _local_worker_settings(self):
        row=dict(self.secrets.get('local') or {})
        row['enabled']=self._local_enabled()
        return row

    def _openai_worker_settings(self):
        row=dict(self.secrets.get('openai') or {})
        row['enabled']=self._openai_fallback_enabled()
        row['keys']=[r.to_dict() for r in self._enabled_openai_keys()]
        try:
            pcfg=load_json(CONFIG_DIR/'providers.json')
            orow=next((x for x in pcfg.get('providers',[]) if x.get('id')=='openai'),{})
            row['endpoint']=str(orow.get('endpoint') or 'https://api.openai.com/v1/responses')
        except Exception:
            row['endpoint']='https://api.openai.com/v1/responses'
        return row

    def _update_ready_state(self):
        self._update_folder_opts_visibility()
        if hasattr(self,'model_caption'): self._sync_model_row()
        has_input=(self.input_mode=='single' and bool(self.source_path) and bool(self.paragraphs)) or (self.input_mode=='folder' and bool(self.batch_files)) or self._preview_edited()
        running=bool(self.worker and self.worker.isRunning()); has_keys=bool(self._enabled_keys()); openai_ready=self._openai_fallback_enabled(); has_model=bool(self._selected_model_id())
        ready_engines=self._ready_engines(); has_provider=bool(ready_engines)
        if hasattr(self, 'first_use_panel'):
            self.first_use_panel.setVisible(not has_provider)
        extracting=self._extracting()
        # The separate Korean TXT picker is available even without a translation source.
        self.refinement_run_btn.setEnabled(not running and not extracting)
        self.start.setEnabled(((running or (has_input and has_provider)) and not extracting) or self._speech_skippable())
        self.restart_btn.setEnabled((not running) and has_input and not extracting)
        checks=[]
        checks.append(('✓' if has_input else '○')+' 원문')
        checks.append(('✓' if has_keys else '○')+f' Gemini 키 {len(self._enabled_keys())}개')
        checks.append(('✓ OpenAI fallback' if openai_ready else '○ OpenAI fallback 꺼짐'))
        checks.append(('✓ 로컬 LLM '+str((self.secrets.get('local') or {}).get('model') or '')) if self._local_enabled() else '○ 로컬 LLM 꺼짐')
        if has_keys:
            checks.append(('✓' if has_model else '○')+' Gemini 모델')
        elif openai_ready:
            checks.append('✓ OpenAI 모델 '+str((self.secrets.get('openai') or {}).get('model') or ''))
        else:
            checks.append('○ 모델')
        checks.append('✓ '+self.pm.active.display_name); checks.append('✓ '+CONTENT_LABELS[self.content_kind])
        checks.append('✓ 인코딩 '+self.encoding.currentText())
        checks.append('✓ '+self.mode.currentText())
        checks.append(f'✓ 자동 언어팩 {len(getattr(self.pm.active,"lexical_guidance",{}) or {}):,}개')
        checks.append('✓ 한국어 자동 통과')
        if self.input_mode=='folder':
            checks.append('✓ '+self.batch_relation.currentText())
        engines_txt=' → '.join(ENGINE_LABELS[n]+('' if n in ready_engines else '(미준비)') for n in self._engine_order()) or '없음'
        checks.insert(1,'번역 엔진: '+engines_txt)
        self.ready_label.setToolTip('<b>준비 상태</b><br>'+'<br>'.join(html.escape(x) for x in checks)); gem_txt=f' · Gemini 키 {len(self._enabled_keys())}개' if 'gemini' in self._engine_order() else ''
        self.ready_label.setText(f'원문 {"선택됨" if has_input else "미선택"} · 엔진 {engines_txt}{gem_txt} · {self.pm.active.display_name} · {self.mode.currentText()}')
        if extracting:
            self._refresh_analysis_status()   # the analysis' own progress line, never "준비됨"
        elif not running and has_input and not has_provider:
            self.status.setText('사용 가능한 번역 엔진이 없습니다 · API 설정에서 엔진 순서와 키·모델을 확인하세요.')
        elif not running and has_input and ready_engines and ready_engines[0]!='gemini':
            out=self._default_output_dir()
            self.status.setText(f'준비됨 · 주 번역 엔진 {ENGINE_LABELS[ready_engines[0]]} · 결과: {out}')
        elif not running and self.input_mode=='folder':
            out=self._default_output_dir()
            self.status.setText(f'준비됨 · {self._count_kinds(self.batch_files)}를 한 파일씩 순차 번역 · 결과: {out}')
        elif not running and self.input_mode=='single' and has_input and has_keys:
            out=self._default_output_dir()
            self.status.setText(f'준비됨 · {len(self.chunks):,} 청크 · 결과: {out}')

    # ------------------------------------------------------------ main-engine model box
    def _main_engine(self) -> str:
        order=self._engine_order(); return order[0] if order else ''

    def _alt_model_id(self) -> str:
        idx=self.alt_model.currentIndex(); typed=self.alt_model.currentText().strip()
        if idx>=0 and typed==self.alt_model.itemText(idx): return str(self.alt_model.itemData(idx) or typed).strip()
        return typed

    def _engine_model_choices(self, engine: str, current: str) -> list[tuple[str,str,str]]:
        out=[]
        if engine=='openai':
            try:
                for m in load_json(CONFIG_DIR/'models.json').get('models',[]):
                    if m.get('provider')=='openai' and m.get('enabled',True):
                        out.append((m.get('display_name') or m['id'],m['id'],str(m.get('note') or '')))
            except Exception: pass
        elif engine=='local':
            endpoint=str((self.secrets.get('local') or {}).get('endpoint') or '')
            out=[(n,n,'') for n in self._local_model_names.get(endpoint,[])]
        if current and current not in [i for _,i,_ in out]: out.insert(0,(current,current,''))
        return out

    def _fetch_local_models(self):
        """Installed local models for the main-window list, fetched once per server address in the background."""
        endpoint=str((self.secrets.get('local') or {}).get('endpoint') or '')
        if not endpoint or endpoint in self._local_model_names or self._local_models_thread is not None: return
        self._local_model_names[endpoint]=[]          # one attempt per address per session
        t=LocalModelsThread(endpoint,self); self._local_models_thread=t
        def done(names,_err,ep=endpoint):
            if names: self._local_model_names[ep]=[str(n) for n in names]
            self._alt_engine=''                        # rebuild the list on the next refresh
            if hasattr(self,'model_caption'): self._sync_model_row()
        def finished():
            self._local_models_thread=None; t.deleteLater()
        t.done.connect(done); t.finished.connect(finished); t.start()

    def _sync_model_row(self):
        """Caption and model box show the model of the main (first checked) engine."""
        order=self._engine_order(); main=order[0] if order else ''
        gem_model=self.model.currentText().strip() or self._selected_model_id()
        if main in ('','gemini'):
            self._alt_engine=''; self.alt_model.hide(); self.model.show()
            self.model_caption.setText('Gemini 모델' if main else 'Gemini 모델 (사용 안 함)')
            self.model_caption.setStyleSheet('' if main else 'color:#9ca3af;')
            self.model_caption.setToolTip('맨 위(주) 번역 엔진의 모델입니다. 엔진 순서는 API 설정에서 바꿉니다.')
            return
        self.model.hide(); self.alt_model.show()
        current=str((self.secrets.get(main) or {}).get('model') or '').strip()
        if main=='openai' and not current:
            try:
                orow=next((x for x in load_json(CONFIG_DIR/'providers.json').get('providers',[]) if x.get('id')=='openai'),{})
                current=str(orow.get('default_model') or '')
            except Exception: current=''
        stale=self._alt_engine!=main or self._alt_model_id()!=current
        if stale and not (self._alt_engine==main and self.alt_model.hasFocus()):
            self.alt_model.blockSignals(True)
            self.alt_model.clear()
            for label,mid,note in self._engine_model_choices(main,current):
                self.alt_model.addItem(label,mid)
                if note: self.alt_model.setItemData(self.alt_model.count()-1,note,Qt.ToolTipRole)
            idx=self.alt_model.findData(current) if current else -1
            if idx>=0: self.alt_model.setCurrentIndex(idx)
            else: self.alt_model.setEditText(current)
            self.alt_model.blockSignals(False)
            self._alt_engine=main
        if main=='local':
            self._fetch_local_models()
            self.alt_model.lineEdit().setPlaceholderText('모델 이름 (목록은 API 설정 → 로컬 LLM에서 불러오기)')
            self.alt_model.setToolTip('로컬 LLM 모델 이름입니다. 설치된 모델 목록은 API 설정 → 로컬 LLM 탭의 "목록 불러오기"로 볼 수 있습니다.')
        else:
            self.alt_model.lineEdit().setPlaceholderText('모델 ID')
            self.alt_model.setToolTip('')
        self.model_caption.setText(f'{ENGINE_LABELS[main]} 모델'); self.model_caption.setStyleSheet('')
        tip=f'맨 위(주) 번역 엔진인 {ENGINE_LABELS[main]}의 모델입니다. 여기서 바꾸면 API 설정에도 저장됩니다. 엔진 순서는 API 설정에서 바꿉니다.'
        if 'gemini' in order: tip+=f'\nGemini(보조) 모델: {gem_model} · Gemini를 맨 위로 올리면 여기서 바꿀 수 있습니다.'
        self.model_caption.setToolTip(tip)

    def _alt_model_changed(self):
        engine=self._alt_engine
        if not engine: return
        mid=self._alt_model_id()
        row=dict(self.secrets.get(engine) or {})
        if not mid or str(row.get('model') or '').strip()==mid: return
        row['model']=mid; candidate=dict(self.secrets); candidate[engine]=row
        try: self.secret_store.save(candidate)
        except Exception as e:
            self.status.setText(f'모델 설정을 저장하지 못했습니다 · {type(e).__name__}: {e}')
            self._alt_engine=''; self._update_ready_state(); return
        self.secrets=candidate; self._update_ready_state()
        self.status.setText(f'{ENGINE_LABELS[engine]} 모델 → {mid} (저장됨)')

    def _update_folder_opts_visibility(self):
        show=self.input_mode=='folder'
        if hasattr(self,'folder_opts_widget'):
            self.folder_opts_widget.setVisible(show); self.folder_opts_caption.setVisible(show)

    # ---------------------------------------------------------------- job clock
    @staticmethod
    def _clock_text(sec: float) -> str:
        sec=int(max(0,sec)); return f'{sec//3600}:{sec%3600//60:02d}:{sec%60:02d}'

    @staticmethod
    def _human_duration(sec: float) -> str:
        sec=int(round(max(0,sec))); h,m,s=sec//3600,sec%3600//60,sec%60
        return (f'{h}시간 ' if h else '')+(f'{m}분 ' if h or m else '')+f'{s}초'

    def _job_elapsed(self) -> float:
        if self._job_t0 is None:
            return float(self._last_job_seconds or 0)
        now=time.monotonic()
        paused=self._job_paused_total+((now-self._job_paused_at) if self._job_paused_at else 0.0)
        return max(0.0, now-self._job_t0-paused)

    def _job_fraction(self) -> float:
        done,total=self._prog
        f=(done/total) if total else 0.0
        if self.input_mode=='folder' and self.batch_files:
            f=(min(self.batch_index,len(self.batch_files))+f)/len(self.batch_files)
        return max(0.0,min(1.0,f))

    def _tick_clock(self):
        if self._job_t0 is None: return
        el=self._job_elapsed(); text=f'경과 {self._clock_text(el)}'
        if self._job_paused_at:
            text+=' · 일시정지'
        else:
            f=self._job_fraction(); f0=self._prog_start if self._prog_start is not None else 0.0
            if f-f0>=0.02 and el>=15 and f<1.0:
                text+=f' · 남은 예상 약 {self._clock_text(el*(1.0-f)/(f-f0))}'
        self.time_label.setText(text)

    def _job_clock_start(self):
        if self._job_t0 is not None: return
        self._job_t0=time.monotonic(); self._job_paused_total=0.0; self._job_paused_at=None
        self._last_job_seconds=None; self._prog=(0,0); self._prog_start=None
        self.time_label.setText('경과 0:00:00'); self._clock.start()

    def _job_clock_stop(self):
        if self._job_t0 is None: return
        if self._job_paused_at:
            self._job_paused_total+=time.monotonic()-self._job_paused_at; self._job_paused_at=None
        self._last_job_seconds=self._job_elapsed(); self._job_t0=None; self._clock.stop()
        self.time_label.setText(f'걸린 시간 {self._clock_text(self._last_job_seconds)}')

    def _toggle_pause(self):
        w=self.worker
        if not (w and w.isRunning()):
            return
        self._user_paused=not self._user_paused
        if self._user_paused:
            self._job_paused_at=time.monotonic()
        elif self._job_paused_at:
            self._job_paused_total+=time.monotonic()-self._job_paused_at; self._job_paused_at=None
        if self._user_paused:
            if hasattr(w,'pause'): w.pause()
            self.pause_btn.setText('다시 진행')
            self.status.setText('일시정지 중 · 진행 중이던 요청을 마무리한 뒤 멈춥니다')
            self.on_worker_issue('⏸ 사용자 일시정지')
        else:
            if hasattr(w,'resume'): w.resume()
            self.pause_btn.setText('일시정지')
            self.status.setText('다시 진행 중')
            self.on_worker_issue('▶ 다시 진행')

    def _lock_inputs(self, locked: bool):
        """Source, settings and glossary are fixed while a job or a term extraction runs.
        Panes the user reads stay scrollable/selectable; they only become read-only."""
        from PySide6.QtWidgets import QAbstractItemView as _AIV
        self.term_table.setEditTriggers(_AIV.NoEditTriggers if locked else (_AIV.DoubleClicked | _AIV.EditKeyPressed | _AIV.AnyKeyPressed))
        if hasattr(self,'speech_table'):   # read-only but still scrollable while a job runs
            self.speech_table.setEditTriggers(_AIV.NoEditTriggers if locked else (_AIV.DoubleClicked | _AIV.EditKeyPressed | _AIV.AnyKeyPressed))
            for r in range(self.speech_table.rowCount()):
                combo=self.speech_table.cellWidget(r,2)
                if combo is not None: combo.setEnabled(not locked)
        self.book_memory.setReadOnly(bool(locked))
        self.preview.setReadOnly(bool(locked) or self.input_mode=='folder' or getattr(self,'_preview_mode','source')=='result')
        for w in (self.health_btn,self.advanced_btn,self.api_btn,self.reset_settings_btn,self.single_btn,self.folder_btn,self.out_btn,self.out_reset,self.lang,self.model,self.alt_model,self.mode,self.encoding,self.lang_option,self.term_add,self.term_delete,self.term_import,self.term_export,self.term_extract,self.name_option,self.speech_add,self.speech_delete,self.speech_generate,self.auto_speech,self.use_common,self.common_btn,self.batch_memory_common,self.restart_btn,self.batch_exclude_btn,self.batch_rescan_btn):
            w.setEnabled(not locked)
        self.include_sub.setEnabled((not locked) and self.input_mode=='folder' and not getattr(self,'batch_selection',None))
        self.batch_relation.setEnabled((not locked) and self.input_mode=='folder')
        self.batch_exclude_btn.setEnabled((not locked) and self.input_mode=='folder' and bool(self.batch_files))
        self.batch_rescan_btn.setEnabled((not locked) and self.input_mode=='folder')
        self._update_batch_memory_mode()
        # While analysing, keep exactly the active task's cancel control reachable.
        task=getattr(self,'_term_thread',None)
        if locked and task is not None and task.isRunning():
            if isinstance(task, SpeechExtractThread):
                self.speech_generate.setEnabled(not task.isInterruptionRequested())
            elif isinstance(task, TermExtractThread):
                self.term_extract.setEnabled(not task.isInterruptionRequested())

    def _extracting(self) -> bool:
        return bool(self._term_thread and self._term_thread.isRunning())

    def _set_running_ui(self, running: bool):
        if hasattr(self,'_clock'):
            self._job_clock_start() if running else self._job_clock_stop()
        self._lock_inputs(bool(running))
        if not running and hasattr(self,'view_result_btn'):
            self.view_result_btn.setText('번역 결과')
        if hasattr(self,'pause_btn'):
            self.pause_btn.setEnabled(bool(running))
            if not running:
                self._user_paused=False; self.pause_btn.setText('일시정지')
        try:
            cfg=load_json(CONFIG_DIR/'app.json')
            prevent=bool(cfg.get('prevent_sleep_while_running',True))
        except Exception:
            prevent=True
        set_translation_awake(bool(running and prevent))

    def _preflight_output(self) -> bool:
        out=self._default_output_dir()
        if out and directory_writable(out):
            return True
        fallback=DATA_DIR/'exports'
        if not directory_writable(fallback):
            QMessageBox.critical(self,'결과 저장 불가','선택한 결과 폴더와 안전 저장 폴더 모두 쓸 수 없습니다. 결과를 저장할 수 없어 번역을 시작하지 않습니다.')
            return False
        if self.input_mode=='folder':
            name=self.batch_root.name if self.batch_root else 'batch'
            self._output_fallback=fallback/name
        else:
            self._output_fallback=fallback
        self._update_output_label()   # the chosen output folder itself is kept for later runs
        QMessageBox.information(self,'결과 저장 폴더 변경','결과 폴더에 쓸 수 없어 이번 작업은 안전 저장 폴더를 사용합니다.\n'+str(self._output_fallback))
        return True

    def _language_mismatch_warning(self) -> bool:
        expected=self.pm.active.source_language
        names={'ja':'일본어','zh':'중국어','en':'영어'}
        if self.input_mode=='single':
            detected,confidence,_det_enc=detect_language_bytes(self.source_bytes) if self.source_bytes else detect_language(self.source_text)+(None,)
            if detected and detected!=expected and confidence>=0.70:
                msg=f'현재 원문 언어는 {names.get(expected,expected)}로 설정되어 있지만, 원문은 {names.get(detected,detected)}일 가능성이 높습니다.\n\n잘못된 언어 프로필로 번역하면 인명·호칭·문체 규칙이 어긋날 수 있습니다. 그래도 현재 설정으로 시작할까요?'
                return QMessageBox.question(self,'원문 언어 확인',msg)==QMessageBox.Yes
            return True
        if self.input_mode=='folder' and self.batch_files:
            mismatches=[]; detected_set=set()
            # Sample several naturally ordered files. This catches common mixed-folder mistakes
            # without reading a huge library into memory before starting.
            for p in self.batch_files[:8]:
                try:
                    if document_kind(p):
                        sample=document_sample_text(p)   # first pages only
                        if not sample.strip(): continue
                        detected,confidence=detect_language(sample)
                        if detected and confidence>=0.70:
                            detected_set.add(detected)
                            if detected!=expected:mismatches.append((p,detected))
                        continue
                    raw=read_txt_sample(p)
                    if (self.encoding.currentData() or 'auto') == 'auto':
                        detected,confidence,_det_enc=detect_language_bytes(raw)
                    else:
                        text,_=decode_txt_bytes(raw,expected,self.encoding.currentData())
                        detected,confidence=detect_language(text)
                except Exception:
                    continue
                if detected and confidence>=0.70:
                    detected_set.add(detected)
                    if detected!=expected:mismatches.append((p,detected))
            if mismatches or len(detected_set)>1:
                rows=[]
                for p,det in mismatches[:5]:
                    try: rel=p.relative_to(self.batch_root)
                    except Exception: rel=p.name
                    rows.append(f'- {rel}: {names.get(det,det)}로 보임')
                extra='\n'.join(rows) or '- 표본 파일들에서 서로 다른 언어가 감지됨'
                msg=(f'폴더 표본 검사에서 현재 {names.get(expected,expected)} 프로필과 다른 언어가 감지되었습니다.\n\n{extra}\n\n'
                     '한 번의 폴더 작업은 하나의 언어 프로필을 사용합니다. 혼합 언어 폴더라면 언어별로 나누는 것이 안전합니다. 그래도 현재 설정으로 시작할까요?')
                return QMessageBox.question(self,'폴더 원문 언어 확인',msg)==QMessageBox.Yes
        return True

    def _restart_translation(self):
        if self._extracting(): return   # the extraction finishes first (a few seconds)
        if self.worker and self.worker.isRunning():
            return
        if self._preview_edited():
            # Text pasted/edited in the 원문 window counts as the source; no file needed.
            if not self._use_preview_as_source(): return
        if self.input_mode not in {'single','folder'}:
            QMessageBox.information(self,'원문 필요','TXT·PDF·EPUB 파일·폴더를 선택하거나, 원문 창에 번역할 글을 붙여넣으세요.')
            return
        label=('원문 창의 글 전체' if self._paste_active else f'선택한 {self._source_doc_kind() or "TXT"} 전체') if self.input_mode=='single' else f'폴더의 {self._count_kinds(self.batch_files)} 전체'
        msg=(f'{label}를 현재 설정으로 처음부터 다시 번역할까요?\n\n'
             '같은 작업의 저장된 완료 청크는 재사용하지 않습니다. 기존 결과 파일이 달라질 경우 자동 백업 후 새 결과를 저장합니다.')
        if QMessageBox.question(self,'처음부터 다시 번역',msg)!=QMessageBox.Yes:
            return
        self.start_translation(True)

    def start_translation(self, force_restart: bool=False):
        if self._extracting():   # started automatically when the extraction ends
            if self._speech_skippable(): self._skip_auto_speech()
            return
        if getattr(self,'_guide_timer',None) and self._guide_timer.isActive():
            self._guide_timer.stop(); self._save_user_guide()
        if self._resume_timer.isActive():
            self._cancel_auto_resume(); return
        self._force_restart=bool(force_restart)
        self._auto_resumes={}
        if not (self.worker and self.worker.isRunning()) and self.input_mode=='folder': self._batch_retried=set()
        if self._preview_edited() and not (self.worker and self.worker.isRunning()):
            if not self._use_preview_as_source(): return
        if self.worker and self.worker.isRunning():
            self._stop_batch=True; self.worker.stop(); self.start.setEnabled(False); self.status.setText('중지 처리 중 · 완료분 저장…'); return
        keys=self._enabled_keys() if 'gemini' in self._engine_order() else []
        if not self._ready_engines():
            QMessageBox.warning(self,'번역 엔진 필요','사용 가능한 번역 엔진이 없습니다. API 설정에서 엔진 순서를 확인하고, 체크한 엔진의 키·모델을 설정하세요.'); return
        model=self._selected_model_id()
        if keys and not model: QMessageBox.warning(self,'모델 필요','사용할 Gemini 모델 ID를 선택하거나 입력하세요.'); return
        if not model: model='gemini-3.1-flash-lite'  # placeholder when starting directly on a backup provider
        self._auto_terms_note=''
        if self._auto_terms_passed:
            self._auto_terms_passed=False
        elif not (self._term_thread and self._term_thread.isRunning()):
            plan,note=self._auto_terms_plan()
            if plan=='ask':
                answer=self._ask_extract_on_resume()
                if answer=='cancel':
                    self._force_restart=False; return
                if answer=='extract':
                    plan='extract'; force_restart=True; self._force_restart=True; self._auto_terms_dialog_force=True
                else:
                    self._auto_terms_declined.add(self._source_sha())
                    note='인명·용어 자동 추출 생략 · 용어집 없이 이어서 번역 (선택함)'
            if plan=='extract':
                self._auto_terms_force=bool(force_restart)
                if self._begin_term_extraction(self.source_text, auto=True):
                    return
                note=getattr(self,'_auto_terms_fail','') or note
            self._auto_terms_note=note
            if self._speech_auto_due():
                self._auto_terms_force=bool(force_restart); self._auto_notes=[note] if note else []
                if self._begin_speech_extraction(auto=True):
                    return
                self._auto_terms_note=' · '.join(x for x in (note,getattr(self,'_speech_fail','')) if x)
        if self.input_mode=='single' and (not self.source_path or not self.paragraphs):
            QMessageBox.warning(self,'번역할 내용 없음',f'선택한 {self._source_doc_kind() or "TXT"}에 처리할 본문이 없습니다.'); return
        if self.input_mode=='folder' and not self.batch_files:
            QMessageBox.warning(self,'번역할 파일 없음','선택한 폴더에 번역할 TXT·PDF·EPUB 파일이 없습니다.'); return
        conflicts=self._term_conflicts()
        if conflicts:
            lines='\n'.join(f'- {src}: {a} / {b}' for src,a,b in conflicts[:8])
            QMessageBox.warning(self,'확정 용어 충돌','같은 원문 용어에 서로 다른 한국어가 지정되어 있습니다. 하나로 정리한 뒤 시작하세요.\n\n'+lines)
            return
        if not self._language_mismatch_warning(): return
        if not self._preflight_output(): return
        self._set_running_ui(True)
        first=self._ready_engines()[0]
        first_model={'gemini':model,'openai':str((self.secrets.get('openai') or {}).get('model') or ''),'local':str((self.secrets.get('local') or {}).get('model') or '')}[first]
        self.error_view.clear(); self.pending_view.clear(); self.api_runtime_label.setText(f'API 상태 · {ENGINE_LABELS[first]} · {first_model} · 번역 요청 준비 중')
        if getattr(self,'_auto_terms_note',''):
            self.on_worker_issue(self._auto_terms_note); self._auto_terms_note=''
        bad_terms=self._suspicious_terms()
        if bad_terms:
            self.on_worker_issue(f'확정 용어집 확인 필요 {len(bad_terms)}개 (한자가 남은 표기·일반 단어): '+', '.join(f'{a}→{b}' for _,a,b in bad_terms[:8])+(' …' if len(bad_terms)>8 else '')+' · 확정 용어집 탭에 빨갛게 표시')
        if self.input_mode=='single':
            self._stop_batch=False; self.batch_index=0; self._start_one_worker(self.source_path,self.source_bytes,self.source_text,self.chunks,keys,model)
        elif self.input_mode=='folder':
            self._stop_batch=False; self.batch_index=0; self.batch_results={}; self.batch_context=''; self._batch_usage={}; self._refresh_job_table(); self._start_next_batch_file(keys,model)

    def _skip_batch_file(self,p:Path,reason:str,keys=None,model=None):
        """Record one folder file as skipped and move on; the queue never stops here."""
        self.batch_results[str(p)]=('파일 오류 · 건너뜀',reason,1)
        try: self._refresh_job_table()
        except Exception: pass
        self.on_worker_issue(f'⏭ {p.name} · 처리하지 못해 건너뛰고 다음 파일 진행 · {reason}')
        if self.batch_relation.currentData()=='continuous': self.batch_context=''
        self.batch_index+=1
        QTimer.singleShot(0,lambda:self._start_next_batch_file(keys or self._enabled_keys(),model or self._selected_model_id()))

    def _start_next_batch_file(self,keys=None,model=None):
        if self._stop_batch:return
        if self.batch_index>=len(self.batch_files):
            self._finish_batch_summary(); return
        p=self.batch_files[self.batch_index]
        try:
            text,enc,raw=self._read_source(p); paras=mark_translatable(split_paragraphs(text),self.pm.active.source_language); target,minimum,maximum,target_chars,min_chars,max_chars=self._mode_chunk_settings(); chunks=make_chunks(paras,self.pm.active.source_language,target,minimum,maximum,target_chars,min_chars,max_chars)
            if not paras: raise ValueError('처리할 본문이 없습니다.')
        except NoTextError:
            self._skip_batch_file(p,self._no_text_reason(p),keys,model); return
        except Exception as e:
            self._skip_batch_file(p,f'파일을 읽지 못함 · {e}',keys,model); return
        try:
            self.source_path=p; self.source_bytes=raw; self.source_text=text; self.paragraphs=paras; self.chunks=chunks
            self.batch_results[str(p)]=(f'번역 중 · {enc}','',0); self._refresh_job_table(); self.status.setText(f'파일 {self.batch_index+1:,}/{len(self.batch_files):,} · {p.name} · {self._size_text(len(raw))} · {len(chunks):,} 청크')
            self._start_one_worker(p,raw,text,chunks,keys or self._enabled_keys(),model or self._selected_model_id())
        except Exception as e:
            if self.worker and self.worker.isRunning():
                raise
            self._skip_batch_file(p,f'작업을 시작하지 못함 · {type(e).__name__}: {e}',keys,model)

    def _start_one_worker(self,p,raw,text,chunks,key_records,model):
        self._grow_common_terms()   # the glossary the user starts with counts as reviewed
        self.start.setText('번역 중지'); self.start.setEnabled(True)
        self._save_workspace()
        seed_context=self.batch_context if self.input_mode=='folder' and self.batch_relation.currentData()=='continuous' else ''
        self.worker=TranslationWorker(p,raw,self.pm.active,chunks,model,[r.to_dict() for r in key_records],self._terms(),self.mode.currentData() or 'balanced',self._output_dir_for(p),self._effective_book_memory(),self._language_options(),seed_context,restart_existing=self._force_restart,openai_settings=self._openai_worker_settings(),all_paragraphs=self.paragraphs,parent=self,local_settings=self._local_worker_settings(),engine_order=self._engine_order(),speech_table=self._speech_rows(),common_terms=self._common_terms_for_job(),sentence_style=self._sentence_style_value())
        self.worker.progress.connect(self.on_progress); self.worker.failed.connect(self.on_failed); self.worker.paused.connect(self.on_paused); self.worker.finished_ok.connect(self.on_finished)
        self.worker.result_ready.connect(self.on_result_ready); self.worker.finished.connect(self._worker_thread_finished); self.worker.key_disabled.connect(self.on_key_disabled); self.worker.issue.connect(self.on_worker_issue); self.worker.api_state.connect(self.on_api_state)
        if hasattr(self.worker,'chunk_translated'): self.worker.chunk_translated.connect(self.on_chunk_translated)
        self._live_started=False
        if self._user_paused and hasattr(self.worker,'pause'):
            self.worker.pause()   # folder run paused between files: the next file waits too
        self.pause_btn.setEnabled(True)
        self.worker.start()

    def on_worker_issue(self,message):
        label=(self.source_path.name+' · ') if self.source_path else ''
        self.error_view.appendPlainText(f'[{time.strftime("%H:%M:%S")}] • '+label+str(message))
        self.error_view.verticalScrollBar().setValue(self.error_view.verticalScrollBar().maximum())

    def on_api_state(self,message):
        self.api_runtime_label.setText('API 상태 · '+message)

    LIVE_HEADER='[진행 중인 번역 · 새로 번역된 부분이 차례로 표시됩니다 · 끝나면 전체 결과로 바뀝니다]'

    def on_chunk_translated(self, position: int, total: int, text: str):
        """Show each finished chunk's Korean right away under 번역 결과 (진행 중)."""
        block=(text or '').strip()
        if not block: return
        if not getattr(self,'_live_started',False):
            self._live_started=True
            head=self.LIVE_HEADER+(f'\n── {self.source_path.name} ──' if self.input_mode=='folder' and self.source_path else '')
            if self.input_mode=='folder' and self._result_text.strip():
                self._result_text=self._result_text.rstrip()+'\n\n'+head   # folder: keep earlier files, add a file header
            else:
                self._result_text=head
            if self._preview_mode=='result':
                self._preview_loading=True; self.preview.setPlainText(self._result_text); self._preview_loading=False
        self._result_text+='\n\n'+block
        self.view_result_btn.setEnabled(True); self.result_copy_btn.setEnabled(True)
        self.view_result_btn.setText('번역 결과 (진행 중)')
        if self._preview_mode=='result':
            sb=self.preview.verticalScrollBar(); follow=sb.value()>=sb.maximum()-4
            self._preview_loading=True; self.preview.appendPlainText('\n'+block); self._preview_loading=False
            if follow: sb.setValue(sb.maximum())
        elif self.input_mode=='folder' and self.tabs.currentWidget() is self.preview_tab:
            self._show_preview('result')   # the source pane is empty in folder mode: show the live translation

    def on_result_ready(self,status,path,failed_count):
        self._last_worker_status=status
        if status in ('completed','partial'): self._grow_common_terms(); self._log_style_check(path,status)
        self._last_leftover=int(getattr(self.worker,'leftover_count',0) or 0) if status in ('completed','partial') else 0
        self._last_failed=int(failed_count or 0)
        if (self.input_mode=='folder' and self.source_path and status in ('completed','partial')
                and (status=='partial' or self._last_leftover) and str(self.source_path) not in self._batch_retried):
            self._batch_retry_pending=True   # retried once in _worker_thread_finished, before moving on
        if self.input_mode=='single' and path:
            try:
                self._set_result(Path(path).read_text(encoding='utf-8-sig'))
                if self._paste_active:
                    self.tabs.setCurrentWidget(self.preview_tab); self._show_preview('result')
            except Exception:
                pass
        if self.source_path:
            label='완료' if status=='completed' else ('사용자 중지 · 부분 저장' if status=='stopped' else ('안전 일시정지 · 부분 저장' if status=='paused' else f'부분 완료 · 미완료 {failed_count}'))
            self.batch_results[str(self.source_path)]=(label,path,failed_count); self.last_result_path=Path(path); self.open_result.setEnabled(True); self.cleanreader_btn.setEnabled(True); self._refresh_job_table()
            if status!='completed':
                legacy=Path(path).with_name(Path(path).stem.replace('.ko.partial','')+'.ko.failed.txt')
                try:
                    # The worker hands over the list (no .ko.failed.txt any more); an older
                    # build's file is read only if the worker has none.
                    detail=getattr(self.worker,'pending_report','') or legacy.read_text(encoding='utf-8-sig')
                    self.pending_view.setPlainText(detail)
                    self.on_worker_issue(f'미완료 {failed_count}개 · 상세 목록은 미완료 목록 탭에서 확인')
                except Exception:
                    if not self.error_view.toPlainText().strip(): self.error_view.setPlainText(f'미완료 청크 {failed_count}개. 자세한 오류 목록은 결과 폴더를 확인하세요.')
            if self.input_mode=='folder' and not self._batch_retry_pending and status not in {'paused'} and not should_continue_after_result(self.batch_relation.currentData(),status):
                # Continuous-volume queues must not advance across a missing section.
                # Paused/stopped states already request a stop elsewhere; this also
                # covers a content-specific PARTIAL result that otherwise looks like
                # a normally finished worker thread.
                self.batch_context=''
                self._stop_batch=True
                if status=='partial':
                    self.status.setText('연속 작품 · 현재 파일 부분 완료 · 다음 파일 진행 중지')

    def on_progress(self,done,total,msg):
        self.progress.setRange(0,total); self.progress.setValue(done)
        self._prog=(done,total)
        if self._prog_start is None and msg!='저장된 번역 사용':
            self._prog_start=max(0.0,self._job_fraction()-(1.0/total if total else 0.0))   # resumed jobs: skip the already-done part
        prefix=f'파일 {self.batch_index+1}/{len(self.batch_files)} · ' if self.input_mode=='folder' and self.batch_files else ''
        self.status.setText(f'{prefix}{done:,}/{total:,} 청크 · {msg}')

    def on_paused(self,msg,user_requested):
        self.on_worker_issue(('사용자 중지 · ' if user_requested else '⏸ 일시정지 · ')+msg)
        self.start.setText('작업 정리 중…'); self.start.setEnabled(False)
        if user_requested:
            self._stop_batch=True
            self.status.setText('사용자 중지 · 부분 결과 저장됨')
            self.api_runtime_label.setText('API 상태 · 중지됨 · 늦은 응답은 반영하지 않음')
            return
        # Never block on a dialog: decide (auto-resume / skip) once the thread has finished.
        self._pending_pause=str(getattr(self.worker,'pause_kind','') or 'TRANSIENT')
        self.status.setText('일시정지 · 부분 결과 저장됨 · 자동 처리 준비 중')
        self.api_runtime_label.setText('API 상태 · 일시정지 · '+self._pending_pause)

    def on_failed(self,msg):
        self.on_worker_issue('작업 오류 · '+msg)
        self.start.setText('작업 정리 중…'); self.start.setEnabled(False)
        if self.input_mode=='folder':
            if self.source_path: self.batch_results[str(self.source_path)]=('오류 · 건너뜀',msg[:200],1); self._refresh_job_table()
            self._pending_pause='ERROR'; self.status.setText('작업 오류 · 이 파일을 건너뛰고 다음 파일로 진행합니다')
            return
        self._stop_batch=True; self.status.setText('작업 오류')
        QMessageBox.warning(self,'번역 오류',msg+'\n\n완료된 내용은 저장되어 있습니다. 같은 작업을 다시 시작하면 이어서 진행합니다.')

    def on_finished(self,msg):
        if self.input_mode=='single':
            self._completion_message=msg
            if self._last_worker_status=='completed':
                self.status.setText('번역 완료 · 작업 정리 중…')
            else:
                self.status.setText('부분 완료 · 미완료 항목 있음 · 작업 정리 중…')
            self.start.setEnabled(False)
        # Folder mode advances only after QThread has fully finished (_worker_thread_finished).

    def _add_batch_usage(self, worker):
        """Folder runs: add each worker's (each file, each auto-resume) usage to one total."""
        if self.input_mode!='folder' or not hasattr(worker,'usage_totals'):
            return
        acc=getattr(self,'_batch_usage',None)
        if acc is None: acc=self._batch_usage={}
        for pid,row in (worker.usage_totals or {}).items():
            t=acc.setdefault(pid,{'requests':0,'failed':0,'input':0,'output':0,'cost':0.0,'wasted':0.0,'unknown':False})
            t['requests']+=row.get('requests',0); t['failed']+=row.get('failed',0); t['input']+=row.get('input',0); t['output']+=row.get('output',0)
            try:
                cost=worker._cost(dict(row,provider=pid)); fail=worker._cost(dict(row,provider=pid),True)
            except Exception:
                cost=fail=None
            if cost is None: t['unknown']=True
            else: t['cost']+=cost; t['wasted']+=fail or 0.0

    def _batch_usage_lines(self) -> list[str]:
        acc=getattr(self,'_batch_usage',None) or {}
        if not acc: return []
        names={'gemini':'Gemini','openai':'OpenAI','local':'로컬 LLM'}
        total=sum(t['cost'] for t in acc.values()); wasted=sum(t['wasted'] for t in acc.values())
        unknown=any(t['unknown'] for pid,t in acc.items() if pid!='local')
        lines=[f'폴더 전체 추정 비용: 약 ${total:.3f}'+(f' (그중 재시도·확인 요청 ${wasted:.3f})' if wasted else '')+(' + 요금표 없는 모델 별도' if unknown else '')]
        for pid,t in acc.items():
            cost='무료' if pid=='local' else ('요금표 없음' if t['unknown'] else f"약 ${t['cost']:.3f}")
            lines.append(f"  {names.get(pid,pid)}: 요청 {t['requests']:,}회{f"(재시도·확인 {t['failed']:,}회 포함)" if t['failed'] else ''} · 입력 {t['input']:,} · 출력 {t['output']:,} 토큰 · {cost}")
        return lines

    def _worker_thread_finished(self):
        try: self._add_batch_usage(self.sender() or self.worker)
        except Exception: pass
        if self._close_after_worker:
            self._close_after_worker=False; self._set_running_ui(False); self.close(); return
        if self._pending_pause and not self._stop_batch:
            kind=self._pending_pause; self._pending_pause=''
            self._handle_unattended_pause(kind); return
        self._pending_pause=''
        if self._stop_batch:
            self.start.setText('번역 시작 / 계속'); self._set_running_ui(False); self._force_restart=False; self._update_ready_state()
            if self._last_worker_status=='stopped':self.status.setText(f'중지 완료 · 부분 결과 저장됨 · 경과 {self._human_duration(self._last_job_seconds or 0)} · 다시 시작하면 이어서 진행')
            return
        if self.input_mode=='single':
            self.start.setText('번역 시작 / 계속'); self._set_running_ui(False); self._force_restart=False; self._update_ready_state()
            took=self._human_duration(self._last_job_seconds or 0)
            self.on_worker_issue(f'작업 끝 · 걸린 시간 {took}')
            if self._completion_message:
                msg=self._completion_message+f'\n걸린 시간: {took}'; self._completion_message=''
                status=self._last_worker_status; self._last_worker_status=''
                if status in ('completed','partial') and (status=='partial' or self._last_leftover):
                    if self._ask_retry_untranslated(msg):
                        self._retry_rounds+=1
                        self.on_worker_issue('↻ 미번역 부분 재시도 · 남은 부분만 다시 번역합니다')
                        QTimer.singleShot(0,self.start_translation)
                    return
                self._retry_rounds=0
                QMessageBox.information(self,'번역 완료',f'번역 결과를 저장했습니다.\n{msg}')
            return
        if self.input_mode=='folder':
            if self._batch_retry_pending and self.source_path:
                self._batch_retry_pending=False; self._batch_retried.add(str(self.source_path))
                self.on_worker_issue(f'↻ {self.source_path.name} · 미번역 부분 자동 재시도 (파일당 1회) · 남은 부분만 다시 번역합니다')
                QTimer.singleShot(0,lambda:self._start_next_batch_file(self._enabled_keys(),self._selected_model_id())); return
            if self.batch_relation.currentData()=='continuous' and self.worker:
                current=self.batch_results.get(str(self.source_path),('', '', 0))[0] if self.source_path else ''
                self.batch_context=self.worker.final_context if (current=='완료' or current.startswith('부분 완료')) else ''
            else:self.batch_context=''
            self.batch_index+=1; QTimer.singleShot(0,lambda:self._start_next_batch_file(self._enabled_keys(),self._selected_model_id()))

    def _untranslated_summary(self) -> str:
        parts=[]
        if self._last_failed: parts.append(f'· 원문으로 남은 부분이 있는 청크 {self._last_failed:,}개')
        if self._last_leftover: parts.append(f'· 한자·가나가 남은 문단 {self._last_leftover:,}개')
        return '\n'.join(parts)

    def _ask_retry_untranslated(self, msg: str) -> bool:
        """After a single-file / pasted-text run that left something untranslated: offer one click to retry it."""
        again='\n이미 한 번 다시 시도했지만 아직 남아 있습니다.' if self._retry_rounds else ''
        box=QMessageBox(self); box.setIcon(QMessageBox.Question); box.setWindowTitle('미번역 부분 있음')
        box.setText('번역 결과를 저장했지만 번역되지 않은 부분이 있습니다.\n'+self._untranslated_summary()+again+'\n\n미번역 부분을 재시도할까요?')
        box.setInformativeText('이미 번역된 부분은 다시 요청하지 않고 남은 부분만 보냅니다. 같은 엔진이 거부·차단한 부분은 다시 막힐 수 있으니, '
                               'API 설정에서 다른 엔진(OpenAI·로컬 LLM)을 켜 두면 더 잘 됩니다. 나중에 [번역 시작 / 계속]을 눌러도 같습니다.')
        box.setMinimumWidth(460)
        yes=box.addButton('재시도',QMessageBox.YesRole); box.addButton('나중에',QMessageBox.NoRole); box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def _unattended_cfg(self) -> dict:
        try: cfg=dict(load_json(CONFIG_DIR/'app.json').get('unattended') or {})
        except Exception: cfg={}
        return {'auto_resume':bool(cfg.get('auto_resume',True)),
                'wait':{'RATE_LIMIT':int(cfg.get('rate_limit_wait_seconds',60)),'TRANSIENT':int(cfg.get('network_wait_seconds',120)),
                        'QUOTA':int(cfg.get('quota_wait_seconds',900))},
                'max':max(1,int(cfg.get('max_auto_resumes_per_file',40)))}

    def _handle_unattended_pause(self, kind: str):
        """Keep going without anyone at the keyboard.

        Waiting fixes quota/rate/network pauses, so the same file is resumed after a
        countdown. Settings problems (bad model, no usable key) do not go away by
        waiting: in folder mode that file is skipped and the queue moves on.
        """
        cfg=self._unattended_cfg(); key=str(self.source_path); done=self._auto_resumes.get(key,0)
        waitable=kind in cfg['wait']
        if cfg['auto_resume'] and waitable and done<cfg['max']:
            self._auto_resumes[key]=done+1
            names={'RATE_LIMIT':'속도 제한','QUOTA':'한도 소진','TRANSIENT':'네트워크/응답 오류'}
            self._resume_reason=f"{names.get(kind,kind)} · 자동 재개 {done+1}/{cfg['max']}회째"
            self._resume_left=cfg['wait'][kind]
            self.on_worker_issue(f"⏳ {self._resume_reason} · {self._resume_left//60}분 {self._resume_left%60}초 뒤 같은 파일을 이어서 번역합니다")
            self.start.setText('자동 재개 취소'); self.start.setEnabled(True)
            self._resume_tick(initial=True); self._resume_timer.start(); return
        if self.input_mode=='folder':
            label={'FATAL':'설정 오류','AUTH':'키 오류','ERROR':'작업 오류'}.get(kind,kind)
            if self.source_path:
                prev=self.batch_results.get(str(self.source_path),('','',1))
                self.batch_results[str(self.source_path)]=(f'오류 · 건너뜀 ({label})',prev[1],1); self._refresh_job_table()
            self.on_worker_issue(f'⏭ {self.source_path.name if self.source_path else ""} · {label} · 이 파일은 건너뛰고 다음 파일로 진행합니다 (완료분은 저장됨)')
            self.batch_context=''; self.batch_index+=1
            QTimer.singleShot(0,lambda:self._start_next_batch_file(self._enabled_keys(),self._selected_model_id())); return
        # Single file and nothing more to try: finish with an explanation.
        self.start.setText('번역 시작 / 계속'); self._set_running_ui(False); self._force_restart=False; self._update_ready_state()
        self.status.setText('일시정지 · 부분 결과 저장됨')
        QMessageBox.warning(self,'번역 일시정지',f'{kind} 오류로 더 진행할 수 없습니다. API 설정(키·모델)을 확인한 뒤 같은 작업을 다시 시작하면 이어서 진행합니다.')

    def _resume_tick(self, initial: bool=False):
        if not initial:
            self._resume_left-=1
        if self._resume_left<=0:
            self._resume_timer.stop(); self._resume_same_file(); return
        m,s=divmod(self._resume_left,60)
        self.status.setText(f'자동 재개까지 {m}:{s:02d} · {self._resume_reason}')
        self.api_runtime_label.setText(f'API 상태 · 대기 중 · {self._resume_reason}')

    def _resume_same_file(self):
        self.on_worker_issue('▶ 자동 재개 · 같은 파일을 이어서 번역합니다')
        self._force_restart=False
        if self.input_mode=='folder':
            self._start_next_batch_file(self._enabled_keys(),self._selected_model_id())
        else:
            self._start_one_worker(self.source_path,self.source_bytes,self.source_text,self.chunks,self._enabled_keys(),self._selected_model_id())

    def _cancel_auto_resume(self):
        self._resume_timer.stop(); self._resume_left=0; self._stop_batch=True
        self.start.setText('번역 시작 / 계속'); self._set_running_ui(False); self._force_restart=False; self._update_ready_state()
        self.status.setText('자동 재개 취소 · 부분 결과 저장됨 · 다시 시작하면 이어서 진행')
        self.on_worker_issue('자동 재개를 취소했습니다.')

    def _finish_batch_summary(self):
        outroot=self._default_output_dir() or (DATA_DIR/'exports'); outroot.mkdir(parents=True,exist_ok=True)
        lines=['야문 Novel Translator 폴더 번역 요약',f'원본 폴더: {self.batch_root}',f'결과 폴더: {outroot}',f'원문 언어: {self.pm.active.display_name}',f'폴더 파일 관계: {self.batch_relation.currentText()}','']
        completed=partial=failed=0
        for p in self.batch_files:
            st,path,n=self.batch_results.get(str(p),('미처리','',1))
            if st=='완료':completed+=1
            elif st.startswith('부분 완료'):partial+=1
            else:failed+=1
            try: rel=p.relative_to(self.batch_root)
            except Exception: rel=p.name
            lines.append(f'{st}\t{rel}\t{path}')
        lines += ['',f'완료 {completed}개 · 부분 완료 {partial}개 · 오류/미처리 {failed}개']
        took=self._human_duration(self._job_elapsed())
        lines += [f'걸린 시간: {took} (일시정지 시간 제외)']
        usage=self._batch_usage_lines()
        if usage: lines += ['']+usage
        summary=outroot/'번역_요약.txt'; atomic_write_text(summary,'\n'.join(map(str,lines))+'\n'); self.last_result_path=summary; self.open_result.setEnabled(True); self.cleanreader_btn.setEnabled(True)
        self.start.setText('번역 시작 / 계속'); self.start.setEnabled(True); self._set_running_ui(False); self._force_restart=False; self.status.setText(f'폴더 번역 완료 · 완료 {completed} · 부분 {partial} · 오류 {failed} · 걸린 시간 {took}')
        QMessageBox.information(self,'폴더 번역 완료',f'{self._count_kinds(self.batch_files)} 순차 처리가 끝났습니다.\n완료 {completed} · 부분 완료 {partial} · 오류/미처리 {failed}\n걸린 시간: {took}'+(('\n'+usage[0]) if usage else '')+f'\n\n요약: {summary}')

    def on_key_disabled(self,provider_id,key_id,reason):
        self.on_worker_issue(f'API 키 비활성 · {provider_id} · {key_id} · {reason}')
        self.api_runtime_label.setText(f'API 상태 · {provider_id} 키 {key_id} 비활성 · {reason}')
        bucket='openai' if provider_id=='openai' else 'gemini'; prefix='OAI' if bucket=='openai' else 'GEM'
        raw=(self.secrets.get(bucket) or {}).get('keys',[]); records=normalize_key_records(raw,prefix); changed=False
        for rec in records:
            if rec.id==key_id: rec.enabled=False; rec.disabled_reason=reason; changed=True; break
        if changed:
            candidate=dict(self.secrets); section=dict(candidate.get(bucket) or {}); section['keys']=[r.to_dict() for r in records]; candidate[bucket]=section
            try:self.secret_store.save(candidate); self.secrets=candidate
            except Exception as e:self.status.setText(f'키 상태 저장 실패 · 번역은 계속됨 ({type(e).__name__})')

    def closeEvent(self,event):
        if self._extracting():
            # Never destroy an active QThread; cancel remaining work and wait only
            # for the bounded current provider request to return.
            self._close_after_extract=True
            self._term_thread.requestInterruption()
            self.status.setText('자동 분석 취소 요청 · 현재 API 요청 정리 후 종료합니다…')
            event.ignore(); return
        self._save_workspace()
        if self._resume_timer.isActive(): self._resume_timer.stop()
        if self.worker and self.worker.isRunning():
            answer=QMessageBox.question(self,'번역 중 종료','번역 작업이 진행 중입니다. 현재 API 요청을 정리한 뒤 안전하게 종료할까요?\n완료된 청크는 이미 저장되어 있습니다.')
            if answer!=QMessageBox.Yes:event.ignore();return
            self._close_after_worker=True; self._stop_batch=True; self.worker.stop(); self.start.setEnabled(False); self.status.setText('종료를 위해 현재 요청 정리 중…'); event.ignore();return
        t=getattr(self,'_local_models_thread',None)
        if t is not None and t.isRunning(): t.wait(10000)   # short local model-list request (a few seconds at most)
        event.accept()
