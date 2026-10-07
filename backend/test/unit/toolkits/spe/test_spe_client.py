"""client.py 出站契约测试（C-11 + 会话创建，假后端）。"""

import json

import httpx
import pytest

from yuxi.agents.toolkits.spe.client import (
    SpeToolError,
    call_spe_biz_get,
    call_spe_biz_mutate,
    call_spe_login,
    call_spe_tool,
    create_spe_session,
)

BASE = "http://spe.test"
TOOL_URL = f"{BASE}/api/v1/agent/tools/list_orders"
SESSION_URL = f"{BASE}/api/v1/agent/sessions"


# ---------- call_spe_tool ----------


async def test_tool_success_full_envelope(httpx_mock) -> None:
    httpx_mock.add_response(
        url=TOOL_URL,
        json={
            "ok": True,
            "data": {"blockId": "blk_1", "ui": "product-table", "v": 2, "data": {"rows": []}},
        },
    )
    block = await call_spe_tool(
        base_url=BASE,
        service_token="svc-token",
        session_id="sess_1",
        tool_name="list_orders",
        args={"page": 0},
    )
    assert block == {
        "blockId": "blk_1",
        "ui": "product-table",
        "v": 2,
        "data": {"rows": []},
    }
    request = httpx_mock.get_requests()[0]
    assert request.headers["X-Agent-Token"] == "svc-token"
    body = json.loads(request.content)
    assert body == {"sessionId": "sess_1", "args": {"page": 0}}


async def test_tool_success_minimal_envelope_defaults(httpx_mock) -> None:
    """缺 blockId/v 时回落空串与 1（桥同款容错）。"""
    httpx_mock.add_response(url=TOOL_URL, json={"ok": True, "data": {"ui": "panel"}})
    block = await call_spe_tool(
        base_url=BASE + "/",  # 尾斜杠被剥掉
        service_token="t",
        session_id="s",
        tool_name="list_orders",
    )
    assert block == {"blockId": "", "ui": "panel", "v": 1, "data": None}


async def test_tool_rejects_envelope_without_ui(httpx_mock) -> None:
    httpx_mock.add_response(url=TOOL_URL, json={"ok": True, "data": {"blockId": "blk"}})
    with pytest.raises(SpeToolError) as info:
        await call_spe_tool(base_url=BASE, service_token="t", session_id="s", tool_name="list_orders")
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "信封不合法" in info.value.message


async def test_tool_error_envelope_passthrough(httpx_mock) -> None:
    httpx_mock.add_response(
        url=TOOL_URL,
        status_code=400,
        json={"ok": False, "error": {"code": "BIZ_NO_STORE", "message": "未绑定店铺"}},
    )
    with pytest.raises(SpeToolError) as info:
        await call_spe_tool(base_url=BASE, service_token="t", session_id="s", tool_name="list_orders")
    assert info.value.code == "BIZ_NO_STORE"
    assert info.value.message == "未绑定店铺"


async def test_tool_error_envelope_missing_fields(httpx_mock) -> None:
    httpx_mock.add_response(url=TOOL_URL, json={"ok": False})
    with pytest.raises(SpeToolError) as info:
        await call_spe_tool(base_url=BASE, service_token="t", session_id="s", tool_name="list_orders")
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert info.value.message == "未知业务错误"


async def test_tool_non_json_response(httpx_mock) -> None:
    httpx_mock.add_response(url=TOOL_URL, status_code=502, text="<html>bad gateway</html>")
    with pytest.raises(SpeToolError) as info:
        await call_spe_tool(base_url=BASE, service_token="t", session_id="s", tool_name="list_orders")
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "非 JSON" in info.value.message
    assert "502" in info.value.message


async def test_tool_timeout_classified(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=TOOL_URL)
    with pytest.raises(SpeToolError) as info:
        await call_spe_tool(base_url=BASE, service_token="t", session_id="s", tool_name="list_orders")
    assert info.value.code == "TOOL_UPSTREAM_TIMEOUT"
    assert "超时" in info.value.message


async def test_tool_network_error_classified(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=TOOL_URL)
    with pytest.raises(SpeToolError) as info:
        await call_spe_tool(base_url=BASE, service_token="t", session_id="s", tool_name="list_orders")
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "连接失败" in info.value.message


# ---------- create_spe_session ----------


async def test_session_success_primary_field(httpx_mock) -> None:
    httpx_mock.add_response(url=SESSION_URL, json={"ok": True, "sessionId": "sess_9"})
    session_id = await create_spe_session(base_url=BASE, user_jwt="jwt-abc", title="yuxi u1")
    assert session_id == "sess_9"
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer jwt-abc"
    assert json.loads(request.content) == {"title": "yuxi u1"}


async def test_session_id_fallback_fields(httpx_mock) -> None:
    httpx_mock.add_response(url=SESSION_URL, json={"id": "sess_id"})
    assert await create_spe_session(base_url=BASE, user_jwt="j", title="t") == "sess_id"
    httpx_mock.add_response(url=SESSION_URL, json={"data": {"id": "sess_data"}})
    assert await create_spe_session(base_url=BASE, user_jwt="j", title="t") == "sess_data"


