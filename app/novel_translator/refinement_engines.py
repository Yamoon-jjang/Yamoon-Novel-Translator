"""Two independently configured engines for Korean novel preparation and editing."""
from __future__ import annotations
from pathlib import Path
from .config import DATA_DIR,load_json,save_json

FILENAME='korean_refinement_engines.json'
STAGES=(('analysis','인물·용어·말투 분석'),('editing','문학적 윤문·이야기 연속성'))
PROVIDERS=('gemini','openai','local')
DEFAULTS={'analysis':{'provider':'local','model':''},
          'editing':{'provider':'local','model':''}}
_OLD=('names_terms','speech','literary_edit','continuity_review')

def validate_routes(value):
    if not isinstance(value,dict) or set(value)!={k for k,_ in STAGES}:
        raise ValueError('재정비 엔진 설정은 분석과 윤문 두 그룹이 필요합니다.')
    out={}
    for key,_ in STAGES:
        row=value[key]
        if not isinstance(row,dict): raise ValueError(key+': 설정 형식 오류')
        provider=str(row.get('provider') or '').strip()
        model=str(row.get('model') or '').strip()
        if provider not in PROVIDERS or len(model)>200 or any(c in model for c in '\r\n\x00'):
            raise ValueError(key+': 엔진 또는 모델명을 확인하세요.')
        out[key]={'provider':provider,'model':model}
    return out

def migrate_routes(value):
    if not isinstance(value,dict): raise ValueError('재정비 설정 형식 오류')
    if set(value)==set(_OLD):
        return validate_routes({'analysis':dict(value['speech']),
                                'editing':dict(value['literary_edit'])})
    return validate_routes(value)

def load_routes(path:Path|None=None):
    p=path or DATA_DIR/FILENAME
    if not p.exists(): return validate_routes({k:dict(v) for k,v in DEFAULTS.items()})
    data=load_json(p)
    if data.get('schema') not in (1,2) or not isinstance(data.get('routes'),dict):
        raise ValueError('지원하지 않는 재정비 설정입니다.')
    return migrate_routes(data['routes'])

def save_routes(routes,path:Path|None=None):
    save_json(path or DATA_DIR/FILENAME,{'schema':2,'routes':validate_routes(routes)})

def resolve_routes(routes,live_models,enabled_providers):
    enabled=set(enabled_providers)
    out={}
    for key,row in validate_routes(routes).items():
        provider=row['provider']
        if provider not in enabled: raise ValueError(f'{key}: {provider} 엔진을 API 설정에서 활성화하세요.')
        model=row['model'] or str(live_models.get(provider) or '').strip()
        if not model: raise ValueError(f'{key}: 사용할 모델을 선택하세요.')
        out[key]={'provider':provider,'model':model}
    return out
