#!/usr/bin/env bash
# speakerctl installer
# Usage: sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Error: run with sudo" >&2
    exit 1
fi

if ! command -v python3.11 &>/dev/null; then
    echo "Error: python3.11 not found."
    echo "  Install with: sudo apt install python3.11 python3.11-venv"
    exit 1
fi

echo "==> speakerctl installer"

# ── System user ─────────────────────────────────────────────────────────────
if ! id speakerctl &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin speakerctl
    echo "    Created system user: speakerctl"
fi
usermod -aG input speakerctl
usermod -aG plugdev speakerctl
usermod -aG audio speakerctl   # needed for amixer to open /dev/snd/controlC*
echo "    User speakerctl in groups: input, plugdev, audio"

# ── Python venv ─────────────────────────────────────────────────────────────
echo "==> Installing Python dependencies..."
VENV=/usr/lib/speakerctl-venv
python3.11 -m venv "$VENV"
# websockets is only needed for [lva] sync, but is installed unconditionally:
# the daemon imports it lazily and runs without it, and a venv that silently
# lacks it turns a config change into a mystery.
"$VENV/bin/pip" install --quiet evdev pyudev websockets
echo "    evdev, pyudev, websockets installed in $VENV"

# ── Daemon ──────────────────────────────────────────────────────────────────
echo "==> Installing daemon..."
mkdir -p /usr/lib/speakerctl
cp -r speakerctl/ /usr/lib/speakerctl/
cp -r speakerctl/ "$VENV/lib/python3.11/site-packages/"

cat > /usr/local/bin/speakerctl << 'EOF'
#!/bin/bash
exec /usr/lib/speakerctl-venv/bin/python3.11 -m speakerctl "$@"
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
echo "==> Installing sudoers rule for pactl..."
cat > /etc/sudoers.d/speakerctl << 'EOF'
# Allow the speakerctl daemon to run pactl as any user, without a password.
# Used only when [startup] pulse_user is configured, so the daemon can adjust
# PulseAudio sink/source volumes in that user's session, and read one second
# of microphone audio to tell muted from live -- the speaker mutes in its own
# firmware, and nothing else on the host reflects that. SETENV is required so
# the daemon can pass XDG_RUNTIME_DIR to reach the user's pulse socket.
speakerctl ALL=(ALL) NOPASSWD: SETENV: /usr/bin/pactl, /usr/bin/parec
EOF
chmod 0440 /etc/sudoers.d/speakerctl
echo "    /etc/sudoers.d/speakerctl installed"

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
echo " speakerctl installed successfully!"
echo ""
systemctl is-active speakerctl && echo "  speakerctl is running" || echo "  speakerctl not running (plug in speaker?)"
echo ""
echo " Configure buttons: /etc/speakerctl/config.toml"
echo " Reload config:     sudo systemctl reload speakerctl"
echo " View logs:         journalctl -u speakerctl -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
