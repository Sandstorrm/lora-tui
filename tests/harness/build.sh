#!/bin/sh
# Build the host harness around the firmware's real engine. FW = path to the Bruce firmware repo.
set -e
FW="${BRUCE_FIRMWARE:-$(cd "$(dirname "$0")/../../.." && pwd)/bruce-firmware}"
SRC="$FW/src/modules/lora"
OUT="$(cd "$(dirname "$0")" && pwd)/device_harness"
clang++ -std=c++17 -O1 -g -Wall -Wextra -Wshadow -fsanitize=address,undefined -I"$SRC" \
    "$(dirname "$0")/device_harness.cpp" "$SRC/lora_link_engine.cpp" -o "$OUT"
echo "built $OUT"
