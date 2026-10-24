"""Separate dialog for Korean literary editing; translation mode remains unchanged."""
from __future__ import annotations
from pathlib import Path
import re
import time
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QLabel,
                               QPushButton, QFileDialog, QProgressBar, QMessageBox, QPlainTextEdit,
                               QSizePolicy)
from ..refinement_runtime import (run_refinement, MAX_REFINEMENT_BYTES, format_duration,
                                  retry_status, peek_guidance)
from ..api_keys import normalize_key_records
from .main_window import _make_provider
from ..textio import read_txt_sample, decode_txt_bytes

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


# Lines kept free for the status text: a finished run's summary is at most four lines (five on a
# large system font), so
# the window keeps one size from opening to finishing (only a rare long error grows it).
STATUS_LINES = 5


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
            result=run_refinement(self.source,self.output,provider,self.model,self.api_key,
                                  stop=lambda: self._stop, analysis_engine=analysis,
                                  update_existing=self.update_existing,
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
        self.resize(620,335)
        self.worker=None
        self._stage_text='대기 중'
        self._stage_started=0.0
        self._timer=QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.source=None
        self.output=None
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
        bar=QHBoxLayout()
        self.start=QPushButton('재정비 시작')
        self.start.clicked.connect(self._start)
        self.start.setEnabled(self._routes_ready)
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
        out=[]
        result=getattr(host,'last_result_path',None)
        if result and Path(result).is_file() and Path(result).name.lower().endswith(('.ko.txt','.ko.partial.txt')):
            out.append(Path(result))
        existing=getattr(host,'source_path',None)
        if existing and Path(existing).is_file():
            out.append(Path(existing))
        return out

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
            self.start.setEnabled(self._routes_ready)

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
        stem = self.source.stem
        suffix = '.refined.txt' if stem.lower().endswith('.ko') else '.ko.refined.txt'
        # The app-wide output folder holds every result, refinement included.
        out_dir = getattr(self.host,'custom_output_dir',None) or self.source.parent
        self.output=Path(out_dir)/(stem+suffix)
        self.source_label.setText(self.source.name)
        self.source_label.setToolTip(str(self.source))
        self._show_done(False)
        self.status.setText(f'결과는 별도 파일에 저장됩니다: {wrap_path(self.output)}')
        self.status.setToolTip(str(self.output))
        self._refresh_guidance()

    def _refresh_guidance(self):
        """Which reference table a run on the selected TXT uses, before it starts."""
        self._guidance_text=''
        if not self.source:
            return
        try:
            where,summary,table=peek_guidance(self.source)
        except Exception:
            where,summary,table='none','',''
        if where=='translation':
            text='기준표: 번역 기록에서 가져옴 · '+summary
        elif where=='analysis':
            text='기준표: '+summary+'(이어서 실행하면 그대로 사용)'
        else:
            text='기준표: 번역 기록 없음 · 시작하면 샘플 분석으로 만듭니다'
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
        d=QDialog(self); d.setWindowTitle('재정비 기준표'); d.resize(560,420)
        v=QVBoxLayout(d)
        note=QLabel('윤문할 때 모든 요청에 함께 보내는 기준표입니다. 번역 기록에서 가져온 표는 번역 화면의 인물 말투·확정 용어를 고쳐 다시 번역하면 바뀌고, '
                    '샘플 분석 표는 자동 추론입니다. 검토 기록(.review.txt) 끝에도 같은 표가 남습니다.')
        note.setWordWrap(True); v.addWidget(note)
        box=QPlainTextEdit(); box.setReadOnly(True); box.setPlainText(self._guidance_text); v.addWidget(box,1)
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
                '한국어 재정비는 10MB 미만의 TXT 파일만 지원합니다.\n'
                '장별로 나눈 파일을 선택하세요. 기존 중·일·영 번역에는 적용되지 않습니다.')
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
        # The worker receives the provider's options dict, not this dialog.
        self.worker=RefinementWorker(self.source,self.output,*edit[:3],edit[3],analysis=analysis,
                                     update_existing=update_existing)
        self.worker.progressed.connect(self._progress)
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
        self._timer.start()
        self.status.setText('교정 중 · 처리한 문단은 복구 정보에 저장됩니다.')
        self.status.setToolTip('')
        self._show_done(False)
        self.worker.start()

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
            parts.append(f'교정 {done:,}/{total:,} 문단'+(f' · 원문 보존 {issues:,}' if issues else ''))
        parts.append(f'경과 {self._clock(elapsed)}')
        if self._base and total:
            t0,d0=self._base; ran=now-t0; did=done-d0
            if did>=max(1,total*0.02) and ran>=15 and done<total:
                parts.append(f'남은 예상 약 {self._clock(ran/did*(total-done))}')
        return ' · '.join(parts)

    def _tick(self):
        if self.worker and self.worker.isRunning():
            elapsed=int(time.monotonic()-self._stage_started)
            self.status.setText(f'{self._stage_text} · 응답 대기 {elapsed}초')
            self.time_info.setText(self._time_text())

    def _progress(self,done,total,issues):
        self.progress.setValue(int(done*100/max(1,total)))
        self._counts=(done,total,issues)
        self.time_info.setText(self._time_text())

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
            self.status.setText(f'총 소요 시간 {format_duration(result.get("seconds",0))} · 기준표 {where}{residue}\n'
                                f'실제 변경 {changed}문단 · 원문과 동일 {identical}문단 · 미교정 원문 보존 {preserved}문단\n'
                                f'결과: {short_name(out.name)} · 검토 기록(.review.txt)도 같은 폴더{retry}')
            self.status.setToolTip(f'결과: {out}\n검토 기록: {rep}')
            self._guidance_text=str(result.get('guidance') or '')
            self.guidance_view.setEnabled(bool(self._guidance_text))
            self.guidance_info.setText(f'기준표: {where}' + (' · 이번 윤문에 쓴 표' if self._guidance_text else ''))
            self.progress.setValue(100)
            self._result_path=Path(str(result['output'])); self._retry_left=int(preserved or 0)
            self._show_done(True)
        else:
            self.status.setText(f'안전 중지했습니다 · 지금까지 소요 시간 {format_duration(result.get("seconds",0))}\n'
                                '동일 TXT와 엔진으로 다시 실행하면 완료한 문단부터 이어지고, 소요 시간도 합산됩니다.')

    def _failed(self,message):
        took=format_duration(time.monotonic()-getattr(self,'_run_started',time.monotonic()))
        self.status.setText(f'재정비 오류 · 이번 실행 {took} · 처리한 문단의 복구 정보는 유지됩니다.\n'+wrap_path(message))
        self.status.setToolTip(message)

    def _thread_stopped(self):
        self._timer.stop()
        self.time_info.hide()      # the result line below says how long it took (its room is kept)
        self.worker=None
        self.pick.setEnabled(True)
        if not self._guidance_text:
            self._refresh_guidance()      # a stopped run keeps its sample analysis
        self._fill_engines()
        self.cancel.setText('닫기')
        self.start.setEnabled(self._routes_ready)

    def _close_or_stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.status.setText('중지 요청됨 · 현재 API 응답 이후 안전하게 중지합니다.')
            return
        self.accept()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self._close_or_stop()
            return
        super().reject()
