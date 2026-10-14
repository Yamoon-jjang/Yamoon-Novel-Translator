import tempfile, unittest
from pathlib import Path
from novel_translator.batch import discover_txt_files, output_dir_for, is_generated_txt

class BatchTests(unittest.TestCase):
    def test_discovers_txt_sequential_order_and_excludes_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'b.txt').write_text('b',encoding='utf-8')
            (root/'a.txt').write_text('a',encoding='utf-8')
            (root/'a.ko.txt').write_text('out',encoding='utf-8')
            (root/'번역_요약.txt').write_text('sum',encoding='utf-8')
            sub=root/'sub'; sub.mkdir(); (sub/'c.txt').write_text('c',encoding='utf-8')
            out=root/'한국어_번역'; out.mkdir(); (out/'old.txt').write_text('old',encoding='utf-8')
            self.assertEqual([p.name for p in discover_txt_files(root,False,out)],['a.txt','b.txt'])
            self.assertEqual([p.relative_to(root).as_posix() for p in discover_txt_files(root,True,out)],['a.txt','b.txt','sub/c.txt'])

    def test_output_preserves_relative_subfolder(self):
        root=Path('/books'); src=root/'author'/'a.txt'; out=Path('/exports')
        self.assertEqual(output_dir_for(src,root,out),out/'author')

    def test_generated_names(self):
        self.assertTrue(is_generated_txt(Path('x.ko.partial.txt')))
        self.assertFalse(is_generated_txt(Path('x.txt')))

if __name__=='__main__': unittest.main()


def test_batch_uses_natural_numeric_order_and_uppercase_txt(tmp_path):
    from novel_translator.batch import discover_txt_files
    for name in ('10.txt','2.txt','1.txt','03.TXT'):
        (tmp_path/name).write_text(name,encoding='utf-8')
    assert [p.name for p in discover_txt_files(tmp_path)] == ['1.txt','2.txt','03.TXT','10.txt']

def test_folder_queue_never_stops_except_explicit_stop():
    from novel_translator.batch import should_continue_after_result
    for relation in ('continuous','independent'):
        for status in ('completed','partial','file_error','skipped'):
            assert should_continue_after_result(relation,status) is True
        assert should_continue_after_result(relation,'stopped') is False
        # A temporary pause is auto-resumed by the main window, never a queue advance by itself.
        assert should_continue_after_result(relation,'paused') is False
