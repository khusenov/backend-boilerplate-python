import asyncio
import ipaddress
import re
import smtplib
import socket
import ssl
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer
from testcontainers.core.docker_client import DockerClient
from testcontainers.core.wait_strategies import HealthcheckWaitStrategy

from app.core.email import EmailRefusedError, SmtpEmailSender

MAILPIT_IMAGE = re.compile(r"^    image: (axllent/mailpit:\S+)$", re.MULTILINE)
SMTP_PORT = 1025
API_PORT = 8025
BYE = b"221 bye\r\n"
PLAINTEXT_REPLIES = {
    b"EHLO": b"250-plaintext\r\n250 AUTH PLAIN\r\n",
    b"AUTH": b"235 ok\r\n",
    b"QUIT": BYE,
}
REFUSAL = b"550 5.1.1 <ada@example.com>: mailbox unavailable\r\n"


def sender_for(host: str, port: int) -> SmtpEmailSender:
    return SmtpEmailSender(
        host=host,
        port=port,
        username=None,
        password=None,
        tls=False,
        from_address="noreply@example.com",
    )


async def send_welcome(sender: SmtpEmailSender) -> None:
    await sender.send(
        to="ada@example.com", subject="Welcome", text="Hi Ada,\n\nWelcome."
    )


async def expect_disconnect(sender: SmtpEmailSender) -> None:
    with pytest.raises(smtplib.SMTPServerDisconnected):
        await send_welcome(sender)


async def received(mailbox: httpx.AsyncClient) -> list[dict[str, Any]]:
    listing = (await mailbox.get("/messages")).raise_for_status().json()
    return [
        (await mailbox.get(f"/message/{message['ID']}")).raise_for_status().json()
        for message in listing["messages"]
    ]


def is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@asynccontextmanager
async def stub_server(
    replies: Mapping[bytes, bytes],
) -> AsyncIterator[tuple[int, list[bytes]]]:
    commands: list[bytes] = []

    async def answer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"220 stub\r\n")
        while line := await reader.readline():
            command = line.split()[0].upper()
            commands.append(command)
            writer.write(replies.get(command, b"250 ok\r\n"))
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1], commands


@pytest.fixture(scope="module")
def mailpit(pytestconfig: pytest.Config) -> Iterator[DockerContainer]:
    compose = (pytestconfig.rootpath / "compose.yaml").read_text()
    (image,) = MAILPIT_IMAGE.findall(compose)
    certificate = f"sans:{DockerClient().host()}"
    container = (
        DockerContainer(image)
        .with_exposed_ports(SMTP_PORT, API_PORT)
        .with_envs(
            MP_SMTP_TLS_CERT=certificate,
            MP_SMTP_TLS_KEY=certificate,
            MP_SMTP_AUTH_ACCEPT_ANY="true",
            MP_SMTP_DISABLE_RDNS="true",
        )
        .waiting_for(HealthcheckWaitStrategy())
    )
    with container:
        yield container


