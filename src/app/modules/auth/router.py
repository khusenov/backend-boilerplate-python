from fastapi import APIRouter, Depends, Response

from app.core.http.authorization import AnonymousRoute, require
from app.core.http.deps import (
    SESSION_COOKIE,
    ClientIp,
    CurrentPrincipal,
    DbSession,
    Hashing,
    HashingSlots,
    Jobs,
    Limiter,
    SecretKey,
    require_trusted_origin,
)
from app.core.security import Permission
from app.modules.auth.schemas import (
    EmailChange,
    EmailChangeConfirm,
    Login,
    PasswordChange,
    PasswordReset,
    PasswordResetConfirm,
    Signup,
    SignupConfirm,
    TokenRead,
)
from app.modules.auth.service import passwords, sessions, verification

SESSION_MAX_AGE = int(sessions.SESSION_LIFETIME.total_seconds())

anonymous_router = APIRouter(prefix="/auth", route_class=AnonymousRoute)
router = APIRouter(prefix="/auth")


@anonymous_router.post(
    "/session",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_trusted_origin)],
)
async def create_session(
    body: Login,
    session: DbSession,
    limiter: Limiter,
    hashing: Hashing,
    hashing_slots: HashingSlots,
    client_ip: ClientIp,
    response: Response,
) -> None:
    token, _ = await sessions.log_in(
        session, limiter, hashing, hashing_slots, client_ip, body
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="lax",
    )


@anonymous_router.post("/tokens", status_code=201)
async def create_token(
    body: Login,
    session: DbSession,
    limiter: Limiter,
    hashing: Hashing,
    hashing_slots: HashingSlots,
    client_ip: ClientIp,
) -> TokenRead:
    token, login = await sessions.log_in(
        session, limiter, hashing, hashing_slots, client_ip, body
    )
    return TokenRead(token=token, expires_at=login.expires_at)


@anonymous_router.post("/signup", status_code=202, response_class=Response)
async def request_signup(
    body: Signup,
    session: DbSession,
    jobs: Jobs,
    limiter: Limiter,
    client_ip: ClientIp,
) -> None:
    await verification.request_signup(session, jobs, limiter, client_ip, body)


@anonymous_router.post("/signup/confirm", status_code=204, response_class=Response)
async def confirm_signup(
    body: SignupConfirm,
    session: DbSession,
    jobs: Jobs,
    limiter: Limiter,
    hashing: Hashing,
    hashing_slots: HashingSlots,
    secret_key: SecretKey,
    client_ip: ClientIp,
) -> None:
    await verification.confirm_signup(
        session, jobs, limiter, hashing, hashing_slots, secret_key, client_ip, body
    )


@anonymous_router.post("/password-reset", status_code=202, response_class=Response)
async def request_password_reset(
    body: PasswordReset,
    session: DbSession,
    jobs: Jobs,
    limiter: Limiter,
    client_ip: ClientIp,
) -> None:
    await passwords.request_password_reset(session, jobs, limiter, client_ip, body)


@anonymous_router.post(
    "/password-reset/confirm", status_code=204, response_class=Response
)
async def confirm_password_reset(
    body: PasswordResetConfirm,
    session: DbSession,
    limiter: Limiter,
    hashing: Hashing,
    hashing_slots: HashingSlots,
    client_ip: ClientIp,
) -> None:
    await passwords.confirm_password_reset(
        session, limiter, hashing, hashing_slots, client_ip, body
    )


@router.delete(
    "/session",
    status_code=204,
    response_class=Response,
    dependencies=[require(Permission.AUTH_LOG_OUT)],
)
async def delete_session(
    principal: CurrentPrincipal, session: DbSession, response: Response
) -> None:
    await sessions.log_out(session, principal.session_id)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")


@router.post(
    "/email-change",
    status_code=202,
    response_class=Response,
    dependencies=[require(Permission.USERS_WRITE_SELF)],
)
async def request_email_change(
    body: EmailChange,
    principal: CurrentPrincipal,
    session: DbSession,
    jobs: Jobs,
    limiter: Limiter,
    hashing: Hashing,
    client_ip: ClientIp,
) -> None:
    await verification.request_email_change(
        session, jobs, limiter, hashing, principal, client_ip, body
    )


@router.post(
    "/email-change/confirm",
    status_code=204,
    response_class=Response,
    dependencies=[require(Permission.USERS_WRITE_SELF)],
)
async def confirm_email_change(
    body: EmailChangeConfirm,
    principal: CurrentPrincipal,
    session: DbSession,
    jobs: Jobs,
    limiter: Limiter,
    secret_key: SecretKey,
    client_ip: ClientIp,
) -> None:
    await verification.confirm_email_change(
        session, jobs, limiter, secret_key, principal, client_ip, body
    )


@router.post(
    "/password-change",
    status_code=204,
    response_class=Response,
    dependencies=[require(Permission.USERS_WRITE_SELF)],
)
async def change_password(
    body: PasswordChange,
    principal: CurrentPrincipal,
    session: DbSession,
    jobs: Jobs,
    limiter: Limiter,
    hashing: Hashing,
    client_ip: ClientIp,
    response: Response,
) -> None:
    await passwords.change_password(
        session, jobs, limiter, hashing, principal, client_ip, body
    )
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
