#!/bin/sh
# Launch the Bruce LoRa remote control (pass --demo to try it without hardware).
cd "$(dirname "$0")" && exec .venv/bin/python -m bruce_lora "$@"
