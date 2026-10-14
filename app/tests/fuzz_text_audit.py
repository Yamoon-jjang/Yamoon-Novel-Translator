from __future__ import annotations
import random, tempfile, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from novel_translator.chunking import split_paragraphs, make_chunks
from novel_translator.rendering import render_snapshot


def random_doc(rng: random.Random) -> str:
    alphabet='abcdefghijklmnopqrstuvwxyz日本語中文가나다라마바사0123456789 <>/P！？。.,!?'
    lines=[]
    for _ in range(rng.randint(1,140)):
        indent=rng.choice(['',' ','  ','\t','　'])
        # Occasionally generate a pathological physical line.
        size=rng.choice([rng.randint(1,220), rng.randint(1200,4000) if rng.random()<0.04 else rng.randint(1,220)])
        body=''.join(rng.choice(alphabet) for _ in range(size))
        if rng.random()<0.12:
            body += '<P999999>'
        lines.append(indent+body)
        lines.extend(['']*rng.randint(0,3))
    prefix='\n'*rng.randint(0,3)
    return prefix+'\n'.join(lines)


def main():
    rng=random.Random(20261007)
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        for i in range(750):
            src=random_doc(rng)
            ps=split_paragraphs(src)
            reconstructed=''.join(p.prefix_before+p.text+p.separator_after for p in ps)
            if reconstructed!=src:
                raise AssertionError(f'layout mismatch case {i}')
            chunks=make_chunks(ps,'ja',target_tokens=420,min_tokens=80,max_tokens=720) if ps else []
            if not chunks:
                continue
            rows=[]
            fail_index = rng.randrange(len(chunks)) if rng.random()<0.25 else None
            for c in chunks:
                if c.index==fail_index:
                    rows.append({'chunk_index':c.index,'source_text':c.source_text,'status':'FAILED','translation':None,'error_type':'SIMULATED','error_message':'fuzz'})
                else:
                    tr='\n\n'.join(f'<P{p.pid:06d}>\n번역문단{p.pid}' for p in c.paragraphs)
                    rows.append({'chunk_index':c.index,'source_text':c.source_text,'status':'COMPLETED','translation':tr,'error_type':None,'error_message':None})
            source=root/f'case_{i}.txt'
            out,failed,report,_=render_snapshot(chunks,rows,source,root/'out',root/'fallback')
            rendered=out.read_text(encoding='utf-8-sig')
            if fail_index is None:
                if failed or '[번역 미완료' in rendered:
                    raise AssertionError(f'unexpected failure marker {i}')
                for p in ps:
                    if f'번역문단{p.pid}' not in rendered:
                        raise AssertionError(f'missing translation pid={p.pid} case={i}')
            else:
                if not failed or not report or (root/'out'/f'case_{i}.ko.failed.txt').exists():
                    raise AssertionError(f'failed report missing case={i}')
                failed_chunk=chunks[fail_index]
                for p in failed_chunk.paragraphs:
                    if p.text not in rendered:
                        raise AssertionError(f'failed source lost pid={p.pid} case={i}')
        print('fuzz_text_audit: PASS cases=750')

if __name__=='__main__':
    main()
