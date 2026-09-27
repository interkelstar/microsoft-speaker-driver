"""
Watch the hidraw node for phone and Teams button HID reports.

Phone press:  05 01 00
Teams press:  9b 01
Both buttons send a trailing release report we can ignore.
"""
from __future__ import annotations

import asyncio
import logging
import os
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


class ButtonDispatcher:
    """
    Turns press reports into actions: debounce, then single or double tap.

    Only press reports reach this. The release report ``05 00 00`` is shared
    with the mute button, which is what made the first gesture layer fire the
    phone action on every mute (removed 2026-04-17); telling taps apart by
    presses alone needs no release at all.

    With a ``double_tap_command`` a single tap waits ``double_tap_seconds`` to
    see whether a second press follows. A button that interrupts playback does
    not wait: a first press during an answer ends it at once, as before.
    """

    # Two press reports closer than this are one press bouncing, not a person
    # pressing twice — nobody double-taps in under 80 ms.
    BOUNCE_SECONDS = 0.08

    def __init__(self, config: Config, lva_client: Optional["LVAClient"] = None) -> None:
        self._config = config
        self._lva = lva_client
        self._last_fire: dict[str, float] = {"phone": 0.0, "teams": 0.0}
        self._pending: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        # Actions run beside the reader, never inside it: a webhook that takes
        # a second must not hold up the next report, least of all the second
        # press of a double tap.
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def press(self, name: str, cfg: ButtonConfig, now: Optional[float] = None) -> None:
        if now is None:
            now = time.monotonic()
        since = now - self._last_fire[name]
        if since < max(cfg.debounce_seconds, self.BOUNCE_SECONDS):
            _LOG.debug("%s button debounced", name)
            return
        self._last_fire[name] = now

        if not cfg.double_tap_command:
            self._spawn(handle_press(name, cfg, self._lva))
            return

        pending = self._pending.pop(name, None)
        if pending is not None and not pending.done():
            pending.cancel()
            _LOG.info("%s button double-tapped", name)
            self._spawn(executor.run(cfg.double_tap_command))
            return

        if cfg.interrupts_playback and self._lva is not None:
            if await self._lva.interrupt_playback():
                _LOG.info("%s button stopped the assistant", name)
                return

        self._pending[name] = self._spawn(self._single_after(name, cfg))

    async def _single_after(self, name: str, cfg: ButtonConfig) -> None:
        await asyncio.sleep(cfg.double_tap_seconds)
        self._pending.pop(name, None)
        await handle_press(name, cfg, self._lva)

    async def report(self, data: bytes) -> None:
        _LOG.debug("hidraw: %s", data.hex())
        if data[:3] == PHONE_MAGIC:
            await self.press("phone", self._config.phone)
        elif data[:2] == TEAMS_MAGIC:
            await self.press("teams", self._config.teams)


def read_pending(fd: int) -> list[bytes]:
    """
    Every report waiting on a non-blocking hidraw fd, and never block.

    The reader used to wake on an ``asyncio.Event`` and call a blocking
    ``read()``. ``add_reader`` is level-triggered, so a callback queued while a
    report was still unread could run *after* the report had been consumed,
    leaving the event set with nothing to read — and the next ``read()`` then
    froze the whole daemon in the kernel until the next button press. Found
    2026-09-27 with ``/proc/<pid>/stack`` in ``hidraw_read`` for 7 hours: the
    assistant connection timed out (no pings) and reconnected only on a press,
    so volume sync and the Teams interrupt were dead most of the time.
    """
    reports = []
    while True:
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return reports
        if not data:
            return reports
        reports.append(data)


async def watch(
    path: str, config: Config, lva_client: Optional["LVAClient"] = None
) -> None:
    """
    Async hidraw reader. Raises OSError if the device disappears (triggers
    reconnect in the daemon supervisor).
    """
    loop = asyncio.get_running_loop()
    dispatcher = ButtonDispatcher(config, lva_client)

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    _LOG.info("Watching %s for phone + teams buttons", path)

    try:
        ready = asyncio.Event()
        loop.add_reader(fd, ready.set)

        while True:
            await ready.wait()
            ready.clear()
            for data in read_pending(fd):
                await dispatcher.report(data)
    finally:
        loop.remove_reader(fd)
        os.close(fd)
