"""Two-group refinement config and old four-stage migration."""
import json
import pytest
from novel_translator.refinement_engines import STAGES,PROVIDERS,load_routes,save_routes,resolve_routes

def test_two_stage_choices_and_save(tmp_path):
    p=tmp_path/'routing.json'
    routes=load_routes(p)
    assert len(STAGES)==2
    routes['analysis']={'provider':'local','model':'12b'}
    routes['editing']={'provider':'openai','model':'gpt-6.1-sol'}
    save_routes(routes,p)
    assert load_routes(p)==routes
    assert resolve_routes(routes,{},PROVIDERS)['editing']['model']=='gpt-6.1-sol'
    assert 'keys' not in p.read_text(encoding='utf-8')

def test_legacy_four_stage_migration_preserves_meaning(tmp_path):
    p=tmp_path/'routing.json'
    old={'names_terms':{'provider':'gemini','model':'a'},
         'speech':{'provider':'local','model':'b'},
         'literary_edit':{'provider':'openai','model':'c'},
         'continuity_review':{'provider':'local','model':'d'}}
    p.write_text(json.dumps({'schema':1,'routes':old}),encoding='utf-8')
    routes=load_routes(p)
    assert routes=={'analysis':old['speech'],'editing':old['literary_edit']}
    assert json.loads(p.read_text(encoding='utf-8'))['schema']==1
    save_routes(routes,p)
    assert json.loads(p.read_text(encoding='utf-8'))['schema']==2

def test_missing_engine_fails_closed(tmp_path):
    routes=load_routes(tmp_path/'missing.json')
    with pytest.raises(ValueError):
        resolve_routes(routes,{'local':'12b'},('openai',))
    result=resolve_routes(routes,{'local':'12b'},('local',))
    assert result['analysis']['model']=='12b'

def test_bad_route_rejected(tmp_path):
    routes=load_routes(tmp_path/'missing.json')
    routes['unknown']={'provider':'gemini','model':''}
    with pytest.raises(ValueError):save_routes(routes,tmp_path/'bad.json')
    del routes['unknown']
    routes['editing']['provider']='x'
    with pytest.raises(ValueError):save_routes(routes,tmp_path/'bad.json')
