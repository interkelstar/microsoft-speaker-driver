"""
Watch evdev nodes for volume and mute button events from the speaker.
Phone and Teams buttons are read from hidraw — evdev is unreliable for BTN_0.
"""
from __future__ import annotations

import asyncio
import logging
from asyncio.subprocess import PIPE
from typing import TYPE_CHECKING, Literal, Optional

import evdev

from .config import Config
from . import executor

if TYPE_CHECKING:  # import cycle at type-check time only; lva imports us for real
    from .lva import LVAClient

_LOG = logging.getLogger(__name__)

Role = Literal["volume", "mute"]

EV_KEY = evdev.ecodes.EV_KEY
KEY_VOLUMEUP = evdev.ecodes.KEY_VOLUMEUP
KEY_VOLUMEDOWN = evdev.ecodes.KEY_VOLUMEDOWN
KEY_MICMUTE = evdev.ecodes.KEY_MICMUTE

KEYUP = 0
KEYDOWN = 1
KEYREPEAT = 2


async def read_mute_state(alsa_card: str) -> Optional[bool]:
    """
    Read the Headset capture switch: True when the mic is muted, None when it
    could not be read. The kernel toggles this itself on a button press, so
    this is a read-back of what already happened, not a decision.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "amixer", "-c", alsa_card, "sget", "Headset",
            stdout=PIPE, stderr=PIPE,
        )
        stdout, _ = await proc.communicate()
        return b"[on]" not in stdout
    except Exception as exc:
        _LOG.warning("Could not read ALSA mute state: %s", exc)
        return None


async def _get_mute_state(alsa_card: str) -> str:
    """The same reading, as the word passed to the mute script's $STATE."""
    muted = await read_mute_state(alsa_card)
    if muted is None:
        return "unknown"
    return "muted" if muted else "unmuted"


async def _volume_step(
    config: Config, lva_client: Optional["LVAClient"], *, up: bool
) -> None:
    """
    Hand the press to the voice assistant, falling back to the local mixer
    when it is not there to take it.

    The fallback carries more weight than it looks. The assistant is a
    container that gets stopped, rebuilt and redeployed, and a volume button
    that goes dead every time that happens would make this a worse speaker
    than the one we started with. Sync is an enhancement; the buttons keep
    working without it.
    """
    if lva_client is not None and await lva_client.volume_step(up):
        return
    await executor.run(config.volume_up.command if up else config.volume_down.command)


async def watch(
    path: str,
    role: Role,
    config: Config,
    lva_client: Optional["LVAClient"] = None,
) -> None:
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
                await _volume_step(config, lva_client, up=True)
            elif code == KEY_VOLUMEDOWN:
                _LOG.info("volume down")
                await _volume_step(config, lva_client, up=False)

        elif role == "mute":
            if event.value != KEYDOWN or code != KEY_MICMUTE:
                continue
            await asyncio.sleep(0.05)  # wait for snd_usb_audio to update ALSA state
            state = await _get_mute_state(config.alsa_card)
            _LOG.info("mute → %s", state)
            await executor.run(config.mute.command, extra_env={"STATE": state})
            # Purely additive: the kernel has already muted the mic, this only
            # lets the assistant know, so it can show the right state in Home
            # Assistant instead of listening to a microphone that is off.
            if lva_client is not None and state != "unknown":
                await lva_client.report_mute(state == "muted")
