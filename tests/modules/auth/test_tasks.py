import asyncio
import base64
import hashlib
import hmac
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import Delete, Update, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.jobs.queue import JobQueue, driver_connection
from app.core.resources import Resources
from app.core.security import Principal, Role
from app.main import create_app
from app.modules.auth.models import LoginSession, ResetToken, VerificationCode
from app.modules.auth.schemas import (
    PasswordReset,
    SendCode,
    SendPasswordChangedNotice,
    SendResetToken,
)
from app.modules.auth.service import passwords, verification
from app.modules.users import service as users_service
from app.modules.users.models import UserStatus
from app.worker import work
from tests import jobs
from tests.clients import bearer, serve
from tests.factories import (
    PASSWORD,
    make_code,
    make_credential,
    make_login_session,
    make_reset_token,
    make_user,
)
from tests.jobs import (
    RollbackError,
    RunJobs,
    defer_raw,
    finished_status,
    job_ids,
    job_row,
    postpone,
    work_until_finished,
)
from tests.outbox import Outbox, SentEmail, TransactionProbe, code_in, token_in

EMAIL = "ada@example.com"
ADMIN = Principal(user_id=uuid.uuid7(), session_id=uuid.uuid7(), role=Role.ADMIN)


@dataclass(slots=True)
class DeliversThenFailsOnce:
    sent: list[SentEmail] = field(default_factory=list)

    async def send(self, *, to: str, subject: str, text: str) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, text=text))
        if len(self.sent) == 1:
            raise ConnectionResetError


async def request_reset(session: AsyncSession, resources: Resources) -> None:
    await passwords.request_password_reset(
        session, resources.jobs, resources.limiter, None, PasswordReset(email=EMAIL)
    )


async def session_owners(session: AsyncSession) -> list[uuid.UUID]:
    return sorted(await session.scalars(select(LoginSession.user_id)))


async def deactivate_and_roll_back(
    sessionmaker: async_sessionmaker[AsyncSession], queue: JobQueue, user_id: uuid.UUID
) -> None:
    async with sessionmaker.begin() as session:
        await users_service.deactivate_user(session, queue, ADMIN, None, user_id)
        raise RollbackError


async def still_valid(session: AsyncSession) -> list[tuple[str, list[bool]]]:
    now = datetime.now(UTC)
    states: list[tuple[str, list[bool]]] = []
    for model in (LoginSession, VerificationCode, ResetToken):
        expiries = await session.scalars(select(model.expires_at))
        valid = sorted(expires_at > now for expires_at in expiries)
        states.append((model.__tablename__, valid))
    return states


async def wait_until_blocked(
    sessionmaker: async_sessionmaker[AsyncSession], statement: str
) -> None:
    async with asyncio.timeout(10):
        while True:
            async with sessionmaker() as session:
                blocked = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "wait_event_type = 'Lock' AND starts_with(query, :statement)"
                    ),
                    {"statement": statement},
                )
            if blocked:
                return
            await asyncio.sleep(0.05)


async def test_codes_are_stored_as_an_hmac_under_the_secret_key(
    session: AsyncSession,
    settings: Settings,
    resources: Resources,
    run_jobs: RunJobs,
    outbox: Outbox,
) -> None:
    requested_from = datetime.now(UTC)
    await verification.request_code(session, resources.jobs, EMAIL, user_id=None)
    requested_until = datetime.now(UTC)

    await run_jobs()

    code = code_in(outbox.sent[0])
    stored = await session.scalar(select(VerificationCode))
    key = settings.secret_key.get_secret_value().encode()
    message = f"verify-email:{EMAIL}:{code}".encode()
    assert stored is not None
    assert (stored.email, stored.user_id) == (EMAIL, None)
    assert stored.code_mac == hmac.digest(key, message, "sha256")
    assert requested_from + timedelta(minutes=15) <= stored.expires_at
    assert stored.expires_at <= requested_until + timedelta(minutes=15)


