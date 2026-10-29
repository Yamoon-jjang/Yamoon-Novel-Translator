"""v2.6 — one window at a time, also for a renamed exe or a copied program folder."""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from novel_translator import single_instance  # noqa: E402

HOLDER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, {root!r})
    from novel_translator import single_instance as s
    print('GOT' if s.acquire(sys.argv[1]) else 'BUSY', flush=True)
    time.sleep(float(sys.argv[2]))
''')


def _start(tmp_path, data_dir, hold, name='holder.py'):
    # A different script name/folder stands in for a renamed exe or a copied program folder.
    script = tmp_path / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(HOLDER.format(root=str(ROOT)), encoding='utf-8')
    return subprocess.Popen([sys.executable, str(script), str(data_dir), str(hold)],
                            stdout=subprocess.PIPE, text=True)


def test_key_depends_on_data_folder_only(tmp_path):
    a = single_instance.instance_key(tmp_path / 'NovelTranslator')
    assert a == single_instance.instance_key(str(tmp_path / 'NovelTranslator'))
    assert a == single_instance.instance_key(tmp_path / 'x' / '..' / 'NovelTranslator')
    assert a != single_instance.instance_key(tmp_path / 'OtherUser')


def test_second_start_is_refused_even_from_a_copy(tmp_path):
    data = tmp_path / 'data'; data.mkdir()
    first = _start(tmp_path, data, 4)
    assert first.stdout.readline().strip() == 'GOT'
    try:
        same = _start(tmp_path, data, 0, 'same.py')
        copied = _start(tmp_path, data, 0, 'copied folder/Renamed Translator.py')
        assert same.communicate(timeout=20)[0].strip() == 'BUSY'
        assert copied.communicate(timeout=20)[0].strip() == 'BUSY'
        # Another account (another data folder) is not affected.
        other = tmp_path / 'other'; other.mkdir()
        alone = _start(tmp_path, other, 0, 'other.py')
        assert alone.communicate(timeout=20)[0].strip() == 'GOT'
    finally:
        first.kill(); first.wait()


def test_lock_is_free_again_after_the_program_ends_even_killed(tmp_path):
    data = tmp_path / 'data'; data.mkdir()
    first = _start(tmp_path, data, 30)
    assert first.stdout.readline().strip() == 'GOT'
    first.kill(); first.wait()            # like a crash: no clean-up code runs
    again = _start(tmp_path, data, 0, 'again.py')
    assert again.communicate(timeout=20)[0].strip() == 'GOT'


def test_a_failing_check_never_blocks_start(tmp_path, monkeypatch):
    monkeypatch.setattr(single_instance, '_held', None)
    monkeypatch.setattr(single_instance, '_acquire_posix', lambda *a: 1 / 0)
    monkeypatch.setattr(single_instance, '_acquire_windows', lambda *a: 1 / 0)
    assert single_instance.acquire(tmp_path) is True


def test_window_note_and_bring_forward_are_harmless(tmp_path):
    single_instance.remember_window(tmp_path, 12345)
    assert '12345' in (tmp_path / single_instance.WINDOW_FILE).read_text(encoding='utf-8')
    # Not Windows / no such window: simply reports False, never raises.
    assert single_instance.bring_running_window_forward(tmp_path / 'missing') is False


def test_main_opens_no_second_window(monkeypatch):
    """app.main(): when another window holds the lock, no MainWindow is built."""
    import novel_translator.app as app_mod
    src = Path(app_mod.__file__).read_text(encoding='utf-8')
    body = src[src.index('def main():'):]
    assert body.index('single_instance.acquire(DATA_DIR)') < body.index('w=MainWindow()')
    assert 'return 0' in body[body.index('single_instance.acquire'):body.index('w=MainWindow()')]
    # Self-tests (build gate) never go through main(), so they are not affected.
    assert 'single_instance' not in src[:src.index('def main():')]
