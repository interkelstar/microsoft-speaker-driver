# speakerctl — Microsoft Modern USB-C Speaker on Linux

A small userspace daemon that makes the **Microsoft Modern USB-C Speaker**'s
buttons work on Linux.

Plug the speaker into a Linux machine and audio works immediately — it is a
class-compliant USB sound card, and nothing here is needed for sound. What does
*not* work is every button on the top of it, including volume. That is what this
fixes.

---

## The device

**Microsoft Modern USB-C Speaker** — USB-C ID `045e:083e`, sold as a Teams-certified
desk speakerphone.

- [Microsoft's own page for it](https://support.microsoft.com/en-us/surface/accessories/certified/use-microsoft-modern-usb-c-speaker-with-microphone-in-microsoft-teams)
- 50 mm driver, 200 Hz–20 kHz for music, two omnidirectional microphones
- 70 × 138 × 29 mm, 191 g, USB-C, 680 mm captive cable
- Five buttons: volume up, volume down, mute, Teams, and a hook switch (phone)

Microsoft lists Windows and macOS as supported. Linux is not mentioned, and on
Linux you get a sound card with five dead buttons.

It is worth knowing about because it is *cheap*. It was an office accessory sold
into a market that has since moved on, so it turns up on clearance and
second-hand for a fraction of its original price — the unit this was developed
on cost **50 zł**, about €12. For that you get a speaker with two microphones
whose echo cancellation is done in firmware and is genuinely good: measured
here, the microphone hears a person in the room at full level while the
speaker's own output is subtracted almost entirely. That makes it a much better
base for a always-listening voice assistant than the price suggests.

<!-- Drop a photo at docs/speaker.jpg and uncomment:
![Microsoft Modern USB-C Speaker](docs/speaker.jpg)
-->

---

## What you get

| Button | What it does |
|---|---|
| Volume up / down | Steps the system volume (`amixer`) |
| Mute | Mutes the microphone in the speaker's firmware; runs your script with `$STATE` |
| Teams | Runs your script |
| Phone (hook switch) | Runs your script |

Every button runs an ordinary shell command from a config file, so "what it
does" is up to you. The shipped examples put a notification on the desktop and
toggle media playback.

There is also **optional** integration with
[linux-voice-assistant](https://github.com/OHF-Voice/linux-voice-assistant), so
the buttons and a voice assistant share one volume and one mute. It is off by
default and everything above works without it. See
[the bottom of this file](#optional-voice-assistant-sync).

---

## Requirements

- Linux with systemd
- **Python 3.11 or newer** (for `tomllib`)
- **`alsa-utils`** — the volume and mute buttons go through `amixer`

Optional, and only needed for the PulseAudio features (see
[`[startup] pulse_user`](#pulseaudio-features)):

- `pulseaudio-utils` or `pipewire-pulse` — provides `pactl` and `parec`

The installer checks all of this before it changes anything, and tells you the
package name for your distribution if something is missing.

---

## Install

```bash
git clone https://github.com/interkelstar/microsoft-speaker-driver
cd microsoft-speaker-driver
sudo ./install.sh
```

The installer creates a `speakerctl` system user, a virtualenv under
`/usr/lib/speakerctl-venv`, a udev rule so the daemon can read the device
without root, and a systemd service. It finishes by running the self-check
below, so you see immediately whether it worked.

Nothing is installed into your home directory, and `sudo ./uninstall.sh`
removes all of it.

---

## Check that it works

```bash
sudo speakerctl --check
```

```
speakerctl 1.0.0 — check

environment
  [  ok  ] Python                              3.11.0

configuration
  [  ok  ] config file                         /etc/speakerctl/config.toml

external commands
  [  ok  ] amixer                              /usr/bin/amixer

device
  [  ok  ] speaker 045e:083e                   found
  [  ok  ] hidraw node                         /dev/hidraw0
  [  ok  ] volume evdev node                   /dev/input/event4
  [  ok  ] mute evdev node                     /dev/input/event6
  [  ok  ] ALSA card 'Speaker'                 present

Everything checks out.
```

This exists because almost everything that goes wrong here goes wrong *quietly*.
`systemctl status speakerctl` says `active (running)` whether or not the speaker
is plugged in, whether or not `amixer` exists, and whether or not the button
nodes were ever identified. A dead volume button and a healthy-looking service
look identical from the outside. `--check` asks the questions the service cannot.

---

## Configure

Buttons are configured in `/etc/speakerctl/config.toml`:

```toml
[device]
alsa_card = "Speaker"          # from `cat /proc/asound/cards`

[volume_up]
command = "amixer -c Speaker -M set 'PCM' 5%+"

[teams]
command = "/etc/speakerctl/scripts/teams-button.sh"
```

```bash
sudo systemctl reload speakerctl     # apply changes without a restart
```

If `--check` reports the ALSA card is not present, look at
`cat /proc/asound/cards` and set `alsa_card` to the name in brackets. Note that
the default volume commands name the card too, so change it in both places.

Example scripts are installed to `/etc/speakerctl/scripts/`. The sources are in
[`examples/`](examples/), with Home-Assistant-flavoured variants in
[`examples/home-assistant/`](examples/home-assistant/).

### PulseAudio features

Setting `[startup] pulse_user` to your desktop login enables three things that
are otherwise off: volume applied to the PulseAudio sink as well as ALSA (so
desktop volume indicators follow the buttons), loading `module-echo-cancel`, and
working out whether the microphone is muted.

That last one needs explaining, because the mute button is not what it looks
like. **This speaker mutes inside its own firmware and reports it nowhere.** The
ALSA capture switch reads the same on both sides of a press, `LED_MUTE` never
changes, and the HID report says "button pressed", not "now muted". The only
thing that actually changes is the audio, so the daemon listens to a second of
microphone and decides from that. Without `pulse_user` it cannot, and `$STATE`
in your mute script is always `unknown`.

---

## Troubleshooting

```bash
sudo speakerctl --check              # start here
journalctl -u speakerctl -f          # then watch it while you press a button
```

| Symptom | Likely cause |
|---|---|
| Nothing happens on any button | Service not running, or the device was not found — `--check` says which |
| Volume buttons dead, others fine | `amixer` missing, or `alsa_card` does not match your system |
| Mute script always sees `$STATE=unknown` | `[startup] pulse_user` is not set |
| One button dead, others fine | Its evdev/hidraw node was not identified — `--check` lists them |
| Everything worked, then stopped after a replug | Check the journal; the daemon re-discovers on udev events |

If a button is dead and `--check` is green, run the daemon in the foreground and
press it:

```bash
sudo systemctl stop speakerctl
sudo speakerctl --debug
```

---

## How it works

```
speakerctl/
├── __main__.py       entry point; --check, --version, SIGHUP reloads config
├── check.py          the self-check above
├── config.py         reads /etc/speakerctl/config.toml
├── daemon.py         asyncio supervisor; handles unplug/replug via pyudev
├── discovery.py      finds the device's nodes by VID/PID in sysfs
├── evdev_watcher.py  volume + mute buttons
├── hidraw_watcher.py phone + Teams buttons
├── aec.py            optionally loads PulseAudio's module-echo-cancel
├── lva.py            optional voice-assistant sync
└── executor.py       runs the configured shell commands
```

The device is found by USB vendor/product ID rather than by name, since names
vary. Phone and Teams are read from hidraw because evdev drops `BTN_0` events
unreliably; volume and mute come from evdev, read directly rather than through
`async_read_loop`, which silently loses events when two arrive close together.

---

## Optional: voice assistant sync

If you run [linux-voice-assistant](https://github.com/OHF-Voice/linux-voice-assistant)
on the same machine, the daemon can talk to its peripheral API so that the
speaker's buttons and the assistant share state:

```toml
[lva]
enabled = true
url = "ws://127.0.0.1:6055"
sync_volume = true
sync_mute = true
```

What that gives you:

- **One volume.** The buttons, the assistant, and Home Assistant's slider all
  move the same number, which lands on the PulseAudio sink. The assistant must
  be started with `--external-volume`, or it attenuates in software as well and
  everything ends up far quieter than the slider claims.
- **One mute.** The physical button and the assistant agree. A session begins by
  forcing the microphone *on* — the probe described above cannot tell a silent
  room from a source that is not delivering, and a wrong "muted" leaves a
  speaker that is deaf with no symptom, so the doubtful case resolves towards
  listening.
- **A button that interrupts.** Set `interrupts_playback = true` on a button and
  it ends the assistant's answer while it is speaking, and runs its normal
  command when it is not.

Two notes: the peripheral API has no authentication, so run the assistant with
`--peripheral-host 127.0.0.1` rather than exposing it to the network. And if
`websockets` is missing from the venv, sync stays off and says so in the log
rather than crashing the daemon.

Everything in this section is optional. With `[lva]` absent — which is the
default — the buttons work exactly as described above.

---

## Tests

```bash
python3 tests/test_lva.py
```

Covers the voice-assistant client against a stand-in peripheral API server: the
echo guard, the forced unmute at session start, that connecting never probes for
mute state, that a hand mute survives a reconnect, the capture-switch token, the
offline fallback to the local mixer, and volume reaching the sink. Needs
`websockets` ≥ 14. The hardware paths were verified on the device.

---

## License

MIT — see [LICENSE](LICENSE).
