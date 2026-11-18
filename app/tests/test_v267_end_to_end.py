"""v2.6.7 end to end, with the real translation window, the real refinement window and the
real refinement run (only the model answers are scripted):

1. a translation ends with two paragraphs still untranslated (.ko.partial.txt) after its
   automatic retry -> the refinement starts by itself and refines it;
2. the translation is finished later ([번역 시작 / 계속]) -> book.ko.txt -> the refinement
   starts by itself again and edits ONLY the two newly translated paragraphs.
"""
from __future__ import annotations

import json
from pathlib import Path

import novel_translator.ui.main_window as ui
import novel_translator.ui.shutdown_dialog as sd
import novel_translator.ui.refinement_run_dialog as rd
import novel_translator.refinement_runtime as rr
import novel_translator.refinement_links as rl
from test_alpha17 import _window, _wait, _fake_worker_class, qt_app, sandbox  # noqa: F401
from test_refinement_reference_and_safety import Editor

ZH = [f'第{i}段。他静静地看着窗外的雨。' for i in range(1, 21)]
KO = [f'어색한 {i}번째 문단. 그는 어색하게 창밖의 비를 바라보았다.' for i in range(1, 21)]
LEFT = (4, 15)          # paragraphs (1-based) left untranslated by the first runs


def _worker(started, plans):
    Base = _fake_worker_class(started)
    class W(Base):
        def run(self):
            done = plans[min(len(started) - 1, len(plans) - 1)]
            paras = [KO[i] if done or (i + 1) not in LEFT else ZH[i] for i in range(len(KO))]
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out = self.out_dir / (self.source_path.stem + ('.ko.txt' if done else '.ko.partial.txt'))
            out.write_text('\n\n'.join(paras), encoding='utf-8')
            if done:
                (self.out_dir / (self.source_path.stem + '.ko.partial.txt')).unlink(missing_ok=True)
            self.leftover_count = 0
            self.result_ready.emit('completed' if done else 'partial', str(out), 0 if done else 2)
            self.finished_ok.emit(str(out))
    return W


def test_partial_then_finished_edits_only_the_new_paragraphs(qt_app, sandbox, monkeypatch):
    root, cfg = sandbox
    monkeypatch.setattr(sd, 'CONFIG_DIR', cfg)
    data = root / 'data'
    monkeypatch.setattr(rr, 'DATA_DIR', data); monkeypatch.setattr(rl, 'DATA_DIR', data)
    monkeypatch.setattr(rd, 'CONFIG_DIR', cfg); monkeypatch.setattr(rd, 'DATA_DIR', data)
    monkeypatch.setattr(rd, 'set_translation_awake', lambda on: True)
    editors = []
    def provider(name, endpoint, opts):
        ed = Editor(); editors.append(ed); return ed
    monkeypatch.setattr(rd, '_make_provider', provider)
    windows = []
    monkeypatch.setattr(rd.RefinementRunDialog, 'exec', lambda self: windows.append(self) or self.show() or 0)
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _worker(started, [False, False, True]))
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a, **k: (_ for _ in ()).throw(AssertionError('asked')))
    w.secrets = dict(w.secrets, local={'model': 'ed:latest', 'endpoint': 'http://localhost:11434/v1/chat/completions'},
                     extraction_engines={'terms': 'local', 'speech': 'local', 'model': 'ed:latest'},
                     refinement_editing={'provider': 'local', 'model': 'ed:latest'})
    src = root / 'book.txt'; src.write_text('\n\n'.join(ZH), encoding='utf-8')
    try:
        w._open_txt_path(str(src)); w._update_ready_state()
        w.auto_refine.setChecked(True)
        # 1. partial after the automatic retry -> refined by itself
        w.start_translation()
        assert _wait(qt_app, lambda: windows and windows[-1].done_badge.isVisible(), 30), (
            w.error_view.toPlainText()[-800:], windows and windows[-1].status.text())
        first = windows[-1]
        assert len(started) == 2 and first.source.name == 'book.ko.partial.txt'
        part_out = Path(first.output); assert part_out.name == 'book.ko.partial.refined.txt' and part_out.is_file()
        first_sent = ''.join(getattr(e, 'last_user', '') for e in editors if 'edit' in e.calls)
        assert all(f'P{n:06d}' in first_sent for n in range(1, 21))      # the whole book was refined
        first.close()
        # 2. the translation is finished later -> book.ko.txt -> only P4 and P15 are edited
        editors.clear()
        w.start_translation()
        assert _wait(qt_app, lambda: len(windows) == 2 and windows[-1].done_badge.isVisible(), 30), (
            w.error_view.toPlainText()[-800:], windows[-1].log_view.toPlainText()[-800:])
        second = windows[-1]
        assert second.source.name == 'book.ko.txt'
        log = second.log_view.toPlainText()
        edits = [e.last_user for e in editors if 'edit' in e.calls]
        assert 'P000004' in edits[0] and 'P000015' in edits[0]
        sent = ''.join(edits)
        assert all(f'P{n:06d}' not in sent for n in range(1, 21) if n not in LEFT), sent   # nothing else asked again
        out = Path(second.output).read_text(encoding='utf-8-sig')
        assert out.count('자연스러운 ') == 20 and '第' not in out and '어색한' not in out
        assert not (root / 'book.ko.partial.txt').exists()
        assert list((data / 'refinement_jobs').glob('*.json')) == []      # nothing left behind
    finally:
        for d in windows: d.close()
        w.close()