async def test_code_jobs_carry_only_the_request_id(
    session: AsyncSession, resources: Resources
) -> None:
    await verification.request_code(session, resources.jobs, EMAIL, user_id=None)

    (job_id,) = await job_ids(session, SendCode.task_name)
    row = await job_row(session, job_id)
    stored = await session.scalar(select(VerificationCode))
    assert stored is not None
    assert row.args == {"code_id": str(stored.id)}
    assert stored.code_mac is None


async def test_a_retried_job_mails_the_code_it_already_mailed(
    client: AsyncClient, session: AsyncSession, settings: Settings, resources: Resources
) -> None:
    email = DeliversThenFailsOnce()
    flaky = replace(resources, email=email)
    await verification.request_code(session, resources.jobs, EMAIL, user_id=None)
    driver = await driver_connection(session)
    with pytest.raises(ConnectionResetError):
        await jobs.run_jobs(driver, settings, flaky)
    (job_id,) = await job_ids(session, SendCode.task_name)
    await postpone(session, job_id, by=timedelta(0))

    await jobs.run_jobs(driver, settings, flaky)

    delivered, retried = (code_in(sent) for sent in email.sent)
    body = {"email": EMAIL, "code": delivered, "name": "Ada", "password": PASSWORD}
    confirmed = await client.post("/v1/auth/signup/confirm", json=body)
    assert delivered == retried
    assert confirmed.status_code == 204


@pytest.mark.parametrize(
    "change",
    [
        delete(VerificationCode),
        update(VerificationCode).values(expires_at=datetime(2000, 1, 1, tzinfo=UTC)),
    ],
    ids=["used", "expired"],
)
async def test_used_and_expired_requests_get_no_email(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    outbox: Outbox,
    change: Delete | Update,
) -> None:
    await verification.request_code(session, resources.jobs, EMAIL, user_id=None)
    await session.execute(change)

    await run_jobs()

    (job_id,) = await job_ids(session, SendCode.task_name)
    row = await job_row(session, job_id)
    assert (row.status, outbox.sent) == ("succeeded", [])


@pytest.mark.parametrize("taken", [False, True], ids=["code", "notice"])
async def test_no_transaction_spans_the_code_email(
    sessionmaker: async_sessionmaker[AsyncSession],
    connection: AsyncConnection,
    settings: Settings,
    resources: Resources,
    taken: bool,
) -> None:
    probe = TransactionProbe(connection)
    async with sessionmaker.begin() as session:
        if taken:
            await make_user(session, email=EMAIL)
        await verification.request_code(session, resources.jobs, EMAIL, user_id=None)
        driver = await driver_connection(session)

    await jobs.run_jobs(driver, settings, replace(resources, email=probe))

    assert probe.savepoint_open == [False]


async def test_reset_tokens_are_stored_as_their_sha256(
    session: AsyncSession,
    settings: Settings,
    resources: Resources,
    run_jobs: RunJobs,
    outbox: Outbox,
) -> None:
    user = await make_user(session, email=EMAIL)
    requested_from = datetime.now(UTC)
    await request_reset(session, resources)
    requested_until = datetime.now(UTC)

    await run_jobs()

    token = token_in(outbox.sent[0])
    stored = await session.scalar(select(ResetToken))
    key = settings.secret_key.get_secret_value().encode()
    assert stored is not None
    digest = hmac.digest(key, b"password-reset-token:" + stored.seed, "sha256")
    assert token == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert (stored.email, stored.user_id) == (EMAIL, user.id)
    assert stored.token_hash == hashlib.sha256(token.encode()).digest()
    assert requested_from + timedelta(minutes=30) <= stored.expires_at
    assert stored.expires_at <= requested_until + timedelta(minutes=30)


async def test_reset_jobs_carry_only_the_request_id(
    session: AsyncSession, resources: Resources
) -> None:
    await make_user(session, email=EMAIL)
    await request_reset(session, resources)

    (job_id,) = await job_ids(session, SendResetToken.task_name)
    row = await job_row(session, job_id)
    stored = await session.scalar(select(ResetToken))
    assert stored is not None
    assert row.args == {"reset_id": str(stored.id)}
    assert (stored.user_id, stored.token_hash) == (None, None)


