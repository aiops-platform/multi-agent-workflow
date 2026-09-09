"""JWT → 派生 tenant_id（design §9.1）。

- ``jwt_secret`` 配置非空 → **JWT 模式**：请求必须携带 ``Authorization: Bearer <JWT>``，
  tenant_id 从 claim 派生（优先级 ``tenant_id`` > ``org_id`` > ``org``），客户端提交的
  tenant 字段一律**忽略**（§9.1 禁止客户端提交作为授权依据）。
- ``jwt_secret`` 为空 → **dev 模式**：回退显式传参（``X-Tenant-ID`` 头 / 请求体
  ``tenant_id``），便于本地联调（CLAUDE.md #7）；API 启动时会打告警。
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt as pyjwt
from fastapi import HTTPException, Request

from ..config import Settings, get_settings

# tenant claim 派生优先级（§9.1：JWT 含 user_id / org_id）
_TENANT_CLAIMS = ("tenant_id", "org_id", "org")


@dataclass
class TenantContext:
    """一次请求的租户上下文（由 JWT 或 dev 回退派生，注入所有下游调用）。"""

    tenant_id: str
    subject: str | None = None  # JWT sub：审批人身份（approve/reject 校验用）
    auth_mode: str = "jwt"  # jwt | dev

    @property
    def is_jwt(self) -> bool:
        return self.auth_mode == "jwt"


def tenant_from_token(token: str, settings: Settings) -> TenantContext:
    """解码并校验 JWT → TenantContext。失败抛 HTTPException(401)。"""
    try:
        payload = pyjwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="JWT 已过期") from exc
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"JWT 无效: {exc}") from exc
    tenant = next((payload[c] for c in _TENANT_CLAIMS if payload.get(c)), None)
    if not tenant:
        raise HTTPException(
            status_code=401,
            detail="JWT 缺少租户 claim（需 tenant_id / org_id / org 之一）",
        )
    return TenantContext(
        tenant_id=str(tenant), subject=payload.get("sub"), auth_mode="jwt"
    )


async def get_tenant_context(request: Request) -> TenantContext:
    """FastAPI 依赖：每个请求派生租户上下文。"""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    if not settings.jwt_secret:
        # dev 回退：显式传参仅联调用（§9.1 禁止作为生产授权依据）
        tenant = request.headers.get("X-Tenant-ID") or "local"
        return TenantContext(tenant_id=tenant, auth_mode="dev")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <JWT>")
    return tenant_from_token(auth[len("Bearer "):].strip(), settings)
