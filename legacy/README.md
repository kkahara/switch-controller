# Legacy — not part of the controller runtime

These files predate the current architecture, in which the Pi is only a
network-controlled virtual Pro Controller. They are kept for reference and are
**not supported**:

- nothing in `procon.py`, `protocol_v1.py`, `procon_send.py`, `procon.service`
  or the tests imports, runs or installs them;
- `deploy.sh` does not copy this directory to the Pi.

| File | What it was | Why it is legacy |
|---|---|---|
| `replay.py` | Typed a Splatoon 3 replay code at the Lobby Terminal through the Unix socket. Keyboard/menu positions were never verified against a screen | Game- and screen-aware open-loop automation. That belongs on the Mac (`switchcap`), driven by visual feedback |
| `review.sh` | Mac script: ssh `replay.py play`, then record with `capture.py` | Same; also Mac-side orchestration living in the Pi repo |
| `capture.py` | Mac HDMI capture helper (ffmpeg + AVFoundation) | Capture is owned by `switchcap.capture` on the Mac |
| `bluetooth.md`, `bt_tap_a.py`, `enable_bluetooth.sh`, `install_joycontrol.sh` | Bluetooth Pro Controller experiment via joycontrol | Never verified on a Switch; USB is the only supported path |

Paths inside these files (for example `~/switch-controller/replay.py`) refer
to their old location at the repository root.

On the Pi, the copies deployed before the move were archived intact on
2026-10-15 to `~/switch-controller-legacy-archive/2026-10-15/` (with a
`MANIFEST` of MD5s, identical to `9c150ab`), together with an old
`switch-init.log`. The joycontrol clone and `.venv-joycontrol/` stay in
`~/switch-controller/` because the venv contains absolute paths; they are
inert and `deploy.sh` never touches them.

Splatoon, capture, vision and automation work belongs in the Mac repository
`switch-capture-automation`. Do not move it back here.
