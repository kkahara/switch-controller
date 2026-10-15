#!/bin/bash
# Copy the controller runtime to the Pi and run its tests there. Never deletes
# remote files (joycontrol/ and the venv live only on the Pi). legacy/ is not
# part of the runtime and is not copied.
#
#   ./deploy.sh                    # copy + tests; the running daemon is untouched
#   ./deploy.sh --install-service  # also install procon.service and restart it (sudo)
#   PI=s3-coaching@SwitchController1.local ./deploy.sh

set -euo pipefail

PI="${PI:-s3-coaching@192.168.0.252}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

rsync -av \
  --exclude .git/ \
  --exclude .vscode/ \
  --exclude joycontrol/ \
  --exclude .venv-joycontrol/ \
  --exclude __pycache__/ \
  --exclude '*.log' \
  --exclude captures/ \
  --exclude diag/ \
  --exclude legacy/ \
  "$ROOT/" "$PI:~/switch-controller/"

# Protocol v1 token: created once, readable only by s3-coaching (root reads it too).
ssh "$PI" 'f=~/.config/procon/v1-token; if [ ! -s "$f" ]; then
  mkdir -p ~/.config/procon && chmod 700 ~/.config/procon
  umask 077 && python3 -c "import secrets; print(secrets.token_hex(24))" > "$f"
  echo "created $f"
fi'

ssh "$PI" 'cd ~/switch-controller && python3 procon.py --self-test && python3 -m unittest discover -s tests'

if [ "${1:-}" = "--install-service" ]; then
  ssh "$PI" 'sudo -n cp ~/switch-controller/procon.service /etc/systemd/system/procon.service \
    && sudo -n systemctl daemon-reload \
    && sudo -n systemctl restart procon.service \
    && sleep 3 && systemctl is-active procon.service'
fi
