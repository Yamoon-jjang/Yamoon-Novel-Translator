"""Separate dialog for Korean literary editing; translation mode remains unchanged."""
from __future__ import annotations
from pathlib import Path
import re
import time
from collections import deque
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QLabel,
                               QPushButton, QFileDialog, QProgressBar, QMessageBox, QPlainTextEdit,
                               QSizePolicy, QCheckBox, QApplication, QTabWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView)
from ..refinement_runtime import (run_refinement, MAX_REFINEMENT_BYTES, MAX_REFINEMENT_LABEL, format_duration,
                                  retry_status, peek_guidance, resume_conflict, estimate_run,
                                  run_log_path, append_run_log, same_result)
from ..api_keys import normalize_key_records
from .main_window import _make_provider
from ..textio import read_txt_sample, decode_txt_bytes
from ..config import CONFIG_DIR, DATA_DIR, load_json, save_json
from ..refinement_links import guidance_tables, guidance_counts
from ..power import set_translation_awake


def _sleep_prevention_wanted() -> bool:
    """The same 고급 설정 option as translation (번역·재정비 중 시스템 절전 방지)."""
    try:
        return bool(load_json(CONFIG_DIR/'app.json').get('prevent_sleep_while_running', True))
    except Exception:
        return True

def _result_style() -> tuple[bool, str]:
    """The translation result's 문장부호 and 따옴표 settings, which the refined TXT follows too (v2.6.2)."""
    try:
        cfg = load_json(CONFIG_DIR/'app.json')
        return bool(cfg.get('korean_punctuation', True)), str(cfg.get('quote_style') or 'straight')
    except Exception:
        return True, 'straight'


# A run of this many editing requests or more is announced before it starts (v2.6.2).
LONG_RUN_REQUESTS = 300
# The live view and the 작업 기록 keep the latest 500 entries, as the translation window's
# 작업 기록 does; the whole result is in the result TXT and the whole log in its .log.txt.
LIVE_ROWS = 500
LOG_ROWS = 500
TABS_MIN_HEIGHT = 170
WINDOW_STATE_FILE = 'refine_window_state.json'
_WEEKDAYS = '월화수목금토일'


def finish_time_text(seconds_left: float, now: float | None = None) -> str:
    """When a run ends at the current pace: 오늘 오후 3:20 / 내일 오전 3:40 / 11월 6일(금) 오전 10:40."""
    import datetime as _dt
    now_dt = _dt.datetime.fromtimestamp(now if now is not None else time.time())
    end = now_dt + _dt.timedelta(seconds=max(0.0, float(seconds_left)))
    ampm = '오전' if end.hour < 12 else '오후'
    clock = f'{ampm} {end.hour % 12 or 12}:{end.minute:02d}'
    days = (end.date() - now_dt.date()).days
    if days == 0:
        return '오늘 ' + clock
    if days == 1:
        return '내일 ' + clock
    return f'{end.month}월 {end.day}일({_WEEKDAYS[end.weekday()]}) ' + clock

KOREAN_MIN_SHARE = 0.3


def korean_share(path) -> float:
    """Share of Hangul among the letters at the start of a TXT (0.0 when unreadable)."""
    try:
        raw=read_txt_sample(Path(path), 65536)
        if len(raw) >= 65536 and b'\n' in raw:
            raw=raw[:raw.rfind(b'\n')]   # never cut a multi-byte character in half
        text,_=decode_txt_bytes(raw, 'ko')
    except Exception:
        return 0.0
    letters=len(re.findall(r'[A-Za-z가-힣一-龥ぁ-ゟ゠-ヿ]', text))
    return len(re.findall(r'[가-힣]', text))/letters if letters else 0.0


