"""数据库模块 - 异步SQLAlchemy引擎与会话管理"""

from config import get_settings
from loguru import logger
from model.database import (ROLE_PRESIDENT, Base, Department,
                            PromptPolicyDraft, User)
from model.org_defaults import (DEFAULT_DEPARTMENTS, PRESIDENT_DEPT_CODE,
                                PRESIDENT_NICKNAME, PRESIDENT_REALNAME,
                                PRESIDENT_USERNAME)
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)
from utils.crypt import hash_password
from utils.generate import generate_password, generate_user_id

config = get_settings()

# 全局引擎和会话工厂
async_engine: AsyncEngine | None = None
async_session: async_sessionmaker | None = None


async def create_db_engine() -> AsyncEngine:
    """创建数据库引擎"""
    engine = create_async_engine(
        config.mysql_url,
        echo=config.db_echo,
        # 策略正文、密码散列等敏感绑定参数不得出现在 SQL echo 或 DBAPI
        # 异常字符串里；SQL 结构本身仍可在开启 echo 时用于排障。
        hide_parameters=True,
        pool_size=config.db_pool_size,
        pool_timeout=config.db_pool_timeout,
        pool_recycle=config.pool_recycle,
        max_overflow=config.db_max_overflow,
    )
    return engine


async def init_db_engine():
    """初始化数据库引擎"""
    global async_engine
    if async_engine is None:
        async_engine = await create_db_engine()
    return async_engine


async def init_async_session():
    """初始化异步会话工厂"""
    global async_session
    engine = await init_db_engine()
    if async_session is None:
        async_session = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
    return async_session


async def get_db():
    """FastAPI 依赖项：获取数据库会话"""
    session = await init_async_session()
    async with session() as session_instance:
        yield session_instance


async def init_db():
    """初始化数据库：建表 + 写入组织架构 + 创建总裁账号（均幂等）"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        exec_dept_id = await _seed_departments(conn)
        await _seed_president(conn, exec_dept_id)
        await _seed_prompt_policy_draft(conn)

        await conn.commit()


async def _seed_departments(conn) -> int | None:
    """按 code 幂等写入默认部门，返回管理部门的部门 ID"""
    existing = {
        row[0]
        for row in (await conn.execute(select(Department.code))).all()
    }

    missing = [(code, name) for code, name in DEFAULT_DEPARTMENTS
               if code not in existing]
    if missing:
        await conn.execute(
            insert(Department),
            [{"code": code, "name": name, "status": 1} for code, name in missing],
        )
        logger.info(f"已写入 {len(missing)} 个默认部门: "
                    f"{'、'.join(name for _, name in missing)}")

    return (
        await conn.execute(
            select(Department.id).where(Department.code == PRESIDENT_DEPT_CODE)
        )
    ).scalar_one_or_none()


async def _seed_president(conn, dept_id: int | None) -> None:
    """没有总裁账号时创建一个，密码随机生成并打印

    只在「一个总裁都没有」时触发；已有总裁则完全不动，
    避免误删账号后重启悄悄多出一个超管。
    """
    has_president = (
        await conn.execute(
            select(User.id).where(User.permission == ROLE_PRESIDENT).limit(1)
        )
    ).scalar_one_or_none()

    if has_president:
        return

    pwd = generate_password()
    await conn.execute(
        insert(User).values(
            uid=generate_user_id(),
            username=PRESIDENT_USERNAME,
            password=hash_password(pwd),
            nickname=PRESIDENT_NICKNAME,
            realname=PRESIDENT_REALNAME,
            permission=ROLE_PRESIDENT,
            dept_id=dept_id,
            status=1,
        )
    )
    # 用『』框住边界：密码里常有 . : $ 等符号，不框起来容易连着标点一起复制
    logger.warning(
        f"未检测到总裁账号，已创建。\n"
        f"  用户名：『{PRESIDENT_USERNAME}』\n"
        f"  初始密码：『{pwd}』（长度 {len(pwd)}）\n"
        f"  请立即登录后台修改密码，此密码只打印这一次。"
    )


async def _seed_prompt_policy_draft(conn) -> None:
    """创建唯一空草稿；首版发布前客户端继续使用安装包基线。"""
    exists = (
        await conn.execute(
            select(PromptPolicyDraft.id).where(PromptPolicyDraft.id == 1)
        )
    ).scalar_one_or_none()
    if exists is not None:
        return

    await conn.execute(
        insert(PromptPolicyDraft).values(
            id=1,
            company_core_policy="",
            default_persona="",
            content_hash=None,
            revision=0,
            base_version=None,
            updated_by_uid=None,
            updated_by_name=None,
        )
    )


async def check_schema_drift() -> list[str]:
    """比对 ORM 模型与实际表结构，返回缺失的列

    为什么需要这个：`create_all` 只会「建缺失的表」，**不会给已存在的表加列**。
    加了字段却忘了跑 `scripts/migrations/*.sql` 时，服务能正常启动，
    直到第一个真实请求才炸在 `Unknown column`——而且往往是在转发链路里炸，
    用户看到的是对话失败，排查得绕一大圈。

    这里在启动时就把差异找出来，早一个数量级发现问题。
    只查缺列不查类型：类型差异误报率高，而漏列是迁移没跑的典型症状。
    """
    from sqlalchemy import inspect

    def _inspect(sync_conn):
        inspector = inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        missing = []

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                # 表本身不存在会由 create_all 补上，不算漂移
                continue
            actual = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in actual:
                    missing.append(f"{table.name}.{column.name}")
        return missing

    async with async_engine.begin() as conn:
        return await conn.run_sync(_inspect)


async def close_db_engine():
    """关闭数据库引擎"""
    global async_engine
    if async_engine:
        await async_engine.dispose()
        async_engine = None
