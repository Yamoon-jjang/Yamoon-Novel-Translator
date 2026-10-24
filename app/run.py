import sys

_SELF_TEST_FLAGS = ('--self-test', '--gui-self-test', '--engine-self-test', '--secret-self-test')
_SELF_TEST = any(flag in sys.argv for flag in _SELF_TEST_FLAGS)


def _utf8_streams() -> None:
    """Self-test reports contain Korean text (e.g. the window title). A Windows
    console/CI pipe may use a legacy code page (cp1252) that cannot encode it, which
    used to raise UnicodeEncodeError and then open a modal error box nobody could
    close, hanging the build. Always write UTF-8 and never fail on a character."""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def _emit(text: str) -> None:
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(text)
    except Exception:
        try:
            data = (str(text) + '\n').encode('utf-8', errors='replace')
            buf = getattr(stream, 'buffer', None)
            if buf is not None:
                buf.write(data); buf.flush()
            else:
                print(data.decode('ascii', errors='replace'))
        except Exception:
            pass


def _native_error(message: str) -> None:
    # Never show a modal box in self-test/CI mode: nobody can click it and the
    # build would hang until its time limit. Report on stderr instead.
    if not _SELF_TEST:
        try:
            if sys.platform == 'win32':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, message, '야문 Novel Translator 시작 오류', 0x10)
                return
        except Exception:
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


if __name__ == '__main__':
    # First of all: a helper process (long PDFs are read by several processes) runs its
    # task here and exits; it never starts the window or the start-up checks.
    import multiprocessing
    multiprocessing.freeze_support()
    _utf8_streams()
    if not _SELF_TEST:
        try:
            # Before anything else is loaded: clear the "downloaded from the web" mark from the
            # program's own files, so Smart App Control lets shortcuts start it (web_mark.py).
            from novel_translator.web_mark import clear_own_marks_at_start
            clear_own_marks_at_start()
        except Exception:
            pass
    try:
        from novel_translator.runtime_audit import install_exception_hook, self_test_report, write_crash_log
        install_exception_hook()
        if '--self-test' in sys.argv:
            ok, report = self_test_report()
            _emit(report)
            raise SystemExit(0 if ok else 2)
        if '--gui-self-test' in sys.argv:
            from novel_translator.app import gui_self_test
            ok, report = gui_self_test()
            _emit(report)
            raise SystemExit(0 if ok else 3)
        if '--engine-self-test' in sys.argv:
            from novel_translator.runtime_audit import engine_self_test
            ok, report = engine_self_test()
            _emit(report)
            raise SystemExit(0 if ok else 4)
        if '--secret-self-test' in sys.argv:
            from novel_translator.runtime_audit import secret_store_self_test
            ok, report = secret_store_self_test()
            _emit(report)
            raise SystemExit(0 if ok else 5)
        ok, report = self_test_report()
        if not ok:
            _native_error('시작 전 자체 점검에 실패했습니다.\n\n' + report)
            raise SystemExit(2)
        from novel_translator.app import main
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            path = write_crash_log(type(e), e, e.__traceback__)
        except Exception:
            path = None
        extra = f'\n\n오류 기록: {path}' if path else ''
        _native_error(f'야문 Novel Translator를 시작하지 못했습니다.\n{type(e).__name__}: {e}{extra}')
        raise SystemExit(1)
