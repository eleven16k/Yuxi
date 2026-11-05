"""SPE 算力积分 HTTP 路由（agent-compute-credit 卡2）。"""

from fastapi import APIRouter, Depends

from server.utils.auth_middleware import get_required_user
from yuxi.services.spe_credit_service import balance_li
from yuxi.storage.postgres.models_business import User

spe_credit = APIRouter(prefix="/agent/spe-credit", tags=["spe-credit"])


@spe_credit.get("")
async def get_spe_credit(_current_user: User = Depends(get_required_user)) -> dict:
    """当前用户租户的 SPE 积分余额（厘）；None=SPE 不可用/无凭据。"""
    return {"balanceLi": await balance_li(uid=str(_current_user.uid))}
