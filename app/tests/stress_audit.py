"""Dependency-light internal stress audit. Not required by end users."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from novel_translator.orchestrator import TranslationOrchestrator, KeyState
from novel_translator.providers.base import TranslationProvider, TranslationResponse, ProviderError, ErrorKind
from novel_translator.profiles import ProfileManager
import re

class Clock:
    def __init__(self): self.t=0.0
    def time(self): return self.t
    def sleep(self, sec): self.t += max(0.0, sec)

class PatternProvider(TranslationProvider):
    name='stress'
    def __init__(self): self.n=0
    def translate(self, req, api_key):
        self.n += 1
        # Deliberately inject recurring, recoverable provider failures.
        if self.n % 997 == 0: raise ProviderError(ErrorKind.TRANSIENT,'503')
        if self.n % 389 == 0: raise ProviderError(ErrorKind.RATE_LIMIT,'429')
        ids=[]
        for x in re.findall(r'<P\d{6}>', req.user_prompt):
            if x not in ids: ids.append(x)
        return TranslationResponse('\n'.join(f'{x}\n정상 번역 문장입니다.' for x in ids))

def main():
    profile=ProfileManager().set_active('ja-ko')
    clock=Clock(); p=PatternProvider()
    keys=[KeyState('A1','s-a1','project-a'),KeyState('A2','s-a2','project-a'),KeyState('B1','s-b1','project-b')]
    o=TranslationOrchestrator(p,keys,'fake',retries=5,sleep_fn=clock.sleep,clock_fn=clock.time)
    for i in range(20000):
        txt=f'<P{i%999999+1:06d}>\n原文です。'
        out,_,_=o.translate_chunk(profile,txt,{},'')
        assert '정상 번역' in out
    assert all(k.enabled for k in keys)
    print('stress_audit: OK', 'logical_requests=20000', f'provider_calls={p.n}', f'virtual_wait={clock.t:.1f}s')

if __name__=='__main__': main()
