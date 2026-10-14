#!/bin/bash
# Copy this tree to the Pi. Never deletes remote files (joycontrol/ and the
# venv live only on the Pi).
#
#   ./deploy.sh                 # default s3-coaching@192.168.0.252
#   PI=s3-coaching@SwitchController1.local ./deploy.sh

set -euo pipefail

PI="${PI:-s3-coaching@192.168.0.252}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

rsync -av \
  --exclude .git/ \
  --exclude joycontrol/ \
  --exclude .venv-joycontrol/ \
  --exclude __pycache__/ \
  --exclude '*.log' \
  --exclude captures/ \
  "$ROOT/" "$PI:~/switch-controller/"

ssh "$PI" 'python3 ~/switch-controller/procon.py --self-test && python3 ~/switch-controller/replay.py --self-test'
