"""
Setting the speaker's volume in the user's PulseAudio/PipeWire session.

Lives apart from daemon.py because both the startup sequence and the assistant
sync need it, and the latter is imported *by* the daemon.
"""
from __future__ import annotations

import logging
import pwd
from pathlib import Path

from . import executor

_LOG = logging.getLogger(__name__)


def pactl_cmd(pulse_user: str, kind: str, percent: int) -> str:
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


def socket_ready(pulse_user: str) -> bool:
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


async def apply_percent(pulse_user: str, kind: str, pct: int) -> bool:
    """Set a sink or source volume if the session is up. Returns success."""
    if not socket_ready(pulse_user):
        _LOG.debug("PulseAudio/PipeWire session for %s not up yet — will retry", pulse_user)
        return False
    return await executor.run(pactl_cmd(pulse_user, kind, pct)) == 0
