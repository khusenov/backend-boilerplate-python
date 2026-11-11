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
from app.modules.auth import service
from app.modules.auth.models import VerificationCode
from app.modules.auth.schemas import SendCode
from tests import jobs
from tests.factories import PASSWORD, make_user
from tests.jobs import RunJobs, job_ids, job_row, postpone
from tests.outbox import Outbox, SentEmail, TransactionProbe, code_in

EMAIL = "ada@example.com"


@dataclass(slots=True)
class DeliversThenFailsOnce:
    sent: list[SentEmail] = field(default_factory=list)

    async def send(self, *, to: str, subject: str, text: str) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, text=text))
        if len(self.sent) == 1:
            raise ConnectionResetError


async def test_codes_are_stored_as_an_hmac_under_the_secret_key(
    session: AsyncSession,
    settings: Settings,
    resources: Resources,
    run_jobs: RunJobs,
    outbox: Outbox,
) -> None:
    requested_from = datetime.now(UTC)
    await service.request_code(session, resources.jobs, EMAIL, user_id=None)
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
    await service.request_code(session, resources.jobs, EMAIL, user_id=None)

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
    await service.request_code(session, resources.jobs, EMAIL, user_id=None)
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
    await service.request_code(session, resources.jobs, EMAIL, user_id=None)
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
        await service.request_code(session, resources.jobs, EMAIL, user_id=None)
        driver = await driver_connection(session)

    await jobs.run_jobs(driver, settings, replace(resources, email=probe))

    assert probe.savepoint_open == [False]
