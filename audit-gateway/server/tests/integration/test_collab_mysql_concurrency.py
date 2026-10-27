"""Opt-in real MySQL checks. Creates and drops ONLY its own random test schema.

COLLAB_MYSQL_TEST_URL supplies an administrative test-instance connection.
The database component of that URL is never used or modified.
"""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from db.inserts import project as writes
from db.inserts import project_turn as turns
from db.selects import project as reads
from model.database import Project, ProjectMember, ProjectMessage, ProjectTask, ProjectAgentTurn, User


@pytest_asyncio.fixture
async def mysql(monkeypatch, request):
    value = os.environ.get('COLLAB_MYSQL_TEST_URL')
    config = request.config.getoption('--collab-mysql-config')
    if config:
        from dotenv import dotenv_values
        value = dotenv_values(config).get('MYSQL_URL')
    if not value:
        pytest.skip('COLLAB_MYSQL_TEST_URL required for real MySQL concurrency')
    url = make_url(value).set(drivername='mysql+aiomysql', database='')
    name = 'collab_test_' + uuid.uuid4().hex
    admin = create_async_engine(url)
    engine = None
    created = False
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4'))
        created = True
        engine = create_async_engine(url.set(database=name), pool_size=10, max_overflow=40)
        tables = [m.__table__ for m in (User, Project, ProjectMember, ProjectMessage, ProjectTask, ProjectAgentTurn)]
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Project.metadata.create_all(sync, tables=tables))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def get_factory(): return factory
        for module in (writes, turns, reads):
            monkeypatch.setattr(module, 'init_async_session', get_factory)
        yield factory
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS `{name}`'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_fifty_competing_agents_get_one_lease_and_can_resume_after_finish(mysql):
    async def acquire(i):
        return await turns.acquire_turn(project_id=1, member_id=i + 1, agent_label='Agent', owner_uid='owner',
            trigger_reason='mention', trigger_seq=1, requester_uid='owner', trigger_request_id=f'test-{i}')
    results = await asyncio.gather(*(acquire(i) for i in range(50)))
    winners = [result for result in results if result]
    assert len(winners) == 1
    assert await turns.finish_turn(turn_id=winners[0]['id'], owner_uid='owner', status='cancelled')
    assert not await turns.heartbeat_turn(turn_id=winners[0]['id'], owner_uid='owner', status_seq=99)
    assert await acquire(51)


@pytest.mark.asyncio
async def test_fifty_concurrent_messages_have_unique_contiguous_sequences(mysql):
    project = await writes.create_project(code='concurrent', name='Concurrent', description='', owner_uid='owner', owner_username='Owner', dept_id=None, dept_code=None)
    async def post(i):
        return await writes.post_message(project_id=project['id'], sender_kind='user', sender_uid='owner', sender_username='Owner', sender_agent_label='', dept_id=None, dept_code=None, content=f'message-{i}', content_original=None, redaction_hits=0, mentions=[])
    results = await asyncio.gather(*(post(i) for i in range(50)))
    assert sorted(result['seq'] for result in results) == list(range(1, 51))
    assert len({result['id'] for result in results}) == 50
    assert (await reads.get_project_by_code('concurrent'))['message_seq'] == 50