async def test_a_retried_job_mails_the_token_it_already_mailed(
    client: AsyncClient, session: AsyncSession, settings: Settings, resources: Resources
) -> None:
    await make_user(session, email=EMAIL)
    email = DeliversThenFailsOnce()
    flaky = replace(resources, email=email)
    await request_reset(session, resources)
    driver = await driver_connection(session)
    with pytest.raises(ConnectionResetError):
        await jobs.run_jobs(driver, settings, flaky)
    (job_id,) = await job_ids(session, SendResetToken.task_name)
    await postpone(session, job_id, by=timedelta(0))

    await jobs.run_jobs(driver, settings, flaky)

    delivered, retried = (token_in(sent) for sent in email.sent)
    body = {"token": delivered, "password": "a brand new passphrase"}
    confirmed = await client.post("/v1/auth/password-reset/confirm", json=body)
    assert delivered == retried
    assert confirmed.status_code == 204


@pytest.mark.parametrize(
    "change",
    [
        delete(ResetToken),
        update(ResetToken).values(expires_at=datetime(2000, 1, 1, tzinfo=UTC)),
    ],
    ids=["used", "expired"],
)
async def test_used_and_expired_reset_requests_get_no_email(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    outbox: Outbox,
    change: Delete | Update,
) -> None:
    await make_user(session, email=EMAIL)
    await request_reset(session, resources)
    await session.execute(change)

    await run_jobs()

    (job_id,) = await job_ids(session, SendResetToken.task_name)
    row = await job_row(session, job_id)
    assert (row.status, outbox.sent) == ("succeeded", [])


async def test_reset_requests_for_deactivated_accounts_get_no_email(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs, outbox: Outbox
) -> None:
    await make_user(session, email=EMAIL, status=UserStatus.DEACTIVATED)
    await request_reset(session, resources)

    await run_jobs()

    stored = await session.scalar(select(ResetToken))
    assert stored is not None
    assert (stored.token_hash, outbox.sent) == (None, [])


async def test_deactivated_users_hear_nothing_of_a_password_change(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs, outbox: Outbox
) -> None:
    user = await make_user(session, status=UserStatus.DEACTIVATED)
    await resources.jobs.defer(session, SendPasswordChangedNotice(user_id=user.id))

    await run_jobs()

    (job_id,) = await job_ids(session, SendPasswordChangedNotice.task_name)
    row = await job_row(session, job_id)
    assert (row.status, outbox.sent) == ("succeeded", [])


@pytest.mark.parametrize("notice", [False, True], ids=["reset", "notice"])
async def test_no_transaction_spans_the_password_emails(
    sessionmaker: async_sessionmaker[AsyncSession],
    connection: AsyncConnection,
    settings: Settings,
    resources: Resources,
    notice: bool,
) -> None:
    probe = TransactionProbe(connection)
    async with sessionmaker.begin() as session:
        user = await make_user(session, email=EMAIL)
        if notice:
            payload = SendPasswordChangedNotice(user_id=user.id)
            await resources.jobs.defer(session, payload)
        else:
            await request_reset(session, resources)
        driver = await driver_connection(session)

    await jobs.run_jobs(driver, settings, replace(resources, email=probe))

    assert probe.savepoint_open == [False]


