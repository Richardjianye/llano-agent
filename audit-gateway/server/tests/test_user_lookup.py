"""用户查询条件的回归测试

起因：登录接口曾经写成 `get_user_info(id=request.username)`，
把用户名当自增主键查，导致任何账号都登录不上（统一返回「用户名或密码错误」）。
外加旧实现固定拼 `or_(uid == ?, id == ?)`，只传一个参数时另一个会退化成 `IS NULL`，
可能命中 uid 为空的脏数据行。

这两条都不依赖数据库就能测——编译出 SQL 直接看条件对不对。
"""

from db.selects.user import build_user_lookup_conditions
from model.database import User
from sqlalchemy import or_, select


def _sql(conditions) -> str:
    return str(select(User).where(or_(*conditions)).compile())


def test_lookup_by_uid_does_not_leak_is_null():
    conditions = build_user_lookup_conditions(uid="abc123")
    assert len(conditions) == 1
    sql = _sql(conditions)
    assert "users.uid" in sql
    # 关键：不能出现 id IS NULL 这种把脏数据捞出来的条件
    assert "IS NULL" not in sql.upper()


def test_lookup_by_id_only():
    conditions = build_user_lookup_conditions(id=7)
    assert len(conditions) == 1
    sql = _sql(conditions)
    assert "users.id" in sql
    assert "IS NULL" not in sql.upper()


def test_lookup_with_both():
    assert len(build_user_lookup_conditions(uid="abc", id=7)) == 2


def test_lookup_without_args_returns_nothing():
    """两个都不传时返回空条件，调用方据此直接返回 None，绝不能查出全表"""
    assert build_user_lookup_conditions() == []


def test_username_is_not_a_valid_id_lookup():
    """用户名是中文花名，塞进 id 只会得到一条永远查不中的条件

    这里固定住语义：想按用户名查就得走 get_user_by_username。
    """
    conditions = build_user_lookup_conditions(id="admin")
    # select(User) 的投影天然包含 users.username；这里只编译 WHERE 条件，
    # 才能验证查询没有误用 username 列。
    sql = str(or_(*conditions).compile())
    assert "users.username" not in sql
