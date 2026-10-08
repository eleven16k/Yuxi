"""SPE 账号联邦登录：外部身份源映射、自动开户与凭据绑定。

流程：调 SPE /api/v1/auth/login 验证用户名密码 → 按 `spe_<用户名>` 命名空间
映射/自动开户 Yuxi 用户（本地密码哈希为不可用随机值——身份源唯一是 SPE，
每次登录按 SPE 角色同步 Yuxi 角色，映射策略已收紧：SUPER_ADMIN 映射 user，
ADMIN 映射 tenant_admin（部门限管理），租户侧永不获得 superadmin，
2026-10-08 修订）→ SPE JWT 写入绑定表（登录即
绑定，聊天内 SPE 工具直接可用）。
部门按 SPE tenantId get-or-create（部门=SPE 租户）；tenantId 缺失回退旧「SPE 外部用户」大部门。密码永不落盘。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.toolkits.spe.client import SpeToolError, call_spe_credit_balance, call_spe_login
from yuxi.config.options import spe_agent_opts
from yuxi.repositories import spe_credential_repository
from yuxi.storage.postgres.models_business import Department, User
from yuxi.utils.auth_utils import AuthUtils

SPE_USER_UID_PREFIX = "spe_"
SPE_DEPARTMENT_NAME = "SPE 外部用户"
SPE_TENANT_DEPARTMENT_PREFIX = "SPE租户-"
# 租户库创建者系统身份：避免首登成员经 created_by 拥有者分支获得越角色写权（spec §6.1）
SPE_TENANT_KB_CREATOR = "spe_system"


def map_spe_role_to_yuxi(spe_role: Any) -> str:
    """SPE 角色 → Yuxi 角色（2026-10-08 spec §4.2 修订）。

    容忍大小写与分隔差异：仅 ADMIN（非 SUPER）→ tenant_admin（部门限管理，
    不入 isAdmin 集合、不进平台管理界面）；SUPER_ADMIN/未知/缺失一律 user
    （fail-safe 向下）。平台运营以本地账号使用 superadmin/admin。
    """
    text = str(spe_role or "").upper()
    if "SUPER" in text:
        return "user"
    if "ADMIN" in text:
        return "tenant_admin"
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


async def _resolve_tenant_department_id(db: AsyncSession, tenant_id: Any) -> int:
    """按 SPE tenantId get-or-create 租户部门（部门=SPE 租户，spec 2026-10-08 §4.1）。

    tenantId 缺失/为空时回退旧「SPE 外部用户」大部门（无租户语义，fail-safe）；
    0 是显式租户值，不吞入回退。
    """
    if tenant_id is None:
        return await _resolve_department_id(db)
    value = str(tenant_id).strip()
    if not value:
        return await _resolve_department_id(db)
    name = f"{SPE_TENANT_DEPARTMENT_PREFIX}{value}"
    department = await db.scalar(select(Department).where(Department.name == name))
    if department is not None:
        return department.id
    department = Department(name=name, description=f"SPE 租户 {value}（联邦登录自动创建）")
    db.add(department)
    await db.flush()
    return department.id


async def _ensure_tenant_kb(*, uid: str, department_id: int, tenant_id: Any) -> None:
    """租户库 get-or-create（spec 2026-10-08 §6.1）：精确单部门匹配即视为已有。

    失败不阻断登录——只记日志，下次登录 get-or-create 自愈。
    """
    import logging

    try:
        from yuxi.knowledge.base import KBNameConflictError
        from yuxi.knowledge.runtime import knowledge_base

        for kb in await knowledge_base.get_databases():
            scope = (kb.share_config or {}).get("read_scope") or {}
            if scope.get("access_level") == "department" and scope.get("department_ids") == [int(department_id)]:
                return

        try:
            await knowledge_base.create_database(
                f"SPE租户-{tenant_id}知识库",
                f"租户 {tenant_id} 的共享知识库：商品资料、SOP、俄语文案素材（联邦登录自动创建）。",
                kb_type="milvus",
                embedding_model_spec="minimax:embo-01",
                share_config={
                    "version": 2,
                    "read_scope": {"access_level": "department", "department_ids": [int(department_id)], "user_uids": []},
                    "manage_scope": {"access_level": "department", "department_ids": [int(department_id)], "user_uids": []},
                },
                created_by=SPE_TENANT_KB_CREATOR,
                created_by_department_id=department_id,
            )
        except KBNameConflictError:
            # 同名库已存在：可能是并发首登（确定性命名即同租户库），也可能是
            # scope 不匹配的同名库挡住了供给——后者会让租户永远建不出库，必须留痕
            logging.getLogger(__name__).warning(
                "租户知识库供给撞名（tenant_id=%s, department_id=%s）——若该库 scope 不匹配请人工核查",
                tenant_id, department_id,
            )
            return
    except Exception as e:  # noqa: BLE001 —— 开户不被 KB 故障拖垮
        logging.getLogger(__name__).warning("联邦登录确保租户知识库失败（uid=%s）：%s", uid, e)


async def _rescope_personal_kbs(*, uid: str, department_id: int) -> None:
    """非空个人库懒改档（spec 2026-10-08 §6.2）：user 档 → 本租户部门档，并入租户知识中心。

    空库交给部署脚本清理；改档不迁移文件。失败不阻断登录。
    """
    import logging

    try:
        from yuxi.knowledge.runtime import knowledge_base
        from yuxi.permissions import normalize_permission_config
        from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

        department_scope = {"access_level": "department", "department_ids": [int(department_id)], "user_uids": []}
        for kb in await knowledge_base.get_databases():
            scope = (kb.share_config or {}).get("read_scope") or {}
            is_own_personal = (
                kb.created_by == uid
                and scope.get("access_level") == "user"
                and uid in (scope.get("user_uids") or [])
            )
            if not is_own_personal or (kb.file_count or 0) <= 0:
                continue
            config = normalize_permission_config(
                {"version": 2, "read_scope": department_scope, "manage_scope": department_scope}
            )
            await KnowledgeBaseRepository().update(kb.kb_id, {"share_config": config})
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("联邦登录个人库改档失败（uid=%s）：%s", uid, e)


async def ensure_tenant_provisioning(*, uid: str, department_id: int | None, tenant_id: Any) -> None:
    """租户域供给入口（spec 2026-10-08 §6）：必须在登录事务提交之后调用。

    部门行由登录事务创建——供给若先于 commit 执行，独立会话落库的租户库可能
    指向回滚后消失的部门（并发首登孤儿库，撞名静默自阻塞），故由路由在
    commit 后调用本入口。tenantId 缺失/为空或无部门时跳过（无租户语义）。
    """
    if tenant_id is None or not str(tenant_id).strip() or department_id is None:
        return
    await _ensure_tenant_kb(uid=uid, department_id=department_id, tenant_id=tenant_id)
    await _rescope_personal_kbs(uid=uid, department_id=department_id)


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

    # 入口闸（spec §8）：零余额租户不进入工作台、也不为其开户；顺序先于用户查建
    try:
        balance = await call_spe_credit_balance(base_url=base_url, user_jwt=str(spe_profile["token"]))
    except SpeToolError as e:
        if e.code in {"AUTH_FAILED", "BIZ_AUTH_FAILED"}:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SPE 登录态失效，请重试") from e
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"积分查询失败：{e.message}") from e
    if balance["creditBalance"] <= 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="积分余额不足：请先在 SPE 平台充值后再使用智能工作台",
        )

    spe_username = spe_profile.get("username") or username.strip()
    tenant_id = spe_profile.get("tenantId")
    uid = spe_uid(spe_username)
    department_id = await _resolve_tenant_department_id(db, tenant_id)
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
            department_id=department_id,
        )
        db.add(user)
        await db.flush()
    else:
        # 身份源拥有角色与租户归属：每次登录同步，防止 SPE 侧降权/换租后 Yuxi 侧残留
        user.role = map_spe_role_to_yuxi(spe_profile.get("role"))
        user.department_id = department_id
    await db.refresh(user)

    # 登录即绑定：SPE JWT 入 per-user 凭据表，聊天内工具直接可用
    # （同事务写入——新用户行尚未提交，凭据行必须与用户行同进同出）
    await spe_credential_repository.upsert_user_jwt(uid=uid, user_jwt=spe_profile["token"], db=db)
    return user, spe_profile
