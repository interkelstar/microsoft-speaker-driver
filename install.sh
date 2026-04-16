#!/usr/bin/env bash
# speakerctl installer
# Usage: sudo ./install.sh [--with-pulseaudio] [--user USERNAME]
set -euo pipefail

WITH_PULSE=0
PULSE_USER="${SUDO_USER:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-pulseaudio) WITH_PULSE=1 ;;
        --user) PULSE_USER="$2"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ── 1. Preflight ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Error: run with sudo" >&2
    exit 1
fi

if ! command -v python3.11 &>/dev/null; then
    echo "Error: python3.11 not found."
    echo "  Install with: sudo apt install python3.11 python3.11-venv"
    exit 1
fi

if [[ "$WITH_PULSE" -eq 1 && -z "$PULSE_USER" ]]; then
    echo "Error: --with-pulseaudio requires --user USERNAME (or run via sudo from the target user)"
    exit 1
fi

echo "==> speakerctl installer"

# ── 2. System user ──────────────────────────────────────────────────────────
if ! id speakerctl &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin speakerctl
    echo "    Created system user: speakerctl"
fi
usermod -aG input speakerctl
usermod -aG plugdev speakerctl
usermod -aG audio speakerctl   # needed for amixer to open /dev/snd/controlC*
echo "    User speakerctl in groups: input, plugdev, audio"

# ── 3. Python dependencies (venv, Ubuntu blocks pip for system python) ──────
echo "==> Installing Python dependencies..."
VENV=/usr/lib/speakerctl-venv
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --quiet evdev pyudev
echo "    evdev, pyudev installed in $VENV"

# ── 4. Daemon ───────────────────────────────────────────────────────────────
echo "==> Installing daemon..."
mkdir -p /usr/lib/speakerctl
cp -r speakerctl/ /usr/lib/speakerctl/
# Also install package into the venv so it's importable
cp -r speakerctl/ "$VENV/lib/python3.11/site-packages/"

cat > /usr/local/bin/speakerctl << 'EOF'
#!/bin/bash
exec /usr/lib/speakerctl-venv/bin/python3.11 -m speakerctl "$@"
EOF
chmod +x /usr/local/bin/speakerctl
echo "    Installed to /usr/lib/speakerctl"

# ── 5. Config ───────────────────────────────────────────────────────────────
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

# ── 6. udev rules ───────────────────────────────────────────────────────────
echo "==> Installing udev rules..."
cp udev/99-microsoft-speaker.rules /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=045e 2>/dev/null || true
echo "    Rules installed, udev reloaded"

# ── 7. systemd ──────────────────────────────────────────────────────────────
echo "==> Installing systemd service..."

# Patch the PYTHONPATH into the service unit
sed "s|ExecStart=|Environment=PYTHONPATH=/usr/lib/speakerctl\nExecStart=|" \
    systemd/speakerctl.service > /etc/systemd/system/speakerctl.service

systemctl daemon-reload
systemctl enable speakerctl
systemctl restart speakerctl
echo "    speakerctl.service enabled and started"

# ── 8. PulseAudio (optional) ────────────────────────────────────────────────
if [[ "$WITH_PULSE" -eq 1 ]]; then
    echo "==> Setting up PulseAudio for user: $PULSE_USER"
    apt-get install -y pulseaudio pulseaudio-module-echo-cancel 2>/dev/null \
        || apt-get install -y pulseaudio 2>/dev/null \
        || true

    PULSE_DIR="/home/$PULSE_USER/.config/pulse"
    mkdir -p "$PULSE_DIR"

    # Remove any previous speakerctl snippet to avoid duplicates
    if [[ -f "$PULSE_DIR/default.pa" ]]; then
        sed -i '/--- speakerctl:/,/--- end speakerctl ---/d' "$PULSE_DIR/default.pa"
    fi

    cat pulseaudio/default.pa.snippet >> "$PULSE_DIR/default.pa"
    chown -R "$PULSE_USER:$PULSE_USER" "$PULSE_DIR"
    echo "    PulseAudio config updated for $PULSE_USER"

    # Restart user PulseAudio
    sudo -u "$PULSE_USER" pulseaudio --kill 2>/dev/null || true
    sleep 1
    sudo -u "$PULSE_USER" pulseaudio --start 2>/dev/null || true
    echo "    PulseAudio restarted"
fi

# ── 9. Summary ──────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " speakerctl installed successfully!"
echo ""
echo " Service status:"
systemctl is-active speakerctl && echo "  ✓ speakerctl is running" || echo "  ✗ speakerctl not running (plug in speaker?)"
echo ""
echo " Configure buttons: /etc/speakerctl/config.toml"
echo " Reload config:     sudo systemctl reload speakerctl"
echo " View logs:         journalctl -u speakerctl -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