def _size(path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def is_unfinished_translation(path) -> bool:
    return Path(path).name.lower().endswith('.ko.partial.txt')


def refined_output_path(source, out_dir=None) -> Path:
    """book.txt → book.ko.refined.txt; this app's results (book.ko.txt, book.ko.partial.txt,
    book.ko.refined.txt) only get '.refined' added, never a second '.ko'."""
    source=Path(source); stem=source.stem
    low=stem.lower()
    suffix='.refined.txt' if low.endswith(('.ko','.ko.partial','.ko.refined')) or '.ko.refined' in low else '.ko.refined.txt'
    # The app-wide output folder holds every result, refinement included.
    return Path(out_dir or source.parent)/(stem+suffix)


def wrap_path(text) -> str:
    """Text that can wrap inside a long path or file name (after folder separators and
    within long unbroken runs), so it never widens the window. Display only."""
    text=str(text).replace('\\', '\\\u200b').replace('/', '/\u200b')
    return re.sub(r'([^\s\u200b]{24})(?=[^\s\u200b])', '\\1\u200b', text)


def short_name(name, keep: int = 44) -> str:
    """A file name shortened in the middle for one status line (the full path is in the tooltip)."""
    name=str(name)
    if len(name) <= keep:
        return name
    head=keep//2-1
    return name[:head]+'…'+name[-(keep-head-1):]


# Lines kept free for the status text: a finished run's summary is at most three lines (v2.6.2 보완:
# its counts are on the 결과 line above, no longer repeated here), so
# the window keeps one size from opening to finishing (only a rare long error grows it).
STATUS_LINES = 3


class _StatusLabel(QLabel):
    """A wrapped status line. Its room is reserved up front (STATUS_LINES); only a message
    longer than that (a long error) lets the window grow, never shrink."""
    def setText(self, text):
        super().setText(text)
        fit = getattr(self.window(), '_fit_height', None)
        if fit:
            fit()


class RefinementWorker(QThread):
    progressed = Signal(int,int,int)
    outcome = Signal(object)
    failed = Signal(str)
    activity = Signal(str)
    edited = Signal(object)    # [(pid, original, result text)] of each newly kept group (live view)
    stats = Signal(object)     # running counts: requests, replies, retries, reviews, changed, identical
    logged = Signal(str)       # a line of the run log (작업 기록)

    def __init__(self, source: Path, output: Path, provider_name: str, model: str,
                 api_key: str, options: dict, parent=None, analysis=None, update_existing=False):
        super().__init__(parent)
        self.update_existing=bool(update_existing)
        self.source=source; self.output=output; self.provider_name=provider_name
        self.model=model; self.api_key=api_key; self.options=options
        self._stop=False
        self.analysis=analysis

    def stop(self):
        self._stop=True

    def run(self):
        try:
            provider=_make_provider(self.provider_name,
                                   self.options.get('endpoint',''),self.options)
            analysis=None
            if self.analysis:
                name,model,key,opts=self.analysis
                analysis=(_make_provider(name,opts.get('endpoint',''),opts),model,key)
            punct,quotes=_result_style()
            result=run_refinement(self.source,self.output,provider,self.model,self.api_key,
                                  stop=lambda: self._stop, analysis_engine=analysis,
                                  update_existing=self.update_existing,
                                  punct=punct, quote_style=quotes,
                                  on_edited=lambda rows: self.edited.emit(rows),
                                  on_stats=lambda d: self.stats.emit(d),
                                  on_log=lambda line: self.logged.emit(line),
                                  log_path=run_log_path(self.output),
                                  event=lambda message:self.activity.emit(message),
                                  progress=lambda a,b,c: self.progressed.emit(a,b,c))
            self.outcome.emit(result)
        except Exception as e:
            self.failed.emit(type(e).__name__+': '+str(e)[:450])


class RefinementRunDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host=host
        self.setWindowTitle('한국어 소설 재정비 · 문학적 윤문')
        self.resize(700,690)
        self.setSizeGripEnabled(True)    # the corner mark that shows the window can be resized (v2.6.2 보완)
        self.worker=None
        self._stage_text='대기 중'
        self._stage_started=0.0
        self._timer=QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.source=None
        self.output=None
        self._too_big=False      # the selected TXT is over the size limit (v2.6.1: shown at once)
        layout=QVBoxLayout(self)
        intro=QLabel('한국어 TXT를 자연스러운 소설 문장으로 다듬습니다. 이름·사건·행동 순서와 존댓말·반말은 그대로 두고 원본은 바꾸지 않습니다. '
                     '이 앱으로 번역한 TXT는 번역 때 확정한 말투·이름을, 그 밖의 TXT는 샘플 분석으로 만든 기준표를 씁니다.')
        intro.setWordWrap(True); layout.addWidget(intro)
        row=QHBoxLayout()
        self.pick=QPushButton('한국어 TXT 선택')
        self.pick.clicked.connect(self._choose)
        row.addWidget(self.pick)
        self.source_label=QLabel('선택한 파일 없음')
        self.source_label.setWordWrap(True)
        self.source_label.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)   # a long name never widens the window
        row.addWidget(self.source_label,1)
        layout.addLayout(row)
        grow=QHBoxLayout()
        self.guidance_info=QLabel('기준표: TXT를 선택하면 표시됩니다')
        self.guidance_info.setWordWrap(True)
        grow.addWidget(self.guidance_info,1)
        self.guidance_view=QPushButton('기준표 보기')
        self.guidance_view.setEnabled(False)
        self.guidance_view.clicked.connect(self._show_guidance)
        grow.addWidget(self.guidance_view)
        layout.addLayout(grow)
        self._guidance_text=''
        # API settings own both provider/model choices. This dialog is read-only.
        self.analysis_info=QLabel()
        self.editing_info=QLabel()
        engines=QGridLayout(); engines.setHorizontalSpacing(8); engines.setVerticalSpacing(2)
        engines.addWidget(QLabel('용어·말투 분석:'),0,0); engines.addWidget(self.analysis_info,0,1)
        engines.addWidget(QLabel('(번역 기록이 없는 TXT에만)'),0,2)
        engines.addWidget(QLabel('문학적 윤문·연속성:'),1,0); engines.addWidget(self.editing_info,1,1)
        engines.setColumnStretch(2,1)
        layout.addLayout(engines)
        self._fill_engines()
        self.help=QLabel('로컬 LLM 전용입니다. 엔진·모델은 API 설정에서만 바꾸며, 용어·말투 분석과 문학적 윤문을 모두 로컬 LLM으로 지정해야 시작됩니다. '
                         '분량과 모델 속도에 따라 오래 걸릴 수 있고, 성인 소설은 검열 해제된 로컬 LLM을 권장합니다(성공·품질은 보장되지 않음).')
        self.help.setStyleSheet('color:#475569;')
        self.help.setWordWrap(True);layout.addWidget(self.help)
        self.progress=QProgressBar();self.progress.setRange(0,100);self.progress.setValue(0)
        # Shown in the progress bar's place once a run has finished: a calm, readable mark.
        self.done_badge=QLabel('✓  정비 완료')
        self.done_badge.setObjectName('refineDone')
        self.done_badge.setAlignment(Qt.AlignCenter)
        self.done_badge.setStyleSheet('#refineDone{color:#0f766e;background:#f0fdfa;border:1px solid #99f6e4;'
                                      'border-radius:8px;padding:7px 18px;font-size:15px;font-weight:600;letter-spacing:1px;}')
        self.done_badge.hide()
        # The bar and the done mark share one cell that keeps the taller one's height,
        # so swapping them never moves the lines below.
        swap=QWidget(); cell=QGridLayout(swap); cell.setContentsMargins(0,0,0,0)
        for w in (self.progress,self.done_badge):
            sp=w.sizePolicy(); sp.setRetainSizeWhenHidden(True); w.setSizePolicy(sp)
            cell.addWidget(w,0,0,Qt.AlignVCenter)
        layout.addWidget(swap)
        # Running counts (v2.6.2): requests, results and pace, three lines whose room is kept
        # from opening to finishing so the window never changes size.
        self.stats_info=QLabel('')
        self.stats_info.setStyleSheet('color:#334155;')
        self.stats_info.setTextFormat(Qt.PlainText)
        probe3=QLabel('가\n가\n가'); probe3.setFont(self.stats_info.font())
        self.stats_info.setMinimumHeight(probe3.sizeHint().height())
        self.stats_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        sp=self.stats_info.sizePolicy(); sp.setRetainSizeWhenHidden(True); self.stats_info.setSizePolicy(sp)
        self.stats_info.hide()
        layout.addWidget(self.stats_info)
        self._stats={}
        # Elapsed time and an estimate of what is left, as the translation window shows them.
        self.time_info=_StatusLabel('')
        self.time_info.setWordWrap(True)
        self.time_info.setStyleSheet('color:#475569;')
        self.time_info.hide()
        sp=self.time_info.sizePolicy(); sp.setRetainSizeWhenHidden(True); self.time_info.setSizePolicy(sp)
        self.time_info.setMinimumHeight(QLabel('경과').sizeHint().height())
        layout.addWidget(self.time_info)
        self._counts=(0,0,0); self._base=None
        self.status=_StatusLabel('준비됨')
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignLeft|Qt.AlignTop)
        probe=QLabel('\n'.join(['가']*STATUS_LINES)); probe.setFont(self.status.font())
        self.status.setMinimumHeight(probe.sizeHint().height()+2)
        layout.addWidget(self.status)
        # Live view (v2.6.2): each group's edited paragraphs as soon as they are kept, with the
        # text before editing beside them on request. Only the latest LIVE_ROWS paragraphs stay
        # here; the result TXT holds the whole book.
        self.tabs=QTabWidget()
        live_page=QWidget(); lv=QVBoxLayout(live_page); lv.setContentsMargins(4,4,4,4)
        lrow=QHBoxLayout()
        self.compare=QCheckBox('다듬기 전 문장도 함께 보기')
        self.compare.setToolTip('켜면 문단마다 다듬기 전 문장(전)과 다듬은 문장(후)을 함께 보여 줍니다.\n'
                                '보기만 바뀌며 재정비 결과에는 영향이 없습니다.')
        try: self.compare.setChecked(bool(load_json(CONFIG_DIR/'app.json').get('refine_live_compare',False)))
        except Exception: pass
        self.compare.toggled.connect(self._compare_toggled)
        lrow.addStretch(1); lrow.addWidget(self.compare)
        lv.addLayout(lrow)
        self.live=QPlainTextEdit(); self.live.setReadOnly(True)
        self.live.setPlaceholderText('재정비를 시작하면 다듬은 문장이 여기에 차례로 나옵니다.\n'
                                     '이전 실행에서 이미 다듬은 문단은 건너뛰므로 여기에 다시 나오지 않습니다.')
        self.live.setMinimumHeight(120)
        lv.addWidget(self.live,1)
        self._live_rows=deque(maxlen=LIVE_ROWS)
        # 작업 기록 (v2.6.2): every event of the run, timestamped as in the translation window; the
        # latest LOG_ROWS lines here, the whole log in the result's .log.txt.
        self.log_view=QPlainTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_ROWS)
        self.log_view.setPlaceholderText('아직 작업 기록이 없습니다.\n'
                                         '재정비를 시작하면 묶음마다 결과(변경·동일·응답 시간)와 재요청·연속성 확인·원문 보존이 시간과 함께 남습니다.\n'
                                         '최근 500줄만 보이며, 전체 기록은 결과 파일 옆 .log.txt에 저장됩니다.')
        self.tabs.addTab(live_page,'실시간 결과')
        self.tabs.addTab(self.log_view,'작업 기록')
        # The tabs take whatever height is left (never less than TABS_MIN_HEIGHT): their own
        # preferred height would make the window too tall for a 768-pixel laptop screen.
        self.tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.tabs.setMinimumHeight(TABS_MIN_HEIGHT)
        layout.addWidget(self.tabs,1)
        bar=QHBoxLayout()
        self.start=QPushButton('재정비 시작')
        self.start.clicked.connect(self._start)
        self.start.setEnabled(self._can_start())
        # After a finished run the start button gives way to opening the result in 야문 클린리더
        # (it stays beside it while preserved paragraphs can still be retried).
        self.reader=QPushButton('클린리더로 열기')
        self.reader.setToolTip('재정비 결과를 야문 클린리더(문단 정리기)로 엽니다.\n'
                               '처음 한 번 YamoonCleanReader.exe 위치를 고르면 기억합니다. 위치를 바꾸려면 Shift를 누른 채 클릭하세요.')
        self.reader.clicked.connect(self._open_in_reader)
        self.reader.hide()
        self._result_path=None; self._retry_left=0
        self.cancel=QPushButton('닫기')
        self.cancel.clicked.connect(self._close_or_stop)
        bar.addStretch(1);bar.addWidget(self.start);bar.addWidget(self.reader);bar.addWidget(self.cancel)
        layout.addLayout(bar)
        self._restore_window_state()
        self._fit_height()
        # Pre-fill only a Korean text: the last translation result if there is one,
        # otherwise the open file when it is itself Korean. The foreign-language
        # original that is open for translation is never pre-selected.
        for candidate in self._prefill_candidates(host):
            if korean_share(candidate) >= KOREAN_MIN_SHARE:
                self._set_source(candidate)
                break

    @staticmethod
    def _prefill_candidates(host):
        """Never an unfinished translation (.ko.partial.txt: finishing it changes the text, so
        a refinement of it would be done again) nor a TXT too large to refine (v2.6.1)."""
        out=[]
        result=getattr(host,'last_result_path',None)
        if result and Path(result).is_file() and Path(result).name.lower().endswith('.ko.txt'):
            out.append(Path(result))
        existing=getattr(host,'source_path',None)
        if existing and Path(existing).is_file() and not Path(existing).name.lower().endswith('.ko.partial.txt'):
            out.append(Path(existing))
        return [p for p in out if _size(p) < MAX_REFINEMENT_BYTES]

    def _can_start(self) -> bool:
        return bool(getattr(self,'_routes_ready',False)) and not self._too_big

    def _fit_height(self):
        """Grow (never shrink) so every wrapped line is fully visible at the current width."""
        layout=self.layout()
        if layout is None:
            return
        need=layout.totalHeightForWidth(self.width())
        if need > self.height():
            self.resize(self.width(), need)

    def _fill_engines(self):
        """Show canonical API routes; never silently change them for refinement."""
        settings=self.host.secrets
        local=dict(settings.get('local') or {})
        analysis=dict(settings.get('extraction_engines') or {})
        editing=dict(settings.get('refinement_editing') or {})
        aname=str(analysis.get('terms') or 'auto')
        ename=str(editing.get('provider') or 'local')
        amodel=str(analysis.get('model') or local.get('model') or '')
        emodel=str(editing.get('model') or local.get('model') or '')
        labels={'gemini':'Gemini','openai':'OpenAI','local':'로컬 LLM','auto':'자동 · 번역 엔진'}
        self._routes_ready=False
        self._route_error=''
        self.analysis_info.setText(labels.get(aname,aname)+' · '+(amodel if aname=='local' else '재정비에 사용 불가'))
        self.editing_info.setText(labels.get(ename,ename)+' · '+(emodel if ename=='local' else '재정비에 사용 불가'))
        if aname!='local' or ename!='local':
            self._route_error=('한국어 소설 재정비는 로컬 LLM만 지원합니다. '
                               'API 설정에서 용어·말투 분석 엔진과 문학적 윤문 엔진을 모두 로컬 LLM으로 선택해 주세요.')
        else:
            try:
                self._analysis_route=self._engine_credentials('local',amodel)
                self._editing_route=self._engine_credentials('local',emodel)
                if not str(local.get('endpoint') or '').strip():
                    raise ValueError('API 설정에서 로컬 LLM 서버 주소를 지정하세요.')
                self._routes_ready=True
            except ValueError as exc:
                self._route_error=str(exc)
        if hasattr(self,'start') and self.worker is None:
            self.start.setEnabled(self._can_start())

    def showEvent(self,event):
        super().showEvent(event)
        if self.worker is None:
            self._fill_engines()

    def _engine_credentials(self,name,model):
        if not str(model or '').strip():
            raise ValueError(f'{name}: API 설정에서 모델을 지정하세요.')
        options=dict(self.host.secrets.get('local') or {}) if name=='local' else {}
        key=''
        if name in ('gemini','openai'):
            prefix='GEM' if name=='gemini' else 'OAI'
            records=normalize_key_records((self.host.secrets.get(name) or {}).get('keys',[]),prefix)
            key=next((record.secret for record in records if record.enabled),'')
            if not key: raise ValueError(f'{name}: 유효한 API 키를 등록하세요.')
        return (name,model,key,options)

    def _set_source(self,path):
        self.source=Path(path)
        self.output=refined_output_path(self.source, getattr(self.host,'custom_output_dir',None))
        self.source_label.setText(self.source.name)
        self.source_label.setToolTip(str(self.source))
        self._show_done(False)
        size=_size(self.source)
        self._too_big=size >= MAX_REFINEMENT_BYTES
        if self.worker is None:
            self.start.setEnabled(self._can_start())
        if self._too_big:
            # Said at once, not after [재정비 시작]; the whole file is not read for nothing.
            self.status.setText(f'이 TXT는 {size/1e6:,.1f}MB라 재정비할 수 없습니다. 재정비는 {MAX_REFINEMENT_LABEL} 미만 TXT만 지원합니다.\n'
                                '나눈 TXT를 선택하세요.')
            self.status.setToolTip(str(self.source))
            self._guidance_text=''
            self.guidance_info.setText(f'기준표: {MAX_REFINEMENT_LABEL} 미만 TXT를 선택하면 표시됩니다')
            self.guidance_view.setEnabled(False)
            return
        note=('\n미완료 번역 결과(.ko.partial.txt)입니다. 번역을 끝내면 내용이 바뀌므로 번역을 마친 .ko.txt를 다듬는 것이 좋습니다.'
              if is_unfinished_translation(self.source) else '')
        self.status.setText(f'결과는 별도 파일에 저장됩니다: {wrap_path(self.output)}'+note)
        self.status.setToolTip(str(self.output))
        self._refresh_guidance()

    def _refresh_guidance(self):
        """Which reference table a run on the selected TXT uses, before it starts."""
        self._guidance_text=''
        self._guidance_source_shown=''
        if not self.source:
            return
        try:
            where,summary,table=peek_guidance(self.source)
        except Exception:
            where,summary,table='none','',''
        if where=='translation':
            text='기준표: 번역 기록에서 가져옴 · '+summary
        elif where=='analysis':
            text='기준표: '+summary+' · '+guidance_counts(table)+' (이어서 실행하면 그대로 사용)'
        else:
            text='기준표: 번역 기록 없음 · 시작하면 샘플 분석(책 전체의 이름 + 대사 표본)으로 인물·말투표를 만듭니다'
        self._guidance_text=table
        self.guidance_info.setText(text)
        self.guidance_view.setEnabled(bool(table))

    def _show_done(self, done: bool):
        """The finished mark replaces the progress bar and [클린리더로 열기] replaces [재정비 시작]
        (kept while preserved paragraphs can be retried); a new file or a new run brings both back."""
        self.done_badge.setVisible(done)
        self.progress.setVisible(not done)
        self.reader.setVisible(done and bool(self._result_path))
        self.start.setVisible(not done or self._retry_left > 0 or not self._result_path)
        if not done:
            self.progress.setValue(0)
            self._result_path=None; self._retry_left=0

    def _open_in_reader(self):
        if not self._result_path or not Path(self._result_path).is_file():
            QMessageBox.information(self,'클린리더','열 재정비 결과 파일이 없습니다.'); return
        launch=getattr(self.host,'launch_cleanreader',None)
        if launch is None:
            QMessageBox.information(self,'클린리더','이 창에서는 클린리더를 열 수 없습니다. 번역 화면의 [클린리더로 열기]를 쓰세요.'); return
        launch([self._result_path], parent=self)

    def _show_guidance(self):
        d=QDialog(self); d.setWindowTitle('재정비 기준표'); d.resize(620,460)
        v=QVBoxLayout(d)
        note=QLabel('윤문할 때 모든 요청에 함께 보내는 기준표입니다. 번역 기록에서 가져온 표는 번역 화면의 인물 말투·확정 용어를 고쳐 다시 번역하면 바뀌고, '
                    '샘플 분석 표는 번역할 때처럼 책 전체에서 이름을 세고 대사 표본으로 말투를 정한 자동 추론입니다. '
                    '검토 기록(.review.txt) 끝에도 같은 표가 남습니다.')
        note.setWordWrap(True); v.addWidget(note)
        # The same rows and columns as the translation window's 인물 말투 and 용어 (v2.6.2 보완 3);
        # 전체 내용 keeps the table exactly as sent, so nothing a row could not read is hidden.
        speech,terms=guidance_tables(self._guidance_text)
        tabs=QTabWidget()
        def table_of(headers, rows):
            t=QTableWidget(len(rows),len(headers)); t.setHorizontalHeaderLabels(headers)
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.verticalHeader().setVisible(False)
            for r,row in enumerate(rows):
                for c,value in enumerate(row):
                    item=QTableWidgetItem(value); item.setToolTip(value); t.setItem(r,c,item)
            h=t.horizontalHeader()
            for c in range(len(headers)-1):
                h.setSectionResizeMode(c,QHeaderView.ResizeToContents)
            h.setSectionResizeMode(len(headers)-1,QHeaderView.Stretch)
            return t
        tabs.addTab(table_of(['화자','상대','말투','관계·메모'],speech),f'인물 말투 ({len(speech)})')
        tabs.addTab(table_of(['이름·용어','종류·내용'],terms),f'이름·용어 ({len(terms)})')
        box=QPlainTextEdit(); box.setReadOnly(True); box.setPlainText(self._guidance_text)
        tabs.addTab(box,'전체 내용')
        if not speech and not terms:
            tabs.setCurrentIndex(2)
        v.addWidget(tabs,1)
        close=QPushButton('닫기'); close.clicked.connect(d.accept)
        b=QHBoxLayout(); b.addStretch(1); b.addWidget(close); v.addLayout(b)
        d.exec()

    def _choose(self):
        chosen,_=QFileDialog.getOpenFileName(self,'교정할 한국어 TXT 선택','','텍스트 (*.txt);;모든 파일 (*)')
        if chosen:
            self._set_source(Path(chosen))

    def _start(self):
        if not self.source or not self.source.is_file():
            QMessageBox.information(self,'TXT 파일 필요','먼저 한국어 TXT 파일을 선택하세요.');return
        if self.source.stat().st_size >= MAX_REFINEMENT_BYTES:
            QMessageBox.warning(self,'파일 크기 초과',
                f'한국어 재정비는 {MAX_REFINEMENT_LABEL} 미만의 TXT 파일만 지원합니다.\n'
                '나눈 파일을 선택하세요. 중·일·영 번역에는 이 제한이 없습니다.')
            return
        if is_unfinished_translation(self.source):
            box=QMessageBox(QMessageBox.Question,'미완료 번역 결과',
                '선택한 TXT는 아직 끝나지 않은 번역 결과(.ko.partial.txt)입니다.\n'
                '번역을 마저 끝내면 내용이 바뀌어, 지금 다듬은 결과를 이어 쓰지 못하고 재정비를 다시 해야 합니다.\n\n'
                '그래도 이 파일로 시작할까요?',QMessageBox.Yes|QMessageBox.No,self)
            box.setDefaultButton(QMessageBox.No)
            if box.exec()!=QMessageBox.Yes:
                return
        if korean_share(self.source) < KOREAN_MIN_SHARE:
            answer=QMessageBox.question(self,'한국어 TXT 확인',
                '선택한 파일은 한국어 문장이 적어 번역 전 원문으로 보입니다.\n'
                '한국어 소설 재정비는 이미 한국어로 번역된 TXT를 다듬는 기능입니다.\n\n'
                '그래도 이 파일로 시작할까요?')
            if answer!=QMessageBox.Yes:
                return
        try:
            self.output.parent.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self,'결과 폴더 사용 불가',f'결과 폴더를 만들거나 쓸 수 없습니다.\n{self.output.parent}\n{exc}'); return
        self._fill_engines()
        if not self._routes_ready:
            QMessageBox.warning(self,'API 엔진 설정 확인',self._route_error or 'API 설정에서 분석 및 윤문 엔진을 지정하세요.')
            return
        edit=self._editing_route
        analysis=self._analysis_route
        update_existing=False
        if self.output.exists():
            retry=retry_status(self.source,self.output,edit[0],edit[1])
            if not retry:
                QMessageBox.warning(self,'결과 파일 존재','같은 이름의 결과 파일이 이미 있습니다. 원본과 이전 결과를 보호하기 위해 덮어쓰지 않습니다. 기존 결과의 이름을 변경한 뒤 다시 시작하세요.');return
            answer=QMessageBox.question(self,'보존한 문단만 다시 시도',
                f'이 TXT의 이전 재정비 결과가 있습니다.\n그때 교정하지 못하고 원문으로 보존한 문단 {retry}개만 다시 시도해, '
                f'같은 결과 파일을 갱신할까요?\n\n이미 다듬은 문단은 그대로 두고 다시 요청하지 않습니다.\n{self.output}')
            if answer!=QMessageBox.Yes:
                return
            update_existing=True
        else:
            # An unfinished earlier run that this editing model cannot continue: ask first,
            # never start over silently (its finished paragraphs would be set aside).
            done,engine=resume_conflict(self.source,edit[0],edit[1],
                                        analysis=(analysis[0],analysis[1]) if analysis else None)
            if done:
                answer=QMessageBox.question(self,'이어서 할 수 없는 이전 진행',
                    f'이 TXT는 이전에 [{engine}]로 {done:,}문단까지 다듬다가 중지했습니다.\n'
                    f'지금 윤문 엔진({edit[1]})으로는 그 진행을 이어 갈 수 없습니다.\n\n'
                    '[예] 지금 엔진으로 처음부터 다시 시작 (이전 진행은 지워짐)\n'
                    '[아니요] 취소 — API 설정에서 윤문 엔진을 이전 모델로 되돌리면 이어서 할 수 있습니다.',
                    QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
                if answer!=QMessageBox.Yes:
                    return
        if not update_existing and not self._confirm_long_run(edit[1]):
            return
        # The worker receives the provider's options dict, not this dialog.
        self.worker=RefinementWorker(self.source,self.output,*edit[:3],edit[3],analysis=analysis,
                                     update_existing=update_existing)
        self.worker.progressed.connect(self._progress)
        # The live view, counts and log are optional for the run (a worker without them still runs).
        for name,slot in (('edited',self._edited),('stats',self._stats_changed),('logged',self._log_line)):
            sig=getattr(self.worker,name,None)
            if sig is not None:
                sig.connect(slot)
        self.worker.activity.connect(self._activity)
        self.worker.outcome.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._thread_stopped)
        self.pick.setEnabled(False);self.start.setEnabled(False)
        self.cancel.setText('안전 중지')
        self._stage_text='재정비 시작 중'
        self._stage_started=time.monotonic()
        self._run_started=time.monotonic()
        self._counts=(0,0,0); self._base=None
        self.time_info.setText('경과 0:00:00'); self.time_info.show()
        self._stats={}; self.stats_info.setText(''); self.stats_info.show()
        self._timer.start()
        self.status.setText('교정 중 · 처리한 문단은 복구 정보에 저장됩니다.')
        self.status.setToolTip('')
        self._show_done(False)
        self._live_rows.clear(); self.live.clear()
        self._shutdown_summary=''
        # A refinement runs as long as a translation: the PC must not go to sleep meanwhile.
        set_translation_awake(_sleep_prevention_wanted())
        self.worker.start()

    def _confirm_long_run(self, model) -> bool:
        """Before a long run (LONG_RUN_REQUESTS editing requests or more): how much it asks of
        the local LLM, so a book of days is never started by surprise. Shorter runs start at once."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            est=estimate_run(self.source, 'local', model)
        except Exception:
            return True       # the run itself reports a file it cannot read
        finally:
            QApplication.restoreOverrideCursor()
        left=int(est.get('requests_left') or 0)
        if left < LONG_RUN_REQUESTS:
            return True
        lo,hi=left*20,left*60
        def hours(sec): return f'{sec/3600:,.0f}시간' if sec>=3600*2 else f'{sec/60:,.0f}분'
        resumed=(f'\n이전 실행에서 다듬은 {est["done"]:,}문단은 건너뛰고 이어서 합니다.' if est.get('done') else '')
        box=QMessageBox(QMessageBox.Question,'오래 걸리는 재정비',
            f'{self.source.name}\n'
            f'{est["paragraphs"]:,}문단 · 로컬 LLM 윤문 요청 약 {left:,}회{resumed}\n\n'
            f'요청 1회에 20초~1분이 걸린다면 약 {hours(lo)}~{hours(hi)}가 걸립니다.\n'
            f'실제 시간은 PC와 모델({model})에 따라 크게 달라지며, 시작하면 경과 시간과 남은 예상 시간을 보여 줍니다.\n'
            '중간에 [안전 중지]해도 다듬은 문단은 저장되고, 다시 시작하면 이어서 합니다.\n\n'
            '시작할까요?', QMessageBox.Yes|QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.Yes)
        return box.exec()==QMessageBox.Yes

    def _live_text(self, row) -> str:
        pid,before,after=row
        if self.compare.isChecked():
            if same_result(before, after):
                # v2.6.2 보완: nothing to compare (only quotes or spaces may differ): one line
                return f'[{pid:,}] (변경 없음) {after.strip()}\n'
            return f'[{pid:,}] 전: {before.strip()}\n[{pid:,}] 후: {after.strip()}\n'
        return after.rstrip()

    def _edited(self, rows):
        bar=self.live.verticalScrollBar(); follow=bar.value()>=bar.maximum()-4
        for row in rows:
            self._live_rows.append(row)
        if len(self._live_rows) == self._live_rows.maxlen:
            self._render_live(follow)       # keep only the latest paragraphs in view
            return
        for row in rows:
            self.live.appendPlainText(self._live_text(row))
        if follow:
            bar.setValue(bar.maximum())

    def _render_live(self, follow=True):
        bar=self.live.verticalScrollBar(); keep=bar.value()
        self.live.setPlainText('\n'.join(self._live_text(r) for r in self._live_rows))
        bar.setValue(bar.maximum() if follow else min(keep, bar.maximum()))

    def _compare_toggled(self, on):
        try:
            cfg=load_json(CONFIG_DIR/'app.json'); cfg['refine_live_compare']=bool(on); save_json(CONFIG_DIR/'app.json',cfg)
        except Exception:
            pass
        if self._live_rows:
            self._render_live(True)

    def _activity(self,message):
        self._stage_text=message
        self._stage_started=time.monotonic()
        if self._base is None and message.startswith('윤문 요청'):
            # The estimate counts from the first editing request: paragraphs finished in an
            # earlier run (resume) and the one-off analysis do not distort the pace.
            self._base=(time.monotonic(), self._counts[0])
        self._tick()

    @staticmethod
    def _clock(sec: float) -> str:
        sec=int(max(0,sec)); return f'{sec//3600}:{sec%3600//60:02d}:{sec%60:02d}'

    def _time_text(self) -> str:
        now=time.monotonic(); elapsed=now-getattr(self,'_run_started',now)
        done,total,issues=self._counts
        parts=[]
        if total:
            # 원문 보존 is on the 결과 line above (v2.6.2), once.
            parts.append(f'교정 {done:,}/{total:,} 문단')
        parts.append(f'경과 {self._clock(elapsed)}')
        if self._base and total:
            t0,d0=self._base; ran=now-t0; did=done-d0
            if did>=max(1,total*0.02) and ran>=15 and done<total:
                parts.append(f'남은 예상 약 {self._clock(ran/did*(total-done))}')
        return ' · '.join(parts)

    def _log_line(self, line):
        """작업 기록: the run's events timestamped as in the translation window (latest LOG_ROWS)."""
        self.log_view.appendPlainText(f'[{time.strftime("%H:%M:%S")}] • {line}')

    def _pace(self):
        """(paragraphs per minute, seconds left) at the current pace, or (None, None) too early."""
        done,total,_=self._counts
        if not (self._base and total):
            return None, None
        t0,d0=self._base; ran=time.monotonic()-t0; did=done-d0
        if did>=max(1,total*0.02) and ran>=15 and done<total:
            return did/ran*60, ran/did*(total-done)
        return None, None

    def _stats_text(self) -> str:
        st=self._stats or {}
        done,total,issues=self._counts
        groups=int(st.get('groups') or 0)
        line1=f'요청  {int(st.get("group") or 0):,}/{groups:,}묶음 · 보낸 요청 {int(st.get("requests") or 0):,}회'
        line1+=f' · 재요청 {int(st.get("retries") or 0):,}회 · 연속성 확인 {int(st.get("reviews") or 0):,}회'
        changed=int(st.get("changed") or 0); same=int(st.get("identical") or 0)
        rate=f' (변경률 {changed*100//(changed+same)}%)' if changed+same else ''
        line2=(f'결과  실제 변경 {changed:,}{rate} · 원문과 동일 {same:,}'
               f' · 원문 보존 {int(issues or 0):,}문단')
        replies=int(st.get('replies') or 0)
        if replies:
            line3=f'속도  최근 응답 {float(st.get("reply_last") or 0):.0f}초 · 평균 {float(st.get("reply_total") or 0)/replies:.0f}초'
        else:
            line3='속도  첫 응답을 기다리는 중'
        per_min,left=self._pace()
        if per_min:
            line3+=f' · 분당 약 {per_min:,.0f}문단 · 예상 완료 {finish_time_text(left)}'
        return '\n'.join((line1,line2,line3))

    def _stats_changed(self, st):
        self._stats=dict(st or {})
        table=self._stats.pop('guidance',None)
        source=self._stats.pop('guidance_source','')
        if table is not None:
            self._live_guidance(str(table),source)
        self.stats_info.setText(self._stats_text())

    def _live_guidance(self, table: str, source: str):
        """The table this run uses, shown as soon as it is ready (v2.6.2 보완 3): a sample
        analysis opens with [기준표 보기] during the run, not only after it ends."""
        if table==self._guidance_text and getattr(self,'_guidance_source_shown','')==source:
            return
        self._guidance_text=table
        self._guidance_source_shown=source
        self.guidance_view.setEnabled(bool(table))
        if source=='translation':
            text='기준표: 번역 기록 · '+guidance_counts(table)
        elif source=='analysis' and table:
            text='기준표: 샘플 분석 · '+guidance_counts(table)+' · 이번 윤문에 쓰는 표'
        else:
            text='기준표: 없음 · 샘플 분석 결과가 없어 기준표 없이 다듬습니다'
        self.guidance_info.setText(text)

    def _tick(self):
        if self.worker and self.worker.isRunning():
            elapsed=int(time.monotonic()-self._stage_started)
            self.status.setText(f'{self._stage_text} · 응답 대기 {elapsed}초')
            self.time_info.setText(self._time_text())
            if self._stats:
                self.stats_info.setText(self._stats_text())

    def _progress(self,done,total,issues):
        self.progress.setValue(int(done*100/max(1,total)))
        self._counts=(done,total,issues)
        self.time_info.setText(self._time_text())
        if self._stats:
            self.stats_info.setText(self._stats_text())

    def _finished(self,result):
        if result['status']=='completed':
            changed=result.get('changed',result["done"])
            identical=result.get('identical',0)
            preserved=result.get('preserved',result["total"]-result["done"])
            where={'translation':'번역 기록','analysis':'샘플 분석'}.get(result.get('guidance_source'),'없음')
            residue=(f' · 한자·가나 남은 문단 {result.get("residue_before",0)}→{result.get("residue_after",0)}개'
                     if result.get('residue_before') else '')
            retry=('\n보존한 문단은 같은 TXT로 [재정비 시작]을 다시 누르면 그 문단만 다시 시도합니다.' if preserved else '')
            out=Path(str(result["output"])); rep=Path(str(result["report"]))
            # The finished counts go on the 결과 line above (v2.6.2 보완), so they are said once.
            self._stats=dict(self._stats or {}); self._stats['changed']=int(changed or 0); self._stats['identical']=int(identical or 0)
            self._counts=(int(result.get('done') or 0), int(result.get('total') or 0), int(preserved or 0))
            self.stats_info.setText(self._stats_text()); self.stats_info.show()
            self.status.setText(f'총 소요 시간 {format_duration(result.get("seconds",0))} · 기준표 {where}{residue}\n'
                                f'결과: {short_name(out.name)} · 검토 기록(.review.txt)도 같은 폴더{retry}')
            self.status.setToolTip(f'결과: {out}\n검토 기록: {rep}')
            self._guidance_text=str(result.get('guidance') or '')
            self.guidance_view.setEnabled(bool(self._guidance_text))
            self.guidance_info.setText(f'기준표: {where}' + (' · 이번 윤문에 쓴 표' if self._guidance_text else ''))
            self.progress.setValue(100)
            self._result_path=Path(str(result['output'])); self._retry_left=int(preserved or 0)
            self._show_done(True)
            # Offered once the worker thread has ended (_thread_stopped), never for a stopped run.
            self._shutdown_summary=(f'한국어 소설 재정비를 마쳤습니다. 총 소요 시간 {format_duration(result.get("seconds",0))}\n'
                                    f'실제 변경 {changed}문단 · 원문과 동일 {identical}문단 · 미교정 원문 보존 {preserved}문단\n'
                                    f'결과: {out}')
        else:
            self.status.setText(f'안전 중지했습니다 · 지금까지 소요 시간 {format_duration(result.get("seconds",0))}\n'
                                '동일 TXT와 엔진으로 다시 실행하면 완료한 문단부터 이어지고, 소요 시간도 합산됩니다.')

    def _failed(self,message):
        line='===== 재정비 오류 · '+str(message)
        self._log_line(line)
        if self.output:
            append_run_log(run_log_path(self.output), line)
        took=format_duration(time.monotonic()-getattr(self,'_run_started',time.monotonic()))
        self.status.setText(f'재정비 오류 · 이번 실행 {took} · 처리한 문단의 복구 정보는 유지됩니다.\n'+wrap_path(message))
        self.status.setToolTip(message)

    def _thread_stopped(self):
        set_translation_awake(False)
        self._timer.stop()
        self.time_info.hide()      # the result line below says how long it took (its room is kept)
        self.worker=None
        self.pick.setEnabled(True)
        if not self._guidance_text:
            self._refresh_guidance()      # a stopped run keeps its sample analysis
        self._fill_engines()
        self.cancel.setText('닫기')
        self.start.setEnabled(self._can_start())
        if getattr(self,'_shutdown_summary',''):
            QTimer.singleShot(0,self._offer_shutdown)

    def _offer_shutdown(self):
        """Finished run (고급 설정 option on): 60-second PC shutdown question. On yes this window
        closes and the main window closes the app the normal way, then shuts the PC down."""
        summary=getattr(self,'_shutdown_summary',''); self._shutdown_summary=''
        offer=getattr(self.host,'_offer_shutdown',None)
        if not summary or offer is None or (self.worker and self.worker.isRunning()):
            return
        if offer('재정비 완료', summary, parent=self, log=False):
            self.accept()
            QTimer.singleShot(300,self.host._shutdown_now)

    def _close_or_stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.status.setText('중지 요청됨 · 진행 중인 요청을 끊고 안전하게 중지합니다. (다듬은 문단은 저장되어 있습니다)')
            return
        self.accept()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self._close_or_stop()
            return
        super().reject()

    def done(self, result):
        self._save_window_state()
        super().done(result)

    # Window position and size come back the next time it opens (v2.6.2), as the translation
    # window's do. Only a window that was on screen is remembered.
    def _save_window_state(self):
        if not self.isVisible():
            return
        try:
            import base64
            DATA_DIR.mkdir(parents=True,exist_ok=True)
            save_json(DATA_DIR/WINDOW_STATE_FILE,{'geometry':base64.b64encode(bytes(self.saveGeometry())).decode('ascii')})
        except Exception:
            pass

    def _restore_window_state(self) -> bool:
        try:
            import base64
            from PySide6.QtCore import QByteArray
            raw=str(load_json(DATA_DIR/WINDOW_STATE_FILE).get('geometry') or '')
            return bool(raw) and bool(self.restoreGeometry(QByteArray(base64.b64decode(raw))))
        except Exception:
            return False
