from dataclasses import replace

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.jobs.queue import driver_connection
from app.core.resources import Resources
from app.core.security import Role
from app.modules.users import service
from app.modules.users.schemas import EmailUpdate, SendWelcomeEmail, UserCreate
from tests import jobs
from tests.clients import bearer
from tests.factories import make_login_session, make_user, principal_for
from tests.jobs import RunJobs, job_ids, job_row
from tests.outbox import Outbox, SentEmail, TransactionProbe

CHANGE = EmailUpdate(email="lovelace@example.com")


async def test_new_users_get_a_welcome_email(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs, outbox: Outbox
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    response = await client.post(
        "/v1/users",
        json={"email": "ada@example.com", "name": "Ada Lovelace"},
        headers=bearer(await make_login_session(session, admin)),
    )
    assert (response.status_code, outbox.sent) == (201, [])

    await run_jobs()

    assert outbox.sent == [
        SentEmail(
            to="ada@example.com",
            subject="Welcome",
            text="Hi Ada Lovelace,\n\nYour account is ready.\n",
        )
    ]


async def test_users_deactivated_before_the_job_runs_get_no_email(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs, outbox: Outbox
) -> None:
    admin = principal_for(await make_user(session, role=Role.ADMIN))
    data = UserCreate(email="ada@example.com", name="Ada Lovelace")
    user = await service.create_user(session, resources.jobs, data)
    await service.deactivate_user(session, admin, None, user.id)

    await run_jobs()

    (job_id,) = await job_ids(session, SendWelcomeEmail.task_name)
    row = await job_row(session, job_id)
    assert (row.status, outbox.sent) == ("succeeded", [])


async def test_no_transaction_spans_the_welcome_email(
    sessionmaker: async_sessionmaker[AsyncSession],
    connection: AsyncConnection,
    settings: Settings,
    resources: Resources,
) -> None:
    probe = TransactionProbe(connection)
    async with sessionmaker.begin() as session:
        data = UserCreate(email="ada@example.com", name="Ada Lovelace")
        await service.create_user(session, resources.jobs, data)
        driver = await driver_connection(session)

    await jobs.run_jobs(driver, settings, replace(resources, email=probe))

    assert probe.savepoint_open == [False]


async def test_the_old_address_hears_of_an_email_change(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs, outbox: Outbox
) -> None:
    user = await make_user(session, email="ada@example.com")
    principal = principal_for(user)
    await service.change_email(session, resources.jobs, principal, None, CHANGE)

    await run_jobs()

    assert outbox.sent == [
        SentEmail(
            to="ada@example.com",
            subject="Your email address was changed",
            text=(
                "Your account no longer uses this email address. "
                "If you did not change it, contact us at once.\n"
            ),
        )
    ]


async def test_no_transaction_spans_the_email_change_notice(
    sessionmaker: async_sessionmaker[AsyncSession],
    connection: AsyncConnection,
    settings: Settings,
    resources: Resources,
) -> None:
    probe = TransactionProbe(connection)
    async with sessionmaker.begin() as session:
        user = await make_user(session, email="ada@example.com")
        principal = principal_for(user)
        await service.change_email(session, resources.jobs, principal, None, CHANGE)
        driver = await driver_connection(session)

    await jobs.run_jobs(driver, settings, replace(resources, email=probe))

    assert probe.savepoint_open == [False]
