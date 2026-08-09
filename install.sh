#!/usr/bin/env bash
# speakerctl installer
# Usage: sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Error: run with sudo" >&2
    exit 1
fi

echo "==> speakerctl installer"

# ── Preflight ───────────────────────────────────────────────────────────────
# Everything that can stop the install is checked here, before a system user,
# a venv or a systemd unit exists. A half-finished install is worse than one
# that never started: it leaves an account and a service behind with nothing
# to uninstall them.

# Any interpreter from 3.11 will do -- the floor is tomllib, nothing narrower.
# Asking specifically for `python3.11` used to be the first line of this script,
# which meant it refused to run on Ubuntu 24.04, Fedora 40+, Arch and Debian 13,
# all of which ship something newer. Note the reverse is also a trap: plain
# `python3` is 3.10 on Ubuntu 22.04, where a real 3.11 sits beside it.
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "Error: no Python 3.11 or newer found (needed for tomllib)." >&2
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv" >&2
    echo "  Fedora:         sudo dnf install python3" >&2
    echo "  Arch:           sudo pacman -S python" >&2
    exit 1
fi
echo "    Python: $PYTHON ($("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"

if ! "$PYTHON" -c 'import venv' &>/dev/null; then
    echo "Error: the venv module is missing for $PYTHON." >&2
    echo "  Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
fi

# amixer is not optional. Both volume buttons, the startup levels and every
# mute write go through it, and without it the service starts, reports
# active (running), and does nothing at all when a button is pressed.
if ! command -v amixer &>/dev/null; then
    echo "Error: amixer not found — the volume and mute buttons need it." >&2
    echo "  Debian/Ubuntu:  sudo apt install alsa-utils" >&2
    echo "  Fedora:         sudo dnf install alsa-utils" >&2
    echo "  Arch:           sudo pacman -S alsa-utils" >&2
    exit 1
fi

# pactl/parec are only needed with [startup] pulse_user, which is off by
# default, so their absence is a warning rather than a stop.
PACTL="$(command -v pactl || true)"
PAREC="$(command -v parec || true)"
if [[ -z "$PACTL" || -z "$PAREC" ]]; then
    echo "    Note: pactl/parec not found. The buttons will work; PulseAudio"
    echo "          volume sync, the echo canceller and the mute probe need them"
    echo "          (package: pulseaudio-utils or pipewire-pulse)."
fi

# ── System user ─────────────────────────────────────────────────────────────
if ! id speakerctl &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin speakerctl
    echo "    Created system user: speakerctl"
fi
# plugdev is a Debian-family convention and does not exist on Fedora, Arch or
# openSUSE. Under `set -e` an unconditional usermod aborted the install there,
# after the account had already been created.
joined=()
for grp in input plugdev audio; do   # audio: amixer opens /dev/snd/controlC*
    if getent group "$grp" &>/dev/null; then
        usermod -aG "$grp" speakerctl
        joined+=("$grp")
    fi
done
echo "    User speakerctl in groups: ${joined[*]}"

# ── Python venv ─────────────────────────────────────────────────────────────
echo "==> Installing Python dependencies..."
VENV=/usr/lib/speakerctl-venv
"$PYTHON" -m venv "$VENV"
# websockets is only needed for [lva] sync, but is installed unconditionally:
# the daemon imports it lazily and runs without it, and a venv that silently
# lacks it turns a config change into a mystery.
"$VENV/bin/pip" install --quiet -r requirements.txt
echo "    $(tr '\n' ' ' < requirements.txt | sed 's/#[^ ]*//g') installed in $VENV"

# ── Daemon ──────────────────────────────────────────────────────────────────
echo "==> Installing daemon..."
mkdir -p /usr/lib/speakerctl
cp -r speakerctl/ /usr/lib/speakerctl/
# Deliberately NOT copied into the venv's site-packages as well. PYTHONPATH
# below already points at /usr/lib/speakerctl and precedes site-packages, so a
# second copy is dead weight that shadows nothing -- but it does survive an
# upgrade, so patching /usr/lib and restarting looks like a successful deploy
# while the old code keeps running. That footgun was documented in CLAUDE.md
# for months; the installer was the thing creating it.
#
# Machines installed by an older version already have one, so it is removed
# here rather than left to shadow every future upgrade.
for stale in "$VENV"/lib/python*/site-packages/speakerctl; do
    if [[ -d "$stale" ]]; then
        rm -rf "$stale"
        echo "    Removed stale package copy from $stale"
    fi
done

