from fastapi import APIRouter, Depends, Response

from app.core.http.deps import (
    SESSION_COOKIE,
    ClientIp,
    CurrentPrincipal,
    DbSession,
    Hashing,
    HashingSlots,
    Limiter,
    require_trusted_origin,
)
from app.modules.auth import service
from app.modules.auth.schemas import Login, TokenRead

SESSION_MAX_AGE = int(service.SESSION_LIFETIME.total_seconds())

router = APIRouter(prefix="/auth")


@router.post(
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
    token, _ = await service.log_in(
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


@router.post("/tokens", status_code=201)
async def create_token(
    body: Login,
    session: DbSession,
    limiter: Limiter,
    hashing: Hashing,
    hashing_slots: HashingSlots,
    client_ip: ClientIp,
) -> TokenRead:
    token, login = await service.log_in(
        session, limiter, hashing, hashing_slots, client_ip, body
    )
    return TokenRead(token=token, expires_at=login.expires_at)


@router.delete("/session", status_code=204, response_class=Response)
async def delete_session(
    principal: CurrentPrincipal, session: DbSession, response: Response
) -> None:
    await service.log_out(session, principal.session_id)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
