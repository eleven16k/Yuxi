"""SPE 算力积分服务测试（agent-compute-credit 卡2）。

契约：
1. 闸门：余额<=100 厘 → HTTPException(403, code=credit_insufficient)；>100 放行
2. superadmin 豁免（不发 HTTP）；无 JWT 放行；SPE 5xx 放行（漏报原则）
3. 余额缓存 60s：第二次调用不再发 HTTP
4. 估算：镜像 {fxRateCnyUsd, models[]} × 分模型桶 → 厘（不 ceil）
5. 上报：成功回填 credits_li；重试 3 次（5/30/120s→测试中 monkeypatch 短延迟）；终败落失败表
6. refId = agent-yuxi-run-<run_id>，payload 键与契约节一致
7. 补充契约点：SPE_CREDIT_GATE_DISABLED=1 直接放行；价表镜像 TTL（TTL 内零 HTTP）；
   估算降级（mirror=None → estimate_credits_li 返回 None）
8. 真实结构保真：以 TokenUsageMiddleware._add_call_to_aggregate 的真实产物形状
   （models[bucket_key] = {"model": {...response_model_ids}, "usage": {...}, "cache_read_input_tokens"}）
   走 buckets_from_run_usage，断言 model 名提取链与桶数值
9. 重试期间 3 次请求 refId 相同且 len(refId) <= 100
"""

import pytest
from fastapi import HTTPException

from yuxi.services import spe_credit_service as svc


@pytest.fixture()
def fresh(monkeypatch):
    monkeypatch.setattr(svc, "_balance_cache", {})
    monkeypatch.setattr(svc, "_price_mirror", None)
    monkeypatch.setattr(svc, "_price_mirror_at", 0.0)
    monkeypatch.setattr(svc, "_REPORT_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))
    yield svc


@pytest.fixture()
def spe_env(monkeypatch):
    """spe_agent_opts.get() 返回 base_url 的假 Option。"""

    class FakeOpts:
        async def get(self, db=None):
            return {"base_url": "http://spe.test"}

    monkeypatch.setattr(svc, "spe_agent_opts", FakeOpts())


async def test_gate_rejects_below_threshold(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 50})
    with pytest.raises(HTTPException) as ei:
        await svc.ensure_run_allowed(uid="u1", role="user")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "credit_insufficient"
    assert ei.value.detail["balanceLi"] == 50


async def test_gate_allows_above_threshold(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 500})
    await svc.ensure_run_allowed(uid="u1", role="user")  # 不抛


async def test_gate_superadmin_exempt_no_http(fresh, spe_env, httpx_mock):
    await svc.ensure_run_allowed(uid="u1", role="superadmin")
    assert httpx_mock.get_requests() == []


async def test_gate_spe_down_allows(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", status_code=500)
    await svc.ensure_run_allowed(uid="u1", role="user")  # 放行不抛


async def test_gate_no_jwt_allows(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return(None))
    await svc.ensure_run_allowed(uid="u1", role="user")
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("disabled_value", ["1", "true", "TRUE", "Yes", "yes"])
async def test_gate_disabled_env_allows_without_http(fresh, spe_env, httpx_mock, monkeypatch, disabled_value):
    """kill-switch 别名对齐 octop：1/true/yes 大小写不敏感。"""
    monkeypatch.setenv("SPE_CREDIT_GATE_DISABLED", disabled_value)
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    await svc.ensure_run_allowed(uid="u1", role="user")
    assert httpx_mock.get_requests() == []


async def test_gate_disabled_false_value_still_gates(fresh, spe_env, httpx_mock, monkeypatch):
    """非关闸取值（如 false）不触发 kill-switch：闸门照常走余额检查。"""
    monkeypatch.setenv("SPE_CREDIT_GATE_DISABLED", "false")
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 500})
    await svc.ensure_run_allowed(uid="u1", role="user")  # 放行：走了余额检查
    assert len(httpx_mock.get_requests()) == 1


async def test_gate_credential_store_failure_allows(fresh, spe_env, httpx_mock, monkeypatch):
    """凭据存储故障（如 DB 不可用）也必须放行：SPE 链路任何故障不阻断聊天。"""

    async def boom(uid):
        raise RuntimeError("credential store down")

    monkeypatch.setattr(svc, "get_user_jwt", boom)
    await svc.ensure_run_allowed(uid="u1", role="user")  # 放行不抛
    assert httpx_mock.get_requests() == []


async def test_gate_balance_cached_60s(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 500})
    await svc.ensure_run_allowed(uid="u1", role="user")
    await svc.ensure_run_allowed(uid="u1", role="user")
    assert len(httpx_mock.get_requests()) == 1


