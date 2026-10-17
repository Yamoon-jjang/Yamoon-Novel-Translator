from __future__ import annotations
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from .runtime_audit import validate_resources

def _pdf_round_trip() -> tuple[bool, str]:
    """Packaged PDF/EPUB parts: write a reading PDF (Qt, Korean font) and EPUB, then read both back."""
    import tempfile, re
    try:
        from .pdf_export import write_reading_pdf
        from .pdf_text import extract_pdf_text
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / '자체점검.ko.pdf'
            body = '\n\n'.join(f'야문번역점검문단{i}입니다.' for i in range(12))
            pages = write_reading_pdf(out, '제1장 시작\n\n' + body + '\n\n제2장 끝\n\n' + body, '자체 점검')
            text = re.sub(r'\s+', '', extract_pdf_text(out).text)
            if pages < 2 or '야문번역점검문단11입니다.' not in text or '제2장' not in text:
                return False, f'PDF round trip text mismatch (pages={pages})'
            from .epub_export import write_reading_epub
            from .epub_text import extract_epub_text
            epub = Path(d) / '자체점검.ko.epub'
            sections = write_reading_epub(epub, '제1장 시작\n\n' + body + '\n\n제2장 끝\n\n' + body, '자체 점검')
            etext = extract_epub_text(epub).text
            if sections != 2 or '야문번역점검문단11입니다.' not in etext or '제2장 끝' not in etext:
                return False, f'EPUB round trip text mismatch (sections={sections})'
        return True, f'PDF=PASS pages={pages} · EPUB=PASS sections={sections}'
    except Exception as e:
        return False, f'PDF/EPUB round trip failed: {type(e).__name__}: {e}'


def gui_self_test() -> tuple[bool, str]:
    """Instantiate the real Qt application and main window without entering the event loop.

    Used by the Windows portable build gate to catch missing Qt plugins/DLLs and
    import-time/UI-construction regressions that a non-GUI self-test cannot see.
    """
    try:
        from .theme import prepare_environment, force_light
        if QApplication.instance() is None: prepare_environment()
        app = QApplication.instance() or QApplication([])
        force_light(app)
        app.setApplicationName('Novel Translator')
        app.setOrganizationName('NovelTranslator')
        issues = validate_resources()
        if issues:
            return False, 'GUI_SELF_TEST=FAIL\n' + '\n'.join('- '+x for x in issues)
        from .ui.main_window import MainWindow
        from .ui.api_dialog import ApiDialog
        from .ui.advanced_dialog import AdvancedSettingsDialog
        w = MainWindow()
        w.ensurePolished()
        if '상태 / 오류 기록' not in [w.tabs.tabText(i) for i in range(w.tabs.count())]:
            w.close()
            return False, 'GUI_SELF_TEST=FAIL\nstatus/error tab missing'
        w.on_worker_issue('self-test · API 재시도 기록')
        w.source_path = Path('selftest.txt')
        w.on_result_ready('completed', 'selftest.ko.txt', 0)
        if 'API 재시도 기록' not in w.error_view.toPlainText():
            w.close()
            return False, 'GUI_SELF_TEST=FAIL\ncompleted job erased diagnostics'
        # Construct the real multi-provider API settings UI too. No network call is
        # made; this catches Qt/widget/signature regressions in the packaged EXE.
        d = ApiDialog([], 'gemini-3.1-flash-lite', {'enabled':False,'model':'gpt-6.1-sol','keys':[]}, 'https://api.openai.com/v1/responses', w)
        d.ensurePolished(); d.close()
        adv=AdvancedSettingsDialog(w); adv.ensurePolished(); adv.close()
        app.processEvents()
        ok_pdf, pdf_note = _pdf_round_trip()
        if not ok_pdf:
            w.close()
            return False, 'GUI_SELF_TEST=FAIL\n' + pdf_note
        title = w.windowTitle()
        w.close()
        app.processEvents()
        return True, f'GUI_SELF_TEST=PASS\nwindow={title}\n{pdf_note}'
    except Exception as e:
        return False, f'GUI_SELF_TEST=FAIL\n{type(e).__name__}: {e}'

def main():
    from .theme import prepare_environment, force_light
    prepare_environment()
    app=QApplication(sys.argv)
    force_light(app)   # the UI is designed for light colours; never follow Windows dark mode
    app.setApplicationName('Novel Translator')
    app.setOrganizationName('NovelTranslator')
    issues=validate_resources()
    if issues:
        QMessageBox.critical(None,'시작 점검 실패','필수 파일 또는 저장 환경에 문제가 있습니다.\n\n'+'\n'.join(issues))
        return 2
    from .ui.main_window import MainWindow
    w=MainWindow(); w.show()
    return app.exec()
