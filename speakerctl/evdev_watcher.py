"""
Watch evdev nodes for button events from the speaker.
Three roles: volume, mute, teams — determined by discovery.py.
"""
from __future__ import annotations

import asyncio
import logging
from asyncio.subprocess import PIPE
from typing import Literal

import evdev

from .config import Config
from .gesture import GestureDetector
from . import executor

_LOG = logging.getLogger(__name__)

Role = Literal["volume", "mute", "teams"]

EV_KEY = evdev.ecodes.EV_KEY
KEY_VOLUMEUP = evdev.ecodes.KEY_VOLUMEUP
KEY_VOLUMEDOWN = evdev.ecodes.KEY_VOLUMEDOWN
KEY_MICMUTE = evdev.ecodes.KEY_MICMUTE
BTN_0 = evdev.ecodes.BTN_0

# event value constants
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
        # amixer sget 'Headset' output contains "[on]" when capturing (unmuted)
        # and "[off]" when muted
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

    # Build gesture detector for teams button if any advanced action is configured.
    teams_detector: GestureDetector | None = None
    if role == "teams":
        cfg = config.teams
        has_dt = bool(cfg.double_tap_command)
        has_hold = bool(cfg.hold_command)
        if has_dt or has_hold:
            async def _on_teams_gesture(gesture: str) -> None:
                if gesture == "tap":
                    _LOG.info("teams button: tap")
                    await executor.run(cfg.command)
                elif gesture == "double_tap":
                    _LOG.info("teams button: double tap")
                    await executor.run(cfg.double_tap_command)
                elif gesture == "hold":
                    _LOG.info("teams button: hold")
                    await executor.run(cfg.hold_command)

            teams_detector = GestureDetector(
                _on_teams_gesture,
                has_double_tap=has_dt,
                has_hold=has_hold,
                double_tap_window=cfg.double_tap_window_seconds,
                hold_threshold=cfg.hold_threshold_seconds,
                has_release=True,
            )

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
            if event.value != KEYDOWN:
                continue
            if code == KEY_MICMUTE:
                await asyncio.sleep(0.05)  # wait for snd_usb_audio to update ALSA state
                state = await _get_mute_state(config.alsa_card)
                _LOG.info("mute → %s", state)
                await executor.run(config.mute.command, extra_env={"STATE": state})

        elif role == "teams":
            if code != BTN_0:
                continue
            if teams_detector:
                if event.value == KEYDOWN:
                    teams_detector.press()
                elif event.value == KEYUP:
                    teams_detector.release()
            else:
                if event.value == KEYDOWN:
                    _LOG.info("teams button")
                    await executor.run(config.teams.command)
