"""SPE 算力积分：阈值闸 / 价表镜像 / Run 结束上报（agent-compute-credit 卡2）。

容错约定（spec §5）：SPE 故障一律放行不阻断聊天（登录零余额闸兜底，漏报=少收钱）；
结算以 SPE deduct 响应为准，镜像估算仅展示。
闸为提交时点检查：排队中请求在派发成 Run 时不复查（已在提交时闸过）；
审批 resume 路径不设闸（对齐上游零影响）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select, update

from yuxi.agents.toolkits.spe.client import SPE_SESSION_TIMEOUT_SECONDS
from yuxi.config.options import spe_agent_opts
from yuxi.repositories.spe_credential_repository import get_user_jwt
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, CreditReportFailed

logger = logging.getLogger(__name__)

CREDIT_GATE_THRESHOLD_LI_DEFAULT = 100
_BALANCE_CACHE_TTL_SECONDS = 60
# 闸门在聊天提交路径上，SPE 挂起必须快速放行，不能拖住会话。
_BALANCE_FETCH_TIMEOUT_SECONDS = 5.0
_PRICE_MIRROR_TTL_SECONDS = 600
_REPORT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (5, 30, 120)

_balance_cache: dict[str, tuple[float, int]] = {}
_price_mirror: dict[str, Any] | None = None
_price_mirror_at: float = 0.0


def gate_threshold_li() -> int:
    """读取闸门阈值（厘），env SPE_CREDIT_GATE_THRESHOLD_LI 可覆盖，非法值回落默认。"""
    raw = os.getenv("SPE_CREDIT_GATE_THRESHOLD_LI", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return CREDIT_GATE_THRESHOLD_LI_DEFAULT
    return value if value > 0 else CREDIT_GATE_THRESHOLD_LI_DEFAULT


def gate_disabled() -> bool:
    """SPE_CREDIT_GATE_DISABLED=1 时一键关闭阈值闸。"""
    return os.getenv("SPE_CREDIT_GATE_DISABLED", "").strip() in ("1", "true")


async def _spe_base_url() -> str | None:
    """读取 SPE 后端地址；配置缺失或读取失败一律返回 None（视为 SPE 不可用）。"""
    try:
        opts = await spe_agent_opts.get()
    except Exception:
        return None
    if not isinstance(opts, Mapping):
        return None
    base_url = opts.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    return base_url.rstrip("/")


async def _fetch_balance_once(*, uid: str, jwt: str | None = None) -> int | None:
    """单次拉取 SPE 余额（厘）；任何失败返回 None（调用方按放行/降级处理）。"""
    base = await _spe_base_url()
    if not base:
        return None
    if jwt is None:
        jwt = await get_user_jwt(uid)
    if not jwt:
        return None
    try:
        async with httpx.AsyncClient(timeout=_BALANCE_FETCH_TIMEOUT_SECONDS) as client:
            res = await client.get(
                f"{base}/api/v1/credit/balance",
                headers={"Authorization": f"Bearer {jwt}"},
            )
        if res.status_code != 200:
            return None
        balance = res.json().get("creditBalance")
        return balance if isinstance(balance, int) else None
    except Exception as exc:
        logger.debug("SPE 余额拉取失败（按放行处理）: uid=%s err=%s", uid, exc)
        return None


async def fetch_balance_li(*, uid: str) -> int | None:
    """强制单次拉取余额（不读缓存）。"""
    return await _fetch_balance_once(uid=uid)


async def _cached_balance_li(*, uid: str) -> int | None:
    """带 60s TTL 的余额读取；仅缓存成功结果，失败不缓存。"""
    now = time.monotonic()
    entry = _balance_cache.get(uid)
    if entry is not None and now - entry[0] < _BALANCE_CACHE_TTL_SECONDS:
        return entry[1]
    balance = await _fetch_balance_once(uid=uid)
    if balance is not None:
        _balance_cache[uid] = (time.monotonic(), balance)
    return balance


async def balance_li(*, uid: str) -> int | None:
    """余额端点公共包装：SPE 不可用/无凭据返回 None。"""
    try:
        return await _cached_balance_li(uid=uid)
    except Exception:
        return None


def invalidate_balance_cache() -> None:
    """扣减成功后清空余额缓存，让下一次过闸读到新余额。"""
    _balance_cache.clear()


async def ensure_run_allowed(*, uid: str, role: str | None) -> None:
    """Run 提交阈值闸：余额<=阈值 403（code=credit_insufficient）；superadmin 豁免；
    SPE 故障/无凭据/gate_disabled 放行（漏报=少收钱，不阻断聊天）。
    SPE 链路（含凭据存储）任何非 HTTPException 故障一律放行。"""
    if role == "superadmin":
        return
    if gate_disabled():
        return
    try:
        jwt = await get_user_jwt(uid)
        if not jwt:
            return
        balance = await _cached_balance_li(uid=uid)
        if balance is None:
            return
        if balance <= gate_threshold_li():
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "credit_insufficient",
                    "message": f"SPE 积分余额不足（{balance} 厘），请先在 SPE 平台充值",
                    "balanceLi": balance,
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("SPE 积分闸内部故障，放行: uid=%s err=%s", uid, exc)
        return


async def price_mirror(*, uid: str) -> dict[str, Any] | None:
    """价表镜像（TTL 10 分钟）；SPE 故障/无凭据返回 None（估算降级）。"""
    global _price_mirror, _price_mirror_at
    now = time.monotonic()
    if _price_mirror is not None and now - _price_mirror_at < _PRICE_MIRROR_TTL_SECONDS:
        return _price_mirror
    mirror = await _fetch_prices_once(uid=uid)
    if mirror is not None:
        _price_mirror = mirror
        _price_mirror_at = time.monotonic()
    return mirror


async def _fetch_prices_once(*, uid: str) -> dict[str, Any] | None:
    base = await _spe_base_url()
    if not base:
        return None
    jwt = await get_user_jwt(uid)
    if not jwt:
        return None
    try:
        async with httpx.AsyncClient(timeout=_BALANCE_FETCH_TIMEOUT_SECONDS) as client:
            res = await client.get(
                f"{base}/api/v1/credit/external/prices",
                headers={"Authorization": f"Bearer {jwt}"},
            )
        if res.status_code != 200:
            return None
        mirror = res.json()
    except Exception as exc:
        logger.debug("SPE 价表镜像拉取失败（估算降级）: uid=%s err=%s", uid, exc)
        return None
    if not isinstance(mirror, Mapping) or not isinstance(mirror.get("models"), list):
        return None
    return dict(mirror)


def _match_pattern(model: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    """价表匹配：精确 == 优先 → 最长前缀（pattern 以 '*' 结尾）→ '*' 兜底行。"""
    for row in models:
        if row.get("pattern") == model:
            return row
    best: dict[str, Any] | None = None
    best_len = -1
    for row in models:
        pattern = row.get("pattern")
        if not isinstance(pattern, str) or not pattern.endswith("*") or pattern == "*":
            continue
        prefix = pattern[:-1]
        if model.startswith(prefix) and len(prefix) > best_len:
            best = row
            best_len = len(prefix)
    if best is not None:
        return best
    for row in models:
        if row.get("pattern") == "*":
            return row
    return None


def estimate_credits_li(mirror: dict[str, Any] | None, usage: dict[str, Any]) -> float | None:
    """展示估算（厘，不 ceil）。usage 为 {model: {"input_tokens","output_tokens","cache_read_tokens"}}。

    mirror 缺失或无任何桶命中价表时返回 None（估算降级）。
    """
    if not isinstance(mirror, Mapping):
        return None
    fx_rate = mirror.get("fxRateCnyUsd")
    if not isinstance(fx_rate, (int, float)) or fx_rate <= 0:
        return None
    models = mirror.get("models") or []
    total = 0.0
    matched_any = False
    for model, bucket in usage.items():
        row = _match_pattern(str(model), models)
        if row is None:
            continue
        input_usd = float(row.get("inputUsdPerMtok") or 0.0)
        output_usd = float(row.get("outputUsdPerMtok") or 0.0)
        cache_usd = row.get("cacheReadUsdPerMtok")
        cache_usd = float(cache_usd) if isinstance(cache_usd, (int, float)) else input_usd
        margin = row.get("margin")
        margin = float(margin) if isinstance(margin, (int, float)) and margin > 0 else 1.0
        input_tokens = int(bucket.get("input_tokens") or 0)
        output_tokens = int(bucket.get("output_tokens") or 0)
        cache_read = int(bucket.get("cache_read_tokens") or 0)
        usd = (input_tokens * input_usd + output_tokens * output_usd + cache_read * cache_usd) / 1e6
        total += usd * margin * float(fx_rate) * 1000
        matched_any = True
    return total if matched_any else None


def _model_name(bucket_key: str, bucket: Mapping[str, Any]) -> str:
    """模型名提取链：response_model_ids[0] → configured_model_id → bucket_key 清洗。"""
    identity = bucket.get("model") if isinstance(bucket.get("model"), Mapping) else {}
    response_ids = identity.get("response_model_ids")
    if isinstance(response_ids, list) and response_ids and isinstance(response_ids[0], str):
        return response_ids[0]
    configured = identity.get("configured_model_id")
    if isinstance(configured, str) and configured:
        return configured
    if bucket_key.startswith("response:"):
        return bucket_key.split(":", 2)[-1]
    if bucket_key.startswith("unattributed:"):
        return bucket_key.split(":")[-1]
    return bucket_key


def buckets_from_run_usage(run_usage: dict[str, Any]) -> list[dict[str, Any]]:
    """token_usage.run → 契约 usage 数组。

    现场核对（Task 2.0）：models[bucket_key] = {"model": {...response_model_ids},
    "usage": {"input_tokens","output_tokens"}, "cache_read_input_tokens"}。
    """
    if not isinstance(run_usage, Mapping):
        return []
    models = run_usage.get("models")
    if not isinstance(models, Mapping):
        return []
    buckets: list[dict[str, Any]] = []
    for bucket_key, bucket in models.items():
        if not isinstance(bucket, Mapping):
            continue
        usage = bucket.get("usage") if isinstance(bucket.get("usage"), Mapping) else {}
        buckets.append(
            {
                "model": _model_name(str(bucket_key), bucket),
                "inputTokens": int(usage.get("input_tokens") or 0),
                "outputTokens": int(usage.get("output_tokens") or 0),
                "cacheReadTokens": int(bucket.get("cache_read_input_tokens") or 0),
            }
        )
    return buckets


def _normalize_run_usage(run_usage: dict[str, Any]) -> dict[str, dict[str, int]]:
    """models 桶 → {model: {"input_tokens","output_tokens","cache_read_tokens"}}（估算入参）。"""
    return {
        bucket["model"]: {
            "input_tokens": bucket["inputTokens"],
            "output_tokens": bucket["outputTokens"],
            "cache_read_tokens": bucket["cacheReadTokens"],
        }
        for bucket in buckets_from_run_usage(run_usage)
    }


async def _post_deduct_once(*, base: str, jwt: str, payload: dict[str, Any]) -> tuple[bool, int | None, str | None]:
    """单次上报；返回 (ok, chargedLi, error)。"""
    try:
        async with httpx.AsyncClient(timeout=SPE_SESSION_TIMEOUT_SECONDS) as client:
            res = await client.post(
                f"{base}/api/v1/credit/external/deduct",
                json=payload,
                headers={"Authorization": f"Bearer {jwt}"},
            )
        if res.status_code != 200:
            return False, None, f"HTTP {res.status_code}"
        charged = res.json().get("chargedLi")
        if not isinstance(charged, int):
            return False, None, "响应缺 chargedLi"
        return True, charged, None
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


async def _post_deduct(*, uid: str, payload: dict[str, Any]) -> tuple[int | None, str | None]:
    """带重试的上报（refId 幂等由 SPE 承接）；终败返回 (None, 最后一次错误)。"""
    base = await _spe_base_url()
    if not base:
        return None, "SPE base_url 未配置"
    jwt = await get_user_jwt(uid)
    if not jwt:
        return None, "用户 SPE 凭据缺失"
    delays = _REPORT_RETRY_DELAYS_SECONDS
    last_error = "unknown"
    for attempt in range(len(delays)):
        ok, charged, error = await _post_deduct_once(base=base, jwt=jwt, payload=payload)
        if ok:
            return charged, None
        last_error = error or "unknown"
        logger.warning(
            "SPE 算力上报失败（第 %s/%s 次）: refId=%s err=%s",
            attempt + 1,
            len(delays),
            payload.get("refId"),
            last_error,
        )
        if attempt < len(delays) - 1:
            await asyncio.sleep(delays[attempt])
    return None, last_error


async def _persist_run_credits(run_id: str, credits_li: int) -> None:
    """上报成功后回填 agent_runs.credits_li（厘）。"""
    async with pg_manager.get_async_session_context() as db:
        await db.execute(update(AgentRun).where(AgentRun.id == run_id).values(credits_li=int(credits_li)))
        await db.commit()


async def _persist_failed_report(run_id: str, payload: dict[str, Any], error: Any) -> None:
    """上报终败落 credit_report_failed 供超管重放；落表失败吞异常不影响主流程。"""
    with contextlib.suppress(Exception):
        async with pg_manager.get_async_session_context() as db:
            db.add(CreditReportFailed(run_id=run_id, payload=payload, error=str(error or "")[:2000]))
            await db.commit()


async def report_run_usage(*, uid: str, role: str | None, run_id: str, usage_by_model: dict[str, Any]) -> None:
    """Run 结束上报（可重试）：buckets_from_run_usage 提取分模型桶（token_usage.run.models 结构）；
    refId=agent-yuxi-run-<run_id>；重试 (5,30,120)s；成功 _persist_run_credits 回填 +
    invalidate_balance_cache；终败 _persist_failed_report。"""
    if role == "superadmin":
        return
    buckets = buckets_from_run_usage(usage_by_model)
    if not buckets:
        return
    payload = {
        "platform": "yuxi",
        "refId": f"agent-yuxi-run-{run_id}",
        "operationType": "agent_compute",
        "usage": buckets,
    }
    charged, error = await _post_deduct(uid=uid, payload=payload)
    if charged is not None:
        await _persist_run_credits(run_id, charged)
        invalidate_balance_cache()
        return
    await _persist_failed_report(run_id, payload, error)


async def report_run_usage_best_effort(
    *, uid: str, role: str | None, run_id: str, usage_by_model: dict[str, Any]
) -> None:
    """fire-and-forget 包装：上报链路任何异常不外溢（run 终态不受影响）。"""
    try:
        await report_run_usage(uid=uid, role=role, run_id=run_id, usage_by_model=usage_by_model)
    except Exception:
        logger.exception("SPE 算力上报意外失败: run_id=%s", run_id)


async def _load_failed_reports() -> list[CreditReportFailed]:
    """读取待重放的失败行（超管重放入口；读取失败向上抛，让调用方看到真实故障）。

    LIMIT 500 防失败表膨胀后单次重放拖垮出站链路；溢出部分下次重放继续（按 id 升序）。
    """
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(select(CreditReportFailed).order_by(CreditReportFailed.id).limit(500))
        return list(result.scalars().all())


async def _drop_failed_report(row: CreditReportFailed) -> None:
    """重放成功后删除失败行；删除失败吞异常（下次重放幂等重试）。"""
    with contextlib.suppress(Exception):
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(CreditReportFailed).where(CreditReportFailed.id == row.id))
            await db.commit()


async def _mark_failed_report_error(row: CreditReportFailed, error: Any) -> None:
    """重放仍失败时更新该行 error（保留行）；更新失败吞异常。"""
    with contextlib.suppress(Exception):
        async with pg_manager.get_async_session_context() as db:
            await db.execute(
                update(CreditReportFailed).where(CreditReportFailed.id == row.id).values(error=str(error or "")[:2000])
            )
            await db.commit()


async def _run_owner_uid(run_id: str) -> str | None:
    """按 run_id 查回属主 uid（重放计费必须归属原用户租户，而不是操作重放的超管）。"""
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(select(AgentRun.uid).where(AgentRun.id == run_id))
        return result.scalar_one_or_none()


async def replay_failed_reports(*, uid: str) -> dict[str, int]:
    """超管重放 credit_report_failed（spec §5.5）：逐行用已存 payload 调 _post_deduct；
    成功删行 + 回填 credits_li + 失效余额缓存；失败行保留并更新 error 后继续下一行。

    计费归属：SPE 按 Bearer JWT 解析租户，重放必须取 run 属主（原用户）的凭据；
    run 已不存在时无法归属租户，保留行不发起出站请求。uid 参数仅为触发者（超管），用于审计日志。
    """
    replayed = 0
    failed = 0
    rows = await _load_failed_reports()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        owner_uid = await _run_owner_uid(row.run_id)
        if owner_uid is None:
            failed += 1
            await _mark_failed_report_error(row, "run 不存在，无法归属租户")
            continue
        charged, error = await _post_deduct(uid=owner_uid, payload=payload)
        if charged is None:
            failed += 1
            await _mark_failed_report_error(row, error)
            continue
        await _persist_run_credits(row.run_id, charged)
        await _drop_failed_report(row)
        replayed += 1
    if replayed:
        invalidate_balance_cache()
    logger.info("SPE 失败表重放完成: by=%s rows=%s replayed=%s failed=%s", uid, len(rows), replayed, failed)
    return {"replayed": replayed, "failed": failed}
