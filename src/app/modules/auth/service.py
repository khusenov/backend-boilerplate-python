import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import uuid
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from limits import RateLimitItem, parse
from limits.aio.strategies import MovingWindowRateLimiter
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import flush
from app.core.errors import ConflictError
from app.core.jobs.queue import JobQueue
from app.core.security import Principal
from app.modules.auth.errors import (
    InvalidCodeError,
    InvalidCredentialsError,
    TooManyAttemptsError,
    WrongPasswordError,
)
from app.modules.auth.models import Credential, LoginSession, VerificationCode
from app.modules.auth.schemas import (
    EmailChange,
    EmailChangeConfirm,
    Login,
    PasswordSet,
    SendCode,
    Signup,
    SignupConfirm,
)
from app.modules.users import public as users

SESSION_LIFETIME = timedelta(days=30)
CODE_LIFETIME = timedelta(minutes=15)
PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
LOGINS_PER_ADDRESS = parse("5/hour")
LOGINS_PER_EMAIL = parse("100/hour")
CODE_REQUESTS_PER_ADDRESS = parse("10/hour")
CODE_REQUESTS_PER_EMAIL = parse("5/hour")
GUESSES_PER_ADDRESS = parse("20/hour")
GUESSES_PER_EMAIL = parse("5/15 minutes")


def network_key(client_ip: str | None) -> str:
    try:
        address = ipaddress.ip_address(client_ip or "")
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return str(ipaddress.IPv6Network((address, 64), strict=False).network_address)


@cache
def dummy_hash() -> str:
    return PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def password_matches(password_hash: str | None, password: str) -> bool:
    try:
        PASSWORD_HASHER.verify(password_hash or dummy_hash(), password)
    except VerificationError, InvalidHashError:
        return False
    return password_hash is not None


async def verify_password(
    hashing: Executor, password_hash: str | None, password: str
) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        hashing, password_matches, password_hash, password
    )