cat > /usr/local/bin/speakerctl << 'EOF'
#!/bin/bash
# Ignore SIGHUP before exec'ing Python. systemd's ExecReload sends it, and for
# the first fraction of a second the process is still starting the interpreter
# and importing evdev/pyudev/websockets -- Python code cannot install a handler
# that early, and the default action is to terminate. A reload issued right
# after a restart, which is exactly what a deploy script does, would kill the
# daemon and leave the buttons dead with no error anywhere.
# An ignored disposition survives exec, and Python's own signal.signal() later
# replaces it with the real reload handler.
trap '' HUP
exec /usr/lib/speakerctl-venv/bin/python -m speakerctl "$@"
EOF
chmod +x /usr/local/bin/speakerctl
echo "    Installed to /usr/lib/speakerctl"

# ── Config ──────────────────────────────────────────────────────────────────
echo "==> Installing config..."
mkdir -p /etc/speakerctl/scripts

if [[ ! -f /etc/speakerctl/config.toml ]]; then
    cp config.toml /etc/speakerctl/config.toml
    echo "    Created /etc/speakerctl/config.toml"
else
    echo "    Skipping config (already exists — not overwriting)"
fi

for script in examples/*.sh; do
    name=$(basename "$script")
    dest="/etc/speakerctl/scripts/$name"
    if [[ ! -f "$dest" ]]; then
        cp "$script" "$dest"
        chmod +x "$dest"
    fi
done
echo "    Example scripts in /etc/speakerctl/scripts/"

# ── udev rules ──────────────────────────────────────────────────────────────
echo "==> Installing udev rules..."
cp udev/99-microsoft-speaker.rules /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=045e 2>/dev/null || true
echo "    Rules installed, udev reloaded"

# ── sudoers for pactl (required by [startup] pulse_user) ────────────────────
# Skipped entirely when the tools are absent: there is nothing to grant, and a
# rule naming a non-existent binary is a puzzle for whoever reads it next. The
# paths are resolved rather than assumed to be /usr/bin, which they are not on
# NixOS or a /usr/local install -- sudo matches on the literal path and silently
# denies anything else.
if [[ -n "$PACTL" && -n "$PAREC" ]]; then
    echo "==> Installing sudoers rule for pactl/parec..."
    cat > /etc/sudoers.d/speakerctl.tmp << EOF
# Allow the speakerctl daemon to run pactl and parec as any user, without a
# password. Used only when [startup] pulse_user is configured, so the daemon can
# adjust PulseAudio sink/source volumes in that user's session, and read one
# second of microphone audio to tell muted from live -- the speaker mutes in its
# own firmware, and nothing else on the host reflects that. SETENV is required
# so the daemon can pass XDG_RUNTIME_DIR to reach the user's pulse socket.
#
# Remove this file if you do not use [startup] pulse_user.
speakerctl ALL=(ALL) NOPASSWD: SETENV: $PACTL, $PAREC
EOF
    chmod 0440 /etc/sudoers.d/speakerctl.tmp
    # Validated before it is put in place: a malformed file under sudoers.d can
    # lock every user out of sudo on the machine.
    if visudo -cqf /etc/sudoers.d/speakerctl.tmp; then
        mv /etc/sudoers.d/speakerctl.tmp /etc/sudoers.d/speakerctl
        echo "    /etc/sudoers.d/speakerctl installed"
        echo "    (grants speakerctl passwordless $PACTL and $PAREC as any user)"
    else
        rm -f /etc/sudoers.d/speakerctl.tmp
        echo "    WARNING: generated sudoers rule failed validation — skipped."
        echo "             [startup] pulse_user features will not work."
    fi
else
    echo "==> Skipping sudoers rule (pactl/parec not installed)"
fi

# ── systemd ─────────────────────────────────────────────────────────────────
echo "==> Installing systemd service..."
sed "s|ExecStart=|Environment=PYTHONPATH=/usr/lib/speakerctl\nExecStart=|" \
    systemd/speakerctl.service > /etc/systemd/system/speakerctl.service

systemctl daemon-reload
systemctl enable speakerctl
systemctl restart speakerctl
echo "    speakerctl.service enabled and started"

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " speakerctl installed."
echo ""

# `systemctl is-active` used to be the last word here, and it is green whether
# or not anything works: the daemon starts happily with no speaker attached and
# waits on udev. It reported success while the volume button was dead. --check
# looks at the things that actually have to be true.
sleep 1
set +e
speakerctl --check
check_rc=$?
set -e

echo ""
echo " Configure buttons: /etc/speakerctl/config.toml"
echo " Re-run this check: sudo speakerctl --check"
echo " Reload config:     sudo systemctl reload speakerctl"
echo " View logs:         journalctl -u speakerctl -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
exit $check_rc
