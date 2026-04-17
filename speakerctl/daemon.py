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
from . import evdev_watcher, executor, hidraw_watcher

_LOG = logging.getLogger(__name__)


class DeviceGoneError(Exception):
    pass


async def _apply_startup_volumes(config: Config) -> None:
    """Set speaker/mic to configured percentages after each device (re)connect."""
    if config.startup_speaker_percent is not None:
        await executor.run(
            f"amixer -c {config.alsa_card} set 'PCM' {config.startup_speaker_percent}%"
        )
        _LOG.info("Set speaker volume to %d%%", config.startup_speaker_percent)
    if config.startup_mic_percent is not None:
        await executor.run(
            f"amixer -c {config.alsa_card} set 'Headset' {config.startup_mic_percent}%"
        )
        _LOG.info("Set mic volume to %d%%", config.startup_mic_percent)


async def _startup_volume_guardian(config: Config) -> None:
    """
    Re-apply startup volumes after delays so we override anything that races
    with us at boot (PulseAudio/PipeWire stream-restore modules, user session
    autostart scripts, etc.).
    """
    for delay in (5, 15):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await _apply_startup_volumes(config)


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

    if devices.teams_evdev:
        tasks.append(loop.create_task(
            _wrap(evdev_watcher.watch(devices.teams_evdev, "teams", config), "teams"),
            name="teams"
        ))
    else:
        _LOG.warning("No teams evdev node found")

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
        if not any([devices.hidraw, devices.volume_evdev, devices.mute_evdev, devices.teams_evdev]):
            _LOG.info("Speaker not found — waiting for device to be plugged in...")
            await _udev_wait_for_device(config.vid, config.pid)
            await asyncio.sleep(0.5)  # brief settle after udev fires
            continue

        _LOG.info("Device found: %s", devices)
        await _apply_startup_volumes(config)
        guardian_task = asyncio.get_event_loop().create_task(
            _startup_volume_guardian(config), name="volume_guardian"
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