async def test_session_envelope_rejection_priority(httpx_mock) -> None:
    """ok:false 信封优先于 HTTP 状态码。"""
    httpx_mock.add_response(
        url=SESSION_URL,
        status_code=401,
        json={"ok": False, "error": {"code": "BIZ_AUTH_FAILED", "message": "token 过期"}},
    )
    with pytest.raises(SpeToolError) as info:
        await create_spe_session(base_url=BASE, user_jwt="bad", title="t")
    assert info.value.code == "SESSION_CREATE_FAILED"
    assert "BIZ_AUTH_FAILED" in info.value.message
    assert "token 过期" in info.value.message


async def test_session_http_error_with_message(httpx_mock) -> None:
    httpx_mock.add_response(url=SESSION_URL, status_code=500, json={"message": "内部错误"})
    with pytest.raises(SpeToolError) as info:
        await create_spe_session(base_url=BASE, user_jwt="j", title="t")
    assert "HTTP 500" in info.value.message
    assert "内部错误" in info.value.message


async def test_session_missing_id(httpx_mock) -> None:
    httpx_mock.add_response(url=SESSION_URL, json={"ok": True, "other": 1})
    with pytest.raises(SpeToolError) as info:
        await create_spe_session(base_url=BASE, user_jwt="j", title="t")
    assert "缺少会话 id" in info.value.message


async def test_session_network_error(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=SESSION_URL)
    with pytest.raises(SpeToolError) as info:
        await create_spe_session(base_url=BASE, user_jwt="j", title="t")
    assert info.value.code == "SESSION_CREATE_FAILED"
    assert "连接失败" in info.value.message


# ---------- call_spe_login（biz 通道：超时与网络失败同归 TOOL_UPSTREAM_ERROR） ----------

LOGIN_URL = f"{BASE}/api/v1/auth/login"


async def test_login_success_returns_body(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LOGIN_URL,
        json={"token": "jwt-1", "role": "ADMIN", "tenantId": 5001, "username": "alice"},
    )
    body = await call_spe_login(base_url=BASE, username="alice", password="pw", bearer="old-jwt")
    assert body["token"] == "jwt-1"
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer old-jwt"
    assert json.loads(request.content) == {"username": "alice", "password": "pw"}


async def test_login_without_bearer_omits_header(httpx_mock) -> None:
    httpx_mock.add_response(url=LOGIN_URL, json={"token": "t"})
    await call_spe_login(base_url=BASE, username="u", password="p")
    request = httpx_mock.get_requests()[0]
    assert "Authorization" not in request.headers


async def test_login_401_maps_to_auth_failed(httpx_mock) -> None:
    httpx_mock.add_response(url=LOGIN_URL, status_code=401, json={"message": "bad creds"})
    with pytest.raises(SpeToolError) as info:
        await call_spe_login(base_url=BASE, username="u", password="wrong")
    assert info.value.code == "AUTH_FAILED"
    assert info.value.message == "用户名或密码错误"


async def test_login_400_maps_to_validation_failed(httpx_mock) -> None:
    httpx_mock.add_response(url=LOGIN_URL, status_code=400, json={"error": "用户名不能为空"})
    with pytest.raises(SpeToolError) as info:
        await call_spe_login(base_url=BASE, username="", password="p")
    assert info.value.code == "BIZ_VALIDATION_FAILED"
    assert "用户名不能为空" in info.value.message


async def test_login_http_error_uses_message(httpx_mock) -> None:
    httpx_mock.add_response(url=LOGIN_URL, status_code=503, json={"message": "维护中"})
    with pytest.raises(SpeToolError) as info:
        await call_spe_login(base_url=BASE, username="u", password="p")
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "HTTP 503" in info.value.message
    assert "维护中" in info.value.message


async def test_login_timeout_stays_upstream_error(httpx_mock) -> None:
    """biz 通道契约：超时不单列码，与网络失败同归 TOOL_UPSTREAM_ERROR。"""
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=LOGIN_URL)
    with pytest.raises(SpeToolError) as info:
        await call_spe_login(base_url=BASE, username="u", password="p")
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "超时" in info.value.message


async def test_login_missing_token_rejected(httpx_mock) -> None:
    httpx_mock.add_response(url=LOGIN_URL, json={"role": "ADMIN"})
    with pytest.raises(SpeToolError) as info:
        await call_spe_login(base_url=BASE, username="u", password="p")
    assert "缺少 token 字段" in info.value.message


# ---------- call_spe_biz_get（C-22：GET + Bearer，401/403/404 特判） ----------

ORDERS_URL = f"{BASE}/api/v1/orders"


