"""
Watch evdev nodes for volume and mute button events from the speaker.
Phone and Teams buttons are read from hidraw — evdev is unreliable for BTN_0.
"""
from __future__ import annotations

import asyncio
import logging
from asyncio.subprocess import PIPE
from typing import Literal

import evdev

from .config import Config
from . import executor

_LOG = logging.getLogger(__name__)

Role = Literal["volume", "mute"]

EV_KEY = evdev.ecodes.EV_KEY
KEY_VOLUMEUP = evdev.ecodes.KEY_VOLUMEUP
KEY_VOLUMEDOWN = evdev.ecodes.KEY_VOLUMEDOWN
KEY_MICMUTE = evdev.ecodes.KEY_MICMUTE

KEYUP = 0
KEYDOWN = 1
KEYREPEAT = 2


async def _get_mute_state(alsa_card: str) -> str:
    """Query ALSA for the current Headset capture switch state after a toggle."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "amixer", "-c", alsa_card, "sget", "Headset",
            stdout=PIPE, stderr=PIPE,
        )
        stdout, _ = await proc.communicate()
        return "unmuted" if b"[on]" in stdout else "muted"
    except Exception as exc:
        _LOG.warning("Could not read ALSA mute state: %s", exc)
        return "unknown"


async def watch(path: str, role: Role, config: Config) -> None:
    """
    Async event loop for one evdev node. Raises OSError if the device disappears.
    The daemon's supervisor catches OSError and triggers reconnect logic.
    """
    device = evdev.InputDevice(path)
    _LOG.info("Watching %s (%s) for %s events", path, device.name, role)

    async for event in device.async_read_loop():
        if event.type != EV_KEY:
            continue

        code = event.code

        if role == "volume":
            if event.value not in (KEYDOWN, KEYREPEAT):
                continue
            if code == KEY_VOLUMEUP:
                _LOG.info("volume up")
                await executor.run(config.volume_up.command)
            elif code == KEY_VOLUMEDOWN:
                _LOG.info("volume down")
                await executor.run(config.volume_down.command)

        elif role == "mute":
            if event.value != KEYDOWN or code != KEY_MICMUTE:
                continue
            await asyncio.sleep(0.05)  # wait for snd_usb_audio to update ALSA state
            state = await _get_mute_state(config.alsa_card)
            _LOG.info("mute → %s", state)
            await executor.run(config.mute.command, extra_env={"STATE": state})
