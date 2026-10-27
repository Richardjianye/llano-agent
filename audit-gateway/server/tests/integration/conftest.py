"""真实 MySQL 集成测试夹具；只允许操作库名以 _test 结尾的数据库。"""

import os

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)

from model.database import (Base, CredentialAccessLog, McpCallLog,
                            McpCapability, McpInstance, McpUserPermission,
                            PlatformCredential, User)


TEST_MODELS = (
    User,
    PlatformCredential,
    CredentialAccessLog,
    McpInstance,
    McpCapability,
    McpUserPermission,
    McpCallLog,
)
TEST_TABLES = tuple(model.__table__ for model in TEST_MODELS)


async def _clear_tables(engine) -> None:
    async with engine.begin() as connection:
        for table in reversed(TEST_TABLES):
            await connection.execute(delete(table))


@pytest_asyncio.fixture
async def mysql_session_factory(monkeypatch):
    raw_url = os.environ.get("AUDIT_GATEWAY_TEST_MYSQL_URL", "")
    if not raw_url:
        pytest.skip("未设置 AUDIT_GATEWAY_TEST_MYSQL_URL")
    url = make_url(raw_url)
    if not url.database or not url.database.endswith("_test"):
        raise RuntimeError("集成测试数据库名必须以 _test 结尾")

    engine = create_async_engine(url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=list(TEST_TABLES)
            )
        )
    await _clear_tables(engine)

    async def init_test_session():
        return maker

    from services import mcp, mcp_credentials

    monkeypatch.setattr(mcp, "init_async_session", init_test_session)
    monkeypatch.setattr(mcp_credentials, "init_async_session", init_test_session)

    yield maker

    await _clear_tables(engine)
    await engine.dispose()

def pytest_addoption(parser):
    parser.addoption('--collab-mysql-config', default=None,
        help='Explicit local test .env file with MYSQL_URL; only random test schemas are modified')
