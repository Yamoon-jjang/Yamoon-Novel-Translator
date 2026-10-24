from __future__ import annotations
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QDialogButtonBox, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QInputDialog, QMessageBox, QLineEdit,
    QCheckBox, QFormLayout, QGridLayout, QGroupBox, QSizePolicy, QTabWidget, QWidget, QComboBox, QSpinBox, QListWidget, QListWidgetItem, QScrollArea
)
from ..api_keys import ApiKeyRecord, normalize_key_records, stable_key_id, MAX_KEYS_PER_PROVIDER
from ..providers.gemini import GeminiProvider
from ..providers.openai import OpenAIProvider
from ..providers.local_llm import LocalLLMProvider
from ..providers.base import TranslationRequest, ProviderError


class ApiKeyTestThread(QThread):
    done = Signal(bool, str)
    def __init__(self, provider_id: str, secret: str, model: str, endpoint: str = '', parent=None, local_options: dict|None=None):
        super().__init__(parent); self.provider_id=provider_id; self.secret=secret; self.model=model; self.endpoint=endpoint; self.local_options=dict(local_options or {})
    def run(self):
        try:
            if self.provider_id == 'gemini':
                provider=GeminiProvider()
            elif self.provider_id == 'openai':
                provider=OpenAIProvider(self.endpoint or None)
            elif self.provider_id == 'local':
                provider=LocalLLMProvider(self.endpoint or None, 180, think=bool(self.local_options.get('think',False)), num_ctx=self.local_options.get('num_ctx'))
            else:
                raise ValueError(f'지원하지 않는 provider: {self.provider_id}')
            resp=provider.translate(TranslationRequest(self.model,'You are a connection test. Return exactly OK.','Reply exactly OK.',timeout=20),self.secret)
            extra=''
            if self.provider_id=='local':
                speed=getattr(provider,'last_speed',None)
                extra=(f' · 생성 속도 {speed:g} 토큰/초' if speed else '')+(' · Ollama 전용 방식(추론 끄기·컨텍스트 적용)' if provider.native_url else ' · OpenAI 호환 방식')
            self.done.emit(True, f'연결 성공 · 응답: {resp.text[:40]}{extra}')
        except ProviderError as e:
            self.done.emit(False, f'{e.kind.value} · {e}')
        except Exception as e:
            self.done.emit(False, f'{type(e).__name__}: {e}')


def openai_model_rows() -> list[dict]:
    try:
        from ..config import CONFIG_DIR, load_json
        rows=[m for m in load_json(CONFIG_DIR/'models.json').get('models',[]) if m.get('provider')=='openai' and m.get('enabled',True)]
    except Exception:
        rows=[]
    return rows or [{'id':'gpt-6.1-sol','note':''}]


def gemini_model_rows() -> list[dict]:
    try:
        from ..config import CONFIG_DIR, load_json
        rows=[m for m in load_json(CONFIG_DIR/'models.json').get('models',[]) if m.get('provider')=='gemini' and m.get('enabled',True)]
    except Exception:
        rows=[]
    return rows or [{'id':'gemini-3.1-flash-lite','display_name':'Gemini 3.1 Flash Lite'}]


class NoWheelCombo(QComboBox):
    """Prevent the wheel from silently changing a model selection."""
    def wheelEvent(self, event):
        event.ignore()


class ClickModelCombo(NoWheelCombo):
    """Editable model picker: clicking its text field opens the list as well."""
    def setEditable(self, editable):
        super().setEditable(editable)
        if editable and self.lineEdit():
            self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, event):
        if (obj is self.lineEdit() and event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.LeftButton and self.isEnabled() and self.count()):
            # A click anywhere on the model-ID field opens its existing list.
            # Keep typing/pasting an arbitrary model ID possible; never change text here.
            QTimer.singleShot(0, lambda: self.showPopup() if self.isVisible() and self.isEnabled() else None)
        return super().eventFilter(obj, event)


class _ModelCombo(ClickModelCombo):
    """Editable model picker that keeps the QLineEdit-style API (text/textChanged)."""
    def __init__(self, rows, current: str, parent=None, vendor: str='OpenAI'):
        super().__init__(parent); self.setEditable(True); self.setInsertPolicy(QComboBox.NoInsert); self._vendor=vendor
        self._rows={str(r['id']):r for r in rows}
        for r in rows: self.addItem(str(r['id']))
        self.setCurrentText(current)
        self.textChanged=self.currentTextChanged
    def text(self) -> str: return self.currentText().strip()
    def setText(self, t: str): self.setCurrentText(t)
    def info(self) -> str:
        r=self._rows.get(self.text())
        if not r: return f'목록에 없는 모델 ID입니다. {self._vendor} 문서의 모델 ID를 그대로 입력하세요. (요금 추정은 표시되지 않습니다)'
        price=f"입력 ${r['price_input_per_m']:g} / 출력 ${r['price_output_per_m']:g} (100만 토큰당)" if 'price_input_per_m' in r else ''
        return ' · '.join(x for x in (r.get('display_name',''),price,r.get('note','')) if x)


class LocalModelsThread(QThread):
    """Fetch the local server's model list without freezing the dialog."""
    done = Signal(object, str)   # list[str] | None, error message
    def __init__(self, endpoint: str, parent=None):
        super().__init__(parent); self.endpoint=endpoint
    def run(self):
        try:
            self.done.emit(LocalLLMProvider(self.endpoint or None).list_models(), '')
        except ProviderError as e:
            self.done.emit(None, e.message)
        except Exception as e:
            self.done.emit(None, f'{type(e).__name__}: {e}')


