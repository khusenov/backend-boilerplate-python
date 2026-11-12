import base64
import hashlib
import hmac
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import Delete, Update, delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.jobs.queue import driver_connection
from app.core.resources import Resources
from app.modules.auth.models import ResetToken, VerificationCode
from app.modules.auth.schemas import (
    PasswordReset,
    SendCode,
    SendPasswordChangedNotice,
    SendResetToken,
)
from app.modules.auth.service import passwords, verification
from app.modules.users.models import UserStatus
from tests import jobs
from tests.factories import PASSWORD, make_user
from tests.jobs import RunJobs, job_ids, job_row, postpone
from tests.outbox import Outbox, SentEmail, TransactionProbe, code_in, token_in

EMAIL = "ada@example.com"


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
