"""SQL contract: department-scoped visibility, publisher attribution, totals, paging.

2026-09-23 起页签和计数都只算调用者看得到的技能（公司空间 ∪ 本部门及上级空间），
别的部门没有可见技能时不出页签。
"""
import pytest
from services.org_tree import expand_ancestors
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from datetime import datetime

from model.database import Base, Department, SkillHubSkill, SkillHubVersion, SkillHubUsageEvent
from db.selects import skill_hub

PARENTS = {1: None, 2: None, 3: None, 4: None, 5: 2, 6: 5, 7: 4}


@pytest.mark.asyncio
async def test_admin_skill_hub_department_names_follow_catalog_without_department_permission(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'admin-names.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[Department.__table__, SkillHubSkill.__table__, SkillHubVersion.__table__, SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions(): return maker
    monkeypatch.setattr(skill_hub, 'init_async_session', sessions)
    async with maker() as session:
        session.add(Department(id=1, code='product', name='产品部'))
        session.add(SkillHubSkill(id=1, slug='report', name='周报', owner_uid='u', owner_username='张三',
                                  owner_dept_id=1, owner_dept_code='product', latest_version=1,
                                  status='listed', scope='dept', scope_dept_id=1))
        session.add(SkillHubVersion(id=1, skill_id=1, version=1, bundle_hash='h', files_json='[]',
                                    publisher_uid='u', publisher_username='张三', publisher_dept_id=1,
                                    publisher_dept_code='product'))
        await session.commit()
    try:
        items, _ = await skill_hub.admin_list_skills()
        assert items[0]['owner_dept_name'] == '产品部'
        assert (await skill_hub.count_skills_by_dept())[0]['dept_name'] == '产品部'
        assert (await skill_hub.count_skills_by_publisher())[0]['dept_name'] == '产品部'
        assert (await skill_hub.list_versions('report'))[0]['publisher_dept_name'] == '产品部'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_only_shows_what_the_viewer_can_see_and_pages_beyond_100(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'catalog.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Department.__table__, SkillHubSkill.__table__, SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async def sessions(): return maker
    monkeypatch.setattr(skill_hub, 'init_async_session', sessions)

    async def ancestors(dept_id):
        return () if dept_id is None else expand_ancestors(int(dept_id), PARENTS)

    monkeypatch.setattr(skill_hub, 'dept_ancestor_ids', ancestors)
    async with maker() as session:
        session.add_all([Department(id=i, code=str(i), name=f'Department {i}') for i in range(1,5)])
        session.add_all([Department(id=5, code="5", name="Child", parent_id=2), Department(id=6, code="6", name="Grandchild", parent_id=5), Department(id=7, code="7", name="Empty child", parent_id=4)])
        session.add_all([SkillHubSkill(id=i, slug=f'skill-{i}', name=f'Skill {i}', owner_uid='test', owner_dept_id=6 if i % 2 else 5,
                          latest_version=3, status='listed', scope='dept', scope_dept_id=2) for i in range(1,106)])
        session.add(SkillHubSkill(id=106, slug='own',name='Own',owner_uid='test',owner_dept_id=1,
                    latest_version=2,status='listed',scope='company',scope_dept_id=None))
        session.add(SkillHubSkill(id=107, slug='removed',name='Removed',owner_uid='test',owner_dept_id=3,
                    latest_version=1,status='delisted',scope='dept',scope_dept_id=3))
        await session.commit()
    try:
        # 一级部门 1 的人：只看得到公司空间那一个；部门 2 空间里的 105 个看不到，页签也不出现
        catalog = await skill_hub.department_catalog(viewer_dept_id=1)
        groups = catalog['departments']
        assert [d['id'] for d in groups] == [1]
        assert [d['count'] for d in groups] == [1]
        assert all(d['skills'] == [] for d in groups)  # tab headers do not fetch every department's skills
        own = await skill_hub.department_catalog(viewer_dept_id=1, department_id=1)
        assert own['departments'][0]['skills'][0]['slug'] == 'own'
        # 点名要部门 2 也拿不到：不能靠拼 department_id 绕过可见性
        assert (await skill_hub.department_catalog(viewer_dept_id=1, department_id=2))['departments'] == []

        # 部门 6（2 → 5 → 6）的人继承部门 2 的空间，另外能看到公司空间
        child = await skill_hub.department_catalog(viewer_dept_id=6)
        assert child['current_department_id'] == 2
        assert [(d['id'], d['count']) for d in child['departments']] == [(2, 105), (1, 1)]
        tab = (await skill_hub.department_catalog(viewer_dept_id=6, department_id=5))['departments'][0]
        assert tab['id'] == 2 and tab['count'] == 105
        assert all(s['publisher_dept_id'] in (5, 6) for s in tab['skills'])
        seen = []
        offset = 0
        while True:
            page = (await skill_hub.department_catalog(viewer_dept_id=6,department_id=2,offset=offset,limit=20))['departments'][0]
            seen.extend(s['slug'] for s in page['skills'])
            if not page['has_more']: break
            assert page['next_offset'] > offset
            offset = page['next_offset']
        assert len(seen) == len(set(seen)) == 105

        # 自己的一级部门哪怕没有技能也保留页签，排在最前
        empty_own = (await skill_hub.department_catalog(viewer_dept_id=7))['departments']
        assert [(d['id'], d['count']) for d in empty_own] == [(4, 0), (1, 1)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downloading_a_skill_does_not_touch_its_update_time(tmp_path, monkeypatch):
    """取包计数 +1 不能顺带刷新 updated_at（TimestampMixin 的 onupdate=now）。"""
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'bundle.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[Department.__table__, SkillHubSkill.__table__, SkillHubVersion.__table__, SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions(): return maker

    async def ancestors(dept_id):
        return () if dept_id is None else expand_ancestors(int(dept_id), PARENTS)

    monkeypatch.setattr(skill_hub, 'init_async_session', sessions)
    monkeypatch.setattr(skill_hub, 'dept_ancestor_ids', ancestors)
    published = datetime(2026, 9, 1, 10, 0, 0)
    async with maker() as session:
        session.add(Department(id=1, code='1', name='D1'))
        session.add(SkillHubSkill(id=1, slug='s', name='S', owner_uid='u', owner_dept_id=1, latest_version=1,
                                  latest_version_id=1, status='listed', scope='dept', scope_dept_id=1,
                                  created_at=published, updated_at=published))
        session.add(SkillHubVersion(id=1, skill_id=1, version=1, bundle_hash='h', files_json='[]',
                                    file_count=0, total_bytes=0, publisher_uid='u', created_at=published))
        await session.commit()
    try:
        assert await skill_hub.get_skill_bundle('s', dept_id=1) is not None
        async with maker() as session:
            row = await session.get(SkillHubSkill, 1)
            assert row.install_count == 1
            assert row.updated_at == published
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_listings_carry_the_latest_version_time_and_publisher(tmp_path, monkeypatch):
    """技能中心每行要显示「谁、什么时候发布的」：取最新版本的时间，不取 skill 行的 updated_at。"""
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'listing.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[Department.__table__, SkillHubSkill.__table__, SkillHubVersion.__table__, SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions(): return maker

    async def ancestors(dept_id):
        return () if dept_id is None else expand_ancestors(int(dept_id), PARENTS)

    monkeypatch.setattr(skill_hub, 'init_async_session', sessions)
    monkeypatch.setattr(skill_hub, 'dept_ancestor_ids', ancestors)
    async with maker() as session:
        session.add(Department(id=1, code='1', name='D1'))
        session.add(SkillHubSkill(id=1, slug='s', name='周报', owner_uid='u', owner_username='admin', owner_dept_id=1,
                                  latest_version=2, latest_version_id=2, status='listed', scope='dept',
                                  scope_dept_id=1, updated_at=datetime(2026, 9, 24, 9, 55, 0)))
        session.add(SkillHubVersion(id=1, skill_id=1, version=1, bundle_hash='a', files_json='[]', file_count=0,
                                    total_bytes=0, publisher_uid='u', publisher_username='admin',
                                    created_at=datetime(2026, 9, 1, 8, 0, 0)))
        session.add(SkillHubVersion(id=2, skill_id=1, version=2, bundle_hash='b', files_json='[]', file_count=0,
                                    total_bytes=0, publisher_uid='v', publisher_username='悟空',
                                    created_at=datetime(2026, 9, 23, 15, 39, 6)))
        await session.commit()
    try:
        found = (await skill_hub.search_skills('', dept_id=1))[0]
        assert found['published_at'] == '2026-09-23T15:39:06'
        assert found['publisher_username'] == '悟空'
        assert found['owner_username'] == 'admin'
        tab = (await skill_hub.department_catalog(viewer_dept_id=1, department_id=1))['departments'][0]
        assert tab['skills'][0]['published_at'] == '2026-09-23T15:39:06'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_skills_include_delisted_and_count_distinct_users(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'mine.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[Department.__table__, SkillHubSkill.__table__, SkillHubVersion.__table__, SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions(): return maker
    monkeypatch.setattr(skill_hub, 'init_async_session', sessions)
    async with maker() as session:
        session.add(Department(id=1, code='product', name='产品部'))
        session.add_all([
            SkillHubSkill(id=1, slug='mine-live', name='周报', owner_uid='me', owner_dept_id=1,
                          latest_version=2, latest_version_id=2, status='listed', scope='dept', scope_dept_id=1),
            SkillHubSkill(id=2, slug='mine-gone', name='旧流程', owner_uid='me', owner_dept_id=1,
                          latest_version=1, status='delisted', delisted_reason='流程已废止', scope='dept', scope_dept_id=1),
            SkillHubSkill(id=3, slug='theirs', name='别人的', owner_uid='other', owner_dept_id=1,
                          latest_version=1, status='listed', scope='dept', scope_dept_id=1),
        ])
        session.add(SkillHubVersion(id=2, skill_id=1, version=2, bundle_hash='h2', files_json='[]', changelog='补充步骤',
                                    publisher_uid='me', publisher_username='我', publisher_dept_id=1))
        session.add_all([SkillHubUsageEvent(user_uid=uid, event_id=f'e{i}', slug='mine-live')
                         for i, uid in enumerate(['a', 'a', 'b'])])
        await session.commit()
    try:
        mine = {item['slug']: item for item in await skill_hub.list_owned_skills('me')}
        assert set(mine) == {'mine-live', 'mine-gone'}, '只有本人首发的，含已下架'
        assert mine['mine-live']['usage_count'] == 3 and mine['mine-live']['user_count'] == 2
        assert mine['mine-live']['version'] == 2 and mine['mine-live']['changelog'] == '补充步骤'
        assert mine['mine-live']['scope_dept_name'] == '产品部'
        assert mine['mine-gone']['status'] == 'delisted' and mine['mine-gone']['delisted_reason'] == '流程已废止'
        assert await skill_hub.list_owned_skills('nobody') == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_updates_do_not_count_as_downloads_and_metrics_carry_update_facts(tmp_path, monkeypatch):
    """更新判定靠 metrics 的 bundle_hash，换新版取包不计下载（2026-10-16）。"""
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'updates.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[Department.__table__, SkillHubSkill.__table__, SkillHubVersion.__table__, SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions(): return maker

    async def ancestors(dept_id):
        return () if dept_id is None else expand_ancestors(int(dept_id), PARENTS)

    monkeypatch.setattr(skill_hub, 'init_async_session', sessions)
    monkeypatch.setattr(skill_hub, 'dept_ancestor_ids', ancestors)
    async with maker() as session:
        session.add(Department(id=1, code='1', name='D1'))
        session.add_all([
            SkillHubSkill(id=1, slug='s', name='周报', owner_uid='u', owner_dept_id=1, latest_version=2,
                          latest_version_id=2, status='listed', scope='dept', scope_dept_id=1),
            SkillHubSkill(id=2, slug='gone', name='旧流程', owner_uid='u', owner_dept_id=1, latest_version=1,
                          status='delisted', delisted_reason='流程已废止', scope='dept', scope_dept_id=1),
            SkillHubSkill(id=3, slug='elsewhere', name='别部门', owner_uid='u', owner_dept_id=3, latest_version=1,
                          status='delisted', delisted_reason='x', scope='dept', scope_dept_id=3),
        ])
        session.add(SkillHubVersion(id=2, skill_id=1, version=2, bundle_hash='h2', files_json='[]', changelog='补充核对步骤',
                                    publisher_uid='u', publisher_username='张三'))
        await session.commit()
    try:
        assert await skill_hub.get_skill_bundle('s', dept_id=1, count_download=False) is not None
        assert await skill_hub.get_skill_bundle('s', dept_id=1, count_download=False) is not None
        async with maker() as session:
            assert (await session.get(SkillHubSkill, 1)).install_count == 0, '更新取包不计下载'
        metrics = await skill_hub.metrics_for_slugs(['s', 'gone', 'elsewhere'], dept_id=1)
        assert metrics['s']['version'] == 2 and metrics['s']['changelog'] == '补充核对步骤'
        assert metrics['s']['display_metadata']['bundle_hash'] == 'h2' and metrics['s']['status'] == 'listed'
        assert metrics['gone'] == {'status': 'delisted', 'delisted_reason': '流程已废止'}
        assert 'elsewhere' not in metrics, '下架状态也只给看得见的人'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_status_tells_available_yours_taken_and_delisted(tmp_path, monkeypatch):
    from db.inserts import skill_hub as inserts

    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'preflight.db'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[SkillHubSkill.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions(): return maker
    monkeypatch.setattr(inserts, 'init_async_session', sessions)
    async with maker() as session:
        session.add_all([
            SkillHubSkill(id=1, slug='mine', name='我的', owner_uid='me', owner_username='我', latest_version=3,
                          status='listed', scope='dept'),
            SkillHubSkill(id=2, slug='theirs', name='别人的', owner_uid='other', owner_username='张三', latest_version=1,
                          status='listed', scope='dept'),
            SkillHubSkill(id=3, slug='gone', name='下架的', owner_uid='me', latest_version=1, status='delisted',
                          delisted_reason='重复', scope='dept'),
        ])
        await session.commit()
    try:
        assert (await inserts.publish_status('fresh', 'me', False))['status'] == 'available'
        assert (await inserts.publish_status('mine', 'me', False)) | {} == {
            'slug': 'mine', 'status': 'yours', 'owner_username': '我', 'version': 3}
        taken = await inserts.publish_status('theirs', 'me', False)
        assert taken['status'] == 'taken' and taken['owner_username'] == '张三'
        assert (await inserts.publish_status('theirs', 'me', True))['status'] == 'yours', '管理员可代发新版本，与发布规则一致'
        assert (await inserts.publish_status('gone', 'me', False))['delisted_reason'] == '重复'
    finally:
        await engine.dispose()
