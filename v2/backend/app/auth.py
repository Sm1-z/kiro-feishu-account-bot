"""JWT 认证与依赖。

飞书 OAuth 回调后签发 JWT（载 open_id/name），前端持 Bearer 调 API。
管理员判定按 settings.admin_id_set()。
"""
from __future__ import annotations

import time

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_ALGO = "HS256"
_TTL = 7 * 24 * 3600  # 7 天

_bearer = HTTPBearer(auto_error=False)


def issue_token(open_id: str, name: str) -> str:
    payload = {"sub": open_id, "name": name, "iat": int(time.time()),
               "exp": int(time.time()) + _TTL}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGO)


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"无效 token: {exc}")


class CurrentUser:
    def __init__(self, open_id: str, name: str):
        self.open_id = open_id
        self.name = name
        self.is_admin = open_id in settings.admin_id_set()


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> CurrentUser:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证")
    data = _decode(cred.credentials)
    return CurrentUser(open_id=data["sub"], name=data.get("name", ""))


def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user
