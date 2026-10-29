from __future__ import annotations
from copy import deepcopy
from PySide6.QtWidgets import (
    QDialog,QVBoxLayout,QFormLayout,QGroupBox,QSpinBox,QDoubleSpinBox,
    QCheckBox,QLabel,QDialogButtonBox,QMessageBox,QComboBox,QTabWidget,QWidget
)
from ..config import CONFIG_DIR,load_json,save_json
from ..translation_settings import CONTENT_LABELS,canonical_kind,apply_content_preset

class AdvancedSettingsDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWindowTitle('고급 설정');self.resize(620,580)
        self.path=CONFIG_DIR/'app.json';self.cfg=load_json(self.path)
        root=QVBoxLayout(self)
        self.content_kind=QComboBox()
        for key,label in CONTENT_LABELS.items():self.content_kind.addItem(label,key)
        self.content_kind.setCurrentIndex(self.content_kind.findData(canonical_kind(self.cfg.get('content_kind'))))
        form=QFormLayout();form.addRow('번역 분야',self.content_kind);root.addLayout(form)
        self.note=QLabel();self.note.setWordWrap(True);root.addWidget(self.note)
        rate_box=QGroupBox('API 요청 속도 · Adaptive RPM');rf=QFormLayout(rate_box)
        self.rpm_min=self._double(2,0.5,300,0.5);self.rpm_start=self._double(10,0.5,300,0.5)
        self.rpm_max=self._double(30,0.5,600,0.5);self.success_step=self._spin(5,1,1000)
        for label,widget in [('최소 RPM',self.rpm_min),('시작 RPM',self.rpm_start),('최대 RPM',self.rpm_max),('RPM +1까지 연속 성공 수',self.success_step)]:rf.addRow(label,widget)
        root.addWidget(rate_box)
        self.mode_controls={};self.mode_tabs=QTabWidget();root.addWidget(self.mode_tabs)
        for mode,label in [('balanced','균형 (권장)'),('quality','품질 우선'),('speed','속도 우선')]:
            panel=QWidget();form=QFormLayout(panel)
            chunk=self._double(1,0.25,3,0.05);retry=self._spin(3,0,10)
            timeout=self._spin(90,15,600);context=self._spin(3000,500,30000);context.setSingleStep(250)
            form.addRow('청크 배율 (기준 약 800자)',chunk);form.addRow('최대 재시도',retry)
            form.addRow('요청 제한 시간 (초)',timeout);form.addRow('이전 번역 문맥 (문자)',context)
            form.addRow('동시 요청 / 파일',QLabel('1 / 1 (고정)'))
            self.mode_controls[mode]=(chunk,retry,timeout,context);self.mode_tabs.addTab(panel,label)
        self.prevent_sleep=QCheckBox('번역·재정비 중 시스템 절전 방지 (화면 꺼짐은 허용)');root.addWidget(self.prevent_sleep)
        self.confirm_shutdown=QCheckBox('번역·재정비가 끝나면 PC 종료 확인 창 띄우기 (60초 안에 응답이 없으면 종료)')
        self.confirm_shutdown.setToolTip('번역(파일·폴더 전체)이나 한국어 재정비가 끝까지 끝났을 때만 확인 창이 뜹니다.\n'
                                         '[예]가 기본이며, 60초 동안 응답이 없으면 결과를 저장하고 프로그램을 닫은 뒤 PC를 종료합니다.\n'
                                         '[아니오]를 누르면 종료하지 않습니다. 사용자가 중지했거나 오류로 멈춘 작업에서는 뜨지 않습니다.\n'
                                         '처음에는 꺼져 있고, 켜 두면 끌 때까지 켜진 채로 기억합니다.')
        root.addWidget(self.confirm_shutdown)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('저장');buttons.button(QDialogButtonBox.Cancel).setText('취소')
        buttons.accepted.connect(self._save);buttons.rejected.connect(self._cancel);root.addWidget(buttons)
        self._populate(self.cfg)
        if parent is not None and hasattr(parent,'mode'):
            self.mode_tabs.setCurrentIndex(['balanced','quality','speed'].index(parent.mode.currentData() or 'balanced'))
        self.content_kind.currentIndexChanged.connect(self._preset_changed)

    def _populate(self,cfg):
        rate=cfg.get('rate_limiter') or {}
        for widget,key,value in [(self.rpm_min,'min_rpm',2),(self.rpm_start,'start_rpm',10),(self.rpm_max,'max_rpm',30),(self.success_step,'successes_before_step_up',5)]:widget.setValue(rate.get(key,value))
        for mode,widgets in self.mode_controls.items():
            row=(cfg.get('modes') or {}).get(mode) or {}
            for widget,key,value in zip(widgets,['chunk_factor','max_retries','timeout_seconds','context_chars'],[1,3,90,3000]):widget.setValue(row.get(key,value))
        self.prevent_sleep.setChecked(bool(cfg.get('prevent_sleep_while_running',True)))
        self.confirm_shutdown.setChecked(cfg.get('confirm_shutdown_after_finish',False) is True)
        kind=self.content_kind.currentData()
        self.note.setText('소설: 대사·말투·인물 관계·원문 수위를 보존합니다. 균형 기준 약 800자, 이전 문맥 3,000자.' if kind=='novel' else '일반 문서: 사실·수치·용어·목록과 문서 구조를 보존합니다. 균형 기준 약 1,200자, 이전 문맥 1,000자.')
        self.note.setToolTip('분야를 바꾸면 고급 옵션에 해당 분야의 기본값이 적용됩니다. 저장 전에는 실제 설정이 변경되지 않습니다. 모델·API 키는 변경하지 않습니다.')

    def _preset_changed(self):
        self.cfg=apply_content_preset(self.cfg,self.content_kind.currentData());self._populate(self.cfg);self.mode_tabs.setCurrentIndex(0)

    @staticmethod
    def _spin(value,mn,mx):
        w=QSpinBox();w.setRange(mn,mx);w.setValue(value);return w
    @staticmethod
    def _double(value,mn,mx,step):
        w=QDoubleSpinBox();w.setRange(mn,mx);w.setDecimals(2);w.setSingleStep(step);w.setValue(value);return w

    def _cancel(self):
        self.reject()

    def _save(self):
        mn,start,mx=self.rpm_min.value(),self.rpm_start.value(),self.rpm_max.value()
        if not 0<mn<=start<=mx:
            QMessageBox.warning(self,'RPM 설정 오류','최소 RPM ≤ 시작 RPM ≤ 최대 RPM 순서여야 합니다.');return
        cfg=deepcopy(self.cfg);cfg['content_kind']=self.content_kind.currentData()
        cfg['rate_limiter'].update(mode='adaptive',min_rpm=mn,start_rpm=start,max_rpm=mx,successes_before_step_up=self.success_step.value())
        for mode,(chunk,retry,timeout,context) in self.mode_controls.items():
            cfg['modes'][mode].update(file_concurrency=1,request_concurrency=1,chunk_factor=chunk.value(),max_retries=retry.value(),timeout_seconds=timeout.value(),context_chars=context.value())
        cfg['prevent_sleep_while_running']=self.prevent_sleep.isChecked()
        cfg['confirm_shutdown_after_finish']=self.confirm_shutdown.isChecked()
        try:save_json(self.path,cfg)
        except Exception as e:
            QMessageBox.critical(self,'고급 설정 저장 실패',str(e));return
        self.cfg=cfg;self.accept()
