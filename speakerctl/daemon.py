"""
Top-level asyncio supervisor. Manages watcher tasks and handles device
plug/unplug via pyudev. Restarts watchers on reconnect without service restart.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pyudev

from .config import Config, load_config
from .discovery import DeviceSet, discover
from . import aec, evdev_watcher, executor, hidraw_watcher, lva, pulse

_LOG = logging.getLogger(__name__)


class DeviceGoneError(Exception):
    pass


def _alsa_card_ready(card: str) -> bool:
    """Check whether an ALSA card with this name is enumerated yet.

    Compares against the parsed card names rather than searching the raw file.
    Each line of /proc/asound/cards carries both a short name in brackets and a
    long description, so a substring test let a host whose *description*
    contained the configured word — "USB Speaker", say — report that a card
    named Speaker was ready. Every amixer call against it then failed, which is
    a broken volume button and a healthy-looking service.
    """
    from .check import alsa_cards
    return card in alsa_cards()


async def _apply_alsa_percent(card: str, control: str, pct: int) -> bool:
    """Set an ALSA mixer control if the card is registered. Returns success."""
    if not _alsa_card_ready(card):
        _LOG.debug("ALSA card %s not registered yet — will retry", card)
        return False
    return await executor.run(f"amixer -c {card} -M set '{control}' {pct}%") == 0


async def _apply_startup_audio(config: Config) -> bool:
    """
    Bring the audio stack to its configured startup state: echo cancellation
    loaded (if enabled), and speaker/mic percentages applied to ALSA and
    optionally PulseAudio/PipeWire. Returns True only once every configured
    target has been confirmed — the guardian keeps retrying for as long as
    this is False.

    AEC is folded in here rather than done once at start-up because it needs
    exactly the same thing the volumes do: a user PulseAudio session that is
    frequently not up yet when the device is enumerated at boot. Reusing the
    guardian means one retry loop covers both.
    """
    ok = True
    pulse_user = config.startup_pulse_user

    if config.aec_enabled:
        if not pulse_user:
            _LOG.error("[aec] enabled but [startup] pulse_user is not set — skipping")
        else:
            ok = ok and await aec.ensure_loaded(
                pulse_user,
                config.aec_source_name,
                config.aec_sink_name,
                config.aec_method,
                config.aec_args,
            )

    if config.startup_speaker_percent is not None:
        pct = config.startup_speaker_percent
        # The two stages can be set apart on purpose. This speaker gates its own
        # microphone while it plays, and the gate keys on how loud it ends up
        # being — so the useful trick is a quiet digital level into a louder
        # amplifier, which lands under the gate at the same loudness in the room.
        alsa_pct = (
            config.startup_speaker_alsa_percent
            if config.startup_speaker_alsa_percent is not None
            else pct
        )
        alsa_ok = await _apply_alsa_percent(config.alsa_card, "PCM", alsa_pct)
        pulse_ok = await pulse.apply_percent(pulse_user, "sink", pct) if pulse_user else True
        if alsa_ok and pulse_ok:
            if alsa_pct == pct:
                _LOG.info("Set speaker volume to %d%%", pct)
            else:
                _LOG.info("Set speaker volume to %d%% digital / %d%% amplifier", pct, alsa_pct)
        ok = ok and alsa_ok and pulse_ok

    if config.startup_mic_percent is not None:
        pct = config.startup_mic_percent
        alsa_ok = await _apply_alsa_percent(config.alsa_card, "Headset", pct)
        pulse_ok = await pulse.apply_percent(pulse_user, "source", pct) if pulse_user else True
        if alsa_ok and pulse_ok:
            _LOG.info("Set mic volume to %d%%", pct)
        ok = ok and alsa_ok and pulse_ok

    return ok


async def _startup_audio_guardian(config: Config, already_applied: bool) -> None:
    """
    Keep re-applying the startup audio state until every target is confirmed.
    PipeWire/PulseAudio runs in the user's own systemd instance and is
    frequently not up yet when the USB device is enumerated at boot, so we
    poll readiness (ALSA card in /proc/asound/cards, pulse/pipewire socket in
    /run/user/<uid>) instead of giving up after a couple of fixed delays.
    Polls every 5s for the first minute, then backs off to every 30s so a
    slow user session doesn't leave us busy-looping forever. Exits as soon as
    a full apply is confirmed, or when cancelled (device gone / reload).
    """
    if already_applied:
        return

    elapsed = 0.0
    while True:
        delay = 5.0 if elapsed < 60.0 else 30.0
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        elapsed += delay

        if await _apply_startup_audio(config):
            _LOG.debug("Startup audio state confirmed — guardian exiting")
            return


def _make_tasks(
    devices: DeviceSet, config: Config, lva_client: "lva.LVAClient | None" = None
) -> list[asyncio.Task]:
    """
    Build the watcher tasks whose death means the device went away.

    Only hardware watchers belong in here: the caller waits on this list with
    FIRST_COMPLETED and reads any finished task as an unplug, so anything that
    can legitimately end (the LVA client, the startup guardian) has to be run
    beside it rather than in it.
    """
    tasks = []
    loop = asyncio.get_event_loop()

    async def _wrap(coro, name: str):
        try:
            await coro
        except OSError as exc:
            _LOG.warning("%s: device gone (%s)", name, exc)
            raise DeviceGoneError(name) from exc

    if devices.volume_evdev:
        tasks.append(loop.create_task(
            _wrap(
                evdev_watcher.watch(devices.volume_evdev, "volume", config, lva_client),
                "volume",
            ),
            name="volume"
        ))
    else:
        _LOG.warning("No volume evdev node found")

    if devices.mute_evdev:
        tasks.append(loop.create_task(
            _wrap(
                evdev_watcher.watch(devices.mute_evdev, "mute", config, lva_client),
                "mute",
            ),
            name="mute"
        ))
    else:
        _LOG.warning("No mute evdev node found")

    if devices.hidraw:
        tasks.append(loop.create_task(
            _wrap(hidraw_watcher.watch(devices.hidraw, config, lva_client), "hidraw"),
            name="hidraw"
        ))
    else:
        _LOG.warning("No hidraw node found")

    return tasks


async def _udev_wait_for_device(vid: str, pid: str) -> None:
    """Block until udev reports the speaker being added."""
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("usb")

    loop = asyncio.get_running_loop()
    found = asyncio.Event()

    def _process() -> None:
        device = monitor.poll(timeout=0)
        if device is None:
            return
        if (device.action == "add"
                and device.get("ID_VENDOR_ID") == vid
                and device.get("ID_MODEL_ID") == pid):
            _LOG.info("Speaker reconnected (udev)")
            found.set()

    monitor.start()
    loop.add_reader(monitor.fileno(), _process)
    try:
        await found.wait()
    finally:
        loop.remove_reader(monitor.fileno())
        monitor.stop()


async def run(config_path: str, reload_event: asyncio.Event) -> None:
    config = load_config(config_path)
    _LOG.info("speakerctl starting (vid=%s pid=%s)", config.vid, config.pid)

    while True:
        # Discover device nodes
        devices = discover(config.vid, config.pid)
        if not any([devices.hidraw, devices.volume_evdev, devices.mute_evdev]):
            _LOG.info("Speaker not found — waiting for device to be plugged in...")
            await _udev_wait_for_device(config.vid, config.pid)
            await asyncio.sleep(0.5)  # brief settle after udev fires
            continue

        _LOG.info("Device found: %s", devices)
        loop = asyncio.get_event_loop()
        initial_ok = await _apply_startup_audio(config)
        guardian_task = loop.create_task(
            _startup_audio_guardian(config, initial_ok), name="audio_guardian"
        )

        # Side tasks: they may end on their own without meaning the device is
        # gone, so they are cancelled with the watchers but never waited on
        # alongside them.
        side_tasks = [guardian_task]
        lva_client = lva.LVAClient(config) if config.lva_enabled else None
        if lva_client is not None:
            side_tasks.append(loop.create_task(lva_client.run(), name="lva"))

        tasks = _make_tasks(devices, config, lva_client)

        if not tasks:
            _LOG.error("No device nodes could be opened — check permissions")
            for t in side_tasks:
                t.cancel()
            await asyncio.sleep(5)
            continue

        # Run until a watcher dies (device gone) or reload is requested
        reload_task = loop.create_task(reload_event.wait(), name="reload")
        all_tasks = tasks + [reload_task]

        done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

        # Cancel everything still running, side tasks included
        for t in list(pending) + side_tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, DeviceGoneError, OSError):
                pass

        # Was it a config reload?
        if reload_task in done and not reload_task.cancelled():
            _LOG.info("Reloading config from %s", config_path)
            reload_event.clear()
            try:
                config = load_config(config_path)
                _LOG.info("Config reloaded OK")
            except Exception as exc:
                _LOG.error("Config reload failed: %s — keeping old config", exc)
            continue

        # Otherwise a watcher died — device gone, wait for reconnect
        for t in done:
            if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                _LOG.info("Watcher %s ended: %s", t.get_name(), t.exception())

        _LOG.info("Watchers stopped — waiting for device reconnect...")
        await _udev_wait_for_device(config.vid, config.pid)
        await asyncio.sleep(0.5)
