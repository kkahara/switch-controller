# Controller protocol v1

Contract between the Pi controller app (`switch-controller`, the server) and
any remote client (the Mac `switchcap` app). This file and
`protocol/v1/fixtures/` are the source of truth. Clients vendor a copy pinned
by `protocol/v1/VERSION`, and both sides test against the fixtures.

The protocol is **controller level only**: buttons, d-pad, sticks, synthetic
IMU tilt presets, neutral, state, ping. It has no notion of games, screens,
menus or navigation.

## Transport

- TCP, default port **47800**. Server: `procon.py --v1-listen 0.0.0.0:47800 --v1-token-file PATH`.
- UTF-8 JSON, **one object per line** (`\n`). Lines over 8192 bytes close the connection.
- Every message carries `"v": 1`.
- The first client message must be `hello`. Anything else closes the connection.

## Clocks

Each side reports only **its own** `time.monotonic_ns()` values (`*_mono_ns`).
The two clocks are never assumed to be synchronized; the server's monotonic
clock is only comparable within one `boot_id`. Clients correlate by command
`id` and may estimate offset/RTT from `ping` (`mac_sent_mono_ns` is echoed in
the ack), always with ±RTT/2 uncertainty. The client's timeline is
authoritative for its own observations.

## Session

```json
{"v":1,"type":"hello","token":"...","client":"switchcap/0.1","session_id":"a1b2c3","role":"control","takeover":false}
```

- `role`: `control` (default) may send every op; `observe` only `state` and `ping`.
- Only **one control session** at a time. A second one is rejected `busy`
  unless it sends `"takeover": true`, which closes the old session.
- Reply:

```json
{"v":1,"type":"hello","status":"ok","server":"procon/1","session_id":"a1b2c3","role":"control",
 "boot_id":"...","daemon_start_mono_ns":1,"pi_mono_ns":2,
 "limits":{"hold_ms":[30,2000],"idle_neutral_ms":2000,"id_cache":256},
 "buttons":["a","b",...],"ops":["press",...]}
```

- Rejection: `{"v":1,"type":"hello","status":"rejected","error":{"code":"unauthorized|busy|bad_version|bad_request","message":"..."}}`, then close.

## Commands

```json
{"v":1,"type":"cmd","id":"a1b2c3-000042","seq":42,"op":"tap",
 "args":{"button":"a","hold_ms":80},"max_queue_ms":1000,"mac_sent_mono_ns":123}
```

- `id`: client-chosen, unique, 1–64 chars. **Required.** Correlation key everywhere.
- `seq`: optional client counter, echoed in the ack.
- `max_queue_ms`: optional (default 1000). If the command waits longer than
  this on the Pi before execution starts, it is not executed (`expired`).
- `mac_sent_mono_ns`: optional, opaque to the server, echoed in the ack.

| op | args |
|---|---|
| `press` / `release` | `{"buttons": ["a", ...]}` |
| `tap` | `{"button": "a", "hold_ms": 80}`. `hold_ms` integer 30–2000 (default 80), timed on the Pi |
| `dpad` | `{"dir": "up" \| "down" \| "left" \| "right" \| null}` (null clears) |
| `stick` | `{"side": "l" \| "r", "x": -1..1, "y": -1..1}` (floats; 0 = center, +y = up) |
| `imu` | `{"preset": "neutral" \| "look_up" \| "look_down"}`; legacy persistent synthetic pitch pose |
| `imu_angle` | `{"axis": "pitch", "angle": -90..90}`; angle is an integer multiple of 15, positive looks up and negative looks down |
| `neutral` | `{}`: release everything, center both sticks, reset synthetic IMU tilt |
| `state` | `{}` |
| `ping` | `{}` |

Buttons: `a b x y l r zl zr plus minus home capture lstick rstick up down left right`.

## Replies

For every command with a valid `id`, `v` and `type`, the server sends an
**ack immediately on receipt** (before execution), then exactly one **result**.

```json
{"v":1,"type":"ack","id":"a1b2c3-000042","op":"tap","seq":42,"pi_recv_mono_ns":5,
 "duplicate":false,"queue_depth":0,"mac_sent_mono_ns":123}
```

```json
{"v":1,"type":"result","id":"a1b2c3-000042","op":"tap","status":"ok","applied":true,
 "pi_recv_mono_ns":5,"pi_start_mono_ns":6,"pi_end_mono_ns":7,
 "first_report_seq":9120,"last_report_seq":9131,"held_ms":80.4,"stream":"on"}
```

`status`:

| status | meaning |
|---|---|
| `ok` | applied **and** seen in a 0x30 input report written to the Switch |
| `not_streaming` | the Switch is not polling input (asleep, or USB stream stopped). Input ops are **not applied**; `neutral` is applied but not transmitted |
| `rejected` | bad request; never executed. `error.code`: `bad_args`, `unknown_op`, `bad_version`, `bad_request`, `observe_only` |
| `expired` | waited longer than `max_queue_ms`; never executed |
| `error` | executed but failed, e.g. `report_timeout` (no 0x30 report within 250 ms), `internal` |

- `applied`: whether controller state changed.
- `first_report_seq` / `last_report_seq`: the server counts successful 0x30
  writes. `first` is the first report carrying the change (the press for
  `tap`), `last` the first report carrying the final state (the release for
  `tap`). They prove the input actually left the Pi.
- `state` results add `"state": {"buttons":[...],"sticks_raw":{"l":[x,y],"r":[x,y]},"stream":"on","imu":true,"imu_preset":"neutral","imu_axis":"pitch","imu_angle":0,"player_lights":1,"report_seq":9131}`.
- Envelope errors with a valid `id` (bad `v`, bad `type`) get a `rejected`
  result with **no ack**. Messages without a usable `id` get
  `{"v":1,"type":"error","error":{...}}`.

## Duplicates, timeouts, safety

- The server remembers the last **256** command IDs. A repeated `id` is
  **never executed again**: the server re-sends the stored ack and result with
  `"duplicate": true` (if the original is still running, the result is sent
  when it finishes).
- Clients own end-to-end timeouts. A result arriving after the client gave up
  must be logged as late, never treated as success.
- Commands from one session execute strictly in order. All command sources on
  the Pi (v1, the Unix socket, stdin) are serialized by one lock.
- **Neutral on loss**: when the control session closes, or it has sent nothing
  for `idle_neutral_ms` (2000) while inputs are held and nothing is executing,
  the server goes neutral and logs it. Clients that hold inputs for long must
  send `ping` more often than that.

## Server log

Each v1 event is one journal line `v1 {json}` with `pi_mono_ns`: `listening`,
`session_open`, `command` (id, op, status, timings, report seqs), `duplicate`,
`takeover`, `session_closed[_neutral]`, `watchdog_neutral`.

## Versioning

`protocol/v1/VERSION` holds `1.<minor>`. Additive changes (new optional fields,
new ops) bump the minor; anything else is v2 with a new `hello` version.
