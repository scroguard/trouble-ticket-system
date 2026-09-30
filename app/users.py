"""User directory (any agent) and user administration (admins only).

Users are never deleted - their comments and assignments are history - so removing
access means deactivating (`is_active: false`), which also ends their sessions.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    token_from_request,
    clear_login_failures,
    current_user,
    require_admin,
    revoke_sessions,
)
from app.db import get_db
from app.errors import APIError
from app.models import ACTIVE_STATUSES, LoginFailure, Ticket, User, UserRole
from app.schemas import PasswordSet, UnlockOut, UserAdminOut, UserOut, UserUpdate
from app.security import hash_password

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(current_user)])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[Session, Depends(get_db)]
AdminOnly = [Depends(require_admin)]


def _get_user(db: Session, user_id: int, *, for_update: bool = False) -> User:
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    user = db.scalar(stmt)
    if user is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.get("", response_model=list[UserOut])
def list_users(
    db: DB,
    role: UserRole | None = None,
    include_inactive: Annotated[bool, Query()] = False,
) -> list[UserOut]:
    stmt = select(User).order_by(User.full_name)
    if role:
        stmt = stmt.where(User.role == role)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    return [UserOut.model_validate(u) for u in db.scalars(stmt)]


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: DB) -> UserOut:
    return UserOut.model_validate(_get_user(db, user_id))


@router.patch("/{user_id}", response_model=UserOut, dependencies=AdminOnly)
def update_user(user_id: int, payload: UserUpdate, db: DB) -> UserOut:
    """Edit a user's name, email, role or active flag. Deactivating ends all their
    sessions immediately. The last active admin can't be demoted or deactivated."""
    fields = payload.model_fields_set
    # Lock every active admin row first: two admins demoting each other at the same
    # moment are serialized, so the "last admin" check can't be raced.
    active_admins = db.scalars(
        select(User.id)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
    ).all()
    user = _get_user(db, user_id, for_update=True)

    loses_admin = user.id in active_admins and (
        ("role" in fields and payload.role != UserRole.ADMIN)
        or ("is_active" in fields and not payload.is_active)
    )
    if loses_admin and len(active_admins) <= 1:
        raise APIError(
            status.HTTP_409_CONFLICT, "Cannot demote or deactivate the last active admin"
        )

    if "email" in fields and payload.email.lower() != user.email:
        email = payload.email.lower()
        if db.scalar(select(User.id).where(User.email == email)):
            raise APIError(status.HTTP_409_CONFLICT, "A user with this email already exists")
        user.email = email
    if "full_name" in fields:
        user.full_name = payload.full_name
    if "role" in fields:
        user.role = payload.role
    if "is_active" in fields and payload.is_active != user.is_active:
        user.is_active = payload.is_active
        if not payload.is_active:
            revoke_sessions(db, user.id)
    db.flush()
    return UserOut.model_validate(user)


@router.post(
    "/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT, dependencies=AdminOnly
)
def reset_password(
    user_id: int, payload: PasswordSet, request: Request, db: DB, admin: CurrentUser
) -> Response:
    """Set a new password for a user (e.g. they forgot theirs). Logs them out
    everywhere and clears their failed-login history."""
    user = _get_user(db, user_id, for_update=True)
    user.password_hash = hash_password(payload.password)
    keep = token_from_request(request) if user.id == admin.id else None
    revoke_sessions(db, user.id, keep_token=keep)
    clear_login_failures(db, user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{user_id}/unlock", response_model=UnlockOut, dependencies=AdminOnly)
def unlock_user(user_id: int, db: DB) -> UnlockOut:
    """Clear a user's failed-login history so rate limits on their account lift
    immediately. (Per-IP limits on the attacker's address are unaffected.)"""
    user = _get_user(db, user_id)
    return UnlockOut(cleared_failures=clear_login_failures(db, user.email))


@admin_router.get("/users", response_model=list[UserAdminOut])
def admin_user_overview(
    db: DB, include_inactive: Annotated[bool, Query()] = True
) -> list[UserAdminOut]:
    """Every user with their open-ticket load and failed sign-ins in the last hour
    (two grouped queries, not one per user)."""
    open_counts = dict(
        db.execute(
            select(Ticket.assigned_to_id, func.count())
            .where(Ticket.assigned_to_id.is_not(None), Ticket.status.in_(ACTIVE_STATUSES))
            .group_by(Ticket.assigned_to_id)
        ).all()
    )
    since = datetime.now(UTC) - timedelta(hours=1)
    failure_counts = dict(
        db.execute(
            select(LoginFailure.email, func.count())
            .where(LoginFailure.attempted_at > since)
            .group_by(LoginFailure.email)
        ).all()
    )
    stmt = select(User).order_by(User.is_active.desc(), User.full_name)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    return [
        UserAdminOut(
            **UserOut.model_validate(u).model_dump(),
            open_tickets=open_counts.get(u.id, 0),
            recent_failed_logins=failure_counts.get(u.email, 0),
        )
        for u in db.scalars(stmt)
    ]