async def hash_password(hashing: Executor, password: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(hashing, PASSWORD_HASHER.hash, password)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def code_mac(secret_key: SecretStr, email: str, code: str) -> bytes:
    message = f"verify-email:{email}:{code}".encode()
    return hmac.digest(secret_key.get_secret_value().encode(), message, "sha256")


def derive_code(secret_key: SecretStr, seed: bytes) -> str:
    message = b"verify-email-code:" + seed
    digest = hmac.digest(secret_key.get_secret_value().encode(), message, "sha256")
    return f"{int.from_bytes(digest) % 1_000_000:06d}"


async def count_attempt(
    limiter: MovingWindowRateLimiter, limit: RateLimitItem, *keys: str
) -> None:
    if not await limiter.hit(limit, *keys):
        raise TooManyAttemptsError


async def count_login_attempt(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, LOGINS_PER_ADDRESS, "login", network, email)
    await count_attempt(limiter, LOGINS_PER_EMAIL, "login", email)


async def count_code_request(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, CODE_REQUESTS_PER_ADDRESS, "code_request", network)
    await count_attempt(limiter, CODE_REQUESTS_PER_EMAIL, "code_request", email)


async def count_guess(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, GUESSES_PER_ADDRESS, "guess", network)
    await count_attempt(limiter, GUESSES_PER_EMAIL, "guess", email)


async def find_credential(session: AsyncSession, email: str) -> Credential | None:
    user = await users.find_user_by_email(session, email)
    if user is None or not user.active:
        return None
    statement = (
        select(Credential)
        .where(Credential.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def start_session(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[str, LoginSession]:
    token = secrets.token_urlsafe(32)
    login = LoginSession(
        user_id=user_id,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + SESSION_LIFETIME,
    )
    session.add(login)
    await flush(session, {})
    return token, login


async def log_in(
    session: AsyncSession,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    hashing_slots: asyncio.Semaphore,
    client_ip: str | None,
    data: Login,
) -> tuple[str, LoginSession]:
    await count_login_attempt(limiter, client_ip, data.email)
    async with hashing_slots:
        credential = await find_credential(session, data.email)
        password_hash = None if credential is None else credential.password_hash
        matches = await verify_password(hashing, password_hash, data.password)
        if credential is None or not matches:
            raise InvalidCredentialsError
        if PASSWORD_HASHER.check_needs_rehash(credential.password_hash):
            credential.password_hash = await hash_password(hashing, data.password)
    audit.record(
        session,
        "auth.logged_in",
        actor_id=credential.user_id,
        target_id=credential.user_id,
        client_ip=client_ip,
        changes={},
    )
    return await start_session(session, credential.user_id)


async def authenticate(session: AsyncSession, token: str) -> Principal | None:
    login = await session.scalar(
        select(LoginSession).where(
            LoginSession.token_hash == hash_token(token),
            LoginSession.expires_at > datetime.now(UTC),
        )
    )
    if login is None:
        return None
    user = await users.get_user(session, login.user_id)
    if not user.active:
        return None
    return Principal(user_id=user.id, session_id=login.id, role=user.role)


async def log_out(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(delete(LoginSession).where(LoginSession.id == session_id))


async def set_password(
    session: AsyncSession, hashing: Executor, user_id: uuid.UUID, data: PasswordSet
) -> None:
    password_hash = await hash_password(hashing, data.password)
    await session.execute(
        insert(Credential)
        .values(user_id=user_id, password_hash=password_hash)
        .on_conflict_do_update(
            constraint="uq_auth_credentials_user_id",
            set_={
                "password_hash": password_hash,
                "updated_at": func.statement_timestamp(),
            },
        )
    )
    await session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))


async def create_admin(
    session: AsyncSession, hashing: Executor, email: str, name: str, data: PasswordSet
) -> users.UserDTO:
    admin = await users.create_admin(session, email, name)
    await set_password(session, hashing, admin.id, data)
    return admin


async def save_verification_code(
    session: AsyncSession, email: str, *, user_id: uuid.UUID | None
) -> uuid.UUID:
    seed = secrets.token_bytes(32)
    expires_at = datetime.now(UTC) + CODE_LIFETIME
    statement = (
        insert(VerificationCode)
        .values(email=email, user_id=user_id, seed=seed, expires_at=expires_at)
        .on_conflict_do_update(
            constraint="uq_auth_verification_codes_email",
            set_={
                "user_id": user_id,
                "seed": seed,
                "code_mac": None,
                "expires_at": expires_at,
                "updated_at": func.statement_timestamp(),
            },
        )
        .returning(VerificationCode.id)
    )
    return (await session.execute(statement)).scalar_one()


async def request_code(
    session: AsyncSession, jobs: JobQueue, email: str, *, user_id: uuid.UUID | None
) -> None:
    code_id = await save_verification_code(session, email, user_id=user_id)
    await jobs.defer(session, SendCode(code_id=code_id))


async def find_verification_code(
    session: AsyncSession, code_id: uuid.UUID
) -> VerificationCode | None:
    statement = (
        select(VerificationCode)
        .where(
            VerificationCode.id == code_id,
            VerificationCode.expires_at > datetime.now(UTC),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def issue_code(
    session: AsyncSession, secret_key: SecretStr, verification: VerificationCode
) -> str | None:
    if await users.find_user_by_email(session, verification.email) is not None:
        return None
    code = derive_code(secret_key, verification.seed)
    verification.code_mac = code_mac(secret_key, verification.email, code)
    await flush(session, {})
    return code


async def use_code(
    session: AsyncSession,
    secret_key: SecretStr,
    email: str,
    *,
    user_id: uuid.UUID | None,
    code: str,
) -> None:
    statement = (
        select(VerificationCode)
        .where(
            VerificationCode.email == email,
            VerificationCode.user_id == user_id,
            VerificationCode.expires_at > datetime.now(UTC),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    verification = await session.scalar(statement)
    expected = code_mac(secret_key, email, code)
    if (
        verification is None
        or verification.code_mac is None
        or not hmac.compare_digest(verification.code_mac, expected)
    ):
        raise InvalidCodeError
    await session.delete(verification)


async def request_signup(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    client_ip: str | None,
    data: Signup,
) -> None:
    await count_code_request(limiter, client_ip, data.email)
    await request_code(session, jobs, data.email, user_id=None)


async def confirm_signup(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    hashing_slots: asyncio.Semaphore,
    secret_key: SecretStr,
    client_ip: str | None,
    data: SignupConfirm,
) -> None:
    await count_guess(limiter, client_ip, data.email)
    async with hashing_slots:
        await use_code(session, secret_key, data.email, user_id=None, code=data.code)
        try:
            user = await users.create_user(session, jobs, data.email, data.name)
        except ConflictError:
            raise InvalidCodeError from None
        password = PasswordSet(password=data.password)
        await set_password(session, hashing, user.id, password)


async def request_email_change(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    principal: Principal,
    client_ip: str | None,
    data: EmailChange,
) -> None:
    user = await users.get_user(session, principal.user_id)
    await count_login_attempt(limiter, client_ip, user.email)
    await count_code_request(limiter, client_ip, data.email)
    password_hash = await session.scalar(
        select(Credential.password_hash).where(Credential.user_id == user.id)
    )
    if not await verify_password(hashing, password_hash, data.password):
        raise WrongPasswordError
    await request_code(session, jobs, data.email, user_id=user.id)


async def confirm_email_change(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    secret_key: SecretStr,
    principal: Principal,
    client_ip: str | None,
    data: EmailChangeConfirm,
) -> None:
    await count_guess(limiter, client_ip, data.email)
    await use_code(
        session, secret_key, data.email, user_id=principal.user_id, code=data.code
    )
    try:
        await users.change_email(session, jobs, principal, client_ip, data.email)
    except ConflictError:
        raise InvalidCodeError from None
