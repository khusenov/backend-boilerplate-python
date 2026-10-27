import asyncio

import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner, Result

from app.cli import cli
from app.core.config import Settings
from app.core.resources import Resources
from app.core.security import Role
from app.main import create_app
from app.modules.auth.models import Credential, LoginSession
from app.modules.users.models import User
from tests.audit import audit_rows
from tests.clients import bearer, connect
from tests.factories import make_login_session, make_user

ADMIN_PASSWORD = "a long admin password"
TYPED = f"{ADMIN_PASSWORD}\n" * 2
PROMPTS = "Password: \nRepeat for confirmation: \n"

pytestmark = pytest.mark.usefixtures("environment")


async def create_admin(*args: str, typed: str = TYPED) -> Result:
    runner = CliRunner()
    command = ["create-admin", *args]
    return await asyncio.to_thread(runner.invoke, cli, command, input=typed)


def admin_args(email: str, name: str = "Ada") -> list[str]:
    return [email, "--name", name]


async def count(session: AsyncSession, model: type[object]) -> int | None:
    return await session.scalar(select(func.count()).select_from(model))


@pytest.mark.committed
async def test_create_admin_twice_leaves_one_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    first = await create_admin(*admin_args("Ada@Example.com"))
    second = await create_admin(*admin_args("ada@example.com"))

    async with sessionmaker() as session:
        users = list(await session.scalars(select(User)))
        credentials = await count(session, Credential)
        events = await audit_rows(session)
    assert (first.exit_code, first.stdout) == (
        0,
        f"{PROMPTS}ada@example.com is an admin\n",
    )
    assert second.exit_code == 0
    assert [(user.email, user.role) for user in users] == [
        ("ada@example.com", Role.ADMIN)
    ]
    created = (None, "users.admin_created", users[0].id, None, None, {"role": "admin"})
    assert credentials == 1
    assert events == [created, created]


@pytest.mark.committed
async def test_new_admins_log_in_with_their_password(
    settings: Settings, resources: Resources
) -> None:
    await create_admin(*admin_args("ada@example.com"))
    app = create_app(settings, resources)

    async with LifespanManager(app) as manager, connect(manager.app) as client:
        login = await client.post(
            "/v1/auth/tokens",
            json={"email": "ada@example.com", "password": ADMIN_PASSWORD},
        )
        headers = bearer(login.json()["token"])
        created = await client.post(
            "/v1/users",
            json={"email": "grace@example.com", "name": "Grace"},
            headers=headers,
        )

    assert (login.status_code, created.status_code) == (201, 201)


@pytest.mark.committed
async def test_create_admin_promotes_a_member_and_ends_their_sessions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        member = await make_user(session, email="ada@example.com")
        await make_login_session(session, member)

    result = await create_admin(*admin_args("ada@example.com", name="Ada Lovelace"))

    async with sessionmaker() as session:
        promoted = await session.get(User, member.id)
        sessions = await count(session, LoginSession)
    assert result.exit_code == 0
    assert promoted is not None
    assert (promoted.name, promoted.role) == ("Ada Lovelace", Role.ADMIN)
    assert sessions == 0


@pytest.mark.committed
async def test_create_admin_prompts_for_the_name_and_a_hidden_password(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    result = await create_admin("ada@example.com", typed=f"Ada\n{TYPED}")

    async with sessionmaker() as session:
        admin = await session.scalar(select(User))
    assert (result.exit_code, result.stdout) == (
        0,
        f"Name: Ada\n{PROMPTS}ada@example.com is an admin\n",
    )
    assert admin is not None
    assert (admin.name, admin.role) == ("Ada", Role.ADMIN)


@pytest.mark.committed
@pytest.mark.parametrize(
    ("args", "password", "message"),
    [
        (
            admin_args("not-an-email"),
            ADMIN_PASSWORD,
            "email: String should match pattern '^",
        ),
        (
            admin_args("ada@example.com", name=" "),
            ADMIN_PASSWORD,
            "name: String should have at least 1 character\n",
        ),
        (
            admin_args("ada@example.com"),
            "fourteen chars",
            "password: String should have at least 15 characters\n",
        ),
    ],
    ids=["email", "name", "password"],
)
async def test_create_admin_refuses_invalid_input_and_writes_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
    args: list[str],
    password: str,
    message: str,
) -> None:
    result = await create_admin(*args, typed=f"{password}\n" * 2)

    async with sessionmaker() as session:
        users = await count(session, User)
        events = await audit_rows(session)
    assert (result.exit_code, result.stdout) == (2, PROMPTS)
    assert result.stderr.startswith(message)
    assert password not in result.stderr
    assert (users, events) == (0, [])
