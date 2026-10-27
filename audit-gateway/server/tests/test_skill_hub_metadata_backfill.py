import json
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from services.skill_hub_metadata_backfill import prepare_plan,persist_plan,apply_plan,snapshot

@pytest.mark.asyncio
async def test_real_sqlite_metadata_only_idempotent_cas_and_rollback(tmp_path):
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'hub.db'))
    async with engine.begin() as connection:
        await connection.execute(text('CREATE TABLE skill_hub_skills (id INTEGER PRIMARY KEY,slug TEXT,name TEXT,description TEXT,latest_version_id INTEGER,updated_at TEXT)'))
        await connection.execute(text('CREATE TABLE skill_hub_versions (id INTEGER PRIMARY KEY,bundle_hash TEXT)'))
        await connection.execute(text("INSERT INTO skill_hub_versions VALUES (1,'immutable-hash')"))
        await connection.execute(text("INSERT INTO skill_hub_skills VALUES (1,'demo','English','保留已有中文介绍',1,'original-time')"))
    factory=async_sessionmaker(engine)
    async with factory() as session:
        rows=await snapshot(session)
        plan=prepare_plan(rows,{'demo':{'name':'公司技能','description':'不要覆盖这段'}})
        path=tmp_path/'plan.json';persist_plan(plan,path)
        assert plan['entries'][0]['updated']['description']=='保留已有中文介绍'
        assert (await apply_plan(session,path))['changed']==['demo']
        assert (await apply_plan(session,path))['changed']==[]
        assert (await snapshot(session))[0]['bundle_hash']=='immutable-hash'
        await session.execute(text("UPDATE skill_hub_skills SET name='管理员修改' WHERE id=1"));await session.commit()
        assert (await apply_plan(session,path,rollback=True))['changed']==[]
        await session.execute(text("UPDATE skill_hub_skills SET name='公司技能' WHERE id=1"));await session.commit()
        assert (await apply_plan(session,path,rollback=True))['changed']==['demo']
        assert (await snapshot(session))[0]['name']=='English'
        # Concurrent version changes prevent both forward and rollback updates.
        await session.execute(text("UPDATE skill_hub_skills SET latest_version_id=2 WHERE id=1"));await session.commit()
        assert (await apply_plan(session,path))['changed']==[]
    await engine.dispose()


def test_plan_is_missing_fields_only_and_preserves_original_snapshot(tmp_path):
    rows=[{'id':1,'slug':'demo','name':'中文标题','description':'English','latest_version_id':7,'bundle_hash':'hash'},
          {'id':2,'slug':'valid','name':'有效标题','description':'有效介绍','latest_version_id':8,'bundle_hash':'hash2'}]
    plan=prepare_plan(rows,{'demo':{'name':'不得改名','description':'补齐用途'},'valid':{'name':'不覆盖','description':'不覆盖'}})
    assert len(plan['entries'])==1 and plan['entries'][0]['original']==rows[0]
    assert plan['entries'][0]['updated']['name']=='中文标题'
    path=tmp_path/'plan.json';persist_plan(plan,path)
    with pytest.raises(FileExistsError):persist_plan(plan,path)
    assert json.loads(path.read_text())==plan

@pytest.mark.asyncio
async def test_same_bundle_publish_repairs_persisted_fields_once_and_returns_canonical(monkeypatch,tmp_path):
    from model.database import SkillHubSkill,SkillHubVersion
    from services.skill_hub import parse_publish
    from db.inserts import skill_hub as writes
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'same-bundle.db'))
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync:SkillHubSkill.__table__.create(sync))
        await connection.run_sync(lambda sync:SkillHubVersion.__table__.create(sync))
    factory=async_sessionmaker(engine,expire_on_commit=False)
    payload={'schema_version':1,'slug':'demo','name':'补齐中文标题','description':'补齐中文介绍',
        'files':[{'path':'SKILL.md','encoding':'utf-8','content':'Body'}]}
    parsed=parse_publish(payload,raw_bytes=len(json.dumps(payload)))
    async with factory() as session:
        session.add(SkillHubSkill(id=1,slug='demo',name='English',description='用户已有中文介绍',owner_uid='u',latest_version_id=1,latest_version=1))
        session.add(SkillHubVersion(id=1,skill_id=1,version=1,bundle_hash=parsed.bundle_hash,files_json=parsed.files_json(),publisher_uid='u'))
        await session.commit()
    async def session_factory():return factory
    monkeypatch.setattr(writes,'init_async_session',session_factory)
    arguments=dict(parsed=parsed,publisher_uid='u',publisher_username=None,publisher_dept_id=None,publisher_dept_code=None)
    first=await writes.publish_skill(**arguments)
    assert first['name']=='补齐中文标题' and first['description']=='用户已有中文介绍' and not first['created']
    second=await writes.publish_skill(**arguments)
    assert first==second
    async with factory() as session:
        rows=await snapshot(session)
        assert len(rows)==1 and rows[0]['name']=='补齐中文标题' and rows[0]['bundle_hash']==parsed.bundle_hash
    await engine.dispose()
