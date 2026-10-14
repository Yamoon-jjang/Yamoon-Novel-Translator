import json
import shutil
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pytest
from PySide6.QtWidgets import QApplication,QScrollArea,QTableWidgetItem
from PySide6.QtCore import QPoint
from novel_translator import translation_settings as settings
from novel_translator.ui import main_window as ui,advanced_dialog as advanced
from novel_translator.prompting import build_system_prompt
from novel_translator.profiles import ProfileManager

@pytest.fixture(scope='module')
def qt_app():
    return QApplication.instance() or QApplication([])

@pytest.fixture
def sandbox(tmp_path):
    cfg=tmp_path/'config';shutil.copytree(ui.CONFIG_DIR,cfg)
    app_cfg=json.loads((cfg/'app.json').read_text(encoding='utf-8')); app_cfg['auto_extract_terms']=False   # no network in UI tests
    (cfg/'app.json').write_text(json.dumps(app_cfg,ensure_ascii=False),encoding='utf-8')
    with patch.object(ui,'CONFIG_DIR',cfg),patch.object(settings,'CONFIG_DIR',cfg),patch.object(advanced,'CONFIG_DIR',cfg),patch.object(ui,'DATA_DIR',tmp_path/'data'):
        yield tmp_path,cfg


def test_presets_have_distinct_document_and_novel_settings(sandbox):
    original=settings.default_settings();before=deepcopy(original)
    document=settings.apply_content_preset(original,'document');novel=settings.apply_content_preset(document,'novel')
    assert original==before
    assert document['modes']['balanced']['chunk_factor']==1.5
    assert document['modes']['balanced']['context_chars']==1000
    assert novel['modes']['balanced']['chunk_factor']==1
    assert novel['modes']['balanced']['context_chars']==3000
    assert novel['rate_limiter']['start_rpm']==document['rate_limiter']['start_rpm']==10
    for cfg in [document,novel]:
        assert all(x['file_concurrency']==x['request_concurrency']==1 for x in cfg['modes'].values())


def test_document_prompt_excludes_fiction_style():
    p=ProfileManager().active
    novel=build_system_prompt(p,'novel');document=build_system_prompt(p,'document')
    assert 'professional document translator' in document
    assert 'every fact, number, date' in document
    assert 'literary translator' in novel
    assert p.system_prompt not in document
    assert '<P######>' in document and 'NTLIT0001' in document


def test_advanced_switch_populates_defaults_and_save_cancel(qt_app,sandbox):
    _,cfg=sandbox;path=cfg/'app.json';before=path.read_bytes()
    dlg=advanced.AdvancedSettingsDialog();dlg.show();qt_app.processEvents()
    dlg.content_kind.setCurrentIndex(dlg.content_kind.findData('document'))
    assert dlg.mode_controls['balanced'][0].value()==1.5
    assert dlg.mode_controls['balanced'][3].value()==1000
    dlg.content_kind.setCurrentIndex(dlg.content_kind.findData('novel'))
    assert dlg.mode_controls['balanced'][0].value()==1
    dlg.reject();assert path.read_bytes()==before
    dlg=advanced.AdvancedSettingsDialog();dlg.content_kind.setCurrentIndex(1);dlg._save()
    assert json.loads(path.read_text())['content_kind']=='document'
    reloaded=advanced.AdvancedSettingsDialog()
    assert reloaded.content_kind.currentData()=='document'
    assert reloaded.mode_controls['balanced'][0].value()==1.5
    reloaded.close()


def test_fixed_menu_fold_preserves_all_values_and_tooltip(qt_app,sandbox):
    root,cfg=sandbox;w=ui.MainWindow();w.show()
    try:
        w.model.setCurrentText('custom-fixture-model');w.mode.setCurrentIndex(1);w.encoding.setCurrentIndex(1);w.lang_option.setCurrentIndex(1)
        w.book_memory.setPlainText('관계 메모');w._add_term_row();w.term_table.setItem(0,0,QTableWidgetItem('Name'));w.term_table.setItem(0,1,QTableWidgetItem('이름'))
        def state():return (w._selected_model_id(),w.mode.currentData(),w.encoding.currentData(),w._language_options(),w.book_memory.toPlainText(),w._terms())
        before=state();config_bytes=(cfg/'app.json').read_bytes()
        for size in [(1120,780),(960,700)]:
            w.resize(*size);qt_app.processEvents()
            assert not w.settings_area.findChildren(QScrollArea)
            for control in [w.single_btn,w.folder_btn,w.lang,w.model,w.mode,w.encoding]:
                bottom=control.mapTo(w.settings_area,QPoint(0,control.height())).y()
                assert 0<bottom<=w.settings_area.height()
            for _ in range(3):
                w.settings_toggle.setChecked(False);qt_app.processEvents();assert state()==before
                w.settings_toggle.setChecked(True);qt_app.processEvents();assert state()==before
        assert (cfg/'app.json').read_bytes()==config_bytes
        tooltip=w.ready_label.toolTip();assert '<br>' in tooltip and '<b>준비 상태</b>' in tooltip
        assert 'QPushButton:checked' in w.settings_toggle.styleSheet()
    finally:w.close()


