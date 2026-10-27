"""破窗重置：直接改库里的密码哈希

## 这个脚本存在的理由

Web 后台里所有的密码操作都受同一条链约束：**要么知道当前密码，要么有更高的
角色**。这条链有一个终点 —— 总裁。没有人比他级别更高，所以一旦总裁账号被人
拿下并改掉密码，从 Web 那一侧就再也进不去了。

破窗走的是另一条完全不同的信任边界：**能登上这台服务器、能读到 .env.prod 里的
库口令的人**。攻击者拿到一个 Web 会话不等于拿到了这个。所以它是最后一道门，
而不是同一道门上的备用钥匙。

同理，这个脚本**没有也不该有**任何远程入口。要它能通过 HTTP 调用，
就等于把「最后一道门」搬回到被攻破的那一侧。

## 用法（必须在 server/ 目录下跑）

    python scripts/reset_account_password.py                 # 默认总裁账号
    python scripts/reset_account_password.py --username 张三
    python scripts/reset_account_password.py --uid 3f2a...
    python scripts/reset_account_password.py --show-only     # 只看，不改

⚠️ 工作目录必须是 server/：`get_settings()` 传给 pydantic 的是相对路径
（".env"、".env.prod"），在别处跑会以「mysql_url Field required」失败 ——
那句话看着像 .env 写错了，其实是目录不对。

## 为什么不是「删掉账号让它重建」

删账号会连带丢掉这个 uid 关联的一切：审计里「谁查阅了什么」、用量归属、
API Key。重建出来的是个同名但不同 uid 的新人，历史记录从此对不上号，
而且没有任何地方会提示这件事发生过。改密码就只是改密码。
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import close_db_engine, init_async_session, init_db_engine  # noqa: E402
from model.database import ROLE_PRESIDENT, User  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from utils.crypt import hash_password  # noqa: E402
from utils.generate import generate_password  # noqa: E402


async def _find(session, *, uid: str, username: str):
    if uid:
        stmt = select(User).where(User.uid == uid)
    elif username:
        stmt = select(User).where(User.username == username)
    else:
        stmt = select(User).where(User.permission == ROLE_PRESIDENT).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _revoke(uid: str) -> str:
    """踢掉现有会话与 API Key。

    这一步失败**不阻断改密码**：破窗的场景通常就是「出事了」，此刻 Redis
    连不上是完全可能的，而拿回账号比清干净会话更紧急。但要说清楚没做成 ——
    悄悄跳过的话，管理员会以为攻击者已经被踢下线了。
    """
    notes = []
    try:
        from utils.token_manager import get_token_manager

        count = await get_token_manager().revoke_user_tokens(uid, reason="break_glass_reset")
        notes.append(f"已吊销 {count} 个登录会话")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"⚠️ 会话吊销失败（密码已改）：{exc}")

    try:
        from db.updates.apikey import revoke_user_api_keys

        await revoke_user_api_keys(uid, reason="password_reset")
        notes.append("已吊销该账号全部 API Key")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"⚠️ API Key 吊销失败（密码已改）：{exc}")

    return "\n".join(f"  {n}" for n in notes)


async def _run(*, uid: str, username: str, show_only: bool) -> int:
    await init_db_engine()
    session_maker = await init_async_session()
    try:
        async with session_maker() as session:
            row = await _find(session, uid=uid, username=username)
            if row is None:
                target = uid or username or "（总裁账号）"
                print(f"没有找到账号：{target}")
                return 1

            print(f"账号：『{row.username}』  uid={row.uid}  角色={row.permission}  状态={row.status}")
            if show_only:
                return 0

            pwd = generate_password()
            await session.execute(
                update(User).where(User.id == row.id).values(password=hash_password(pwd))
            )
            await session.commit()

            notes = await _revoke(row.uid)

            # 与 _seed_president 一样用『』框住：密码里常有 . : $ 等符号，
            # 不框起来容易连着标点一起复制。
            print(f"新密码：『{pwd}』（长度 {len(pwd)}）")
            if notes:
                print(notes)
            print("这条只打印一次 —— 现在就登录后台改成自己的。")
            if row.status != 1:
                print("⚠️ 该账号当前是禁用状态，改了密码也登不上，需要先启用。")
            return 0
    finally:
        await close_db_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description="破窗重置账号密码（需服务器权限）")
    parser.add_argument("--uid", default="", help="按 uid 精确指定")
    parser.add_argument("--username", default="", help="按用户名指定")
    parser.add_argument("--show-only", action="store_true", help="只显示账号信息，不改密码")
    args = parser.parse_args()
    return asyncio.run(
        _run(uid=args.uid.strip(), username=args.username.strip(), show_only=args.show_only)
    )


if __name__ == "__main__":
    raise SystemExit(main())