@pytest.fixture(scope="module")
def mailpit_certificate(
    mailpit: DockerContainer, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    host, port = mailpit.get_container_host_ip(), mailpit.get_exposed_port(SMTP_PORT)
    if is_ip_address(host):
        pytest.skip(f"Mailpit's certificate names DNS hosts, never {host}")
    with smtplib.SMTP(host, port, timeout=5) as smtp:
        smtp.starttls()
        assert isinstance(smtp.sock, ssl.SSLSocket)
        certificate = smtp.sock.getpeercert(binary_form=True)
    assert certificate is not None
    path = tmp_path_factory.mktemp("mailpit") / "certificate.pem"
    path.write_text(ssl.DER_cert_to_PEM_cert(certificate))
    return path


@pytest.fixture
def sender(mailpit: DockerContainer) -> SmtpEmailSender:
    return sender_for(
        mailpit.get_container_host_ip(), mailpit.get_exposed_port(SMTP_PORT)
    )


@pytest.fixture
async def mailbox(mailpit: DockerContainer) -> AsyncIterator[httpx.AsyncClient]:
    host, port = mailpit.get_container_host_ip(), mailpit.get_exposed_port(API_PORT)
    async with httpx.AsyncClient(base_url=f"http://{host}:{port}/api/v1") as client:
        (await client.delete("/messages")).raise_for_status()
        yield client


@pytest.fixture
def silent_server() -> Iterator[int]:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()[1]


@pytest.fixture
async def plaintext_server() -> AsyncIterator[tuple[int, list[bytes]]]:
    async with stub_server(PLAINTEXT_REPLIES) as server:
        yield server


async def test_mail_reaches_the_smtp_server(
    sender: SmtpEmailSender, mailbox: httpx.AsyncClient
) -> None:
    await send_welcome(sender)

    (message,) = await received(mailbox)
    assert (
        message["From"]["Address"],
        [recipient["Address"] for recipient in message["To"]],
        message["Subject"],
        message["Text"].splitlines(),
        message["Username"],
    ) == (
        "noreply@example.com",
        ["ada@example.com"],
        "Welcome",
        ["Hi Ada,", "", "Welcome."],
        "",
    )


async def test_mail_is_dated_and_identified_by_the_sender_domain(
    sender: SmtpEmailSender, mailbox: httpx.AsyncClient
) -> None:
    await send_welcome(sender)

    (message,) = await received(mailbox)
    response = await mailbox.get(f"/message/{message['ID']}/headers")
    headers = response.raise_for_status().json()
    assert (
        parsedate_to_datetime(headers["Date"][0]).utcoffset(),
        headers["Message-Id"][0].rpartition("@")[2],
    ) == (timedelta(0), "example.com>")


async def test_tls_refuses_an_untrusted_certificate(sender: SmtpEmailSender) -> None:
    secure = replace(sender, tls=True)

    with pytest.raises(ssl.SSLCertVerificationError, match="self-signed certificate"):
        await send_welcome(secure)


async def test_tls_logs_in_once_the_certificate_is_trusted(
    sender: SmtpEmailSender,
    mailbox: httpx.AsyncClient,
    mailpit_certificate: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(mailpit_certificate))
    secure = replace(
        sender,
        tls=True,
        username=SecretStr("staging"),
        password=SecretStr("smtp-password"),
    )

    await send_welcome(secure)

    (message,) = await received(mailbox)
    assert message["Username"] == "staging"


async def test_tls_never_falls_back_to_plaintext(
    plaintext_server: tuple[int, list[bytes]],
) -> None:
    port, commands = plaintext_server
    secure = replace(
        sender_for("127.0.0.1", port),
        tls=True,
        username=SecretStr("staging"),
        password=SecretStr("smtp-password"),
    )

    with pytest.raises(smtplib.SMTPNotSupportedError, match="STARTTLS"):
        await send_welcome(secure)

    assert commands == [b"EHLO", b"QUIT"]


@pytest.mark.parametrize(
    ("command", "refusal"),
    [(b"RCPT", "recipient refused: [550]"), (b"DATA", "message refused: 550")],
)
async def test_refusals_report_their_code_but_not_the_address(
    command: bytes, refusal: str
) -> None:
    async with stub_server({command: REFUSAL, b"QUIT": BYE}) as (port, _):
        with pytest.raises(EmailRefusedError) as caught:
            await send_welcome(sender_for("127.0.0.1", port))

    error = caught.value
    assert (str(error), error.__cause__, error.__suppress_context__) == (
        refusal,
        None,
        True,
    )


async def test_a_silent_server_trips_the_timeout(silent_server: int) -> None:
    impatient = replace(sender_for("127.0.0.1", silent_server), timeout=0.1)

    async with asyncio.timeout(5):
        with pytest.raises(smtplib.SMTPServerDisconnected, match="timed out"):
            await send_welcome(impatient)


async def test_waiting_on_the_server_leaves_the_event_loop_free(
    silent_server: int,
) -> None:
    impatient = replace(sender_for("127.0.0.1", silent_server), timeout=0.5)
    loop = asyncio.get_running_loop()

    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        group.create_task(expect_disconnect(impatient))
        started = loop.time()
        await asyncio.sleep(0.05)
        slept = loop.time() - started

    assert slept < 0.25
