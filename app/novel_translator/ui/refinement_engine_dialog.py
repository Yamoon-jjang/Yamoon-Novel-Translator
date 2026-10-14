"""Stage-specific model pickers; no translation routes or secrets are modified."""
from __future__ import annotations
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QGridLayout, QLabel, QComboBox,
                               QPushButton, QHBoxLayout, QMessageBox)
from ..config import CONFIG_DIR, load_json
from ..providers.local_llm import LocalLLMProvider
from ..refinement_engines import STAGES, PROVIDERS, load_routes, save_routes
from .api_dialog import LocalModelsThread, NoWheelCombo

LABELS = {'gemini': 'Gemini API', 'openai': 'OpenAI API', 'local': '로컬 LLM'}


def configured_models(provider, local_settings=None):
    """Only catalog models for online services. No API calls or secret reads."""
    if provider == 'local':
        return []
    try:
        rows = load_json(CONFIG_DIR / 'models.json').get('models', [])
        return [(str(m['id']), str(m.get('display_name') or m['id'])) for m in rows
                if m.get('provider') == provider and m.get('enabled', True) and m.get('id')]
    except (OSError, ValueError, KeyError, TypeError):
        return []


class RefinementEngineDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('한국어 소설 재정비 · 단계별 엔진 설정')
        self.resize(710, 340)
        self.rows = {}
        self._models_thread = None
        self._local_names = []
        self._local_settings = dict(getattr(parent, 'secrets', {}).get('local') or {})
        self._saved = load_routes()
        outer = QVBoxLayout(self)
        intro = QLabel('분석과 윤문에 각각 하나의 모델을 설정합니다. 기존 번역 API 키는 변경하지 않습니다.')
        intro.setWordWrap(True)
        outer.addWidget(intro)
        hint = QLabel('엔진을 변경하면 해당 엔진의 모델만 표시합니다. 기존 4단계 설정은 두 그룹으로 자동 읽기 호환됩니다.')
        hint.setWordWrap(True)
        outer.addWidget(hint)
        grid = QGridLayout()
        for column, caption in enumerate(('작업 단계', '엔진', '사용 모델')):
            grid.addWidget(QLabel(caption), 0, column)
        for i, (key, title) in enumerate(STAGES, 1):
            provider = QComboBox(self)
            for item in PROVIDERS:
                provider.addItem(LABELS[item], item)
            model = NoWheelCombo(self)
            model.setMinimumWidth(250)
            model.setToolTip('이 단계에서 사용하는 모델입니다. 다른 단계의 선택에는 영향을 주지 않습니다.')
            grid.addWidget(QLabel(title), i, 0)
            grid.addWidget(provider, i, 1)
            grid.addWidget(model, i, 2)
            self.rows[key] = (provider, model)
            provider.setCurrentIndex(provider.findData(self._saved[key]['provider']))
            provider.currentIndexChanged.connect(lambda _i, stage=key: self._fill_stage(stage, ''))
        outer.addLayout(grid)
        advice = QLabel('성인 장면이 많은 소설은 표현을 생략·완화하지 않는 로컬 LLM 모델을 권장합니다. 검열 해제 모델이라도 한국어 편집 품질은 별도로 확인하세요.')
        advice.setWordWrap(True)
        outer.addWidget(advice)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        refresh = QPushButton('로컬 모델 목록 새로고침')
        refresh.clicked.connect(self._refresh_local)
        outer.addWidget(refresh)
        self.refresh_button = refresh
        for key, _ in STAGES:
            self._fill_stage(key, self._saved[key]['model'])
        outer.addStretch(1)
        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton('취소')
        save = QPushButton('저장')
        cancel.clicked.connect(lambda: self.reject())
        save.clicked.connect(self._save)
        bar.addWidget(cancel)
        bar.addWidget(save)
        outer.addLayout(bar)
        if any(p.currentData() == 'local' for p, _ in self.rows.values()):
            self._refresh_local()

    def _fill_stage(self, key, selected=None):
        provider, combo = self.rows[key]
        current = '' if selected is None else selected
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('API 설정 기본 모델', '')
        kind = provider.currentData()
        if kind == 'local':
            available = [(name, name) for name in self._local_names]
        else:
            available = configured_models(kind)
        for model_id, display in available:
            combo.addItem(display, model_id)
        if current:
            index = combo.findData(current)
            if index < 0:
                self.status.setText('저장된 모델이 현재 목록에 없습니다. 올바른 모델을 다시 선택하세요.') if hasattr(self,'status') else None
                combo.setCurrentIndex(0)
            else:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _refresh_local(self):
        if self._models_thread is not None and self._models_thread.isRunning():
            return
        endpoint = str(self._local_settings.get('endpoint') or LocalLLMProvider.DEFAULT_ENDPOINT).strip()
        self.status.setText('로컬 모델 목록을 확인하는 중입니다…')
        self.refresh_button.setEnabled(False)
        thread = LocalModelsThread(endpoint, self)
        self._models_thread = thread
        thread.done.connect(self._local_loaded)
        thread.finished.connect(lambda: self.refresh_button.setEnabled(True))
        thread.start()

    def _local_loaded(self, names, error):
        if names is None:
            self.status.setText('로컬 LLM에 연결할 수 없습니다. API 설정에서 서버를 확인하세요. ' + str(error))
            return
        self._local_names = [str(n) for n in names if str(n).strip()]
        for key, (provider, combo) in self.rows.items():
            if provider.currentData() == 'local':
                self._fill_stage(key, combo.currentData())
        self.status.setText(f'로컬 모델 {len(self._local_names)}개 확인 · 없는 경우 API 설정에서 먼저 모델을 등록하세요.')

    def done(self, code):
        thread = self._models_thread
        if thread is not None and thread.isRunning():
            thread.wait(5000)
            if thread.isRunning():
                QMessageBox.information(self, '모델 확인 중', '로컬 모델 목록 조회가 끝나면 창을 닫을 수 있습니다.')
                return
        super().done(code)

    def _save(self):
        routes = {key: {'provider': pair[0].currentData(), 'model': pair[1].currentData() or ''}
                  for key, pair in self.rows.items()}
        try:
            save_routes(routes)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '설정을 저장하지 못했습니다', str(exc))
            return
        self.accept()
