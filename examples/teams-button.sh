#!/usr/bin/env bash
# Called when the Teams button is pressed.
curl -s -X POST "http://127.0.0.1:8123/api/webhook/teams_button_clicked"
