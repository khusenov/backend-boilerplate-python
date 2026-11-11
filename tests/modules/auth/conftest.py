import asyncio
import threading
from collections.abc import AsyncIterator

import pytest

from app.core.resources import Resources


@pytest.fixture
async def resume_hashing(resources: Resources) -> AsyncIterator[threading.Event]:
    resume = threading.Event()
    loop = asyncio.get_running_loop()
    busy = [loop.run_in_executor(resources.hashing, resume.wait, 5) for _ in range(4)]
    yield resume
    resume.set()
    await asyncio.gather(*busy)
