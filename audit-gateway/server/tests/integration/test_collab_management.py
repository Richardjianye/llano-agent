"""Exercise the real repository transactions on an isolated SQLite database.

SQLite validates state transitions and rollback; these tests do not prove
MySQL row-lock concurrency.
"""
import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.ext.compiler import compiles

from model.database import User, Project, ProjectMember, ProjectTask, ProjectMessage, ProjectAgentTurn
from db.inserts import project as writes
from db.inserts import project_turn as turn_writes
from db.selects import project as reads
from router.v1 import projects
from services.project import ProjectRejected
from utils.gateway_auth import Caller
from utils.errors import AppError

@compiles(BigInteger, 'sqlite')
def sqlite_bigint(element, compiler, **kw):
    return 'INTEGER'

@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/collab.db')
    tables = [m.__table__ for m in (User, Project, ProjectMember, ProjectTask, ProjectMessage, ProjectAgentTurn)]
    async with engine.begin() as conn:
        await conn.run_sync(lambda connection: Project.metadata.create_all(connection, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def get_factory(): return factory
    for module in (writes, reads, turn_writes, __import__('db')):
        monkeypatch.setattr(module, 'init_async_session', get_factory)
    yield factory
    await engine.dispose()

async def seed():
    project = await writes.create_project(code='test-project', name='Test', description='', owner_uid='owner', owner_username='Owner', dept_id=None, dept_code=None)
    return project['id']

def caller(uid):
    return Caller(uid=uid, username=uid, role='employee', dept_id=None, dept_code=None)

@pytest.mark.asyncio
async def test_last_owner_must_transfer_before_leaving(database):
    project_id = await seed()
    owner = await reads.get_membership(project_id, 'owner')
    with pytest.raises(ProjectRejected):
        await writes.remove_member(member_id=owner['id'])
    assert (await reads.get_membership(project_id, 'owner'))['status'] == 'active'
    async with database() as session:
        member = ProjectMember(project_id=project_id, member_kind='user', user_uid='next', username='Next', agent_label='', role='member', status='active')
        session.add(member)
        await session.commit()
        target_id = member.id
    await projects.transfer_project('test-project', Response(), member_id=target_id, caller=caller('owner'))
    assert (await reads.get_membership(project_id, 'next'))['role'] == 'owner'
    assert (await reads.get_project_by_code('test-project'))['owner_uid'] == 'next'
    assert await writes.remove_member(member_id=owner['id'])
    assert (await reads.get_membership(project_id, 'owner'))['status'] == 'left'

@pytest.mark.asyncio
async def test_non_owner_cannot_edit_or_transfer(database):
    project_id = await seed()
    async with database() as session:
        session.add(ProjectMember(project_id=project_id, member_kind='user', user_uid='member', agent_label='', role='member', status='active'))
        await session.commit()
    with pytest.raises(AppError):
        await projects.update_project('test-project', Response(), name='Changed', description='', caller=caller('member'))
    with pytest.raises(AppError):
        await projects.transfer_project('test-project', Response(), member_id=1, caller=caller('member'))
    assert (await reads.get_project_by_code('test-project'))['name'] == 'Test'

@pytest.mark.asyncio
async def test_foreign_task_cannot_be_claimed_or_confirmed(database):
    await seed()
    async with database() as session:
        task = ProjectTask(project_id=999, seq=1, title='Foreign', body='do not execute', source_kind='chat', untrusted=True, created_by_uid='owner', status='claimed', claimed_by_uid='owner')
        session.add(task)
        await session.commit()
        task_id = task.id
    with pytest.raises(AppError):
        await projects.claim('test-project', task_id, Response(), agent_label='', caller=caller('owner'))
    with pytest.raises(AppError):
        await projects.confirm('test-project', task_id, Response(), caller=caller('owner'))
    assert (await reads.get_task(task_id))['status'] == 'claimed'

@pytest.mark.asyncio
async def test_cancelled_reply_is_not_published_or_numbered(database):
    project_id = await seed()
    async with database() as session:
        turn = ProjectAgentTurn(project_id=project_id, member_id=1, owner_uid='owner', agent_label='Assistant', status='cancelled', trigger_seq=1, trigger_reason='mention', requester_uid='owner', trigger_request_id='request-1')
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    with pytest.raises(ProjectRejected):
        await writes.post_message(project_id=project_id, sender_kind='agent', sender_uid='owner', sender_username='Owner', sender_agent_label='Assistant', dept_id=None, dept_code=None, content='late reply', content_original=None, redaction_hits=0, mentions=[], expected_turn_id=turn_id)
    assert (await reads.get_project_by_code('test-project'))['message_seq'] == 0
    assert await reads.list_messages(project_id) == []

@pytest.mark.asyncio
async def test_task_result_is_atomic_idempotent_and_requires_acceptance(database):
    project_id = await seed()
    async with database() as session:
        task = ProjectTask(project_id=project_id, seq=1, title='Task', body='Original', source_kind='chat', untrusted=True, created_by_uid='owner', status='claimed', claimed_by_uid='owner')
        session.add(task)
        await session.commit()
        task_id = task.id
    # Later can be resumed without competing with the caller's own claim.
    resumed = await projects.claim('test-project', task_id, Response(), agent_label='', caller=caller('owner'))
    assert resumed['confirmation']['body'] == 'Original'
    args = dict(project_id=project_id, sender_kind='user', sender_uid='owner', sender_username='Owner', sender_agent_label='', dept_id=None, dept_code=None, content='Reviewed result', content_original=None, redaction_hits=0, mentions=[], expected_task_id=task_id)
    with pytest.raises(ProjectRejected):
        await writes.post_message(**args)
    await projects.confirm('test-project', task_id, Response(), caller=caller('owner'))
    first = await writes.post_message(**args)
    again = await writes.post_message(**{**args, 'content': 'repeated request'})
    assert first['id'] == again['id']
    assert len(await reads.list_messages(project_id)) == 1
    task = await reads.get_task(task_id)
    assert task['status'] == 'submitted' and task['result_message_id'] == first['id']
    public_result = await projects.read_task_result('test-project', task_id, Response(), caller=caller('owner'))
    assert public_result['message']['content'] == 'Reviewed result'
    assert 'content_original' not in public_result['message']
    await projects.accept_task_result('test-project', task_id, Response(), caller=caller('owner'))
    assert (await reads.get_task(task_id))['status'] == 'done'
    with pytest.raises(ProjectRejected):
        await writes.post_message(**args)


@pytest.mark.asyncio
async def test_departure_releases_tasks_and_blocks_late_claims(database):
    project_id = await seed()
    member = await writes.add_member(project_id=project_id, member_kind='user', user_uid='member', username='Member', agent_label='', dept_id=None, dept_code=None, role='member')
    await writes.add_member(project_id=project_id, member_kind='agent', user_uid='member', username='Member', agent_label='Assistant', dept_id=None, dept_code=None, role='member')
    task = await writes.create_task(project_id=project_id, title='Task', body='Original', source_kind='manual', source_message_id=None, untrusted=False, created_by_uid='owner', created_by_username='Owner')
    assert await writes.claim_task(task_id=task['id'], uid='member', username='Member', agent_label='')
    # Use the public removal route, including Agent and task cleanup.
    await projects.kick('test-project', member['id'], Response(), reason='', caller=caller('member'))
    released = await reads.get_task(task['id'])
    assert released['status'] == 'open' and released['returned_count'] == 1
    assert not await writes.claim_task(task_id=task['id'], uid='member', username='Member', agent_label='')
    assert not any(m['user_uid'] == 'member' for m in await reads.list_members(project_id))


@pytest.mark.asyncio
async def test_removed_agent_cannot_publish_with_a_still_running_lease(database):
    project_id = await seed()
    member = await writes.add_member(project_id=project_id, member_kind='agent', user_uid='owner', username='Owner', agent_label='Assistant', dept_id=None, dept_code=None, role='member')
    async with database() as session:
        turn = ProjectAgentTurn(project_id=project_id, member_id=member['id'], owner_uid='owner', agent_label='Assistant', status='running', trigger_seq=1, trigger_reason='mention', requester_uid='owner', trigger_request_id='request-live')
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    args = dict(project_id=project_id, sender_kind='agent', sender_uid='owner', sender_username='Owner', sender_agent_label='Assistant', dept_id=None, dept_code=None, content='reply', content_original=None, redaction_hits=0, mentions=[], expected_turn_id=turn_id)
    await writes.post_message(**args)
    await writes.remove_member(member_id=member['id'])
    with pytest.raises(ProjectRejected):
        await writes.post_message(**args)
    assert len(await reads.list_messages(project_id)) == 1

@pytest.mark.asyncio
async def test_member_names_resolve_from_authoritative_accounts(database):
    project_id = await seed()
    async with database() as session:
        session.add(User(uid='next', username='阿乙', password='unused', status=1))
        session.add(ProjectMember(project_id=project_id, member_kind='user', user_uid='next', username='', agent_label='', role='member', status='active'))
        await session.commit()
    member = next(m for m in await reads.list_members(project_id) if m['user_uid'] == 'next')
    assert member['username'] == '阿乙'
    from services.project import parse_mentions
    assert parse_mentions('@阿乙 你好', [member])

@pytest.mark.asyncio
async def test_invite_ignores_client_supplied_username(database, monkeypatch):
    await seed()
    from db.selects import user as user_reads
    async def factory(): return database
    monkeypatch.setattr(user_reads, 'init_async_session', factory)
    async with database() as session:
        session.add(User(uid='invitee', username='阿乙', password='unused', status=1))
        await session.commit()
    result = await projects.invite('test-project', Response(), member_kind='user', user_uid='invitee', username='伪造名称', agent_label='', role='member', caller=caller('owner'))
    async with database() as session:
        member = (await session.execute(select(ProjectMember).where(ProjectMember.user_uid == 'invitee'))).scalar_one()
        assert member.username == '阿乙'