async def test_deactivation_deletes_the_users_sessions_once_its_job_runs(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    leaver, stayer = await make_user(session), await make_user(session)
    headers = bearer(await make_login_session(session, admin))
    for user in (leaver, leaver, stayer):
        await make_login_session(session, user)

    response = await client.delete(f"/v1/users/{leaver.id}", headers=headers)
    before = await session_owners(session)
    await run_jobs()

    assert response.status_code == 204
    assert before == sorted([admin.id, leaver.id, leaver.id, stayer.id])
    assert await session_owners(session) == sorted([admin.id, stayer.id])


@pytest.mark.committed
async def test_the_worker_deletes_sessions_for_committed_deactivations_only(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    async with sessionmaker.begin() as session:
        admin = await make_user(session, role=Role.ADMIN)
        stayer, leaver = await make_user(session), await make_user(session)
        headers = bearer(await make_login_session(session, admin))
        for user in (stayer, leaver, leaver):
            await make_login_session(session, user)
    with pytest.raises(RollbackError):
        await deactivate_and_roll_back(sessionmaker, resources.jobs, stayer.id)
    async with serve(create_app(settings)) as client:
        response = await client.delete(f"/v1/users/{leaver.id}", headers=headers)
    async with sessionmaker() as session:
        (job_id,) = await job_ids(session, "auth.delete_sessions")
        before = await session_owners(session)

    statuses = await work_until_finished(settings, sessionmaker, job_id)

    async with sessionmaker() as session:
        after = await session_owners(session)
    assert response.status_code == 204
    assert before == sorted([admin.id, stayer.id, leaver.id, leaver.id])
    assert (statuses, after) == (["succeeded"], sorted([admin.id, stayer.id]))


@pytest.mark.committed
async def test_logins_during_a_deactivation_leave_no_session(
    app: FastAPI,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resume_hashing: threading.Event,
) -> None:
    async with sessionmaker.begin() as session:
        admin = await make_user(session, role=Role.ADMIN)
        leaver = await make_user(session)
        await make_credential(session, leaver)
        headers = bearer(await make_login_session(session, admin))
    login = {"email": leaver.email, "password": PASSWORD}
    async with serve(app) as client, asyncio.TaskGroup() as group:
        worker = group.create_task(work(settings))
        logging_in = group.create_task(client.post("/v1/auth/tokens", json=login))
        await asyncio.sleep(0.2)
        deactivating = group.create_task(
            client.delete(f"/v1/users/{leaver.id}", headers=headers)
        )
        await asyncio.sleep(0.2)
        resume_hashing.set()
        await deactivating
        async with sessionmaker() as session:
            (job_id,) = await job_ids(session, "auth.delete_sessions")
        status = await finished_status(sessionmaker, job_id)
        worker.cancel()
    async with sessionmaker() as session:
        owners = await session_owners(session)

    replies = [logging_in.result().status_code, deactivating.result().status_code]
    assert (replies, status, owners) == ([201, 204], "succeeded", [admin.id])


async def test_pruning_removes_only_expired_sessions_codes_and_tokens(
    session: AsyncSession, settings: Settings, run_jobs: RunJobs
) -> None:
    expired = datetime(2000, 1, 1, tzinfo=UTC)
    ada = await make_user(session)
    grace = await make_user(session)
    await make_login_session(session, ada)
    await make_login_session(session, grace, expires_at=expired)
    await make_code(session, settings.secret_key, "new@example.com")
    await make_code(session, settings.secret_key, "old@example.com", expires_at=expired)
    await make_reset_token(session, settings.secret_key, ada)
    await make_reset_token(session, settings.secret_key, grace, expires_at=expired)
    await defer_raw(session, "auth.prune_expired_rows", timestamp=0)

    await run_jobs()

    assert await still_valid(session) == [
        ("auth_sessions", [True]),
        ("auth_verification_codes", [True]),
        ("auth_reset_tokens", [True]),
    ]


@pytest.mark.committed
async def test_pruning_spares_a_code_renewed_while_it_runs(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker.begin() as session:
        expired = datetime(2000, 1, 1, tzinfo=UTC)
        await make_code(session, settings.secret_key, EMAIL, expires_at=expired)
        prune = await defer_raw(session, "auth.prune_expired_rows", timestamp=0)
    async with asyncio.TaskGroup() as group:
        async with sessionmaker.begin() as session:
            await verification.save_verification_code(session, EMAIL, user_id=None)
            worker = group.create_task(work(settings))
            await wait_until_blocked(
                sessionmaker, "DELETE FROM auth_verification_codes"
            )
        status = await finished_status(sessionmaker, prune)
        worker.cancel()
    async with sessionmaker() as session:
        codes = await session.scalars(select(VerificationCode.email))

    assert (status, list(codes)) == ("succeeded", [EMAIL])
