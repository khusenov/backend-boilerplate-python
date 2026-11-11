from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import psycopg
from procrastinate import App, Blueprint, PsycopgConnector
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Payload(Message):
    task_name: ClassVar[str]


class Event(Message):
    event_type: ClassVar[str]


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
    subscribers: Mapping[str, tuple[str, ...]]

    async def defer(self, session: AsyncSession, payload: Payload) -> int:
        async with session.begin_nested():
            deferrer = self.app.configure_task(
                payload.task_name,
                allow_unknown=False,
                connection=await driver_connection(session),
            )
            return await deferrer.defer_async(**payload.model_dump(mode="json"))

    async def publish(self, session: AsyncSession, event: Event) -> None:
        fields = event.model_dump(mode="json")
        connection = await driver_connection(session)
        for task_name in self.subscribers.get(event.event_type, ()):
            deferrer = self.app.configure_task(
                task_name, allow_unknown=False, connection=connection
            )
            await deferrer.defer_async(**fields)