def test_reset_keeps_keys_source_and_translation_files(qt_app,sandbox):
    root,cfg=sandbox;w=ui.MainWindow()
    try:
        w.secrets={'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'openai':{'enabled':False,'model':'gpt-6.1-sol','keys':[]}}
        w.secret_store.save(w.secrets);before=w.secret_store.path.read_bytes();secret_state=deepcopy(w.secrets)
        source=root/'book.txt';raw='他站在窗边看着外面的雨。'.encode();source.write_bytes(raw)
        out=root/'book.ko.txt';out.write_text('보존할 번역')
        w.source_path=source;w.source_bytes=raw;w.source_text=raw.decode();w.input_mode='single'
        w.model.setCurrentText('custom-fixture');w.book_memory.setPlainText('이전 메모');w.custom_output_dir=root/'custom'
        w._reset_settings()
        assert w.secrets==secret_state and w.secret_store.path.read_bytes()==before
        assert source.read_bytes()==raw and out.read_text()=='보존할 번역'
        assert w.book_memory.toPlainText()=='' and w._terms()=={}
        assert w.encoding.currentData()=='auto' and w.mode.currentData()=='balanced'
        assert w._selected_model_id()=='gemini-3.1-flash-lite' and w.content_kind=='novel'
        assert w.custom_output_dir==root/'custom'   # app-wide output folder: not part of one book's settings
        w._apply_profile('en-ko');w._apply_profile('zh-ko')
        assert w._terms()=={} and w.book_memory.toPlainText()==''
    finally:w.close()


def test_pending_report_never_erases_or_scrolls_live_errors(qt_app,sandbox):
    root,_=sandbox;w=ui.MainWindow()
    try:
        w.source_path=root/'book.txt';w.on_worker_issue('POLICY · 입력 차단 원인')
        report=root/'book.ko.failed.txt';report.write_text('청크 7\tPENDING\n'*1000,encoding='utf-8-sig')
        w.on_result_ready('stopped',str(root/'book.ko.partial.txt'),1000)
        assert '입력 차단 원인' in w.error_view.toPlainText()
        assert 'PENDING' not in w.error_view.toPlainText()
        assert 'PENDING' in w.pending_view.toPlainText()
    finally:w.close()


def test_reset_openai_options_preserves_both_key_lists(qt_app,sandbox):
    _,_=sandbox;w=ui.MainWindow()
    try:
        w.secrets={'gemini':{'keys':[{'id':'g','secret':'dummy-g','enabled':True}]},'openai':{'enabled':True,'model':'custom-openai','keys':[{'id':'o','secret':'dummy-o','enabled':True}]}}
        expected=deepcopy(w.secrets)
        w._reset_settings()
        assert w.secrets['gemini']['keys']==expected['gemini']['keys']
        assert w.secrets['openai']['keys']==expected['openai']['keys']
        assert w.secrets==expected                     # the API setup (engines, models, keys) is kept as it is
    finally:w.close()


def test_selected_lite_reaches_worker_without_folding_settings(qt_app,sandbox):
    root,_=sandbox;w=ui.MainWindow()
    try:
        source=root/'fixture.txt';source.write_text('これはテストです。',encoding='utf-8')
        w.input_mode='single';w.source_path=source;w.source_bytes=source.read_bytes();w.source_text=source.read_text();w._rechunk()
        w.secrets={'gemini':{'keys':[{'id':'g','secret':'dummy','enabled':True}]}}
        w.model.setCurrentIndex(w.model.findData('gemini-3.1-flash-lite'))
        w._save_workspace();w.model.blockSignals(True);w.model.setCurrentIndex(0);w.model.blockSignals(False);w._load_workspace()
        assert w.model.currentData()=='gemini-3.1-flash-lite'
        for expanded in [True,False]:
            w.settings_toggle.setChecked(expanded)
            with patch.object(w,'_language_mismatch_warning',return_value=True),patch.object(w,'_preflight_output',return_value=True),patch.object(w,'_start_one_worker') as start:
                w.start_translation()
                assert start.call_args.args[-1]=='gemini-3.1-flash-lite'
                assert w.settings_toggle.isChecked()==expanded
                assert 'gemini-3.1-flash-lite' in w.api_runtime_label.text()
            w._set_running_ui(False)
        for p in w.pm.all():
            w.pm.set_active(p.id);w._refresh_content_ui()
            assert '내장 규칙' in w.lang_info.toolTip() and '내장 규칙' not in w.lang_info.text()
        assert w.term_table.rowCount()==0
    finally:w.close()


def test_provider_events_include_actual_model():
    from novel_translator.ui.worker import TranslationWorker
    w=TranslationWorker(Path('fixture.txt'),b'',ProfileManager().active,[],'gemini-3.1-flash-lite',[],{},'balanced')
    messages=[];w.issue.connect(messages.append)
    w._provider_event('Gemini','FORMAT · retry')
    assert 'gemini-3.1-flash-lite' in messages[-1]
    w.openai_settings={'model':'fixture-openai'};w._provider_event('OpenAI','FORMAT · retry')
    assert 'fixture-openai' in messages[-1] and 'flash-lite' not in messages[-1]


def test_folder_queue_runs_to_the_end_unattended(qt_app,sandbox,monkeypatch):
    """Quota pause -> automatic resume; settings error -> skip; queue always finishes."""
    import time
    from PySide6.QtCore import QThread, Signal
    root,cfg=sandbox
    app_cfg=json.loads((cfg/'app.json').read_text(encoding='utf-8'))
    app_cfg['unattended'].update(quota_wait_seconds=0,rate_limit_wait_seconds=0,network_wait_seconds=0)
    (cfg/'app.json').write_text(json.dumps(app_cfg,ensure_ascii=False),encoding='utf-8')
    books=root/'books'; books.mkdir()
    for i,name in enumerate(['1.txt','2.txt','3.txt','10.txt']):
        (books/name).write_text(f'第{i}章\n\n他静静地看着窗外的雨，很久没有说话。她也沉默着。',encoding='utf-8')
    script={'1.txt':['completed'],'2.txt':['QUOTA','completed'],'3.txt':['FATAL'],'10.txt':['completed']}
    started=[]

    class FakeWorker(QThread):
        progress=Signal(int,int,str); failed=Signal(str); paused=Signal(str,bool); finished_ok=Signal(str)
        result_ready=Signal(str,str,int); key_disabled=Signal(str,str,str); issue=Signal(str); api_state=Signal(str)
        def __init__(self,source_path,*a,**k):
            super().__init__(k.get('parent')); self.source_path=source_path; self.final_context='맥락'; self.pause_kind=''
            started.append(source_path.name)
        def stop(self): pass
        def run(self):
            outcome=script[self.source_path.name].pop(0)
            out=str(self.source_path.with_suffix('.ko.txt'))
            if outcome=='completed':
                self.result_ready.emit('completed',out,0); self.finished_ok.emit(out)
            else:
                self.pause_kind=outcome
                self.result_ready.emit('paused',out,1); self.paused.emit(f'{outcome} 테스트 일시정지',False)

    monkeypatch.setattr(ui,'TranslationWorker',FakeWorker)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    monkeypatch.setattr(ui.QMessageBox,'warning',lambda *a,**k:(_ for _ in ()).throw(AssertionError('unattended run opened a blocking warning')))
    w=ui.MainWindow()
    try:
        w.secrets={'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'openai':{'enabled':False,'model':'','keys':[]}}
        w._prepare_new_input(); w.input_mode='folder'; w.batch_root=books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
        assert [p.name for p in w.batch_files]==['1.txt','2.txt','3.txt','10.txt']
        w.start_translation()
        deadline=time.time()+20
        while time.time()<deadline and not (w.last_result_path and w.last_result_path.name=='번역_요약.txt'):
            qt_app.processEvents(); time.sleep(0.01)
        assert w.last_result_path and w.last_result_path.name=='번역_요약.txt', w.error_view.toPlainText()
        assert started==['1.txt','2.txt','2.txt','3.txt','10.txt'], started
        status={Path(k).name:v[0] for k,v in w.batch_results.items()}
        assert status['1.txt']=='완료' and status['2.txt']=='완료' and status['10.txt']=='완료'
        assert status['3.txt'].startswith('오류 · 건너뜀')
        log=w.error_view.toPlainText()
        assert '자동 재개' in log and '건너뛰고 다음 파일' in log
        assert w.start.text()=='번역 시작 / 계속'
    finally:
        w.close()


def test_source_window_paste_and_append_are_translated(qt_app,sandbox,monkeypatch,tmp_path):
    """원문 창: pasted text without a file, and text appended to an opened file, are both translated."""
    import time
    from PySide6.QtCore import QThread, Signal
    root,cfg=sandbox
    seen=[]

    class FakeWorker(QThread):
        progress=Signal(int,int,str); failed=Signal(str); paused=Signal(str,bool); finished_ok=Signal(str)
        result_ready=Signal(str,str,int); key_disabled=Signal(str,str,str); issue=Signal(str); api_state=Signal(str)
        def __init__(self,source_path,raw,profile,chunks,model,keys,terms,mode,out_dir,*a,**k):
            super().__init__(k.get('parent')); self.source_path=source_path; self.out_dir=Path(out_dir)
            self.final_context=''; self.pause_kind=''
            seen.append({'path':source_path,'text':raw.decode('utf-8'),'terms':dict(terms)})
        def stop(self): pass
        def run(self):
            self.out_dir.mkdir(parents=True,exist_ok=True)
            out=self.out_dir/(self.source_path.stem+'.ko.txt'); out.write_text('번역: '+self.source_path.stem,encoding='utf-8')
            self.result_ready.emit('completed',str(out),0); self.finished_ok.emit(str(out))

    monkeypatch.setattr(ui,'TranslationWorker',FakeWorker)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    monkeypatch.setattr(ui.QMessageBox,'warning',lambda *a,**k:(_ for _ in ()).throw(AssertionError('unexpected warning')))
    w=ui.MainWindow()
    def wait_done(n):
        deadline=time.time()+10
        while time.time()<deadline and not (len(seen)>=n and not (w.worker and w.worker.isRunning()) and w._result_text):
            qt_app.processEvents(); time.sleep(0.01)
    try:
        w.secrets={'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'openai':{'enabled':False,'model':'','keys':[]}}
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        assert not w.start.isEnabled()
        # 1) No file: paste a sentence into the 원문 window.
        w.preview.setPlainText('他静静地看着窗外的雨。')
        assert w._preview_edited() and w.start.isEnabled()
        w.start_translation(); wait_done(1)
        assert len(seen)==1 and seen[0]['text']=='他静静地看着窗外的雨。'
        assert seen[0]['path'].parent==root/'data'/'paste' and seen[0]['path'].name.startswith('붙여넣기_')
        assert w._preview_mode=='result' and '번역:' in w.preview.toPlainText() and w.preview.isReadOnly()
        assert w.result_copy_btn.isEnabled() and w.result_save_btn.isEnabled()
        w._copy_result(); assert QApplication.clipboard().text().startswith('번역:')
        w._show_preview('source'); assert w.preview.toPlainText()=='他静静地看着窗外的雨。' and not w.preview.isReadOnly()

        # 2) Open a file, then append pasted text to it in the 원문 window.
        book=tmp_path/'book.txt'; book.write_text('第一章\n\n她也沉默着。',encoding='utf-8')
        monkeypatch.setattr(ui.QFileDialog,'getOpenFileName',lambda *a,**k:(str(book),''))
        w.open_txt()
        assert w.preview.toPlainText().startswith('第一章') and not w._preview_edited() and not w._paste_active
        w.term_table.setRowCount(1); w.term_table.setItem(0,0,QTableWidgetItem('林雨')); w.term_table.setItem(0,1,QTableWidgetItem('린위'))
        w.preview.moveCursor(w.preview.textCursor().MoveOperation.End); w.preview.insertPlainText('\n\n林雨推开了门。')
        assert w._preview_edited() and '수정본' in w.file_label.text()
        w.start_translation(); wait_done(2)
        assert len(seen)==2, seen
        job=seen[1]
        assert job['path'].name.startswith('book_수정_') and job['text'].endswith('林雨推开了门。') and job['text'].startswith('第一章')
        assert job['terms'].get('林雨')=='린위'
        assert book.read_text(encoding='utf-8')=='第一章\n\n她也沉默着。'   # the user's original file is untouched
        assert w._preview_mode=='result' and '번역:' in w.preview.toPlainText()
    finally:
        w.close()


def test_pasted_text_passes_restart_and_term_extraction_checks(qt_app,sandbox,monkeypatch):
    """With no file open, text in the 원문 window must not trigger 'select a file first'."""
    started=[]; infos=[]
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:infos.append(a[2] if len(a)>2 else ''))
    monkeypatch.setattr(ui.QMessageBox,'question',lambda *a,**k:ui.QMessageBox.Yes)
    w=ui.MainWindow()
    try:
        w.secrets={'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'openai':{'enabled':False,'model':'','keys':[]}}
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        assert w.input_mode=='none'
        # Term extraction reads the pasted text (no 'select a TXT' message).
        w.preview.setPlainText('林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。')
        seen={}
        monkeypatch.setattr(ui,'term_candidates',lambda text,lang:(seen.setdefault('text',text),[])[1])
        w._extract_terms()
        assert seen['text'].startswith('林雨说') and not any('선택' in m for m in infos), infos
        # 처음부터 다시 uses the pasted text as the source.
        monkeypatch.setattr(w,'start_translation',lambda force=False:started.append((force,w.source_path)))
        w._restart_translation()
        assert not any('선택' in m for m in infos), infos
        assert started and started[0][0] is True and started[0][1].name.startswith('붙여넣기_') and w.input_mode=='single'
    finally:
        w.close()


def test_view_toggle_buttons_use_neutral_checked_style(qt_app,sandbox):
    w=ui.MainWindow()
    try:
        css=w.view_source_btn.styleSheet()
        assert 'QPushButton:checked' in css and '#e2e8f0' in css and '#0f172a' in css
        assert w.view_result_btn.styleSheet()==css
    finally:
        w.close()


def test_pause_button_pauses_and_resumes_the_running_worker(qt_app,sandbox,monkeypatch):
    import time
    from PySide6.QtCore import QThread, Signal
    state={'paused':False,'release':False}

    class FakeWorker(QThread):
        progress=Signal(int,int,str); failed=Signal(str); paused=Signal(str,bool); finished_ok=Signal(str)
        result_ready=Signal(str,str,int); key_disabled=Signal(str,str,str); issue=Signal(str); api_state=Signal(str)
        def __init__(self,source_path,raw,profile,chunks,model,keys,terms,mode,out_dir,*a,**k):
            super().__init__(k.get('parent')); self.source_path=source_path; self.out_dir=Path(out_dir); self.final_context=''; self.pause_kind=''
        def pause(self): state['paused']=True
        def resume(self): state['paused']=False
        def is_paused(self): return state['paused']
        def stop(self): state['release']=True
        def run(self):
            while not state['release']: time.sleep(0.01)
            self.out_dir.mkdir(parents=True,exist_ok=True); out=self.out_dir/'x.ko.txt'; out.write_text('번역',encoding='utf-8')
            self.result_ready.emit('completed',str(out),0); self.finished_ok.emit(str(out))

    monkeypatch.setattr(ui,'TranslationWorker',FakeWorker)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    w=ui.MainWindow()
    try:
        w.secrets={'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'openai':{'enabled':False,'model':'','keys':[]}}
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        assert not w.pause_btn.isEnabled()
        w.preview.setPlainText('他静静地看着窗外的雨。'); w.start_translation()
        assert w.pause_btn.isEnabled() and w.pause_btn.text()=='일시정지'
        w.pause_btn.click(); assert state['paused'] and w.pause_btn.text()=='다시 진행'
        w.pause_btn.click(); assert not state['paused'] and w.pause_btn.text()=='일시정지'
        w.pause_btn.click(); assert state['paused']
        state['release']=True
        deadline=time.time()+10
        while time.time()<deadline and (w.worker and w.worker.isRunning() or w.pause_btn.isEnabled()):
            qt_app.processEvents(); time.sleep(0.01)
        assert not w.pause_btn.isEnabled() and w.pause_btn.text()=='일시정지' and not w._user_paused
    finally:
        w.close()
