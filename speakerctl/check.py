"""
Answer "why isn't it working?" in one screen.

Almost everything that goes wrong with this driver on someone else's machine
fails *quietly and green*: systemd reports ``active (running)`` while a volume
button does nothing, because ``amixer`` is not installed, or the ALSA card is
not called Speaker, or the evdev node was never identified, or ``pulse_user``
is unset so half the features silently turned themselves off. The service has
no way to say any of that, and the user has no reason to suspect it.

So this walks every dependency the daemon has and prints what it found. It is
deliberately dumb — no daemon, no event loop, nothing held open — so it can be
run while the service is running, or instead of it, or by the installer as its
last step.

    speakerctl --check

Exit status is 0 when nothing is broken, 1 when something is, so it can be used
in a script. Warnings alone do not fail: a missing optional feature is not the
same as a driver that cannot work.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__
from . import discovery

OK, WARN, FAIL = "ok", "warn", "fail"

_MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


class Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, status: str, what: str, detail: str = "") -> None:
        self.rows.append((status, what, detail))

    def section(self, title: str) -> None:
        self.rows.append(("", title, ""))

    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)

    def warned(self) -> bool:
        return any(s == WARN for s, _, _ in self.rows)

    def render(self) -> None:
        for status, what, detail in self.rows:
            if not status:
                print(f"\n{what}")
                continue
            line = f"  [{_MARK[status]}] {what}"
            if detail:
                line = f"{line:<46} {detail}"
            print(line)


def alsa_cards() -> List[str]:
    """Card names from /proc/asound/cards.

    Parsed rather than substring-matched. The file looks like::

         0 [PCH            ]: HDA-Intel - HDA Intel PCH

    and an earlier version asked whether the configured name appeared anywhere
    in the whole file. A host with a card *described* as "USB Speaker" then
    reported that a card *named* Speaker was ready, and every amixer call
    against it failed afterwards.
    """
    try:
        text = Path("/proc/asound/cards").read_text()
    except OSError:
        return []
    names = []
    for line in text.splitlines():
        if "[" in line and "]" in line:
            names.append(line.split("[", 1)[1].split("]", 1)[0].strip())
    return names


def _check_python(rep: Report) -> None:
    v = sys.version_info
    if v >= (3, 11):
        rep.add(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        rep.add(FAIL, "Python", f"{v.major}.{v.minor} — need 3.11+ for tomllib")


def _check_tools(rep: Report, need_pulse: bool) -> None:
    amixer = shutil.which("amixer")
    if amixer:
        rep.add(OK, "amixer", amixer)
    else:
        rep.add(FAIL, "amixer", "missing — install alsa-utils; volume and mute "
                                "both go through it")
    for tool, pkg in (("pactl", "pulseaudio-utils"), ("parec", "pulseaudio-utils")):
        path = shutil.which(tool)
        if path:
            rep.add(OK, tool, path)
        elif need_pulse:
            rep.add(FAIL, tool, f"missing — install {pkg}")
        else:
            rep.add(WARN, tool, f"missing ({pkg}); only needed with [startup] pulse_user")


def _check_device(rep: Report) -> discovery.DeviceSet:
    devs = discovery.discover()
    found = any((devs.hidraw, devs.volume_evdev, devs.mute_evdev))
    if not found:
        rep.add(FAIL, f"speaker {discovery.VENDOR_ID}:{discovery.PRODUCT_ID}",
                "not on the USB bus — plugged in?")
        return devs
    rep.add(OK, f"speaker {discovery.VENDOR_ID}:{discovery.PRODUCT_ID}", "found")

    for label, node, what in (
        ("hidraw node", devs.hidraw, "phone + teams buttons"),
        ("volume evdev node", devs.volume_evdev, "volume up/down"),
        ("mute evdev node", devs.mute_evdev, "mute button"),
    ):
        if not node:
            rep.add(FAIL, label, f"not identified — {what} will not work")
        elif not os.access(node, os.R_OK):
            rep.add(FAIL, label, f"{node} not readable — udev rule applied? "
                                 "is the service user in group 'input'?")
        else:
            rep.add(OK, label, node)
    return devs


def _check_alsa(rep: Report, card: str) -> None:
    cards = alsa_cards()
    if not cards:
        rep.add(FAIL, "ALSA cards", "/proc/asound/cards unreadable — no sound stack?")
    elif card in cards:
        rep.add(OK, f"ALSA card '{card}'", "present")
    else:
        rep.add(FAIL, f"ALSA card '{card}'", "not present; found: " + ", ".join(cards)
                + " — set [device] alsa_card")


def _check_pulse(rep: Report, pulse_user: Optional[str]) -> None:
    if not pulse_user:
        rep.add(WARN, "[startup] pulse_user", "unset — PulseAudio volume, the AEC "
                                              "module and the mute probe are all off")
        return
    from . import pulse
    if pulse.socket_ready(pulse_user):
        rep.add(OK, f"PulseAudio session for '{pulse_user}'", "reachable")
    else:
        rep.add(FAIL, f"PulseAudio session for '{pulse_user}'",
                "no socket under /run/user/<uid> — is that user logged in?")


def _check_lva(rep: Report, cfg) -> None:
    if not getattr(cfg, "lva_enabled", False):
        rep.add(WARN, "[lva] voice assistant sync", "disabled (this is the default; "
                                                    "buttons work without it)")
        return
    from .lva import _import_connect
    if _import_connect() is None:
        rep.add(FAIL, "websockets package", "missing — [lva] sync will stay off; "
                                            "re-run install.sh")
    else:
        rep.add(OK, "websockets package", "importable")

    url = getattr(cfg, "lva_url", "")
    host, port = _split_ws_url(url)
    if host is None:
        rep.add(WARN, "[lva] url", f"cannot parse {url!r}")
        return
    try:
        with socket.create_connection((host, port), timeout=2):
            rep.add(OK, f"assistant at {host}:{port}", "listening")
    except OSError as exc:
        rep.add(WARN, f"assistant at {host}:{port}",
                f"not reachable ({exc.__class__.__name__}) — it is a separate "
                "service and may simply not be running")


def _split_ws_url(url: str) -> Tuple[Optional[str], int]:
    try:
        rest = url.split("://", 1)[1]
        hostport = rest.split("/", 1)[0]
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            return host, int(port)
        return hostport, 80
    except (IndexError, ValueError):
        return None, 0


def _check_coherence(rep: Report, cfg) -> None:
    """Combinations that are accepted, start cleanly, and then do nothing.

    Each of these turns a feature off at runtime with at most a DEBUG line, so
    the only symptom is that something the config appears to enable never
    happens.
    """
    pulse_user = getattr(cfg, "startup_pulse_user", None)
    if getattr(cfg, "lva_enabled", False) and not pulse_user:
        if getattr(cfg, "lva_sync_volume", False):
            rep.add(WARN, "[lva] sync_volume", "on, but [startup] pulse_user is unset "
                                               "— the assistant's volume lands nowhere")
        if getattr(cfg, "lva_sync_mute", False):
            rep.add(WARN, "[lva] sync_mute", "on, but [startup] pulse_user is unset "
                                             "— the mute button cannot report")
    if getattr(cfg, "aec_enabled", False) and not pulse_user:
        rep.add(WARN, "[aec] enabled", "on, but [startup] pulse_user is unset — "
                                       "the module is never loaded")
    if not getattr(cfg, "lva_enabled", False):
        for name in ("teams", "phone", "mute", "volume_up", "volume_down"):
            btn = getattr(cfg, name, None)
            if btn is not None and getattr(btn, "interrupts_playback", False):
                rep.add(WARN, f"[{name}] interrupts_playback",
                        "on, but [lva] is disabled — it does nothing")


def run_check(config_path: str) -> int:
    # The modules below log their own troubles at WARNING, which lands on
    # stderr ahead of this report and says the same thing twice, less clearly.
    # The report is the output here; the log is not.
    import logging
    logging.getLogger("speakerctl").setLevel(logging.CRITICAL)

    print(f"speakerctl {__version__} — check\n")
    rep = Report()

    rep.section("environment")
    _check_python(rep)

    rep.section("configuration")
    cfg = None
    if not Path(config_path).exists():
        rep.add(FAIL, "config file", f"{config_path} does not exist")
    else:
        try:
            from .config import load_config
            cfg = load_config(config_path)
            rep.add(OK, "config file", config_path)
        except Exception as exc:  # noqa: BLE001 - any parse error is the same news
            rep.add(FAIL, "config file", f"{config_path}: {exc}")

    pulse_user = getattr(cfg, "startup_pulse_user", None) if cfg else None

    rep.section("external commands")
    _check_tools(rep, need_pulse=bool(pulse_user))

    rep.section("device")
    _check_device(rep)
    if cfg is not None:
        _check_alsa(rep, getattr(cfg, "alsa_card", "Speaker"))

    if cfg is not None:
        rep.section("audio session")
        _check_pulse(rep, pulse_user)

        rep.section("voice assistant (optional)")
        _check_lva(rep, cfg)

        _check_coherence(rep, cfg)

    rep.render()

    print()
    if rep.failed():
        print("Something is broken — see the FAIL lines above.")
        return 1
    if rep.warned():
        print("Working, with optional features off — see the warn lines above.")
        return 0
    print("Everything checks out.")
    return 0
