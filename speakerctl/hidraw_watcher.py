"""
Watch the hidraw node for phone and Teams button HID reports.

Phone press:  05 01 00
Teams press:  9b 01
Both buttons send a trailing release report we can ignore.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Config, ButtonConfig
from . import executor

_LOG = logging.getLogger(__name__)

PHONE_MAGIC = b"\x05\x01\x00"
TEAMS_MAGIC = b"\x9b\x01"
READ_SIZE = 64


async def watch(path: str, config: Config) -> None:
    """
    Async hidraw reader. Raises OSError if the device disappears (triggers
    reconnect in the daemon supervisor).
    """
    loop = asyncio.get_running_loop()
    last_fire: dict[str, float] = {"phone": 0.0, "teams": 0.0}

    async def _handle(name: str, cfg: ButtonConfig) -> None:
        now = time.monotonic()
        if now - last_fire[name] < cfg.debounce_seconds:
            _LOG.debug("%s button debounced", name)
            return
        last_fire[name] = now
        _LOG.info("%s button pressed", name)
        await executor.run(cfg.command)

    fd = open(path, "rb", buffering=0)
    _LOG.info("Watching %s for phone + teams buttons", path)

    try:
        ready = asyncio.Event()
        loop.add_reader(fd.fileno(), ready.set)

        while True:
            await ready.wait()
            ready.clear()

            data = fd.read(READ_SIZE)
            if not data:
                continue

            _LOG.debug("hidraw: %s", data.hex())

            if data[:3] == PHONE_MAGIC:
                await _handle("phone", config.phone)
            elif data[:2] == TEAMS_MAGIC:
                await _handle("teams", config.teams)

    finally:
        loop.remove_reader(fd.fileno())
        fd.close()
