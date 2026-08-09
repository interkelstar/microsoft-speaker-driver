# Home Assistant button scripts

These fire a [webhook trigger](https://www.home-assistant.io/docs/automation/trigger/#webhook-trigger)
in Home Assistant. They will do nothing until you create the matching
automation, and nothing says so at the time — a failed `curl` leaves only a
`Command exited 7` line at DEBUG.

Before using them:

1. Create an automation in Home Assistant with a **Webhook** trigger, and set
   its ID to `phone_button_clicked` (or `teams_button_clicked`).
2. Edit the URL in the script if Home Assistant is not on this machine at the
   default port 8123.
3. Copy it into place — `install.sh` only installs the scripts directly under
   `examples/`, so these are not deployed by default:

   ```bash
   sudo cp phone-button.sh /etc/speakerctl/scripts/phone-button-ha.sh
   sudo chmod +x /etc/speakerctl/scripts/phone-button-ha.sh
   ```

4. Point `config.toml` at it and reload:

   ```toml
   [phone]
   command = "/etc/speakerctl/scripts/phone-button-ha.sh"
   ```

   ```bash
   sudo systemctl reload speakerctl
   ```

Webhooks need no authentication token, which is why these scripts carry no
secret — but it also means anything that can reach that URL can trigger the
automation. Keep Home Assistant off the open internet, or use a webhook ID that
is not guessable.
