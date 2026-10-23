"""v2.41 보완: a folder run retries each file's untranslated parts automatically, the same
way a single file does (up to AUTO_RETRY_ROUNDS, only while they keep shrinking), and
reports only what is still left at the very end (one list per file, kept for the run)."""
import novel_translator.ui.main_window as ui

from test_alpha17 import _window, _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)


def _per_file_worker(started, scripts):
    """scripts: {file name: [(status, failed chunks, leftover paragraphs), ...]} taken in turn."""
    Base=_fake_worker_class(started)
    class Scripted(Base):
        def run(self):
            name=self.source_path.name
            n=sum(1 for s in started if s.source_path.name==name)-1
            seq=scripts[name]; status,failed,leftover=seq[min(n,len(seq)-1)]
            self.out_dir.mkdir(parents=True,exist_ok=True)
            out=self.out_dir/(self.source_path.stem+('.ko.txt' if status=='completed' else '.ko.partial.txt'))
            out.write_text('번역',encoding='utf-8'); self.leftover_count=leftover
            self.pending_report=('' if status=='completed' else
                '야문 Novel Translator 미완료 구간\n※ 안내\n\n'+''.join(f'청크 {i+3}\tPARTIAL\tgemini\tm\tREFUSAL\t일부 문단 원문 유지 (1개: P00001{i})\n' for i in range(failed)))
            self.result_ready.emit(status,str(out),failed); self.finished_ok.emit(str(out))
    return Scripted


def _folder(root, names=('1.txt','2.txt')):
    books=root/'books'; books.mkdir()
    for n in names: (books/n).write_text('第一章\n\n他静静地看着窗外的雨。',encoding='utf-8')
    return books


def _run_folder(qt_app, monkeypatch, books, started, scripts, force=False):
    monkeypatch.setattr(ui,'TranslationWorker',_per_file_worker(started,scripts))
    w=_window(monkeypatch); infos=[]
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k: infos.append((a[1],a[2])))
    w._prepare_new_input(); w.input_mode='folder'; w.batch_root=books
    w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
    w.start_translation(force_restart=force) if force else w.start_translation()
    assert _wait(qt_app,lambda: infos and w.last_result_path is not None and w.last_result_path.name=='번역_요약.txt'), infos
    for _ in range(20): qt_app.processEvents()
    return w, infos


def test_folder_file_is_retried_until_finished(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=_folder(root); started=[]
    w,infos=_run_folder(qt_app,monkeypatch,books,started,
        {'1.txt':[('partial',3,0),('partial',2,0),('completed',0,0)],'2.txt':[('completed',0,0)]})
    try:
        assert [s.source_path.name for s in started]==['1.txt','1.txt','1.txt','2.txt']
        log=w.error_view.toPlainText()
        assert '(1/2회)' in log and '(2/2회)' in log
        assert infos[-1][0]=='폴더 번역 완료'                                  # nothing left: the plain report
        summary=w.last_result_path.read_text(encoding='utf-8')
        assert '완료 2개 · 부분 완료 0개' in summary and '남은 미번역 상세' not in summary
        assert w.pending_view.toPlainText()==''                             # the finished file's list is gone
    finally: w.close()


def test_folder_reports_only_what_is_left_after_the_retries(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=_folder(root,('1.txt','2.txt','3.txt')); started=[]
    w,infos=_run_folder(qt_app,monkeypatch,books,started,{
        '1.txt':[('partial',5,0),('partial',3,0),('partial',1,0)],          # capped at 2 retries
        '2.txt':[('completed',0,0)],
        '3.txt':[('partial',2,0),('partial',2,0)]})                          # no progress: stop after 1
    try:
        assert [s.source_path.name for s in started]==['1.txt']*3+['2.txt']+['3.txt']*2
        title,text=infos[-1]
        assert title=='폴더 번역 완료 · 일부 남음'
        assert '1.txt' in text and '3.txt' in text and '2.txt' not in text.split('남은 파일:')[1].split('\n\n')[0]
        summary=w.last_result_path.read_text(encoding='utf-8')
        assert '완료 1개 · 부분 완료 2개' in summary
        assert '자동 재시도 2회 뒤' in summary and '자동 재시도로도 줄지 않음' in summary
        assert '── 1.txt ──' in summary and '── 3.txt ──' in summary and 'P000010' in summary
        pending=w.pending_view.toPlainText()                                 # every file's list, not just the last
        assert '── 1.txt ──' in pending and '── 3.txt ──' in pending and '2.txt' not in pending
        assert '폴더 번역 끝 · 미번역 남음 · 자동 재시도 뒤에도 남은 파일 2개' in w.error_view.toPlainText()
    finally: w.close()


def test_folder_leftover_script_paragraphs_are_retried_too(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=_folder(root,('1.txt',)); started=[]
    w,infos=_run_folder(qt_app,monkeypatch,books,started,{'1.txt':[('completed',0,3),('completed',0,0)]})
    try:
        assert len(started)==2 and infos[-1][0]=='폴더 번역 완료'
    finally: w.close()


def test_a_retry_never_restarts_the_file_from_scratch(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=_folder(root); started=[]
    monkeypatch.setattr(ui.MainWindow,'_ask_extract_on_resume',lambda self: 'skip',raising=False)
    w,infos=_run_folder(qt_app,monkeypatch,books,started,
        {'1.txt':[('partial',1,0),('completed',0,0)],'2.txt':[('completed',0,0)]},force=True)
    try:
        assert [(s.source_path.name,s.restart_existing) for s in started]==[('1.txt',True),('1.txt',False),('2.txt',True)]
    finally: w.close()


def test_single_file_rule_is_unchanged(qt_app,sandbox,monkeypatch):
    assert ui.MainWindow.AUTO_RETRY_ROUNDS==2