class _KeyTable(QWidget):
    def __init__(self, provider_id: str, records, model_getter, endpoint_getter=lambda:'', parent=None):
        super().__init__(parent)
        self.provider_id=provider_id; self._records=normalize_key_records(records, 'GEM' if provider_id=='gemini' else 'OAI')
        self.model_getter=model_getter; self.endpoint_getter=endpoint_getter; self._tester=None; self._test_result=(False,'',None)
        self.on_change=None   # notified after keys are added/removed/toggled
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0)
        self.table=QTableWidget(0,7)
        self.table.setHorizontalHeaderLabels(['사용','이름','프로젝트 그룹','등급','API 키','상태','삭제'])
        for col in (0,6): self.table.horizontalHeader().setSectionResizeMode(col,QHeaderView.ResizeToContents)
        for col in (1,2,4): self.table.horizontalHeader().setSectionResizeMode(col,QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5,QHeaderView.ResizeToContents)
        self.table.setColumnHidden(3, provider_id != 'gemini')
        self.table.verticalHeader().setVisible(False); self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setMinimumHeight(165)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        v.addWidget(self.table,1)
        actions=QHBoxLayout()
        self.add_btn=QPushButton('키 추가'); self.add_btn.clicked.connect(self._add_key)
        self.test_btn=QPushButton('선택 키 연결 테스트'); self.test_btn.clicked.connect(self._test_selected)
        self.up_btn=QPushButton('우선순위 ↑'); self.up_btn.clicked.connect(lambda:self._move_selected(-1))
        self.down_btn=QPushButton('우선순위 ↓'); self.down_btn.clicked.connect(lambda:self._move_selected(1))
        actions.addWidget(self.add_btn); actions.addWidget(self.test_btn); actions.addWidget(self.up_btn); actions.addWidget(self.down_btn); actions.addStretch(1)
        v.addLayout(actions)
        self._refresh()
        self.table.itemChanged.connect(lambda _item: self._notify())

    def _notify(self):
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    def enabled_count(self) -> int:
        self._sync_edits(); return sum(1 for r in self._records if r.enabled)

    def _sync_edits(self):
        by_id={r.id:r for r in self._records}
        for row in range(self.table.rowCount()):
            item=self.table.item(row,0); rid=item.data(Qt.UserRole) if item else None; rec=by_id.get(rid)
            if not rec: continue
            was_enabled=rec.enabled; rec.enabled=item.checkState()==Qt.Checked
            # A cell that does not exist yet (row still being built) never overwrites the saved value.
            if self.table.item(row,1) is not None:
                rec.label=self.table.item(row,1).text().strip()
            if self.table.item(row,2) is not None:
                rec.project_group=self.table.item(row,2).text().strip() or 'default'
            tier_box=self.table.cellWidget(row,3)
            if self.provider_id=='gemini' and tier_box is not None:
                rec.tier=tier_box.currentData() or 'unknown'
            if rec.enabled and (not was_enabled or rec.disabled_reason): rec.disabled_reason=''

    def _refresh(self, select_id: str|None=None):
        # No change notifications while the rows are rebuilt: a listener reading the table
        # mid-rebuild would see half-filled rows (the new key's name/group still empty).
        self.table.blockSignals(True)
        try:
            self._fill_rows(select_id)
        finally:
            self.table.blockSignals(False)
        self.add_btn.setEnabled(len(self._records)<MAX_KEYS_PER_PROVIDER)
        self._notify()

    def _fill_rows(self, select_id: str|None=None):
        self.table.setRowCount(0)
        for rec in self._records:
            row=self.table.rowCount(); self.table.insertRow(row)
            enabled=QTableWidgetItem(''); enabled.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable|Qt.ItemIsSelectable); enabled.setCheckState(Qt.Checked if rec.enabled else Qt.Unchecked); enabled.setData(Qt.UserRole,rec.id); self.table.setItem(row,0,enabled)
            self.table.setItem(row,1,QTableWidgetItem(rec.label)); self.table.setItem(row,2,QTableWidgetItem(rec.project_group))
            from .table_combo import TableComboBox
            tier_box=TableComboBox(); tier_box.addItem('미지정','unknown'); tier_box.addItem('무료','free'); tier_box.addItem('유료','paid')
            tier_index=tier_box.findData(rec.tier); tier_box.setCurrentIndex(tier_index if tier_index>=0 else 0)
            tier_box.currentIndexChanged.connect(lambda _i:self._notify())
            self.table.setCellWidget(row,3,tier_box)
            masked=QTableWidgetItem(rec.masked()); masked.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable); self.table.setItem(row,4,masked)
            status=rec.disabled_reason or ('사용' if rec.enabled else '사용 안 함'); st=QTableWidgetItem(status); st.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable); self.table.setItem(row,5,st)
            btn=QPushButton('삭제'); btn.clicked.connect(lambda _=False,rid=rec.id:self._delete_key(rid)); self.table.setCellWidget(row,6,btn)
            if select_id and rec.id==select_id:self.table.selectRow(row)

    def _add_key(self):
        self._sync_edits()   # keep names / groups typed into the table before it is redrawn
        if len(self._records)>=MAX_KEYS_PER_PROVIDER:
            QMessageBox.information(self,'키 개수 제한',f'한 Provider에는 API 키를 최대 {MAX_KEYS_PER_PROVIDER}개까지 저장할 수 있습니다.'); return
        label_name='Gemini' if self.provider_id=='gemini' else 'OpenAI'
        secret,ok=QInputDialog.getText(self,'API 키 추가',f'{label_name} API 키:',QLineEdit.Password); secret=secret.strip()
        if not ok or not secret:return
        if any(r.secret==secret for r in self._records): QMessageBox.information(self,'중복 키','이미 저장된 API 키입니다.'); return
        label,ok2=QInputDialog.getText(self,'키 이름','구분용 이름 (선택):'); label=label.strip() if ok2 else ''
        # Each new key gets its own group (P1, P2 …) unless the user says it shares a project:
        # keys from different Google projects have separate limits and must not wait for each other.
        used={(r.project_group or '').strip() for r in self._records}
        group_default=next(f'P{n}' for n in range(1,100) if f'P{n}' not in used)
        same=', '.join(sorted(g for g in used if g)) or '없음'
        group,ok3=QInputDialog.getText(self,'프로젝트 그룹',
            '이 키를 만든 Google 프로젝트를 구분하는 이름입니다. 한도는 프로젝트마다 따로입니다.\n\n'
            f'· 새 프로젝트에서 만든 키 → 그대로 두세요 ({group_default})\n'
            f'· 이미 등록한 키와 같은 프로젝트의 키 → 그 키의 그룹 이름을 입력하세요 (지금 있는 그룹: {same})',
            text=group_default); group=group.strip() if ok3 else group_default
        prefix='GEM' if self.provider_id=='gemini' else 'OAI'
        rec=ApiKeyRecord(stable_key_id(secret,prefix),secret,label,group or 'default',True,''); self._records.append(rec); self._refresh(rec.id)

    def _delete_key(self,rid:str):
        self._sync_edits()   # keep edits of the other rows
        rec=next((r for r in self._records if r.id==rid),None)
        if not rec:return
        if QMessageBox.question(self,'API 키 삭제',f'"{rec.display_label()}" 키를 저장 목록에서 삭제할까요?')!=QMessageBox.Yes:return
        self._records=[r for r in self._records if r.id!=rid]; self._refresh()

    def _move_selected(self,delta:int):
        row=self.table.currentRow()
        if row<0:return
        self._sync_edits()
        target=row+delta
        if target<0 or target>=len(self._records):return
        self._records[row],self._records[target]=self._records[target],self._records[row]
        self._refresh(self._records[target].id)

    def _test_selected(self):
        row=self.table.currentRow()
        if row<0: QMessageBox.information(self,'키 선택','테스트할 API 키 행을 먼저 선택하세요.'); return
        item=self.table.item(row,0); rid=item.data(Qt.UserRole) if item else None; rec=next((r for r in self._records if r.id==rid),None)
        if not rec:return
        model=(self.model_getter() or '').strip()
        if not model: QMessageBox.warning(self,'모델 필요','연결 테스트에 사용할 모델 ID를 먼저 입력하세요.'); return
        self.test_btn.setEnabled(False); self.table.setItem(row,5,QTableWidgetItem('연결 테스트 중…'))
        self._tester=ApiKeyTestThread(self.provider_id,rec.secret,model,(self.endpoint_getter() or '').strip(),self)
        self._test_result=(False,'연결 테스트가 종료되었습니다.',rid)
        self._tester.done.connect(lambda ok,msg,rid=rid:setattr(self,'_test_result',(ok,msg,rid)))
        def finished():
            ok,msg,rid=self._test_result; self.test_btn.setEnabled(True)
            rec=next((r for r in self._records if r.id==rid),None)
            if rec is not None:
                if ok:
                    rec.disabled_reason=''; rec.enabled=True
                elif msg.startswith('AUTH ·') or msg.startswith('KEY_BLOCKED ·'):
                    rec.disabled_reason=msg.split(' ·',1)[0]
                    rec.enabled=False
            rr=next((i for i in range(self.table.rowCount()) if self.table.item(i,0) and self.table.item(i,0).data(Qt.UserRole)==rid),-1)
            if rr>=0:
                st=QTableWidgetItem('정상' if ok else msg); st.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable); self.table.setItem(rr,5,st)
                if rec is not None: self.table.item(rr,0).setCheckState(Qt.Checked if rec.enabled else Qt.Unchecked)
            tester=self._tester; self._tester=None
            if tester is not None: tester.deleteLater()
            QMessageBox.information(self,'API 연결 테스트' if ok else 'API 연결 테스트 실패',msg)
        self._tester.finished.connect(finished); self._tester.start()

    def is_testing(self): return bool(self._tester and self._tester.isRunning())
    def records(self) -> list[dict]: self._sync_edits(); return [r.to_dict() for r in self._records]


