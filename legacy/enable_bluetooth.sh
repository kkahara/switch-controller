#!/bin/bash
# Enable onboard Bluetooth for Switch Pro Controller emulation (joycontrol).
#
# UNTESTED ON A REAL SWITCH. Stops the USB procon daemon so it is not also
# advertising; does not delete USB gadget files. Leaves g_cdc boot config alone.
#
# Usage (as root, on SwitchController1):
#   sudo ./enable_bluetooth.sh
#   sudo ./enable_bluetooth.sh status

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "run as root (sudo $0 $*)"
  fi
}

find_bluetoothd() {
  if [ -x /usr/libexec/bluetooth/bluetoothd ]; then
    echo /usr/libexec/bluetooth/bluetoothd
  elif [ -x /usr/lib/bluetooth/bluetoothd ]; then
    echo /usr/lib/bluetooth/bluetoothd
  else
    die "bluetoothd not found (apt install bluetooth bluez)"
  fi
}

stop_usb_emulator() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop procon.service 2>/dev/null || true
  fi
  pkill -f '/home/s3-coaching/switch-controller/procon.py' 2>/dev/null || true
}

write_input_dropin() {
  local bluetoothd="$1"
  local dir=/etc/systemd/system/bluetooth.service.d
  mkdir -p "$dir"
  # Empty ExecStart= is required so systemd replaces the vendor command.
  cat >"$dir/01-disable-input.conf" <<EOF
[Service]
ExecStart=
ExecStart=${bluetoothd} --noplugin=input
EOF
  systemctl daemon-reload
}

show_status() {
  echo "=== rfkill ==="
  rfkill list bluetooth || rfkill list || true
  echo
  echo "=== bluetooth.service ==="
  systemctl is-active bluetooth.service || true
  systemctl show -p ExecStart bluetooth.service || true
  echo
  echo "=== bluetoothd process ==="
  ps aux | grep -E '[b]luetoothd' || echo "(no bluetoothd)"
  echo
  echo "=== adapter ==="
  if command -v hciconfig >/dev/null 2>&1; then
    hciconfig -a || true
  fi
  bluetoothctl show 2>/dev/null || true
}

cmd="${1:-enable}"

need_root
case "$cmd" in
  status)
    show_status
    exit 0
    ;;
  enable|"")
    ;;
  *)
    die "usage: $0 [enable|status]"
    ;;
esac

echo "Stopping USB procon emulator (files stay on disk)..."
stop_usb_emulator

echo "Unblocking Bluetooth..."
command -v rfkill >/dev/null 2>&1 || die "rfkill missing"
rfkill unblock bluetooth || true

echo "Installing BlueZ input-plugin drop-in..."
bluetoothd="$(find_bluetoothd)"
echo "bluetoothd: $bluetoothd"
write_input_dropin "$bluetoothd"

echo "Starting bluetooth.service..."
systemctl enable bluetooth.service
systemctl restart bluetooth.service
sleep 1

if command -v hciconfig >/dev/null 2>&1; then
  hciconfig hci0 up 2>/dev/null || true
fi
bluetoothctl power on >/dev/null 2>&1 || true

if ! ps aux | grep -E '[b]luetoothd' | grep -q 'noplugin=input'; then
  echo "warning: bluetoothd is running but --noplugin=input not visible in ps" >&2
  echo "         check: systemctl cat bluetooth.service" >&2
fi

show_status
echo
echo "Next: ~/switch-controller/install_joycontrol.sh"
echo "Then Switch Change Grip/Order, then as root:"
echo "  sudo ~/switch-controller/.venv-joycontrol/bin/python3 \\"
echo "    ~/switch-controller/joycontrol/run_controller_cli.py PRO_CONTROLLER"
