#!/bin/bash
# Install mart1nro/joycontrol for Pi Zero W (armv6l, 32-bit Bookworm).
#
# UNTESTED ON A REAL SWITCH. Uses a venv with --system-site-packages so
# python3-dbus comes from apt (do not compile dbus-python on a Zero).
#
# Usage (as s3-coaching, not root — sudo is used only for apt):
#   ./install_joycontrol.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv-joycontrol"
SRC="$ROOT/joycontrol"
REPO="https://github.com/mart1nro/joycontrol.git"

if [ "$(id -u)" -eq 0 ]; then
  echo "error: run as s3-coaching, not root (script sudo's apt itself)" >&2
  exit 1
fi

echo "Installing apt packages (bluetooth, hidapi, dbus)..."
sudo apt-get update
sudo apt-get install -y \
  bluetooth bluez rfkill \
  python3 python3-venv python3-pip python3-dbus \
  libhidapi-hidraw0 libudev-dev \
  git pkg-config

if [ ! -d "$SRC/.git" ]; then
  echo "Cloning joycontrol into $SRC ..."
  git clone --depth 1 "$REPO" "$SRC"
else
  echo "joycontrol already cloned at $SRC"
fi

if [ ! -d "$VENV" ]; then
  echo "Creating venv with system site-packages (python3-dbus)..."
  python3 -m venv --system-site-packages "$VENV"
fi

# hid + aioconsole from pip; dbus-python from Debian via system-site-packages.
# --no-deps on joycontrol itself so pip does not try to build dbus-python==sdist
# on armv6.
"$VENV/bin/pip" install -U pip setuptools wheel
"$VENV/bin/pip" install 'hid' 'aioconsole'
"$VENV/bin/pip" install --no-deps -e "$SRC"

echo
echo "joycontrol venv: $VENV"
"$VENV/bin/python3" -c "import joycontrol, dbus, hid; print('imports ok', joycontrol.__file__)"
echo
echo "Pair (Change Grip/Order open on the Switch):"
echo "  sudo $VENV/bin/python3 $SRC/run_controller_cli.py PRO_CONTROLLER"
echo "Tap A after a pair:"
echo "  sudo $VENV/bin/python3 $ROOT/bt_tap_a.py -r AA:BB:CC:DD:EE:FF"