ENGINE_LABELS={'gemini':'Gemini','openai':'OpenAI','local':'로컬 LLM'}


def initial_engine_order(engines, openai_settings: dict|None, local_settings: dict|None) -> list[tuple[str,bool]]:
    """Saved order, or the legacy meaning (Gemini first, enabled backups after)."""
    out=[]
    if isinstance(engines,list):
        for row in engines:
            if isinstance(row,dict) and row.get('id') in ENGINE_LABELS and row['id'] not in [x for x,_ in out]:
                out.append((row['id'],bool(row.get('enabled',True))))
    if not out:
        local=dict(local_settings or {}); oai=dict(openai_settings or {})
        backups=['openai','local'] if str(local.get('order') or 'local_first')=='openai_first' else ['local','openai']
        out=[('gemini',True)]+[(b,bool((oai if b=='openai' else local).get('enabled'))) for b in backups]
    for name in ENGINE_LABELS:
        if name not in [x for x,_ in out]: out.append((name,False))
    return out


class ApiDialog(QDialog):
    """Gemini primary pool + optional OpenAI fallback settings."""
    def __init__(self, gemini_keys, gemini_model: str='gemini-3.1-flash-lite', openai_settings: dict|None=None, openai_endpoint: str='', parent=None, local_settings: dict|None=None, engines=None, extraction_engines=None, refinement_editing=None):
        super().__init__(parent); self.setWindowTitle('API 설정'); self.resize(900,560)
        self._gemini_model=gemini_model or 'gemini-3.1-flash-lite'; self._openai=dict(openai_settings or {}); self._openai_endpoint=openai_endpoint
        self._local=dict(local_settings or {}); self._local_tester=None
        extraction_engines=dict(extraction_engines or {})
        self._saved_analysis=dict(extraction_engines)
        self._saved_editing=dict(refinement_editing or {})
        outer=QVBoxLayout(self); outer.setSpacing(4); outer.setContentsMargins(9,7,9,7)
        self.settings_scroll=QScrollArea(self)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        body=QWidget(self.settings_scroll)
        v=QVBoxLayout(body); v.setSpacing(5); v.setContentsMargins(0,0,0,0)
        self.settings_scroll.setWidget(body)
        outer.addWidget(self.settings_scroll,1)
        eg=QGroupBox('번역 시 사용 엔진 · 우선순위'); ev=QVBoxLayout(eg); ev.setSpacing(3); ev.setContentsMargins(8,9,8,7); el=QHBoxLayout(); ev.addLayout(el)
        self.engine_list=QListWidget()
        self.engine_list.setFixedHeight(91)
        self.engine_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.engine_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for name,on in initial_engine_order(engines,openai_settings,local_settings):
            item=QListWidgetItem('번역 · '+ENGINE_LABELS[name]); item.setData(Qt.UserRole,name)
            item.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsUserCheckable); item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.engine_list.addItem(item)
        el.addWidget(self.engine_list,1)
        eb=QVBoxLayout(); up=QPushButton('위로 ↑'); dn=QPushButton('아래로 ↓')
        up.clicked.connect(lambda:self._move_engine(-1)); dn.clicked.connect(lambda:self._move_engine(1)); eb.addWidget(up); eb.addWidget(dn); eb.addStretch(1); el.addLayout(eb)
        eh=QLabel('체크된 엔진을 위에서부터 사용합니다. Gemini가 거부한 구간은 활성화된 OpenAI·로컬 LLM 등 다음 엔진으로 재시도할 수 있습니다.')
        eh.setWordWrap(True); eh.setStyleSheet('color:#4b5563;'); ev.addWidget(eh)
        eg.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum); v.addWidget(eg)
        analysis=QGroupBox('용어·말투·윤문 엔진 · API 통합 설정')
        af=QGridLayout(analysis)
        af.setHorizontalSpacing(10); af.setVerticalSpacing(3); af.setContentsMargins(8,9,8,7)
        self.analysis_engine=NoWheelCombo()
        self.editing_engine=NoWheelCombo()
        self.analysis_model=ClickModelCombo(); self.editing_model=ClickModelCombo()
        self.analysis_model.setEditable(True); self.editing_model.setEditable(True)
        self.analysis_model.setInsertPolicy(QComboBox.NoInsert)
        self.editing_model.setInsertPolicy(QComboBox.NoInsert)
        for label,key in [('자동 · 번역 엔진 순서','auto'),('Gemini','gemini'),('OpenAI','openai'),('로컬 LLM','local')]:
            self.analysis_engine.addItem(label,key)
        for label,key in [('Gemini','gemini'),('OpenAI','openai'),('로컬 LLM','local')]:
            self.editing_engine.addItem(label,key)
        prior_terms=extraction_engines.get('terms','auto')
        prior_speech=extraction_engines.get('speech','auto')
        self._legacy_analysis_conflict=prior_terms!=prior_speech
        self.analysis_engine.setCurrentIndex(max(0,self.analysis_engine.findData(prior_terms)))
        self.editing_engine.setCurrentIndex(max(0,self.editing_engine.findData(self._saved_editing.get('provider','local'))))
        af.addWidget(QLabel('작업'),0,0); af.addWidget(QLabel('엔진'),0,1); af.addWidget(QLabel('모델'),0,2)
        af.addWidget(QLabel('용어·말투 분석 · 공통'),1,0); af.addWidget(self.analysis_engine,1,1); af.addWidget(self.analysis_model,1,2)
        af.addWidget(QLabel('문학적 윤문·연속성 · 재정비'),2,0); af.addWidget(self.editing_engine,2,1); af.addWidget(self.editing_model,2,2)
        self.analysis_hint=QLabel('인명·용어·말투 분석은 번역과 한국어 재정비가 함께 사용합니다. 엔진을 고르면 번역 엔진과 상관없이 그 엔진(과 모델)만 씁니다. 자동이면 위에서 체크한 번역 엔진만 그 순서대로 씁니다.')
        self.analysis_hint.setWordWrap(True); af.addWidget(self.analysis_hint,3,0,1,3)
        if self._legacy_analysis_conflict:
            self.analysis_hint.setText('기존 용어 검출과 말투표 엔진이 서로 다릅니다. 저장하면 위에서 선택한 하나의 분석 엔진으로 통합합니다. 취소하면 기존 값은 유지됩니다.')
        guide=QLabel('한국어 소설 재정비는 온라인 AI의 토큰 비용·출력 길이 제한·긴 대기·일부 내용의 교정 거부 문제를 줄이기 위해 로컬 LLM만 지원합니다. 로컬 LLM에도 문맥 길이·처리 속도 제한은 있습니다. 재정비를 사용하려면 위 용어·말투 분석과 문학적 윤문 엔진을 모두 로컬 LLM으로 선택하세요. 번역 시 사용 엔진(Gemini·OpenAI·로컬 LLM)은 별개로 유지됩니다. 로컬 LLM도 품질은 보장되지 않습니다.')
        guide.setWordWrap(True); af.addWidget(guide,4,0,1,3)
        af.setColumnStretch(2,2); af.setColumnStretch(1,1)
        v.addWidget(analysis)
        tabs=QTabWidget(); tabs.setMinimumHeight(275); v.addWidget(tabs,1); self.tabs=tabs

        gem=QWidget(); gv=QVBoxLayout(gem)
        info=QLabel(f'API 키는 최대 {MAX_KEYS_PER_PROVIDER}개까지 저장할 수 있고 위쪽 키가 우선입니다. 정상인 1번 키를 계속 쓰고, 키가 무효·차단되거나 그 프로젝트가 한도에 걸리면 다른 프로젝트의 키로 넘어갑니다. 한도는 키가 아니라 Google 프로젝트 단위이므로, 같은 프로젝트에서 만든 키는 같은 "프로젝트 그룹" 이름을, 다른 프로젝트의 키는 다른 이름(P1, P2…)을 쓰세요. 등급은 사용자가 Google AI Studio에서 확인한 무료/유료를 직접 지정하세요. 미지정 키는 기존 사용 방식이 유지되며 무료로 간주하지 않습니다. 칸을 두 번 클릭하면 고칠 수 있습니다.')
        info.setWordWrap(True); gv.addWidget(info)
        gform=QFormLayout(); self.gemini_model=_ModelCombo(gemini_model_rows(),self._gemini_model,vendor='Google Gemini')
        self.gemini_model_info=QLabel(''); self.gemini_model_info.setWordWrap(True); self.gemini_model_info.setStyleSheet('color:#475569;')
        self.gemini_model.currentTextChanged.connect(lambda _t: self.gemini_model_info.setText(self.gemini_model.info())); self.gemini_model_info.setText(self.gemini_model.info())
        gform.addRow('Gemini 모델 ID',self.gemini_model); gform.addRow('',self.gemini_model_info); gv.addLayout(gform)
        self.gemini_table=_KeyTable('gemini',gemini_keys,lambda:self.gemini_model.text() or self._gemini_model,parent=gem); gv.addWidget(self.gemini_table); tabs.addTab(gem,'Gemini')

        oai=QWidget(); ov=QVBoxLayout(oai)
        oi=QLabel('보조로 쓰면 앞 엔진이 거부·차단한 문단을 받아 번역하고, 앞 엔진이 한도 소진·장애로 멈추면 그 파일의 남은 청크를 이어서 번역합니다. 주 엔진으로 쓰면 처음부터 OpenAI로 번역합니다. API 비용은 별도이며 구매한 크레딧은 1년 뒤 소멸합니다.')
        oi.setWordWrap(True); ov.addWidget(oi)
        form=QFormLayout(); self.openai_model=_ModelCombo(openai_model_rows(),str(self._openai.get('model') or 'gpt-6.1-sol'))
        self.openai_model_info=QLabel(''); self.openai_model_info.setWordWrap(True); self.openai_model_info.setStyleSheet('color:#475569;')
        self.openai_model.currentTextChanged.connect(lambda _t: self.openai_model_info.setText(self.openai_model.info())); self.openai_model_info.setText(self.openai_model.info())
        form.addRow('OpenAI 모델 ID',self.openai_model); form.addRow('',self.openai_model_info); endpoint_label=QLabel(openai_endpoint or OpenAIProvider.DEFAULT_ENDPOINT); endpoint_label.setTextInteractionFlags(Qt.TextSelectableByMouse); form.addRow('Endpoint',endpoint_label); ov.addLayout(form)
        self.openai_table=_KeyTable('openai',self._openai.get('keys',[]),lambda:self.openai_model.text(),lambda:openai_endpoint or OpenAIProvider.DEFAULT_ENDPOINT,parent=oai); ov.addWidget(self.openai_table); tabs.addTab(oai,'OpenAI')

        loc=QWidget(); lv=QVBoxLayout(loc)
        li=QLabel('내 PC에서 실행하는 로컬 LLM(Ollama, LM Studio 등)입니다. API 요금과 서버 차단이 없고, 같은 번역 범위 지침(미성년자 관련 성적 내용 제외)이 적용됩니다. 품질은 클라우드 모델보다 낮을 수 있고 그래픽카드 메모리가 넉넉해야 합니다. Ollama는 설치 후 모델을 받아 두면(예: ollama pull 모델이름) 자동으로 실행됩니다.')
        li.setWordWrap(True); lv.addWidget(li)
        lform=QFormLayout()
        self.local_endpoint=QLineEdit(str(self._local.get('endpoint') or LocalLLMProvider.DEFAULT_ENDPOINT))
        preset=QHBoxLayout(); preset.addWidget(self.local_endpoint,1)
        b1=QPushButton('Ollama'); b1.clicked.connect(lambda:(self.local_endpoint.setText(LocalLLMProvider.OLLAMA_ENDPOINT),self._load_local_models()))
        b2=QPushButton('LM Studio'); b2.clicked.connect(lambda:(self.local_endpoint.setText(LocalLLMProvider.LMSTUDIO_ENDPOINT),self._load_local_models()))
        preset.addWidget(b1); preset.addWidget(b2); lform.addRow('주소',preset)
        self.local_model=_ModelCombo([],str(self._local.get('model') or ''))
        self.local_model.lineEdit().setPlaceholderText('목록에서 고르거나 직접 입력 (Ollama: ollama list에 보이는 이름)')
        self.local_models_btn=QPushButton('목록 불러오기'); self.local_models_btn.setToolTip('실행 중인 Ollama / LM Studio에 등록된 모델 목록을 가져옵니다.')
        self.local_models_btn.clicked.connect(lambda: self._load_local_models(True))
        mrow=QHBoxLayout(); mrow.addWidget(self.local_model,1); mrow.addWidget(self.local_models_btn)
        lform.addRow('모델 이름',mrow)
        self.local_models_status=QLabel(''); self.local_models_status.setWordWrap(True); self.local_models_status.setStyleSheet('color:#6b7280;')
        lform.addRow('',self.local_models_status)
        self._models_thread=None; self._models_requested=False
        self.local_timeout=QSpinBox(); self.local_timeout.setRange(60,1800); self.local_timeout.setSuffix(' 초'); self.local_timeout.setValue(int(self._local.get('timeout_seconds') or 300))
        self.local_timeout.setToolTip('로컬 LLM이 글자를 하나도 보내지 않고 멈춰 있어도 기다리는 최대 시간입니다. 답을 쓰는 중이면 길어져도 끊지 않습니다. 모델을 처음 불러올 때 시간이 걸리니 너무 줄이지 마세요.')
        lform.addRow('응답 대기 한도',self.local_timeout)
        self.local_nothink=QCheckBox('추론(생각) 출력 끄기 · 훨씬 빠름 (권장)')
        self.local_nothink.setChecked(not bool(self._local.get('think',False)))
        self.local_nothink.setToolTip('Qwen3 등 추론 모델은 답 전에 긴 생각을 먼저 생성해 번역이 몇 배 느려집니다. 번역에는 필요 없습니다.')
        lform.addRow('속도',self.local_nothink)
        self.local_ctx=QSpinBox(); self.local_ctx.setRange(4096,65536); self.local_ctx.setSingleStep(2048); self.local_ctx.setSuffix(' 토큰')
        self.local_ctx.setValue(int(self._local.get('num_ctx') or LocalLLMProvider.DEFAULT_NUM_CTX))
        self.local_ctx.setToolTip('한 번 요청(번역 지침 + 앞 문맥 + 원문 + 번역문)에 약 9천~1만 토큰이 필요합니다. 너무 작으면 Ollama가 지침을 잘라내고, 너무 크면 그래픽카드 메모리를 더 써서 느려집니다.')
        lform.addRow('컨텍스트 길이',self.local_ctx)
        lv.addLayout(lform)
        lnote=QLabel('추론 끄기와 컨텍스트 길이는 Ollama 주소(포트 11434)에 적용됩니다. 그래픽카드 메모리보다 큰 모델은 일부가 CPU에서 돌아 매우 느려집니다 (16GB 그래픽카드라면 14B 이하 Q4~Q6 모델이 빠릅니다).')
        lnote.setWordWrap(True); lnote.setStyleSheet('color:#6b7280;'); lv.addWidget(lnote)
        trow=QHBoxLayout(); self.local_test_btn=QPushButton('로컬 LLM 연결 테스트'); self.local_test_btn.clicked.connect(self._test_local)
        self.local_status=QLabel(''); self.local_status.setWordWrap(True); trow.addWidget(self.local_test_btn); trow.addWidget(self.local_status,1); lv.addLayout(trow)
        lv.addStretch(1); tabs.addTab(loc,'로컬 LLM')

        box=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); box.button(QDialogButtonBox.Save).setText('저장'); box.button(QDialogButtonBox.Cancel).setText('취소'); box.accepted.connect(self._save_and_accept); box.rejected.connect(self.reject); outer.addWidget(box)

        # Engine list <-> tabs are linked both ways; roles/status refresh live.
        self._tab_of={'gemini':0,'openai':1,'local':2}; self._syncing=False
        self.gemini_table.on_change=self._refresh_engines; self.openai_table.on_change=self._refresh_engines
        self.engine_list.currentRowChanged.connect(self._engine_selected)
        self.engine_list.itemChanged.connect(lambda _item: self._refresh_engines())
        tabs.currentChanged.connect(self._tab_selected)
        self.openai_model.textChanged.connect(lambda _t: self._refresh_engines())
        self.gemini_model.textChanged.connect(lambda _t: self._refresh_engines())
        self.local_model.textChanged.connect(lambda _t: self._refresh_engines())
        self.local_endpoint.textChanged.connect(lambda _t: self._refresh_engines())
        self._refresh_engines()
        self.engine_list.setCurrentRow(0)
        self.analysis_engine.currentIndexChanged.connect(lambda _i: self._role_model_changed('analysis'))
        self.editing_engine.currentIndexChanged.connect(lambda _i: self._role_model_changed('editing'))
        self._role_model_changed('analysis', self._saved_analysis.get('model',''))
        self._role_model_changed('editing', self._saved_editing.get('model',''))
        # Fetch the local catalog once when the dialog first opens, even if Gemini is the selected tab.
        # The request is asynchronous and does not change the saved analysis/editing selection.

    def _role_model_changed(self, role: str, selected: str|None=None):
        """Show only models belonging to the selected provider; keep an unknown saved ID visible."""
        provider_combo=self.analysis_engine if role=='analysis' else self.editing_engine
        model_combo=self.analysis_model if role=='analysis' else self.editing_model
        provider=provider_combo.currentData()
        if selected is None: selected=''
        model_combo.blockSignals(True)
        model_combo.clear()
        if provider=='auto':
            model_combo.addItem('번역 엔진의 기본 모델 사용','')
            model_combo.setEnabled(False)
        else:
            rows=gemini_model_rows() if provider=='gemini' else openai_model_rows() if provider=='openai' else []
            if provider=='local':
                rows=[{'id':self.local_model.itemText(i)} for i in range(self.local_model.count())]
            model_combo.addItem('해당 제공자의 기본 모델 사용','')
            for row in rows:
                mid=str(row.get('id') or '').strip()
                if mid and model_combo.findData(mid)<0: model_combo.addItem(str(row.get('display_name') or mid),mid)
            if selected and model_combo.findData(selected)<0:
                # Until the local server's list arrives (about a second after opening), a saved
                # local model is shown by its plain name, so the field does not first read
                # "저장된 모델 · …" and then change. Only a name the loaded list lacks is marked.
                pending=provider=='local' and not getattr(self,'_local_list_loaded',False)
                model_combo.addItem(selected if pending else '저장된 모델 · '+selected,selected)
            idx=model_combo.findData(selected)
            model_combo.setCurrentIndex(idx if idx>=0 else 0)
            model_combo.setEnabled(True)
        model_combo.blockSignals(False)

    def _role_selected_model(self, combo):
        """An entered model ID must not silently turn into the provider default."""
        index=combo.currentIndex()
        if index>=0:
            return str(combo.currentData() or '').strip()
        return combo.currentText().strip()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, '_initial_position_set', False):
            return
        self._initial_position_set = True
        if not self._models_requested:
            self._models_requested=True
            QTimer.singleShot(0, self._load_local_models)
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # FHD 1920x1080 and scaled desktops: never exceed available work area.
        # The settings content scrolls; Save/Cancel stays visible below it.
        limit_w=max(1, available.width()-24)
        limit_h=max(1, available.height()-24)
        self.setMaximumSize(limit_w,limit_h)
        self.resize(min(980,limit_w),min(900,limit_h))
        frame=self.frameGeometry()
        x=available.x()+(available.width()-frame.width())//2
        y=available.y()+(available.height()-frame.height())//2
        self.move(max(available.left(),x),max(available.top(),y))

    def _local_testing(self): return bool(self._local_tester and self._local_tester.isRunning())

    def _load_local_models(self, announce: bool = False):
        """Fill the model dropdown from the local server (background; never blocks the dialog)."""
        if self._models_thread and self._models_thread.isRunning():
            return
        self.local_models_btn.setEnabled(False); self.local_models_status.setText('모델 목록을 가져오는 중…')
        t=LocalModelsThread(self.local_endpoint.text().strip(), self); self._models_thread=t
        t.done.connect(self._local_models_loaded); t.finished.connect(lambda: self.local_models_btn.setEnabled(True))
        t.start()

    def _local_models_loaded(self, names, error):
        if names is None:
            self.local_models_status.setText('목록을 가져오지 못했습니다 · '+error+' · 모델 이름을 직접 입력해도 됩니다.'); return
        self._local_list_loaded=True
        current=self.local_model.text()
        self.local_model.blockSignals(True)
        self.local_model.clear(); self.local_model.addItems(names)
        if current:
            self.local_model.setCurrentText(current)
        elif names:
            self.local_model.setCurrentIndex(0)
        self.local_model.blockSignals(False)
        for role, provider_combo, model_combo in [('analysis',self.analysis_engine,self.analysis_model),('editing',self.editing_engine,self.editing_model)]:
            if provider_combo.currentData()=='local':
                selected=model_combo.currentData() or model_combo.currentText().strip()
                self._role_model_changed(role, selected if selected!='해당 제공자의 기본 모델 사용' else '')
        self._refresh_engines()
        if not names:
            self.local_models_status.setText('서버에 등록된 모델이 없습니다. Ollama라면 모델을 먼저 받아 두세요 (ollama pull 모델이름).')
        else:
            known=current in names or any(n.split(':')[0]==current for n in names) or not current
            self.local_models_status.setText(f'등록된 모델 {len(names)}개' + ('' if known else f' · 지금 입력된 "{current}"는 목록에 없습니다'))

    def done(self, result):
        self._wait_models_thread()   # never destroy the dialog while the list lookup runs
        super().done(result)

    def _wait_models_thread(self):
        if self._models_thread and self._models_thread.isRunning():
            self._models_thread.wait(6000)

    def _move_engine(self, delta: int):
        row=self.engine_list.currentRow(); target=row+delta
        if row<0 or target<0 or target>=self.engine_list.count(): return
        item=self.engine_list.takeItem(row); self.engine_list.insertItem(target,item); self.engine_list.setCurrentRow(target)
        self._refresh_engines()

    def _engine_status(self, name: str) -> tuple[bool, str]:
        """(ready, short status) for the list line."""
        if name=='gemini':
            n=self.gemini_table.enabled_count()
            model=self.gemini_model.text().strip()
            return (n>0 and bool(model)), (f'키 {n}개 · {model}' if n else '키 없음') if model else '모델 ID 없음'
        if name=='openai':
            n=self.openai_table.enabled_count(); model=self.openai_model.text().strip()
            return (n>0 and bool(model)), (f'키 {n}개 · {model}' if n else '키 없음') if model else '모델 ID 없음'
        model=self.local_model.text().strip(); endpoint=self.local_endpoint.text().strip()
        return (bool(model) and bool(endpoint)), (f'{model}' if model else '모델 이름 없음')

    def _roles(self) -> dict[str,str]:
        roles={}; rank=0
        for i in range(self.engine_list.count()):
            item=self.engine_list.item(i); name=item.data(Qt.UserRole)
            if item.checkState()==Qt.Checked:
                rank+=1; roles[name]='주 엔진' if rank==1 else f'보조 {rank-1}'
            else:
                roles[name]='사용 안 함'
        return roles

    def _refresh_engines(self):
        if self._syncing or not hasattr(self,'tabs'): return
        self._syncing=True
        try:
            roles=self._roles(); rank=0
            self.engine_list.blockSignals(True)
            for i in range(self.engine_list.count()):
                item=self.engine_list.item(i); name=item.data(Qt.UserRole); checked=item.checkState()==Qt.Checked
                ready,status=self._engine_status(name)
                if checked: rank+=1
                prefix=f'{rank}. ' if checked else '–  '
                warn='' if (ready or not checked) else '  ⚠ 설정 필요'
                item.setText(f'{prefix}{ENGINE_LABELS[name]}  —  {roles[name]} · {status}{warn}')
            self.engine_list.blockSignals(False)
            for name,idx in self._tab_of.items():
                self.tabs.setTabText(idx,f'{ENGINE_LABELS[name]} ({roles.get(name,"사용 안 함")})')
            total=sum(max(self.engine_list.sizeHintForRow(i),self.engine_list.fontMetrics().height()+4) for i in range(self.engine_list.count()))
            self.engine_list.setFixedHeight(max(total,1)+2*self.engine_list.frameWidth()+4)
        finally:
            self._syncing=False

    def _engine_selected(self, row: int):
        if self._syncing or row<0: return
        name=self.engine_list.item(row).data(Qt.UserRole)
        self._syncing=True
        try: self.tabs.setCurrentIndex(self._tab_of.get(name,0))
        finally: self._syncing=False

    def _tab_selected(self, index: int):
        if index==self._tab_of.get('local') and not self._models_requested:
            self._models_requested=True; self._load_local_models()   # first visit of the 로컬 LLM tab
        if self._syncing: return
        name=next((n for n,i in self._tab_of.items() if i==index),None)
        self._refresh_engines()
        for row in range(self.engine_list.count()):
            if self.engine_list.item(row).data(Qt.UserRole)==name:
                self._syncing=True
                try: self.engine_list.setCurrentRow(row)
                finally: self._syncing=False
                break

    def engine_rows(self) -> list[dict]:
        rows=[]
        for i in range(self.engine_list.count()):
            item=self.engine_list.item(i); rows.append({'id':item.data(Qt.UserRole),'enabled':item.checkState()==Qt.Checked})
        return rows

    def _test_local(self):
        model=self.local_model.text().strip()
        if not model: QMessageBox.warning(self,'모델 이름 필요','로컬 LLM 모델 이름을 먼저 입력하세요.'); return
        self.local_test_btn.setEnabled(False); self.local_status.setText('연결 테스트 중… (모델을 처음 불러오면 1~2분 걸릴 수 있습니다)')
        self._local_tester=ApiKeyTestThread('local','local',model,self.local_endpoint.text().strip(),self,
                                            local_options={'think':not self.local_nothink.isChecked(),'num_ctx':int(self.local_ctx.value())})
        self._local_result=(False,'연결 테스트가 종료되었습니다.')
        self._local_tester.done.connect(lambda ok,msg:setattr(self,'_local_result',(ok,msg)))
        def finished():
            ok,msg=self._local_result; self.local_test_btn.setEnabled(True); self.local_status.setText(('정상 · ' if ok else '실패 · ')+msg)
            tester=self._local_tester; self._local_tester=None
            if tester is not None: tester.deleteLater()
        self._local_tester.finished.connect(finished); self._local_tester.start()

    def _save_and_accept(self):
        if self.gemini_table.is_testing() or self.openai_table.is_testing() or self._local_testing(): QMessageBox.information(self,'연결 테스트 중','API 연결 테스트가 끝난 뒤 저장해 주세요.'); return
        engines=self.engine_rows(); on={r['id'] for r in engines if r['enabled']}
        if not on:
            QMessageBox.warning(self,'번역 엔진 없음','번역 엔진을 하나 이상 체크하세요.'); return
        gem=self.gemini_table.records(); opkeys=self.openai_table.records(); enabled='openai' in on; model=self.openai_model.text().strip()
        if 'gemini' in on and not any(r.get('enabled',True) for r in gem):
            QMessageBox.warning(self,'Gemini 준비 안 됨','Gemini를 체크했지만 사용 가능한 Gemini 키가 없습니다. 키를 추가하거나 Gemini 체크를 해제하세요.'); return
        # A Google project has one billing tier regardless of how many API keys it has.
        group_tiers={}
        for item in gem:
            group=item.get('project_group') or 'default'
            tier=item.get('tier') or 'unknown'
            if group in group_tiers and group_tiers[group]!=tier:
                QMessageBox.warning(self,'프로젝트 등급 불일치',f'같은 프로젝트 그룹 {group}의 무료/유료 등급이 서로 다릅니다. 프로젝트 그룹과 등급을 확인하세요.'); return
            group_tiers[group]=tier
        gmodel=self.gemini_model.text().strip().removeprefix('models/')
        if 'gemini' in on and not gmodel:
            QMessageBox.warning(self,'Gemini 모델 필요','Gemini 모델 ID를 고르거나 입력하세요.'); return
        if enabled and (not model or not any(r.get('enabled',True) for r in opkeys)):
            QMessageBox.warning(self,'OpenAI fallback 준비 안 됨','OpenAI fallback을 사용하려면 모델 ID와 사용 가능한 OpenAI API 키가 하나 이상 필요합니다.'); return
        local_enabled='local' in on; local_model=self.local_model.text().strip(); local_endpoint=self.local_endpoint.text().strip()
        if local_enabled and (not local_model or not local_endpoint):
            QMessageBox.warning(self,'로컬 LLM 준비 안 됨','로컬 LLM을 사용하려면 주소와 모델 이름이 필요합니다.'); return
        self._saved={'gemini':{'keys':gem},'openai':{'enabled':enabled,'model':model,'keys':opkeys},
                     'local':{'enabled':local_enabled,'endpoint':local_endpoint or LocalLLMProvider.DEFAULT_ENDPOINT,'model':local_model,
                              'timeout_seconds':int(self.local_timeout.value()),
                              'think':not self.local_nothink.isChecked(),'num_ctx':int(self.local_ctx.value())},
                     'engines':engines,'gemini_model':gmodel or self._gemini_model,
                      'extraction_engines':{'terms':self.analysis_engine.currentData(),
                                            'speech':self.analysis_engine.currentData(),
                                            'model':self._role_selected_model(self.analysis_model)},
                     'refinement_editing':{'provider':self.editing_engine.currentData(),
                                           'model':self._role_selected_model(self.editing_model)}}
        self.accept()

    def reject(self):
        if self.gemini_table.is_testing() or self.openai_table.is_testing() or self._local_testing(): QMessageBox.information(self,'연결 테스트 중','API 연결 테스트가 끝난 뒤 창을 닫아 주세요.'); return
        self._wait_models_thread()
        super().reject()
    def closeEvent(self,event):
        if self.gemini_table.is_testing() or self.openai_table.is_testing() or self._local_testing(): event.ignore(); QMessageBox.information(self,'연결 테스트 중','API 연결 테스트가 끝난 뒤 창을 닫아 주세요.'); return
        self._wait_models_thread()
        event.accept()
    def settings_payload(self) -> dict: return dict(getattr(self,'_saved',{}))
