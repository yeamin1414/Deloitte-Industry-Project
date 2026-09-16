from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.core.security import User, decode_token
from app.platform import Platform

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def get_platform(request: Request) -> Platform:
    return request.app.state.platform


def current_user(token: str = Depends(oauth2), platform: Platform = Depends(get_platform)) -> User:
    try:
        return decode_token(token, platform.settings.jwt_secret)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


def require(permission: str):
    def checker(user: User = Depends(current_user)) -> User:
        if not user.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role {user.role} lacks permission {permission}")
        return user

    return checker
