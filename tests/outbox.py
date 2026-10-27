from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True, kw_only=True)
class SentEmail:
    to: str
    subject: str
    text: str


@dataclass(slots=True)
class Outbox:
    sent: list[SentEmail] = field(default_factory=list)

    async def send(self, *, to: str, subject: str, text: str) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, text=text))


@dataclass(slots=True)
class TransactionProbe:
    connection: AsyncConnection
    savepoint_open: list[bool] = field(default_factory=list)

    async def send(self, *, to: str, subject: str, text: str) -> None:
        self.savepoint_open.append(self.connection.in_nested_transaction())
