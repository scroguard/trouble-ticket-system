"""Authentication: server-side sessions via HttpOnly cookie or Bearer token.

Routes: POST /auth/register, /auth/login, /auth/logout, /auth/change-password;
GET /auth/me.

Brute-force protection uses sliding windows over the `login_failures` log:
  * per account + IP  (LOGIN_MAX_ATTEMPTS in LOGIN_LOCKOUT_MINUTES) - stops guessing
    one password without letting a stranger lock the real user out from elsewhere;
  * per IP            (LOGIN_MAX_ATTEMPTS_PER_IP in LOGIN_LOCKOUT_MINUTES) - stops
    spraying many accounts from one address;
  * per account       (LOGIN_MAX_ATTEMPTS_PER_ACCOUNT per hour, all IPs) - caps
    distributed attacks on one account.
Limits are checked before the (deliberately slow) password hash, so blocked
requests cost almost nothing.
Dependencies: `current_user` (401 if unauthenticated), `require_admin` (403).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.models import LoginFailure, User, UserRole, UserSession
from app.schemas import LoginIn, LoginOut, PasswordChange, ProfileUpdate, RegisterIn, UserOut
from app.security import hash_password, hash_token, needs_rehash, new_session_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

DB = Annotated[Session, Depends(get_db)]
# Arbitrary constant: serializes concurrent "first user" registrations.
FIRST_USER_LOCK_KEY = 0x7475736572  # "tuser"
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
ACCOUNT_WINDOW = timedelta(hours=1)


# ------------------------------------------------------------------ dependencies


def token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, credentials = auth.partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return request.cookies.get(get_settings().session_cookie_name)


def _session_for(db: Session, token: str | None) -> UserSession | None:
    if not token:
        return None
    now = datetime.now(UTC)
    session = db.scalar(
        select(UserSession)
        .join(UserSession.user)
        .where(
            UserSession.token_hash == hash_token(token),
            UserSession.expires_at > now,
            User.is_active.is_(True),
        )
    )
    if session and (not session.last_seen_at or now - session.last_seen_at > SESSION_TOUCH_INTERVAL):
        session.last_seen_at = now  # throttled so reads don't write on every request
    return session


def optional_user(request: Request, db: DB) -> User | None:
    session = _session_for(db, token_from_request(request))
    return session.user if session else None


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise APIError(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return user


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != UserRole.ADMIN:
        raise APIError(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


# ------------------------------------------------------------------ helpers


def _set_session_cookie(response: Response, token: str, expires_at: datetime) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name,
        token,
        expires=expires_at,
        httponly=True,          # not readable from JS
        secure=s.cookie_secure,
        samesite="lax",         # not sent on cross-site POST/PATCH (CSRF defence #1)
        path="/",
    )


def _client_ip(request: Request) -> str | None:
    # With FORWARDED_ALLOW_IPS set, uvicorn has already replaced this with the real
    # client address from X-Forwarded-For (only when sent by a trusted proxy).
    return request.client.host if request.client else None


def revoke_sessions(db: Session, user_id: int, *, keep_token: str | None = None) -> int:
    """Log a user out everywhere (optionally except the session making the request)."""
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_token:
        stmt = stmt.where(UserSession.token_hash != hash_token(keep_token))
    return db.execute(stmt).rowcount


def clear_login_failures(db: Session, email: str) -> int:
    return db.execute(delete(LoginFailure).where(LoginFailure.email == email.lower())).rowcount


def _login_retry_after(db: Session, email: str, ip: str | None, now: datetime) -> int | None:
    """Seconds until this email/IP may try again, or None if not rate limited."""
    s = get_settings()
    window = timedelta(minutes=s.login_lockout_minutes)
    rules = [(LoginFailure.email == email, s.login_max_attempts_per_account, ACCOUNT_WINDOW)]
    if ip:
        rules += [
            (and_(LoginFailure.email == email, LoginFailure.ip_address == ip), s.login_max_attempts, window),
            (LoginFailure.ip_address == ip, s.login_max_attempts_per_ip, window),
        ]
    waits = []
    for condition, limit, span in rules:
        recent = select(LoginFailure.attempted_at).where(condition, LoginFailure.attempted_at > now - span)
        count = db.scalar(select(func.count()).select_from(recent.subquery()))
        if count >= limit:
            # Unblocked once enough of the oldest failures age out of the window.
            pivot = db.scalar(recent.order_by(LoginFailure.attempted_at).offset(count - limit).limit(1))
            waits.append(max(1, int((pivot + span - now).total_seconds()) + 1))
    return max(waits) if waits else None


def _raise_rate_limited(retry_after: int) -> None:
    raise APIError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many failed login attempts; try again later",
        {"retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


# ------------------------------------------------------------------ routes


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterIn, db: DB, actor: Annotated[User | None, Depends(optional_user)]
) -> User:
    """Create a user. Open only while no users exist (that first user is always an
    admin); afterwards an admin session is required."""
    # Transaction-scoped lock: two simultaneous "first user" requests can't both win.
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": FIRST_USER_LOCK_KEY})
    is_first_user = db.scalar(select(func.count()).select_from(User)) == 0

    if not is_first_user:
        if actor is None:
            raise APIError(status.HTTP_401_UNAUTHORIZED, "Authentication required")
        if actor.role != UserRole.ADMIN:
            raise APIError(status.HTTP_403_FORBIDDEN, "Admin role required")

    email = payload.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise APIError(status.HTTP_409_CONFLICT, "A user with this email already exists")

    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN if is_first_user else payload.role,
    )
    db.add(user)
    db.flush()
    db.refresh(user)
    return user


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, request: Request, response: Response, db: DB) -> LoginOut:
    s = get_settings()
    now = datetime.now(UTC)
    email = payload.email.lower()
    ip = _client_ip(request)

    if retry_after := _login_retry_after(db, email, ip, now):
        _raise_rate_limited(retry_after)

    user = db.scalar(select(User).where(User.email == email))
    # Always runs a full hash check, even for unknown emails (no user enumeration).
    valid = verify_password(user.password_hash if user else None, payload.password)
    if not valid or user is None or not user.is_active:
        db.add(LoginFailure(email=email, ip_address=ip))
        db.commit()  # persist the failure; the 401 below would otherwise roll it back
        raise APIError(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    # Success resets this account+IP pair (per-IP/per-account history stays).
    stmt = delete(LoginFailure).where(LoginFailure.email == email)
    db.execute(stmt.where(LoginFailure.ip_address == ip) if ip else stmt.where(LoginFailure.ip_address.is_(None)))
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    # Opportunistic cleanup of this user's expired sessions.
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at <= now))

    token = new_session_token()
    expires_at = now + timedelta(hours=s.session_ttl_hours)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=expires_at,
            last_seen_at=now,
            ip_address=ip,
            user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        )
    )
    _set_session_cookie(response, token, expires_at)
    return LoginOut(user=UserOut.model_validate(user), access_token=token, expires_at=expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: DB) -> Response:
    """Revoke the current session server-side and clear the cookie. Idempotent."""
    if token := token_from_request(request):
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    s = get_settings()
    response.delete_cookie(
        s.session_cookie_name, path="/", httponly=True, secure=s.cookie_secure, samesite="lax"
    )
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user


@router.patch("/me", response_model=UserOut)
def update_me(payload: ProfileUpdate, user: CurrentUser, db: DB) -> UserOut:
    """Update your own profile (currently: your reply signature)."""
    if "signature" in payload.model_fields_set:
        user.signature = payload.signature
    db.flush()
    return UserOut.model_validate(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChange, request: Request, db: DB, user: CurrentUser
) -> Response:
    """Change your own password. Other sessions are logged out; this one stays."""
    now = datetime.now(UTC)
    ip = _client_ip(request)
    # Same limits as login, so a hijacked session can't brute-force the password.
    if retry_after := _login_retry_after(db, user.email, ip, now):
        _raise_rate_limited(retry_after)
    if not verify_password(user.password_hash, payload.current_password):
        db.add(LoginFailure(email=user.email, ip_address=ip))
        db.commit()
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "Validation failed",
            [{"field": "current_password", "message": "Current password is incorrect"}],
        )
    user.password_hash = hash_password(payload.new_password)
    revoke_sessions(db, user.id, keep_token=token_from_request(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