async def test_price_mirror_ttl_zero_http_within_ttl(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(
        url="http://spe.test/api/v1/credit/external/prices",
        json={"models": [], "fxRateCnyUsd": 7.2, "creditRate": 100},
    )
    first = await svc.price_mirror(uid="u1")
    second = await svc.price_mirror(uid="u1")
    assert first is not None and second is first
    assert len(httpx_mock.get_requests()) == 1
    # TTL 归零后重新拉取
    monkeypatch.setattr(svc, "_PRICE_MIRROR_TTL_SECONDS", 0.0)
    httpx_mock.add_response(
        url="http://spe.test/api/v1/credit/external/prices",
        json={"models": [], "fxRateCnyUsd": 7.2, "creditRate": 100},
    )
    third = await svc.price_mirror(uid="u1")
    assert third is not None
    assert len(httpx_mock.get_requests()) == 2


async def test_estimate_matches_contract_formula(fresh):
    mirror = {
        "fxRateCnyUsd": 7.2,
        "models": [
            {
                "pattern": "glm-5*",
                "inputUsdPerMtok": 1.0,
                "outputUsdPerMtok": 5.0,
                "cacheReadUsdPerMtok": 0.1,
                "margin": 1.5,
            }
        ],
    }
    usage = {"glm-5.2": {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 0}}
    li = svc.estimate_credits_li(mirror, usage)
    # (1_000_000*1.0 + 100_000*5.0)/1e6 = 1.5 usd → 1.5 × margin1.5 × fx7.2 × 1000
    assert li == pytest.approx(1.5 * 7.2 * 1000 * 1.5)


def test_estimate_degrades_without_mirror(fresh):
    usage = {"glm-5.2": {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 0}}
    assert svc.estimate_credits_li(None, usage) is None


def test_estimate_star_fallback_row_and_cache_read(fresh):
    mirror = {
        "fxRateCnyUsd": 7.0,
        "models": [
            {"pattern": "*", "inputUsdPerMtok": 2.0, "outputUsdPerMtok": 4.0, "cacheReadUsdPerMtok": 0.5, "margin": 1.0}
        ],
    }
    usage = {"mystery-model": {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 1_000_000}}
    li = svc.estimate_credits_li(mirror, usage)
    # '*' 兜底：1M×2.0 + 1M×0.5 = 2.5 usd → 2.5 × 1.0 × 7.0 × 1000
    assert li == pytest.approx(2.5 * 7.0 * 1000)


def test_buckets_from_real_middleware_aggregate(fresh):
    """以 TokenUsageMiddleware._add_call_to_aggregate 的真实产物形状走提取链。"""
    from yuxi.agents.middlewares import token_usage as tu

    aggregate = tu._empty_aggregate()
    aggregate = tu._add_call_to_aggregate(
        aggregate,
        bucket_key="response:openai:glm-5.2",
        identity_source="response_metadata",
        identity={"provider_type": "openai", "response_model_id": "glm-5.2"},
        usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": 900},
        },
    )
    aggregate = tu._add_call_to_aggregate(
        aggregate,
        bucket_key="deepseek-v4-flash",
        identity_source="configured_metadata",
        identity={"configured_model_spec": "deepseek-v4-flash"},
        usage={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
    )
    run_usage = {"models": aggregate["models"], "total": aggregate.get("total", {})}

    buckets = svc.buckets_from_run_usage(run_usage)
    by_model = {b["model"]: b for b in buckets}
    # model 名提取：response_model_ids[0] 优先
    assert by_model["glm-5.2"] == {"model": "glm-5.2", "inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 900}
    # configured_model_spec 作为 bucket_key 时原样透出
    assert by_model["deepseek-v4-flash"]["inputTokens"] == 10


def test_buckets_model_fallback_to_configured_model_id(fresh):
    run_usage = {
        "models": {
            "configured:whatever": {
                "model": {"configured_model_id": "glm-5.2", "identity_source": "configured_metadata"},
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                "cache_read_input_tokens": 0,
            }
        }
    }
    buckets = svc.buckets_from_run_usage(run_usage)
    assert buckets[0]["model"] == "glm-5.2"
    assert buckets[0]["outputTokens"] == 2


async def test_report_success_persists_credits(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(
        url="http://spe.test/api/v1/credit/external/deduct",
        json={"chargedLi": 315, "balanceAfter": 4200, "replay": False, "detail": []},
    )
    persisted = {}

    async def fake_persist(run_id, credits_li):
        persisted["args"] = (run_id, credits_li)

    monkeypatch.setattr(svc, "_persist_run_credits", fake_persist)
    await svc.report_run_usage(
        uid="u1",
        role="user",
        run_id="r1",
        usage_by_model={
            "models": {
                "glm-5.2": {"usage": {"input_tokens": 1000, "output_tokens": 200}, "cache_read_input_tokens": 900}
            }
        },
    )
    assert persisted["args"] == ("r1", 315)
    req = httpx_mock.get_requests()[0]
    body = req.read()
    assert b"agent-yuxi-run-r1" in body
    assert b"cacheReadTokens" in body and b"900" in body


async def test_report_retries_then_failure_table(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    for _ in range(3):
        httpx_mock.add_response(url="http://spe.test/api/v1/credit/external/deduct", status_code=503)
    failed = {}

    async def fake_failed(run_id, payload, error):
        failed["args"] = (run_id, payload["refId"], error)

    monkeypatch.setattr(svc, "_persist_failed_report", fake_failed)
    await svc.report_run_usage(
        uid="u1",
        role="user",
        run_id="r1",
        usage_by_model={
            "models": {"m": {"usage": {"input_tokens": 10, "output_tokens": 1}, "cache_read_input_tokens": 0}}
        },
    )
    assert failed["args"][0] == "r1"
    assert failed["args"][1] == "agent-yuxi-run-r1"
    assert failed["args"][2]  # 有失败原因
    assert len(httpx_mock.get_requests()) == 3


async def test_report_retry_keeps_same_refId_within_budget(fresh, spe_env, httpx_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/external/deduct", status_code=500)
    httpx_mock.add_response(
        url="http://spe.test/api/v1/credit/external/deduct",
        json={"chargedLi": 8, "balanceAfter": 100, "replay": False, "detail": []},
    )
    monkeypatch.setattr(svc, "_persist_run_credits", _async_return(None))
    await svc.report_run_usage(
        uid="u1",
        role="user",
        run_id="r1",
        usage_by_model={
            "models": {"m": {"usage": {"input_tokens": 10, "output_tokens": 1}, "cache_read_input_tokens": 0}}
        },
    )
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    import json as _json

    bodies = [_json.loads(r.read()) for r in requests]
    assert {b["refId"] for b in bodies} == {"agent-yuxi-run-r1"}
    assert all(len(b["refId"]) <= 100 for b in bodies)


async def test_report_superadmin_skipped(fresh, spe_env, httpx_mock):
    await svc.report_run_usage(
        uid="u1",
        role="superadmin",
        run_id="r1",
        usage_by_model={
            "models": {"m": {"usage": {"input_tokens": 10, "output_tokens": 1}, "cache_read_input_tokens": 0}}
        },
    )
    assert httpx_mock.get_requests() == []


async def test_report_success_refreshes_balance_cache_with_balance_after(
    fresh, spe_env, httpx_mock, monkeypatch
):
    """结算响应增量刷新余额（联合终审）：deduct 的 balanceAfter 直接写缓存，
    再次过闸不再发余额请求。"""
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 500})
    await svc.ensure_run_allowed(uid="u1", role="user")
    httpx_mock.add_response(
        url="http://spe.test/api/v1/credit/external/deduct",
        json={"chargedLi": 315, "balanceAfter": 185, "replay": False, "detail": []},
    )
    monkeypatch.setattr(svc, "_persist_run_credits", _async_return(None))
    await svc.report_run_usage(
        uid="u1",
        role="user",
        run_id="r1",
        usage_by_model={
            "models": {"m": {"usage": {"input_tokens": 10, "output_tokens": 1}, "cache_read_input_tokens": 0}}
        },
    )
    # balanceAfter=185 已入缓存：读余额零 HTTP，且值就是结算响应值
    assert await svc.balance_li(uid="u1") == 185
    await svc.ensure_run_allowed(uid="u1", role="user")
    balance_calls = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/balance")]
    assert len(balance_calls) == 1


async def test_report_success_without_balance_after_invalidates(fresh, spe_env, httpx_mock, monkeypatch):
    """响应缺 balanceAfter 时回退为缓存失效：下次过闸重新拉余额。"""
    monkeypatch.setattr(svc, "get_user_jwt", _async_return("jwt-1"))
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 500})
    await svc.ensure_run_allowed(uid="u1", role="user")
    httpx_mock.add_response(
        url="http://spe.test/api/v1/credit/external/deduct",
        json={"chargedLi": 315, "replay": False, "detail": []},
    )
    httpx_mock.add_response(url="http://spe.test/api/v1/credit/balance", json={"creditBalance": 185})
    monkeypatch.setattr(svc, "_persist_run_credits", _async_return(None))
    await svc.report_run_usage(
        uid="u1",
        role="user",
        run_id="r1",
        usage_by_model={
            "models": {"m": {"usage": {"input_tokens": 10, "output_tokens": 1}, "cache_read_input_tokens": 0}}
        },
    )
    await svc.ensure_run_allowed(uid="u1", role="user")
    balance_calls = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/balance")]
    assert len(balance_calls) == 2


async def test_report_best_effort_swallows_errors(fresh, spe_env, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(svc, "report_run_usage", boom)
    # 不向上抛异常（fire-and-forget 安全）
    await svc.report_run_usage_best_effort(uid="u1", role="user", run_id="r1", usage_by_model={"models": {}})


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner
