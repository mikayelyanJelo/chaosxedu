from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    PasswordChangeRequiredError,
    PermissionDeniedError,
    SchoolSuspendedError,
)
from app.core.i18n import Messages
from app.core.security import decode_access_token
from app.models import PlatformSettings, School, User, UserRole
from app.services.domain import school_settings

DbSession = Annotated[AsyncSession, Depends(get_db)]


def _extract_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError(Messages.TOKEN_INVALID)
    return token


async def get_current_user(request: Request, session: DbSession) -> User:
    payload = decode_access_token(_extract_token(request))
    if payload is None:
        raise AuthenticationError(Messages.TOKEN_INVALID)

    user = await session.get(User, payload.user_id)
    if user is None or user.deleted_at is not None or not user.is_active:
        raise AuthenticationError(Messages.TOKEN_INVALID)

    # Токен устаревшей версии выдан до входа в аккаунт с другого устройства —
    # такая сессия отзывается сразу, а не по истечении токена.
    if payload.token_version != user.token_version:
        raise AuthenticationError(Messages.TOKEN_EXPIRED)

    # Приостановка школы тоже действует немедленно.
    if user.school_id is not None:
        school = await session.get(School, user.school_id)
        if school is None or not school.is_accessible:
            raise SchoolSuspendedError(Messages.SCHOOL_SUSPENDED)

    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_active_user(user: CurrentUser) -> User:
    # До смены выданного пароля открыты только вход, выход и сама смена.
    if user.must_change_password:
        raise PasswordChangeRequiredError(Messages.PASSWORD_CHANGE_REQUIRED)
    return user


ActiveUser = Annotated[User, Depends(get_active_user)]


def require_roles(
    *roles: UserRole,
) -> Callable[[User], Coroutine[Any, Any, User]]:
    async def dependency(user: ActiveUser) -> User:
        if user.role not in roles:
            raise PermissionDeniedError(Messages.PERMISSION_DENIED)
        return user

    return dependency


OwnerUser = Annotated[User, Depends(require_roles(UserRole.OWNER))]
DirectorUser = Annotated[User, Depends(require_roles(UserRole.DIRECTOR))]
TeacherUser = Annotated[User, Depends(require_roles(UserRole.TEACHER))]
StudentUser = Annotated[User, Depends(require_roles(UserRole.STUDENT))]
StaffUser = Annotated[
    User, Depends(require_roles(UserRole.OWNER, UserRole.DIRECTOR, UserRole.TEACHER))
]


async def require_detailed_stats(user: ActiveUser, session: DbSession) -> User:
    # Настройка школы закрывает целый раздел из нескольких обработчиков,
    # включая те, что появятся позже, поэтому проверка стоит в зависимости.
    if user.role != UserRole.STUDENT:
        return user
    school = (
        await session.get(School, user.school_id) if user.school_id is not None else None
    )
    if not school_settings.students_see_detailed_stats(school):
        raise PermissionDeniedError(Messages.STATS_HIDDEN)
    return user


AnalyticsStudent = Annotated[User, Depends(require_detailed_stats)]


async def get_platform_settings(session: DbSession) -> PlatformSettings:
    config = await session.get(PlatformSettings, 1)
    if config is None:
        config = PlatformSettings(id=1)
        session.add(config)
        await session.commit()
        await session.refresh(config)
    return config


Settings = Annotated[PlatformSettings, Depends(get_platform_settings)]


async def get_school_scope(user: ActiveUser, session: DbSession) -> School:
    # Владелец платформы к школе не привязан: его обработчики принимают
    # идентификатор школы явным параметром.
    if user.school_id is None:
        raise PermissionDeniedError(Messages.PERMISSION_DENIED)
    school = await session.get(School, user.school_id)
    if school is None:
        raise NotFoundError(Messages.SCHOOL_NOT_FOUND)
    return school


SchoolScope = Annotated[School, Depends(get_school_scope)]


async def resolve_school_for_actor(
    session: AsyncSession, actor: User, school_id: uuid.UUID | None
) -> School:
    if actor.role == UserRole.OWNER:
        if school_id is None:
            raise NotFoundError(Messages.SCHOOL_NOT_FOUND)
        target_id = school_id
    else:
        if actor.school_id is None:
            raise PermissionDeniedError(Messages.PERMISSION_DENIED)
        if school_id is not None and school_id != actor.school_id:
            raise PermissionDeniedError(Messages.PERMISSION_DENIED)
        target_id = actor.school_id

    school = await session.get(School, target_id)
    if school is None:
        raise NotFoundError(Messages.SCHOOL_NOT_FOUND)
    return school


def assert_same_school(actor: User, target: User) -> None:
    if actor.role == UserRole.OWNER:
        return
    if actor.school_id is None or actor.school_id != target.school_id:
        raise PermissionDeniedError(Messages.PERMISSION_DENIED)


def get_client_ip(request: Request) -> str:
    # X-Forwarded-For — цепочка «заявленное клиентом ..., что видел прокси»:
    # каждый доверенный прокси дописывает адрес соединения справа. Поэтому
    # берём не первый элемент (его подделывает сам клиент), а дописанный
    # ближайшим доверенным прокси. Без доверенных прокси заголовку не верим.
    hops = settings.trusted_proxy_count
    if hops > 0:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            parts = [item.strip() for item in forwarded.split(",") if item.strip()]
            if len(parts) >= hops:
                return parts[-hops]
    return request.client.host if request.client else "unknown"
