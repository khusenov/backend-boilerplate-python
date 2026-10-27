import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

from pydantic import SecretStr


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, text: str) -> None: ...


class EmailRefusedError(Exception):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class SmtpEmailSender:
    host: str
    port: int
    username: SecretStr | None
    password: SecretStr | None
    tls: bool
    from_address: str
    timeout: float = 10

    async def send(self, *, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["Date"] = datetime.now(UTC)
        message["Message-ID"] = make_msgid(domain=self.from_address.rpartition("@")[2])
        message["From"] = self.from_address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        await asyncio.to_thread(self.deliver, message)

    def deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username is not None and self.password is not None:
                smtp.login(
                    self.username.get_secret_value(), self.password.get_secret_value()
                )
            try:
                smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as error:
                codes = [code for code, _ in error.recipients.values()]
                raise EmailRefusedError(f"recipient refused: {codes}") from None
            except smtplib.SMTPDataError as error:
                raise EmailRefusedError(f"message refused: {error.smtp_code}") from None
