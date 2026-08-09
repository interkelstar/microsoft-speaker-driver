#!/usr/bin/env bash
# Called when the mute button is pressed.
# $STATE is "muted", "unmuted", or "unknown".
#
# The speaker mutes inside its own firmware and reports it nowhere on the host,
# so the daemon works the state out by listening to the microphone. That needs
# [startup] pulse_user to be set; without it $STATE is always "unknown".
#
# This is a notification hook only — the muting itself has already happened.
logger -t speakerctl "Mic is now $STATE"
if [ "$STATE" != "unknown" ] && command -v notify-send >/dev/null; then
    notify-send "Speaker" "Microphone $STATE"
fi
