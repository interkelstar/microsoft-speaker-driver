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
from typing import Optional, TYPE_CHECKING

from .config import Config, ButtonConfig
from . import executor

if TYPE_CHECKING:
    from .lva import LVAClient

_LOG = logging.getLogger(__name__)

PHONE_MAGIC = b"\x05\x01\x00"
TEAMS_MAGIC = b"\x9b\x01"
READ_SIZE = 64


async def handle_press(
    name: str, cfg: ButtonConfig, lva_client: Optional["LVAClient"] = None
) -> None:
    """
    What a press does, once it has survived debouncing.

    A button marked as interrupting means one press, two meanings: cut the
    answer off if there is one, otherwise do the configured thing. Without it
    the only way to stop a long answer is to be heard over the speaker — and
    pressing the button mid-answer used to end the answer and immediately open
    a fresh conversation, asking what it could help with, which is not what
    anyone reaching for a button mid-sentence wants.
    """
    _LOG.info("%s button pressed", name)

    if cfg.interrupts_playback and lva_client is not None:
        if await lva_client.interrupt_playback():
            _LOG.info("%s button stopped the assistant", name)
            return

    await executor.run(cfg.command)


async def watch(
    path: str, config: Config, lva_client: Optional["LVAClient"] = None
) -> None:
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
        await handle_press(name, cfg, lva_client)

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
