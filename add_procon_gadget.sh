#!/bin/bash
# HID-only Nintendo Switch Pro Controller USB gadget for Raspberry Pi Zero W.
#
# UNTESTED ON A REAL SWITCH. Identity + 203-byte report descriptor copied from
# mzyy94's public dump / gist (VID/PID 0x057E/0x2009):
#   https://gist.github.com/mzyy94/60ae253a45e2759451789a117c59acf9
#
# Default boot on this Pi must remain g_cdc (Mac CDC Composite). This script is
# the *separate* HID gadget you run over Wi-Fi SSH *before* plugging into a Switch.
#
# Usage (as root):
#   ./add_procon_gadget.sh          # unbind g_cdc, bind HID-only Pro gadget
#   ./add_procon_gadget.sh remove   # unbind HID, restore g_cdc for Mac setup

set -euo pipefail

GADGET_NAME=procon
CONFIGFS=/sys/kernel/config
GADGET="$CONFIGFS/usb_gadget/$GADGET_NAME"

# 203-byte real Pro Controller HID report descriptor (mzyy94 dump). Do not edit.
REPORT_DESC_HEX="050115000904A1018530050105091901290A150025017501950A5500650081020509190B290E150025017501950481027501950281030B01000100A1000B300001000B310001000B320001000B35000100150027FFFF0000751095048102C00B39000100150025073500463B0165147504950181020509190F2912150025017501950481027508953481030600FF852109017508953F8103858109027508953F8103850109037508953F9183851009047508953F9183858009057508953F9183858209067508953F9183C0"

die() { echo "error: $*" >&2; exit 1; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "run as root (sudo $0 $*)"
  fi
}

unload_canned_gadgets() {
  # g_cdc / g_serial / g_ether own the UDC; HID configfs cannot bind until they go.
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop getty@ttyGS0.service 2>/dev/null || true
  fi
  modprobe -r g_cdc 2>/dev/null || true
  modprobe -r g_serial 2>/dev/null || true
  modprobe -r g_ether 2>/dev/null || true
}

unbind_udc() {
  local udc_file="$1"
  if [ -e "$udc_file" ]; then
    echo "" > "$udc_file" || true
  fi
}

remove_procon() {
  if [ -d "$GADGET" ]; then
    unbind_udc "$GADGET/UDC"
    sleep 0.2
    rm -f "$GADGET/configs/c.1/hid.usb0" 2>/dev/null || true
    [ -d "$GADGET/functions/hid.usb0" ] && rmdir "$GADGET/functions/hid.usb0" || true
    [ -d "$GADGET/configs/c.1/strings/0x409" ] && rmdir "$GADGET/configs/c.1/strings/0x409" || true
    [ -d "$GADGET/configs/c.1" ] && rmdir "$GADGET/configs/c.1" || true
    [ -d "$GADGET/strings/0x409" ] && rmdir "$GADGET/strings/0x409" || true
    rmdir "$GADGET" || true
  fi
}

restore_g_cdc() {
  remove_procon
  modprobe g_cdc
  if command -v systemctl >/dev/null 2>&1; then
    systemctl start getty@ttyGS0.service 2>/dev/null || true
  fi
  echo "Restored g_cdc (Mac CDC Composite Gadget + ACM serial)."
  echo "Plug the Pi USB (next to mini-HDMI) into the Mac if you need that console."
}

add_procon() {
  unload_canned_gadgets
  modprobe libcomposite
  modprobe configfs 2>/dev/null || true
  if ! mountpoint -q "$CONFIGFS"; then
    mount -t configfs none "$CONFIGFS"
  fi
  [ -d "$CONFIGFS/usb_gadget" ] || die "configfs usb_gadget is missing"

  # Idempotent: tear down a leftover procon gadget before recreating it.
  remove_procon

  mkdir -p "$GADGET"
  cd "$GADGET"

  echo 0x057e > idVendor
  echo 0x2009 > idProduct
  echo 0x0200 > bcdDevice
  echo 0x0200 > bcdUSB
  echo 0x00 > bDeviceClass
  echo 0x00 > bDeviceSubClass
  echo 0x00 > bDeviceProtocol

  mkdir -p strings/0x409
  echo "000000000001" > strings/0x409/serialnumber
  echo "Nintendo Co., Ltd." > strings/0x409/manufacturer
  echo "Pro Controller" > strings/0x409/product

  mkdir -p configs/c.1/strings/0x409
  echo "Nintendo Switch Pro Controller" > configs/c.1/strings/0x409/configuration
  echo 500 > configs/c.1/MaxPower
  echo 0xa0 > configs/c.1/bmAttributes

  mkdir -p functions/hid.usb0
  echo 0 > functions/hid.usb0/protocol
  echo 0 > functions/hid.usb0/subclass
  echo 64 > functions/hid.usb0/report_length

  python3 - "$REPORT_DESC_HEX" functions/hid.usb0/report_desc <<'PY'
import sys
from pathlib import Path
blob = bytes.fromhex(sys.argv[1])
if len(blob) != 203:
    raise SystemExit(f"report descriptor must be 203 bytes, got {len(blob)}")
Path(sys.argv[2]).write_bytes(blob)
PY

  ln -s functions/hid.usb0 configs/c.1/

  UDC="$(ls /sys/class/udc | head -n1 || true)"
  [ -n "$UDC" ] || die "no UDC in /sys/class/udc (is dtoverlay=dwc2,dr_mode=peripheral set?)"
  echo "$UDC" > UDC

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ -e /dev/hidg0 ]; then
      echo "Pro Controller HID gadget bound on $UDC"
      echo "/dev/hidg0 is ready. Start procon.py, then plug the Pi USB port into the Switch."
      echo "UNTESTED ON A REAL SWITCH."
      return
    fi
    sleep 0.2
  done
  die "/dev/hidg0 did not appear after binding $UDC"
}

need_root "$@"
case "${1:-}" in
  ""|add|start|bind)
    add_procon
    ;;
  remove|stop|unbind|restore)
    restore_g_cdc
    ;;
  *)
    die "usage: $0 [add|remove]"
    ;;
esac
