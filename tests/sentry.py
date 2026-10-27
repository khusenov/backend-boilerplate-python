from typing import Any, override

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

FAKE_DSN = "https://public@sentry.invalid/1"


class CapturingTransport(Transport):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.envelopes: list[Envelope] = []
        self.flushes = 0

    @override
    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    @override
    def flush(self, timeout: float, callback: Any | None = None) -> None:
        self.flushes += 1

    def items(self, item_type: str) -> list[dict[str, Any]]:
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == item_type and item.payload.json is not None
        ]

    def events(self) -> list[dict[str, Any]]:
        return self.items("event")

    def payload(self) -> bytes:
        return b"".join(envelope.serialize() for envelope in self.envelopes)


def capture_sentry() -> CapturingTransport:
    transport = sentry_sdk.get_client().transport
    assert isinstance(transport, CapturingTransport)
    return transport
