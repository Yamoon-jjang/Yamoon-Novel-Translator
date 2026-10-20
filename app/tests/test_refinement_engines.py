"""Pre-v2.0 refinement routing file: still read once to carry the old choice over."""
import json
import pytest
from novel_translator.refinement_engines import STAGES, load_routes, validate_routes


def test_two_stage_defaults(tmp_path):
    routes = load_routes(tmp_path / 'missing.json')
    assert len(STAGES) == 2
    assert set(routes) == {'analysis', 'editing'}
    assert routes['editing']['provider'] == 'local'


def test_legacy_four_stage_migration_preserves_meaning(tmp_path):
    p = tmp_path / 'routing.json'
    old = {'names_terms': {'provider': 'gemini', 'model': 'a'},
           'speech': {'provider': 'local', 'model': 'b'},
           'literary_edit': {'provider': 'openai', 'model': 'c'},
           'continuity_review': {'provider': 'local', 'model': 'd'}}
    p.write_text(json.dumps({'schema': 1, 'routes': old}), encoding='utf-8')
    assert load_routes(p) == {'analysis': old['speech'], 'editing': old['literary_edit']}
    # Reading never rewrites the old file.
    assert json.loads(p.read_text(encoding='utf-8'))['schema'] == 1


def test_bad_route_rejected(tmp_path):
    routes = load_routes(tmp_path / 'missing.json')
    routes['unknown'] = {'provider': 'gemini', 'model': ''}
    with pytest.raises(ValueError):
        validate_routes(routes)
    del routes['unknown']
    routes['editing']['provider'] = 'x'
    with pytest.raises(ValueError):
        validate_routes(routes)
