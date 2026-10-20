"""API settings are the sole source of the two refinement model routes."""
from PySide6.QtWidgets import QApplication, QWidget, QComboBox
from novel_translator.ui.refinement_run_dialog import RefinementRunDialog


class Host(QWidget):
    def __init__(self):
        super().__init__()
        self.source_path=None
        self.secrets={
            'local':{'model':'base:latest','endpoint':'http://localhost:11434/v1/chat/completions'},
            'extraction_engines':{'terms':'local','speech':'local','model':'analysis:latest'},
            'refinement_editing':{'provider':'local','model':'editor:latest'},
        }
    def _selected_model_id(self): return 'gemini-3.1-flash-lite'
    def _engine_order(self): return ['local']


def test_refinement_reads_models_directly_without_duplicate_selectors():
    app=QApplication.instance() or QApplication([])
    h=Host()
    d=RefinementRunDialog(h)
    try:
        assert not d.findChildren(QComboBox)
        assert d.analysis_info.text()=='로컬 LLM · analysis:latest'
        assert d.editing_info.text()=='로컬 LLM · editor:latest'
        assert d._analysis_route[:2]==('local','analysis:latest')
        assert d._editing_route[:2]==('local','editor:latest')
        assert d._routes_ready
        assert d.start.isEnabled()
        h.secrets['refinement_editing']['model']='new:latest'
        d._fill_engines()
        assert d.editing_info.text()=='로컬 LLM · new:latest'
        assert d._editing_route[1]=='new:latest'
    finally:
        d.close()


def test_refinement_cannot_start_without_configured_editing_model():
    app=QApplication.instance() or QApplication([])
    h=Host()
    h.secrets['refinement_editing']={'provider':'local','model':''}
    h.secrets['local']['model']=''
    d=RefinementRunDialog(h)
    try:
        assert not d._routes_ready
        assert not d.start.isEnabled()
        assert not d.findChildren(QComboBox)
    finally:
        d.close()


def test_online_engine_is_blocked_without_changing_api_settings():
    app=QApplication.instance() or QApplication([])
    h=Host()
    h.secrets['extraction_engines']['terms']='gemini'
    before=str(h.secrets)
    d=RefinementRunDialog(h)
    try:
        assert not d._routes_ready and not d.start.isEnabled()
        assert '모두 로컬 LLM' in d._route_error
        assert '재정비에 사용 불가' in d.analysis_info.text()
        assert str(h.secrets)==before
    finally:
        d.close()


def test_dialog_shows_which_reference_table_a_run_uses(tmp_path, monkeypatch):
    import novel_translator.refinement_links as links
    import novel_translator.refinement_runtime as runtime
    monkeypatch.setattr(links, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(runtime, 'DATA_DIR', tmp_path)
    app=QApplication.instance() or QApplication([])
    linked=tmp_path/'book.ko.txt'; linked.write_text('“어디 가세요?” 은지가 물었다.\n민수가 웃었다.',encoding='utf-8')
    links.record_translation(linked, linked.read_text(encoding='utf-8'),
                             [{'speaker':'은지','listener':'민수','level':'해요체'}], {'恩智':'은지'})
    plain=tmp_path/'other.txt'; plain.write_text('“그래.” 그가 말했다.\n밤이 깊었다.',encoding='utf-8')
    d=RefinementRunDialog(Host())
    try:
        d._set_source(linked)
        assert '번역 기록에서 가져옴' in d.guidance_info.text() and '말투 1쌍' in d.guidance_info.text()
        assert d.guidance_view.isEnabled() and '은지 → 민수: 해요체' in d._guidance_text
        d._set_source(plain)
        assert '샘플 분석' in d.guidance_info.text()
        assert not d.guidance_view.isEnabled()
    finally:
        d.close()


def test_finished_run_shows_the_done_mark_instead_of_the_bar(tmp_path):
    app=QApplication.instance() or QApplication([])
    d=RefinementRunDialog(Host())
    try:
        d.show(); app.processEvents()
        assert d.progress.isVisible() and not d.done_badge.isVisible()
        d._finished({'status':'completed','done':3,'total':3,'changed':2,'identical':1,'preserved':0,
                     'seconds':65,'output':'out.txt','report':'out.txt.review.txt','guidance_source':'analysis'})
        app.processEvents()
        assert d.done_badge.isVisible() and not d.progress.isVisible()
        assert '정비 완료' in d.done_badge.text() and '총 소요 시간 1분 05초' in d.status.text()
        # A stopped run never shows the done mark.
        d._show_done(False); d._finished({'status':'stopped','seconds':5}); app.processEvents()
        assert not d.done_badge.isVisible() and d.progress.isVisible()
        # Choosing another TXT brings the bar back.
        d._show_done(True); f=tmp_path/'next.txt'; f.write_text('다음 책.',encoding='utf-8'); d._set_source(f); app.processEvents()
        assert d.progress.isVisible() and not d.done_badge.isVisible() and d.progress.value()==0
    finally:
        d.close()


def test_job_clock_carries_the_main_run_into_an_automatic_retry():
    from novel_translator.ui.main_window import MainWindow
    import time

    class Clock:
        _job_t0=None; _last_job_seconds=120.0; _job_continue=True
        class _clock:
            @staticmethod
            def start(): pass
        class time_label:
            @staticmethod
            def setText(t): pass
    c=Clock()
    MainWindow._job_clock_start(c)
    assert MainWindow._job_elapsed(c) >= 120.0          # continues from the main run
    c2=Clock(); c2._job_continue=False
    MainWindow._job_clock_start(c2)
    assert MainWindow._job_elapsed(c2) < 5.0            # a new job starts from zero
