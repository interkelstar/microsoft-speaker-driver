"""
Watch the hidraw node for the phone button HID report.
The phone button sends the 3-byte sequence 0x05 0x01 0x00.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Config
from .gesture import GestureDetector
from . import executor

_LOG = logging.getLogger(__name__)

PHONE_MAGIC = b"\x05\x01\x00"
# Release report sent when the phone button is lifted (report ID 0x05, state 0x00).
PHONE_RELEASE_MAGIC = b"\x05\x00\x00"
READ_SIZE = 64  # max HID report size

# The phone button sends rapid duplicate press reports on a single physical press.
# This debounce gap (seconds) is applied between detector.press() calls in gesture mode
# so that only the first report of each physical press is counted.
# Must be shorter than double_tap_window (default 0.4s) to allow double-tap detection.
_GESTURE_PRESS_DEBOUNCE = 0.15


async def watch(path: str, config: Config) -> None:
    """
    Async hidraw reader. Uses loop.add_reader() to avoid blocking.
    Raises OSError if the device disappears (triggers reconnect in daemon).
    """
    loop = asyncio.get_running_loop()
    cfg = config.phone
    last_fire: float = 0.0
    last_press: float = 0.0  # tracks last detector.press() call for gesture debounce

    # Build gesture detector if any advanced action is configured.
    has_dt = bool(cfg.double_tap_command)
    has_hold = bool(cfg.hold_command)
    detector: GestureDetector | None = None

    if has_dt or has_hold:
        async def _on_phone_gesture(gesture: str) -> None:
            if gesture == "tap":
                _LOG.info("phone button: tap")
                await executor.run(cfg.command)
            elif gesture == "double_tap":
                _LOG.info("phone button: double tap")
                await executor.run(cfg.double_tap_command)
            elif gesture == "hold":
                _LOG.info("phone button: hold")
                await executor.run(cfg.hold_command)

        # has_release=True: we attempt to detect PHONE_RELEASE_MAGIC below.
        # If your device doesn't send a release report, hold detection won't fire;
        # in that case remove hold_command from config and only use tap/double_tap.
        detector = GestureDetector(
            _on_phone_gesture,
            has_double_tap=has_dt,
            has_hold=has_hold,
            double_tap_window=cfg.double_tap_window_seconds,
            hold_threshold=cfg.hold_threshold_seconds,
            has_release=True,
        )

    fd = open(path, "rb", buffering=0)
    _LOG.info("Watching %s for phone button", path)

    try:
        ready = asyncio.Event()

        def _on_readable() -> None:
            ready.set()

        loop.add_reader(fd.fileno(), _on_readable)

        while True:
            await ready.wait()
            ready.clear()

            try:
                data = fd.read(READ_SIZE)
            except OSError:
                raise  # propagate so daemon supervisor triggers reconnect

            if not data:
                continue

            _LOG.debug("hidraw: %s", data.hex())

            if data[:3] == PHONE_MAGIC:
                if detector:
                    now = time.monotonic()
                    if now - last_press >= _GESTURE_PRESS_DEBOUNCE:
                        last_press = now
                        detector.press()
                    else:
                        _LOG.debug("phone gesture debounced (%.3fs since last press)", now - last_press)
                else:
                    now = time.monotonic()
                    if now - last_fire >= cfg.debounce_seconds:
                        last_fire = now
                        _LOG.info("phone button pressed")
                        await executor.run(cfg.command)
                    else:
                        _LOG.debug("phone button debounced (%.2fs since last)", now - last_fire)

            elif detector and data[:3] == PHONE_RELEASE_MAGIC:
                detector.release()

    finally:
        loop.remove_reader(fd.fileno())
        fd.close()