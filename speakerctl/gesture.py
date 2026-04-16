"""
Button gesture detector: single-tap, double-tap, hold.

Hold fires from a timer started on press — while the button is still held.
Release cancels the hold timer (for short presses) and resolves tap/double-tap.

has_release=True  (evdev): tap fires on release; double-tap waits double_tap_window after release.
has_release=False (HID):   tap/double-tap resolved by timer; hold uses same timer with hold_threshold window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Literal

_LOG = logging.getLogger(__name__)

Gesture = Literal["tap", "double_tap", "hold"]


class GestureDetector:
    """
    Stateful gesture recognizer for one button.

    Usage::

        detector = GestureDetector(my_async_callback, has_double_tap=True, has_hold=True)
        detector.press()    # call on button-down
        detector.release()  # call on button-up (skip if release events unavailable)
    """

    def __init__(
        self,
        on_gesture: Callable[[Gesture], Awaitable[None]],
        *,
        has_double_tap: bool = False,
        has_hold: bool = False,
        double_tap_window: float = 0.4,
        hold_threshold: float = 0.8,
        has_release: bool = True,
    ) -> None:
        self._on_gesture = on_gesture
        self._has_double_tap = has_double_tap
        self._has_hold = has_hold
        self._double_tap_window = double_tap_window
        self._hold_threshold = hold_threshold
        self._has_release = has_release

        self._tap_count: int = 0
        self._hold_fired: bool = False
        self._hold_task: asyncio.Task | None = None
        self._resolve_task: asyncio.Task | None = None

    # ── Public event inputs ─────────────────────────────────────────────────

    def press(self) -> None:
        """Call on button-down."""
        _LOG.debug("gesture press (has_release=%s has_double_tap=%s has_hold=%s tap_count=%d)",
                   self._has_release, self._has_double_tap, self._has_hold, self._tap_count)
        if self._has_release:
            # Clear stale hold state from a previous press whose release was never received.
            self._hold_fired = False
            if self._has_hold:
                # Only start the hold timer if one isn't already running.
                # The device sends burst duplicate press reports for a single physical press;
                # restarting the timer on each duplicate would push hold indefinitely.
                if not self._hold_task or self._hold_task.done():
                    self._hold_task = asyncio.ensure_future(self._hold_timer())
            # Cancel any pending tap/double-tap resolve so a new press resets the window.
            if self._has_double_tap:
                self._cancel(self._resolve_task)
                self._resolve_task = None
        else:
            # No release events: timer-based for everything.
            self._tap_count += 1
            if self._has_hold or self._has_double_tap:
                # Use hold_threshold when hold is configured so a sustained single press
                # reaches the threshold; use double_tap_window otherwise (shorter latency).
                window = self._hold_threshold if self._has_hold else self._double_tap_window
                self._cancel(self._hold_task)
                self._hold_task = asyncio.ensure_future(self._timer_resolve(window))
            else:
                asyncio.ensure_future(self._fire("tap"))

    def release(self) -> None:
        """Call on button-up (meaningful only when has_release=True)."""
        if not self._has_release:
            return

        if self._hold_fired:
            # Hold already fired during this press; ignore the release.
            _LOG.debug("gesture release ignored (hold already fired)")
            self._hold_fired = False
            return

        # Short press: cancel the hold timer and handle tap / double-tap.
        self._cancel(self._hold_task)
        self._hold_task = None
        _LOG.debug("gesture release (tap_count=%d has_double_tap=%s)", self._tap_count, self._has_double_tap)

        if self._has_double_tap:
            self._tap_count += 1
            self._cancel(self._resolve_task)
            self._resolve_task = asyncio.ensure_future(self._double_tap_resolve())
        else:
            asyncio.ensure_future(self._fire("tap"))

    # ── Internal ────────────────────────────────────────────────────────────

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task and not task.done():
            task.cancel()

    async def _hold_timer(self) -> None:
        """Fires after hold_threshold from press (has_release=True path)."""
        _LOG.debug("hold timer started (threshold=%.2fs)", self._hold_threshold)
        try:
            await asyncio.sleep(self._hold_threshold)
        except asyncio.CancelledError:
            _LOG.debug("hold timer cancelled")
            return
        _LOG.debug("hold timer fired → hold")
        # Cancel any pending double-tap window.
        self._cancel(self._resolve_task)
        self._resolve_task = None
        self._tap_count = 0
        self._hold_fired = True
        await self._fire("hold")

    async def _double_tap_resolve(self) -> None:
        """Fires after double_tap_window from last release (has_release=True path)."""
        try:
            await asyncio.sleep(self._double_tap_window)
        except asyncio.CancelledError:
            return
        count = self._tap_count
        self._tap_count = 0
        if count >= 2:
            await self._fire("double_tap")
        else:
            await self._fire("tap")

    async def _timer_resolve(self, window: float) -> None:
        """Fires after window from last press (has_release=False path)."""
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return
        count = self._tap_count
        self._tap_count = 0
        if count >= 2 and self._has_double_tap:
            await self._fire("double_tap")
        elif self._has_hold:
            await self._fire("hold")
        else:
            await self._fire("tap")

    async def _fire(self, gesture: Gesture) -> None:
        try:
            await self._on_gesture(gesture)
        except Exception:
            _LOG.exception("Error in gesture callback for %s", gesture)
