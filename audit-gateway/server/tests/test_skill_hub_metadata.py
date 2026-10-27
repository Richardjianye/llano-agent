import json
from types import SimpleNamespace
import pytest
from fastapi import Request
from starlette.responses import JSONResponse
from services.skill_hub import parse_publish,SkillHubRejected
from services.skill_hub_metadata import complete_publish_metadata,generate_metadata,has_chinese


def parsed(**values):
    payload={'schema_version':1,'slug':'demo','name':'English','description':'English description',
             'files':[{'path':'SKILL.md','encoding':'utf-8','content':'---\nname: demo\n---\nCreate reports'}],**values}
    return parse_publish(payload,raw_bytes=len(json.dumps(payload)),require_chinese_metadata=False)

def caller():
    return SimpleNamespace(uid='u',dept_id=1,role='employee',dept_code='rd',as_subject=lambda:object())

def request():
    return Request({'type':'http','method':'POST','path':'/v1/skill-hub/skills','headers':[]})

@pytest.mark.asyncio
async def test_reuse_current_server_metadata_without_model_charge(monkeypatch):
    value=parsed()
    async def existing(*a,**kw):return {'bundle_hash':value.bundle_hash,'name':'技能标题','description':'技能用途'}
    monkeypatch.setattr('db.selects.skill_hub.get_skill_meta',existing)
    async def forbidden(*a,**kw):raise AssertionError('must not charge again')
    monkeypatch.setattr('services.skill_hub_metadata.generate_metadata',forbidden)
    result,repaired=await complete_publish_metadata(value,request(),caller())
    assert repaired and result.name=='技能标题' and result.files==value.files and result.bundle_hash==value.bundle_hash

@pytest.mark.asyncio
async def test_forwarded_completion_receives_real_request_preserves_chinese(monkeypatch):
    async def models(*a):return [object()]
    monkeypatch.setattr('services.model_access.list_available_models',models)
    monkeypatch.setattr('services.model_access.model_public_name',lambda x:'deepseek-chat')
    monkeypatch.setattr('services.image_generation.is_image_model',lambda x:False)
    seen=[]
    async def forward(req,identity):
        data=await req.json();seen.append(data)
        return JSONResponse({'choices':[{'message':{'content':json.dumps({'name':'生成标题','description':'根据说明生成报表'},ensure_ascii=False)}}]})
    monkeypatch.setattr('router.v1.chat.chat_completions',forward)
    async def none(*a,**kw):return None
    monkeypatch.setattr('db.selects.skill_hub.get_skill_meta',none)
    value=parsed(name='用户中文标题')
    result,_=await complete_publish_metadata(value,request(),caller())
    assert result.name=='用户中文标题' and result.description=='根据说明生成报表'
    assert len(seen)==1 and seen[0]['stream'] is False and seen[0]['_llano_audit']['messages']==seen[0]['messages']
    assert result.files==value.files and result.bundle_hash==value.bundle_hash

@pytest.mark.asyncio
async def test_generated_english_failure_explicit_not_published(monkeypatch):
    async def models(*a):return [object()]
    monkeypatch.setattr('services.model_access.list_available_models',models)
    monkeypatch.setattr('services.model_access.model_public_name',lambda x:'deepseek-chat')
    monkeypatch.setattr('services.image_generation.is_image_model',lambda x:False)
    async def forward(*a):return JSONResponse({'choices':[{'message':{'content':'{"name":"English","description":"English"}'}}]})
    monkeypatch.setattr('router.v1.chat.chat_completions',forward)
    with pytest.raises(SkillHubRejected,match='尚未上传'):await generate_metadata(parsed(),request(),caller())

@pytest.mark.parametrize('status', ['listed','delisted'])
def test_preflight_owner_and_delisted_reject_before_generation(status):
    from db.inserts.skill_hub import check_publish_permission
    row=SimpleNamespace(owner_uid='other',owner_username='同事',status=status)
    with pytest.raises(SkillHubRejected):check_publish_permission(row,'demo','u',False)
    if status=='delisted':
        with pytest.raises(SkillHubRejected):check_publish_permission(row,'demo','other',False)


def test_chinese_proportion_shared_contract():
    assert has_chinese('Git 项目助手')
    assert not has_chinese('Very long English description with just one Chinese word 中')
