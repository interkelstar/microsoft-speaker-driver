#!/usr/bin/env bash
# speakerctl uninstaller
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Error: run with sudo" >&2
    exit 1
fi

echo "==> Removing speakerctl..."

systemctl stop speakerctl 2>/dev/null || true
systemctl disable speakerctl 2>/dev/null || true
rm -f /etc/systemd/system/speakerctl.service
systemctl daemon-reload

rm -f /usr/local/bin/speakerctl
rm -rf /usr/lib/speakerctl
rm -f /etc/udev/rules.d/99-microsoft-speaker.rules
rm -f /etc/sudoers.d/speakerctl
udevadm control --reload-rules

echo ""
echo "Config and scripts preserved at /etc/speakerctl/ — remove manually if desired."
echo "System user 'speakerctl' preserved — remove with: sudo userdel speakerctl"
echo "Done."
