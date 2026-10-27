from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import psycopg
from procrastinate import App, Blueprint, PsycopgConnector
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    task_name: ClassVar[str]


def build_job_app(blueprints: Iterable[Blueprint]) -> App:
    job_app = App(connector=PsycopgConnector())
    for blueprint in blueprints:
        job_app.add_tasks_from(blueprint, namespace="")
    return job_app


async def driver_connection(session: AsyncSession) -> psycopg.AsyncConnection[Any]:
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    return cast(psycopg.AsyncConnection[Any], raw.driver_connection)


@dataclass(frozen=True, slots=True)
class JobQueue:
    app: App

    async def defer(self, session: AsyncSession, payload: Message) -> int:
        async with session.begin_nested():
            deferrer = self.app.configure_task(
                payload.task_name,
                allow_unknown=False,
                connection=await driver_connection(session),
            )
            return await deferrer.defer_async(**payload.model_dump(mode="json"))
