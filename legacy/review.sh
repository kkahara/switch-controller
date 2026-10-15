#!/bin/bash
# Record a Splatoon 3 replay end to end, from the Mac.
#
# Before running: Switch in the Lobby with the Terminal menu open (cursor on
# the first item), procon.service streaming, capture card plugged into the Mac.
#
#   ./review.sh RXE0-4NDK-KSEC-A05W
#   DURATION=400 ./review.sh RXE0-4NDK-KSEC-A05W

set -euo pipefail

CODE="${1:?usage: $0 REPLAY-CODE}"
PI="${PI:-s3-coaching@192.168.0.252}"
DURATION="${DURATION:-330}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/captures/replay-${CODE//-/}-$(date +%Y%m%d-%H%M%S).mp4"

ssh "$PI" "python3 ~/switch-controller/procon_send.py status" | grep -q 'stream=on' \
  || { echo "controller is not streaming to the Switch; fix that first" >&2; exit 1; }

python3 "$ROOT/capture.py" record --duration "$DURATION" "$OUT" &
REC=$!
trap 'kill -INT $REC 2>/dev/null || true' EXIT
sleep 3

ssh "$PI" "python3 ~/switch-controller/replay.py play '$CODE'"

wait "$REC"
trap - EXIT
echo "saved $OUT"
