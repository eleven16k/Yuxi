"""SPE 账号联邦登录：外部身份源映射、自动开户与凭据绑定。

流程：调 SPE /api/v1/auth/login 验证用户名密码 → 按 `spe_<用户名>` 命名空间
映射/自动开户 Yuxi 用户（本地密码哈希为不可用随机值——身份源唯一是 SPE，
每次登录按 SPE 角色同步 Yuxi 角色，映射策略含超管全映射，2026-10-07 用户
拍板）→ SPE JWT 写入绑定表（登录即绑定，聊天内 SPE 工具直接可用）。
部门自动归入「SPE 外部用户」。密码永不落盘。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_login
from yuxi.config.options import spe_agent_opts
from yuxi.repositories import spe_credential_repository
from yuxi.storage.postgres.models_business import Department, User
from yuxi.utils.auth_utils import AuthUtils

SPE_USER_UID_PREFIX = "spe_"
SPE_DEPARTMENT_NAME = "SPE 外部用户"


def map_spe_role_to_yuxi(spe_role: Any) -> str:
    """SPE 角色 → Yuxi 角色（含超管全映射，用户 2026-10-07 拍板）。

    容忍大小写与分隔差异：含 SUPER→superadmin；其余含 ADMIN→admin；
    未知/缺失一律 user（fail-safe 向下）。
    """
    text = str(spe_role or "").upper()
    if "SUPER" in text and "ADMIN" in text:
        return "superadmin"
    if "ADMIN" in text:
        return "admin"
    return "user"


def spe_uid(username: str) -> str:
    """SPE 用户名 → Yuxi uid（命名空间隔离，防与本地 uid 冲突）。"""
    return f"{SPE_USER_UID_PREFIX}{username}"


async def _resolve_department_id(db: AsyncSession) -> int:
    """SPE 外部用户部门 get-or-create。"""
    department = await db.scalar(select(Department).where(Department.name == SPE_DEPARTMENT_NAME))
    if department is not None:
        return department.id
    department = Department(name=SPE_DEPARTMENT_NAME)
    db.add(department)
    await db.flush()
    return department.id


async def login_with_spe(
    *,
    username: str,
    password: str,
    db: AsyncSession,
) -> tuple[User, dict[str, Any]]:
    """SPE 凭据换取已映射的 Yuxi 用户；失败以 401/502 语义上抛。

    返回 (user, spe_profile)；spe_profile 含 token/role/tenantId/username。
    """
    if not username.strip() or not password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SPE 用户名或密码错误")

    config = await spe_agent_opts.get()
    base_url = (config.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SPE 未配置：请管理员在系统配置（spe_agent_opts）设置 base_url 后再使用 SPE 账号登录",
        )

    try:
        spe_profile = await call_spe_login(base_url=base_url, username=username.strip(), password=password)
    except SpeToolError as e:
        if e.code in {"AUTH_FAILED", "BIZ_AUTH_FAILED"}:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SPE 用户名或密码错误") from e
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"SPE 登录失败：{e.message}") from e

    spe_username = spe_profile.get("username") or username.strip()
    uid = spe_uid(spe_username)
    user = await db.scalar(select(User).where(User.uid == uid, User.is_deleted == 0))
    if user is None:
        # username 唯一列：与本地同名时加后缀（登录标识是 uid/手机号，不受影响）
        display_name = spe_username
        if await db.scalar(select(User).where(User.username == display_name)):
            display_name = f"{spe_username}(SPE)"
        user = User(
            username=display_name,
            uid=uid,
            password_hash=AuthUtils.hash_password(secrets.token_urlsafe(32)),  # 不可用：身份源唯一是 SPE
            role=map_spe_role_to_yuxi(spe_profile.get("role")),
            department_id=await _resolve_department_id(db),
        )
        db.add(user)
        await db.flush()
    else:
        # 身份源拥有角色：每次登录同步，防止 SPE 侧降权后 Yuxi 侧残留
        user.role = map_spe_role_to_yuxi(spe_profile.get("role"))
    await db.refresh(user)

    # 登录即绑定：SPE JWT 入 per-user 凭据表，聊天内工具直接可用
    await spe_credential_repository.upsert_user_jwt(uid=uid, user_jwt=spe_profile["token"])
    return user, spe_profile
