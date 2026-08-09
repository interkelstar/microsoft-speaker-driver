#!/usr/bin/env bash
# Called when the Teams button is pressed.
#
# With [lva] enabled and interrupts_playback = true, this only runs while the
# assistant is *quiet* — mid-answer the press stops the assistant instead. See
# the README section on the voice assistant.
#
# As shipped it just puts a notification on the desktop, which is a safe default
# that proves the button works. Replace it with whatever you want.
# For a Home Assistant webhook instead, see examples/home-assistant/.
if command -v notify-send >/dev/null; then
    notify-send "Speaker" "Teams button pressed"
else
    logger -t speakerctl "Teams button pressed"
fi
