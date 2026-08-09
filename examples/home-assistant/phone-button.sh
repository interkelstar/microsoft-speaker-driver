#!/usr/bin/env bash
# Called when the phone button is pressed.
curl -s -X POST "http://127.0.0.1:8123/api/webhook/phone_button_clicked"
