"""
Client for the voice assistant's peripheral WebSocket API.

Gives the speaker's buttons and the assistant one shared idea of volume and
mute, so that pressing a key on the desk and moving the slider in Home
Assistant are the same act rather than two unrelated ones.

Which gain stage owns the level
-------------------------------
There are two places the level could live — the assistant's software player
and the PulseAudio sink — and applying it in both attenuates twice, so
exactly one has to own it. It is the sink:

    button ─┐
    HA slider ─┼→ assistant's volume (a number, no gain) ─→ pactl set-sink-volume
    assistant ─┘

so that the volume in Home Assistant and the volume in the mixer are the same
quantity rather than two that drift. This requires the assistant to run with
``--external-volume``; without it the software player attenuates as well and
everything is far too quiet.

An earlier revision put the level in the software player instead, reasoning
that ``module-echo-cancel`` taps its playback reference between the two
stages, so a hardware change would leave the adaptive filter matched against
a reference that no longer describes the room. That reasoning does not apply
here: the assistant plays straight to the hardware sink, ``aec_speaker`` sits
suspended outside the path, and the speaker cancels its own output in
firmware anyway — measured at −98 dBFS in the microphone during playback,
some 38 dB below the quiet room floor. Nothing is left for the host canceller
to converge on. Keep this in mind before copying the module to hardware that
does not do its own cancellation, where the old argument would hold.

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
from . import evdev_watcher, executor, pulse

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

    async def _apply_volume(self, volume: Any) -> None:
        """
        Put the assistant's volume onto the system mixer.

        This is the half that makes one slider out of two numbers: whatever
        moved it — a button here, the slider in Home Assistant, the assistant
        itself — arrives as the same broadcast, and lands on the sink. The
        assistant is configured with --external-volume so it no longer
        attenuates in software; without that flag this would apply the
        reduction twice and everything would be far too quiet.
        """
        if not self._config.lva_sync_volume or not self._config.startup_pulse_user:
            return
        try:
            level = float(volume)
        except (TypeError, ValueError):
            _LOG.debug("[lva] ignoring volume %r", volume)
            return

        percent = max(0, min(100, round(level * 100)))
        if self._volume is not None and percent == round(self._volume * 100):
            return
        self._volume = level

        if await pulse.apply_percent(self._config.startup_pulse_user, "sink", percent):
            _LOG.info("[lva] speaker volume now %d%%", percent)
        else:
            _LOG.warning("[lva] could not set the speaker sink to %d%%", percent)

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
            _LOG.info(
                "[lva] connected — assistant volume %s, mute %s",
                data.get("volume"), data.get("muted"),
            )
            # Volume *is* applied from a snapshot, unlike mute: the mixer is
            # ours to drive and the assistant's number is the one the user set
            # in Home Assistant, so a restart on either side re-converges here.
            await self._apply_volume(data.get("volume"))
            # Mute is not applied from a snapshot; see _reconcile.
        elif event == "volume_changed":
            await self._apply_volume(data.get("volume"))
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
                        try:
                            await self._handle_event(raw)
                        except Exception:  # noqa: BLE001
                            # Without this the outer handler catches it, logs
                            # "disconnected" at DEBUG and reconnects — so a
                            # plain bug in here reads as a flaky socket. It
                            # cost an hour once; keep it loud and keep going.
                            _LOG.exception("[lva] error handling an event")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - everything here is retryable
                _LOG.debug("[lva] disconnected (%s)", exc)
            finally:
                self._ws = None
            await asyncio.sleep(_RECONNECT_SECONDS)
