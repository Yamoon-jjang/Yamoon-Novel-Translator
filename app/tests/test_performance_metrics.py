"""Performance counters must not change provider outcomes or expose prompts."""
import time
from novel_translator.orchestrator import TranslationOrchestrator, KeyState
from novel_translator.providers.base import ProviderError, ErrorKind


class Dummy:
    def translate(self, request, secret):
        return "OK"


def test_provider_timing_without_credentials_or_text():
    o=TranslationOrchestrator(Dummy(), [KeyState('k','DO_NOT_LOG','P1')], 'm', cancel_check=None)
    assert o._call_provider(object(), 'DO_NOT_LOG') == 'OK'
    assert o.perf['requests'] == 1
    assert o.perf['provider_seconds'] >= 0
    assert 'DO_NOT_LOG' not in repr(o.perf)


def test_pacing_metrics_do_not_modify_wait():
    clock=[100.0]
    waits=[]
    def sleep(sec):
        waits.append(sec)
        clock[0]+=sec
    o=TranslationOrchestrator(Dummy(), [KeyState('a','SECRET','P1')], 'm',
        target_rpm=10, min_rpm=2, max_rpm=30,
        clock_fn=lambda:clock[0],sleep_fn=sleep)
    o._pace()
    o._pace()
    assert len(waits)==1 and abs(waits[0]-6.0)<1e-6
    assert abs(o.perf['pace_seconds']-6.0)<1e-6
    assert o._pick_key().project_group == 'P1'
    o._cool_project(o.keys[0],30)
    assert o._pick_key().project_group == 'P1'
    assert abs(o.perf['cooldown_seconds']-30)<1e-6
