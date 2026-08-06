"""
Client for the voice assistant's peripheral WebSocket API.

Gives the speaker's buttons and the assistant one shared idea of volume and
mute, so that pressing a key on the desk and moving the slider in Home
Assistant are the same act rather than two unrelated ones.

Which gain stage owns the level
-------------------------------
The assistant applies volume in software, before its audio reaches
PulseAudio. The buttons drive the hardware ALSA mixer, after. Letting both
act on one press attenuates twice, so exactly one of them has to own the
number — and it has to be the software one, because of where echo
cancellation sits:

    assistant (software volume) -> aec_speaker -> hardware sink (ALSA PCM)
                                        ^
                          module-echo-cancel taps its playback
                          reference here, between the two stages

A software volume change is already contained in that reference, so
cancellation keeps working across it. A hardware change rescales what the
speaker actually emits *after* the tap, leaving the adaptive filter to
reconverge against a reference that no longer matches the room. Volume
buttons therefore report intent ("up", "down") and let the assistant own the
level; the hardware mixer stays where startup put it.

Mute is synced both ways, but it is not the simple boolean it looks like --
the speaker mutes in its own firmware and nothing on the host reports it, so
the state has to be measured from the audio. See CLAUDE.md and
evdev_watcher.read_mute_state().
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .config import Config
from . import evdev_watcher, executor

_LOG = logging.getLogger(__name__)

# Command names from the assistant's peripheral API (its LVACommand enum).
_CMD_VOLUME_UP = "volume_up"
_CMD_VOLUME_DOWN = "volume_down"
_CMD_MUTE_MIC = "mute_mic"
_CMD_UNMUTE_MIC = "unmute_mic"

_CONNECT_TIMEOUT = 5.0
_RECONNECT_SECONDS = 5.0


def _import_connect():
    """
    Find the websockets client factory, or None if the package is missing.

    Imported here rather than at module scope so that a daemon whose venv
    predates this feature keeps running with sync switched off, instead of
    crash-looping on an ImportError at startup. install.sh pins no versions,
    so both the pre- and post-14 module layouts are worth trying.
    """
    try:
        from websockets.asyncio.client import connect  # websockets >= 14
        return connect
    except ImportError:
        pass
    try:
        from websockets.client import connect  # websockets 10-13
        return connect
    except ImportError:
        return None


class LVAClient:
    """Keeps one WebSocket to the assistant, for as long as the daemon lives."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._ws: Any = None
        # Last mute state we either applied or reported. Both directions write
        # it, which is what makes it usable as the echo guard in _apply_mute.
        self._muted: Optional[bool] = None
        self._volume: Optional[float] = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # ── outbound ────────────────────────────────────────────────────────────

    async def _send(self, command: str, data: Optional[dict] = None) -> bool:
        ws = self._ws
        if ws is None:
            return False
        payload: dict[str, Any] = {"command": command}
        if data:
            payload["data"] = data
        try:
            await ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - a dead socket is not special
            _LOG.debug("[lva] could not send %s: %s", command, exc)
            return False
        _LOG.debug("[lva] -> %s", command)
        return True

    async def volume_step(self, up: bool) -> bool:
        """Ask the assistant to step its own volume. False if it wasn't told."""
        if not self._config.lva_sync_volume:
            return False
        return await self._send(_CMD_VOLUME_UP if up else _CMD_VOLUME_DOWN)

    async def report_mute(self, muted: bool) -> bool:
        """Tell the assistant what the hardware mute button just did."""
        if not self._config.lva_sync_mute:
            return False
        # Recorded before sending: the assistant broadcasts the resulting
        # state back to us, and this is what stops us acting on our own echo.
        self._muted = muted
        return await self._send(_CMD_MUTE_MIC if muted else _CMD_UNMUTE_MIC)

    # ── inbound ─────────────────────────────────────────────────────────────

    async def _reconcile(self) -> None:
        """
        On every connect, tell the assistant what the microphone is really doing.

        Deliberately one-way. The assistant's stored mute state can be stale —
        it may have restarted while the speaker did not — and acting on a stale
        "not muted" would silently reopen a microphone the user muted with the
        physical button. Hardware is the honest source, so hardware wins the
        initial disagreement; the assistant only leads once it is in sync.
        """
        if not self._config.lva_sync_mute:
            return
        muted = await evdev_watcher.read_mute_state(self._config)
        if muted is None:
            return
        self._muted = muted
        if await self._send(_CMD_MUTE_MIC if muted else _CMD_UNMUTE_MIC):
            _LOG.info("[lva] reported mic as %s", "muted" if muted else "live")

    async def _apply_mute(self, muted: bool) -> None:
        """
        Follow a mute change made on the assistant's side.

        Skipped when it only echoes what we just reported: the peripheral API
        broadcasts to every client including the sender, and carries no id
        saying which one caused it. Comparing against the last state we know
        about is the whole loop prevention.
        """
        if not self._config.lva_sync_mute or muted == self._muted:
            return
        self._muted = muted
        # 'Headset' is a capture switch (cswitch, no pswitch), so the tokens
        # are cap/nocap — amixer rejects mute/unmute on it with exit 1.
        token = "nocap" if muted else "cap"
        rc = await executor.run(
            f"amixer -c {self._config.alsa_card} set Headset {token}"
        )
        if rc == 0:
            _LOG.info("[lva] mic %s by the assistant", "muted" if muted else "unmuted")
        else:
            _LOG.warning("[lva] amixer exited %s setting Headset %s", rc, token)

    async def _handle_event(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            _LOG.debug("[lva] ignoring unparseable frame")
            return
        if not isinstance(msg, dict):
            return

        event = msg.get("event", "")
        data = msg.get("data") or {}

        if event == "snapshot":
            self._volume = data.get("volume")
            _LOG.info(
                "[lva] connected — assistant volume %s, mute %s",
                self._volume, data.get("muted"),
            )
            # Mute is not applied from a snapshot; see _reconcile.
        elif event == "volume_changed":
            self._volume = data.get("volume")
            _LOG.debug("[lva] volume now %s", self._volume)
        elif event == "muted":
            # The assistant omits data when it means "muted".
            await self._apply_mute(bool(data.get("muted", True)))

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Connect, and stay connected, retrying forever.

        A dropped WebSocket is a reconnect, never an exit: the assistant is a
        container that gets restarted and rebuilt, and this task outliving
        that is the difference between sync being reliable and being a thing
        that worked until the last deploy.
        """
        connect = _import_connect()
        if connect is None:
            _LOG.error(
                "[lva] sync enabled but the websockets package is missing — "
                "staying off. Re-run install.sh to add it."
            )
            return

        # websockets logs a ping/pong pair at DEBUG every 20s. Left alone that
        # is ~26k journal lines a day, which buries the handful of lines the
        # daemon actually emits. Its INFO and above stay.
        logging.getLogger("websockets").setLevel(logging.INFO)

        synced = [
            name for name, on in (
                ("volume", self._config.lva_sync_volume),
                ("mute", self._config.lva_sync_mute),
            ) if on
        ]
        if not synced:
            _LOG.warning("[lva] enabled but neither volume nor mute is synced")
            return

        url = self._config.lva_url
        _LOG.info("[lva] syncing %s with %s", " and ".join(synced), url)

        while True:
            try:
                async with connect(url, open_timeout=_CONNECT_TIMEOUT) as ws:
                    self._ws = ws
                    await self._reconcile()
                    async for raw in ws:
                        await self._handle_event(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - everything here is retryable
                _LOG.debug("[lva] disconnected (%s)", exc)
            finally:
                self._ws = None
            await asyncio.sleep(_RECONNECT_SECONDS)
