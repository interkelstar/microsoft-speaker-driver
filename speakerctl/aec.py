"""
Acoustic echo cancellation.

Loads PulseAudio's module-echo-cancel into the desktop user's session under
known source and sink names, so anything that wants echo-cancelled audio can
point at them by name instead of guessing at the default devices.

Why this lives on the host: the module is loaded into the *host* PulseAudio
either way — a container can only ever reach it through the mounted socket —
so doing it here lets the voice-assistant container run a stock upstream
image instead of carrying a private patch for it.
"""
from __future__ import annotations

import logging

from . import executor

_LOG = logging.getLogger(__name__)


def _ensure_cmd(pulse_user: str, source_name: str, sink_name: str, method: str) -> str:
    """
    Build a shell command that leaves the named echo-cancel source and sink
    loaded, and exits non-zero if it could not manage that.

    Idempotent: an instance already providing both names is left alone, so
    this is safe to call on every reconnect. A module loaded under *different*
    names is unloaded first — PulseAudio will happily hold several
    module-echo-cancel instances, and a stale one is both a waste and a
    source of confusion about which device is actually cancelled.
    """
    sudo = f"sudo -u {pulse_user} "
    xdg = f'XDG_RUNTIME_DIR="/run/user/$(id -u {pulse_user})"'
    pactl = f"{sudo}{xdg} pactl"
    return (
        f'have_src=$({pactl} list sources short | awk -v n={source_name} \'$2==n {{print $2; exit}}\'); '
        f'have_snk=$({pactl} list sinks   short | awk -v n={sink_name}   \'$2==n {{print $2; exit}}\'); '
        f'if [ -n "$have_src" ] && [ -n "$have_snk" ]; then exit 0; fi; '
        f'for m in $({pactl} list modules short | awk \'/module-echo-cancel/ {{print $1}}\'); do '
        f'{pactl} unload-module "$m" || true; done; '
        f"{pactl} load-module module-echo-cancel "
        f"source_name={source_name} sink_name={sink_name} aec_method={method}"
    )


async def ensure_loaded(
    pulse_user: str,
    source_name: str = "aec_mic",
    sink_name: str = "aec_speaker",
    method: str = "webrtc",
) -> bool:
    """Ensure the echo-cancel source and sink exist. Returns True if they do."""
    rc = await executor.run(_ensure_cmd(pulse_user, source_name, sink_name, method))
    if rc == 0:
        _LOG.debug("AEC ready (source=%s sink=%s)", source_name, sink_name)
        return True
    _LOG.debug("AEC not ready yet (pactl exited %s) — will retry", rc)
    return False