async def test_biz_get_success_with_query(httpx_mock) -> None:
    httpx_mock.add_response(url=ORDERS_URL + "?page=0&size=20", json={"total": 1})
    data = await call_spe_biz_get(base_url=BASE, jwt="jwt-1", path="/api/v1/orders", query={"page": 0, "size": 20})
    assert data == {"total": 1}
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer jwt-1"
    assert "X-Agent-Token" not in request.headers


async def test_biz_get_status_mappings(httpx_mock) -> None:
    cases = [(401, "BIZ_AUTH_FAILED"), (403, "BIZ_FORBIDDEN"), (404, "BIZ_NOT_FOUND"), (502, "TOOL_UPSTREAM_ERROR")]
    for status, code in cases:
        httpx_mock.add_response(url=ORDERS_URL, status_code=status, json={"message": "x"})
        with pytest.raises(SpeToolError) as info:
            await call_spe_biz_get(base_url=BASE, jwt="j", path="/api/v1/orders", query={})
        assert info.value.code == code


async def test_biz_get_non_json_2xx_rejected(httpx_mock) -> None:
    """GET 侧 2xx 空 body 无宽容（与写通道不同）。"""
    httpx_mock.add_response(url=ORDERS_URL, status_code=200, text="")
    with pytest.raises(SpeToolError) as info:
        await call_spe_biz_get(base_url=BASE, jwt="j", path="/api/v1/orders", query={})
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "非 JSON" in info.value.message


async def test_biz_get_timeout_not_separate_code(httpx_mock) -> None:
    """C-22：超时不单列码，与网络失败同归 TOOL_UPSTREAM_ERROR。"""
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=ORDERS_URL)
    with pytest.raises(SpeToolError) as info:
        await call_spe_biz_get(base_url=BASE, jwt="j", path="/api/v1/orders", query={})
    assert info.value.code == "TOOL_UPSTREAM_ERROR"
    assert "超时" in info.value.message


# ---------- call_spe_biz_mutate（C-26 写通道：400/402 特判 + 空 body 宽容） ----------

APPLY_URL = f"{BASE}/api/v1/pricing/dynamic/7/apply"
GIFT_URL = f"{BASE}/api/v1/credit/admin/gift"


async def test_mutate_empty_body_returns_none(httpx_mock) -> None:
    httpx_mock.add_response(url=APPLY_URL, status_code=200, text="")
    assert (
        await call_spe_biz_mutate(base_url=BASE, jwt="j", method="POST", path="/api/v1/pricing/dynamic/7/apply") is None
    )


async def test_mutate_400_maps_validation_failed(httpx_mock) -> None:
    httpx_mock.add_response(url=APPLY_URL, status_code=400, json={"error": "价格记录不存在"})
    with pytest.raises(SpeToolError) as info:
        await call_spe_biz_mutate(base_url=BASE, jwt="j", method="POST", path="/api/v1/pricing/dynamic/7/apply")
    assert info.value.code == "BIZ_VALIDATION_FAILED"
    assert "价格记录不存在" in info.value.message


async def test_mutate_402_maps_insufficient_credit(httpx_mock) -> None:
    httpx_mock.add_response(url=GIFT_URL, status_code=402, json={"message": "积分余额不足"})
    with pytest.raises(SpeToolError) as info:
        await call_spe_biz_mutate(base_url=BASE, jwt="j", method="POST", path="/api/v1/credit/admin/gift")
    assert info.value.code == "BIZ_INSUFFICIENT_CREDIT"
    assert "积分余额不足" in info.value.message


async def test_mutate_status_and_body_shape(httpx_mock) -> None:
    httpx_mock.add_response(url=GIFT_URL + "?x=1", json={"balance": 88})
    result = await call_spe_biz_mutate(
        base_url=BASE,
        jwt="j",
        method="POST",
        path="/api/v1/credit/admin/gift",
        body={"tenantId": 5, "amount": 10},
        query={"x": 1},
    )
    assert result == {"balance": 88}
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == "Bearer j"
    assert "x=1" in str(request.url)
    assert json.loads(request.content) == {"tenantId": 5, "amount": 10}


async def test_mutate_list_response_preserved(httpx_mock) -> None:
    """collect_products 返回数组——错误文案视图不得吞掉非 dict 响应。"""
    url = f"{BASE}/api/v1/products/collect/sku"
    httpx_mock.add_response(url=url, json=[{"name": "jacket"}])
    result = await call_spe_biz_mutate(
        base_url=BASE, jwt="j", method="POST", path="/api/v1/products/collect/sku", body={"skus": ["S1"]}
    )
    assert result == [{"name": "jacket"}]


async def test_mutate_401_403_404_same_as_get(httpx_mock) -> None:
    for status, code in [(401, "BIZ_AUTH_FAILED"), (403, "BIZ_FORBIDDEN"), (404, "BIZ_NOT_FOUND")]:
        httpx_mock.add_response(url=APPLY_URL, status_code=status, json={})
        with pytest.raises(SpeToolError) as info:
            await call_spe_biz_mutate(base_url=BASE, jwt="j", method="POST", path="/api/v1/pricing/dynamic/7/apply")
        assert info.value.code == code
