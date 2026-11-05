"""租户知识库兜底配额测试（spec 2026-10-08 §8）。"""

from types import SimpleNamespace

import pytest

from yuxi.services.knowledge_quota_service import TenantKBQuotaExceededError, ensure_tenant_kb_quota


def _detail(*, access_level="department", file_count=499, total_size=100):
    return SimpleNamespace(
        kb_id="kb1",
        file_count=file_count,
        total_size=total_size,
        share_config={
            "version": 2,
            "read_scope": {"access_level": access_level, "department_ids": [7], "user_uids": []},
            "manage_scope": None,
        },
    )


@pytest.fixture
def env(monkeypatch):
    holder = {"detail": _detail()}
    opts = {"max_file_count": "500", "max_total_bytes": "2147483648"}

    class _Opts:
        async def get(self):
            return dict(opts)

    from yuxi.config import options as options_mod
    from yuxi.knowledge import runtime as kb_runtime

    async def fake_info(kb_id):
        return holder.get("detail")

    monkeypatch.setattr(kb_runtime, "knowledge_base", SimpleNamespace(get_database_info=fake_info))
    monkeypatch.setattr(options_mod, "spe_kb_quota_opts", _Opts())
    return SimpleNamespace(holder=holder, opts=opts)


async def test_file_count_over_quota_rejected(env):
    with pytest.raises(TenantKBQuotaExceededError) as info:
        await ensure_tenant_kb_quota("kb1", incoming_count=2)
    assert "文件数" in str(info.value)


async def test_bytes_over_quota_rejected(env):
    env.holder["detail"] = _detail(file_count=1, total_size=2147483647)
    with pytest.raises(TenantKBQuotaExceededError) as info:
        await ensure_tenant_kb_quota("kb1", incoming_count=1, incoming_bytes=10)
    assert "字节" in str(info.value)


async def test_official_kb_exempt(env):
    env.holder["detail"] = _detail(access_level="global", file_count=10**9, total_size=10**12)
    await ensure_tenant_kb_quota("kb1", incoming_count=5, incoming_bytes=999)  # 不抛


async def test_zero_config_disables_quota(env):
    env.opts.update(max_file_count="0", max_total_bytes="0")
    env.holder["detail"] = _detail(file_count=10**9, total_size=10**12)
    await ensure_tenant_kb_quota("kb1", incoming_count=5, incoming_bytes=999)  # 不抛


async def test_within_quota_passes(env):
    await ensure_tenant_kb_quota("kb1", incoming_count=1, incoming_bytes=50)  # 不抛


async def test_dirty_config_treated_as_unlimited(env):
    """防御分支：配置脏值（int() 抛 TypeError/ValueError）按不限制处理，不阻断业务。"""
    env.holder["detail"] = _detail(file_count=10**9, total_size=10**12)
    env.opts.update(max_file_count="abc", max_total_bytes=["not-int"])  # ValueError / TypeError
    await ensure_tenant_kb_quota("kb1", incoming_count=5, incoming_bytes=999)  # 不抛


async def test_kb_not_found_returns_silently(monkeypatch, env):
    """防御分支：库不存在直接 return（由后续权限/登记链路报错），且不读取配额配置。"""
    from yuxi.config import options as options_mod

    env.holder["detail"] = None

    async def _fail_get():
        raise AssertionError("库不存在时不应读取配额配置")

    monkeypatch.setattr(options_mod.spe_kb_quota_opts, "get", _fail_get)
    await ensure_tenant_kb_quota("missing", incoming_count=1, incoming_bytes=10)  # 不抛、不取配置
