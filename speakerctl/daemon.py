"""
Top-level asyncio supervisor. Manages watcher tasks and handles device
plug/unplug via pyudev. Restarts watchers on reconnect without service restart.
"""
from __future__ import annotations

import asyncio
import logging
import pwd
from pathlib import Path

import pyudev

from .config import Config, load_config
from .discovery import DeviceSet, discover
from . import evdev_watcher, executor, hidraw_watcher

_LOG = logging.getLogger(__name__)


class DeviceGoneError(Exception):
    pass


def _pactl_cmd(pulse_user: str, kind: str, percent: int) -> str:
    """
    Build a shell command that finds the Microsoft USB-C Speaker sink or source
    in `pulse_user`'s PulseAudio session and sets its volume.
    """
    assert kind in ("sink", "source")
    needs_sudo = f"sudo -u {pulse_user} "
    xdg = f'XDG_RUNTIME_DIR="/run/user/$(id -u {pulse_user})"'
    # Exclude .monitor and .echo-cancel — we want the raw hardware sink/source.
    list_cmd = (
        f'{needs_sudo}{xdg} pactl list {kind}s short '
        f'| awk \'/Modern_USB-C_Speaker/ && !/monitor|echo-cancel/ {{print $2; exit}}\''
    )
    # NAME empty => no matching sink/source yet: exit 1 so callers can tell
    # "not found" apart from "pactl ran and succeeded".
    return (
        f'NAME=$({list_cmd}); '
        f'if [ -n "$NAME" ]; then {needs_sudo}{xdg} pactl set-{kind}-volume "$NAME" {percent}%; '
        f'else echo "speakerctl: no PulseAudio {kind} found for speaker" >&2; exit 1; fi'
    )


def _alsa_card_ready(card: str) -> bool:
    """Check whether an ALSA card with this name is enumerated yet."""
    try:
        return card in Path("/proc/asound/cards").read_text()
    except OSError:
        return False


def _pulse_socket_ready(pulse_user: str) -> bool:
    """Check whether pulse_user's PulseAudio/PipeWire user session is up yet.

    We run as our own system user, so /run/user/<uid> (mode 0700) is not ours
    to traverse. Path.exists() swallows ENOENT but lets EACCES through, so a
    naive check raises instead of answering. Permission denied is in fact the
    answer we want: the directory is there, which means the session is up —
    only ENOENT means it is not. Whether pactl can actually talk to it is
    decided by the call itself, which reports its own exit code.
    """
    try:
        uid = pwd.getpwnam(pulse_user).pw_uid
    except KeyError:
        return False
    run_dir = Path(f"/run/user/{uid}")
    for candidate in (run_dir / "pulse" / "native", run_dir / "pipewire-0"):
        try:
            if candidate.exists():
                return True
        except PermissionError:
            return True
        except OSError:
            continue
    return False


async def _apply_alsa_percent(card: str, control: str, pct: int) -> bool:
    """Set an ALSA mixer control if the card is registered. Returns success."""
    if not _alsa_card_ready(card):
        _LOG.debug("ALSA card %s not registered yet — will retry", card)
        return False
    return await executor.run(f"amixer -c {card} -M set '{control}' {pct}%") == 0


async def _apply_pulse_percent(pulse_user: str, kind: str, pct: int) -> bool:
    """Set a PulseAudio/PipeWire sink or source volume if the session is up. Returns success."""
    if not _pulse_socket_ready(pulse_user):
        _LOG.debug("PulseAudio/PipeWire session for %s not up yet — will retry", pulse_user)
        return False
    return await executor.run(_pactl_cmd(pulse_user, kind, pct)) == 0


async def _apply_startup_volumes(config: Config) -> bool:
    """
    Apply configured speaker/mic percentages to ALSA and (optionally)
    PulseAudio/PipeWire. Returns True only once every configured target has
    been confirmed applied — the guardian keeps retrying for as long as this
    is False.
    """
    ok = True
    pulse_user = config.startup_pulse_user

    if config.startup_speaker_percent is not None:
        pct = config.startup_speaker_percent
        alsa_ok = await _apply_alsa_percent(config.alsa_card, "PCM", pct)
        pulse_ok = await _apply_pulse_percent(pulse_user, "sink", pct) if pulse_user else True
        if alsa_ok and pulse_ok:
            _LOG.info("Set speaker volume to %d%%", pct)
        ok = ok and alsa_ok and pulse_ok

    if config.startup_mic_percent is not None:
        pct = config.startup_mic_percent
        alsa_ok = await _apply_alsa_percent(config.alsa_card, "Headset", pct)
        pulse_ok = await _apply_pulse_percent(pulse_user, "source", pct) if pulse_user else True
        if alsa_ok and pulse_ok:
            _LOG.info("Set mic volume to %d%%", pct)
        ok = ok and alsa_ok and pulse_ok

    return ok


async def _startup_volume_guardian(config: Config, already_applied: bool) -> None:
    """
    Keep re-applying startup volumes until every target is confirmed set.
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

        if await _apply_startup_volumes(config):
            _LOG.debug("Startup volumes confirmed applied — guardian exiting")
            return


def _make_tasks(devices: DeviceSet, config: Config) -> list[asyncio.Task]:
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
            _wrap(evdev_watcher.watch(devices.volume_evdev, "volume", config), "volume"),
            name="volume"
        ))
    else:
        _LOG.warning("No volume evdev node found")

    if devices.mute_evdev:
        tasks.append(loop.create_task(
            _wrap(evdev_watcher.watch(devices.mute_evdev, "mute", config), "mute"),
            name="mute"
        ))
    else:
        _LOG.warning("No mute evdev node found")

    if devices.hidraw:
        tasks.append(loop.create_task(
            _wrap(hidraw_watcher.watch(devices.hidraw, config), "hidraw"),
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
        initial_ok = await _apply_startup_volumes(config)
        guardian_task = asyncio.get_event_loop().create_task(
            _startup_volume_guardian(config, initial_ok), name="volume_guardian"
        )
        tasks = _make_tasks(devices, config)

        if not tasks:
            _LOG.error("No device nodes could be opened — check permissions")
            await asyncio.sleep(5)
            continue

        # Run until a watcher dies (device gone) or reload is requested
        reload_task = asyncio.get_event_loop().create_task(reload_event.wait(), name="reload")
        all_tasks = tasks + [reload_task]

        done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

        # Cancel everything still running (including the volume guardian)
        guardian_task.cancel()
        for t in list(pending) + [guardian_task]:
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
