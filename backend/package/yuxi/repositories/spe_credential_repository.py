"""SPE 用户凭据数据访问层。

每个方法自开会话（知识库 repository 同款模式）；user_jwt 只进不出——读取
仅返回原文供出站 Bearer 使用，永不经 API 回显。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import UserSpeCredential
from yuxi.utils.datetime_utils import utc_now_naive


async def get_user_jwt(uid: str) -> str | None:
    """读取指定用户的 SPE JWT；未登录返回 None。"""
    async with pg_manager.get_async_session_context() as session:
        result = await session.execute(select(UserSpeCredential.user_jwt).where(UserSpeCredential.uid == uid))
        return result.scalar_one_or_none()


async def upsert_user_jwt(*, uid: str, user_jwt: str, db: AsyncSession | None = None) -> None:
    """login 成功后覆盖式写入该用户的 SPE JWT（二次调用即换号）。

    传入 db 时加入调用方事务（联邦登录的新用户行尚未提交，自开会话会因
    FK 看不见该行而违反外键——真实 PG 集成下必现，2026-10-07 冒烟发现）；
    不传则自开会话并自行提交（独立调用方语义不变）。
    """
    statement = (
        pg_insert(UserSpeCredential)
        .values(uid=uid, user_jwt=user_jwt, updated_at=utc_now_naive())
        .on_conflict_do_update(
            index_elements=[UserSpeCredential.uid],
            set_={"user_jwt": user_jwt, "updated_at": utc_now_naive()},
        )
    )
    if db is not None:
        await db.execute(statement)
        return
    async with pg_manager.get_async_session_context() as session:
        await session.execute(statement)
        await session.commit()


async def delete_user_jwt(uid: str) -> None:
    """logout：删除该用户的 SPE JWT。"""
    async with pg_manager.get_async_session_context() as session:
        await session.execute(delete(UserSpeCredential).where(UserSpeCredential.uid == uid))
        await session.commit()
