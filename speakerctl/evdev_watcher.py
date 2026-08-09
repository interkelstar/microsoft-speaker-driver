"""
Watch evdev nodes for volume and mute button events from the speaker.
Phone and Teams buttons are read from hidraw — evdev is unreliable for BTN_0.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
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


# Peak sample value below which the microphone is considered muted. A muted
# mic reads exactly 1 (digital silence plus a dither bit); a live one in a
# quiet room never measured below 50 across dozens of samples. Anywhere in
# between is comfortable.
_SILENCE_PEAK = 8
_PROBE_SECONDS = 1
_PROBE_RATE = 16000


def _probe_cmd(pulse_user: str) -> str:
    """
    Build a command that exits 0 if the microphone is silent, 1 if it is live.

    The exit code *is* the answer, because executor.run only hands back a
    return code — which suits a yes/no question and keeps the captured audio
    inside the pipe, measured and discarded, never written anywhere.

    parec is stopped by `head` closing the pipe under it, not by a signal.
    It runs behind sudo, and signals do not reliably reach through: an
    earlier version backgrounded it and killed $!, which was sudo's pid and
    left the real parec recording forever — seven of them accumulated in one
    session. SIGPIPE needs nobody's cooperation. The outer timeout is only a
    backstop for a source that never produces a sample at all, in which case
    nothing would ever close the pipe.
    """
    uid = f"$(id -u {pulse_user})"
    sudo = f"sudo -n -u {pulse_user} "
    xdg = f'XDG_RUNTIME_DIR="/run/user/{uid}"'
    want_bytes = _PROBE_RATE * 2 * _PROBE_SECONDS
    script = (
        f'SRC=$({sudo}{xdg} pactl list sources short '
        f'| awk \'/Modern_USB-C_Speaker/ && !/monitor|echo-cancel/ {{print $2; exit}}\'); '
        f'if [ -z "$SRC" ]; then exit 2; fi; '
        f'{sudo}{xdg} parec --device="$SRC" --format=s16le '
        f'--rate={_PROBE_RATE} --channels=1 --raw --latency-msec=100 '
        f'| head -c {want_bytes} '
        f'| python3 -c \'import sys,struct;d=sys.stdin.buffer.read();n=len(d)//2;'
        f'sys.exit(2 if n==0 else (0 if max(abs(x) for x in '
        f'struct.unpack(f"<{{n}}h",d[:n*2]))<={_SILENCE_PEAK} else 1))\''
    )
    return f"timeout -s KILL {_PROBE_SECONDS + 4} sh -c {shlex.quote(script)}"


async def read_mute_state(config: Config) -> Optional[bool]:
    """
    Decide whether the microphone is muted by listening to it.

    This device mutes inside its own firmware. Measured directly, *nothing*
    on the host reflects that: the Headset capture switch reads [on] on both
    sides of a transition, no other mixer control moves, evdev's LED_MUTE
    stays inactive, and the HID reports encode "button pressed" rather than
    "now muted". Meanwhile the samples go from ~2400 peak to exactly 1. The
    audio itself is the only honest source of truth there is, so that is what
    we read.

    Returns None when it could not be measured, which callers must treat as
    "unknown" rather than as either state.
    """
    pulse_user = config.startup_pulse_user
    if not pulse_user:
        return None
    rc = await executor.run(_probe_cmd(pulse_user), quiet=True)
    if rc == 0:
        return True
    if rc == 1:
        return False
    _LOG.warning("Could not measure microphone state (probe exited %s)", rc)
    return None


async def _get_mute_state(config: Config) -> str:
    """The same reading, as the word passed to the mute script's $STATE."""
    muted = await read_mute_state(config)
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


async def _events(device: "evdev.InputDevice"):
    """
    Yield input events, reading the descriptor the same way hidraw_watcher
    does instead of using evdev's own async_read_loop.

    async_read_loop settles one future per batch and raises InvalidStateError
    when the next batch lands before the previous one has been consumed. The
    traceback is noisy, but the real cost is silent: the event that triggered
    it is **dropped**. Observed directly — a mute press that lit the LED,
    silenced the microphone and arrived on hidraw left no mute line in the
    log at all, only the traceback. This repo already reads phone and Teams
    from hidraw for the same class of unreliability; this makes the evdev
    path just as literal about it.

    OSError from a vanished device is re-raised in the consuming task, which
    is what the supervisor turns into "device gone".
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    failure: list[BaseException] = []

    def _drain() -> None:
        try:
            for event in device.read():
                queue.put_nowait(event)
        except BlockingIOError:
            pass
        except OSError as exc:
            failure.append(exc)
            queue.put_nowait(None)  # wake the consumer so it can re-raise

    loop.add_reader(device.fileno(), _drain)
    try:
        while True:
            event = await queue.get()
            if event is None:
                raise failure[0]
            yield event
    finally:
        loop.remove_reader(device.fileno())


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

    async for event in _events(device):
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
            await asyncio.sleep(0.25)  # let the firmware settle before listening
            state = await _get_mute_state(config)
            _LOG.info("mute → %s", state)
            await executor.run(config.mute.command, extra_env={"STATE": state})
            # Purely additive: the speaker's own firmware has already muted the
            # mic, this only lets the assistant know, so it can show the right
            # state in Home Assistant instead of listening to a microphone that
            # is off. Nothing on the host reports that mute, which is why the
            # state above had to be measured rather than read.
            if lva_client is not None and state != "unknown":
                await lva_client.report_mute(state == "muted")
