#!/usr/bin/env bash
# Called when the phone (hook switch) button is pressed.
#
# Answers or hangs up a call in whatever is playing, via MPRIS — which covers
# most Linux media players, browsers included. This is only an example: the
# button runs whatever you put in config.toml, so replace it with anything.
#
# Needs playerctl (Debian/Ubuntu: apt install playerctl).
# For a Home Assistant webhook instead, see examples/home-assistant/.
if command -v playerctl >/dev/null; then
    playerctl play-pause
else
    logger -t speakerctl "phone button pressed (playerctl not installed)"
fi
