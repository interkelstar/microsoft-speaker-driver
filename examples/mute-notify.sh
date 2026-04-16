#!/usr/bin/env bash
# Called when the mute button is pressed.
# $STATE is "muted" or "unmuted".
#
# The kernel (snd_usb_audio) already toggled the mic — this is for notifications.
# Add HA webhook calls here if you want HA to know about physical button presses.
logger -t speakerctl "Mic is now $STATE"
