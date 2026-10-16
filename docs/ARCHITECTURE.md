# Architecture — Pi controller appliance

> **Pi knows controllers. Mac knows the Switch. SCLC knows goals.**

This repository is a network-controlled virtual Nintendo Pro Controller. It
answers one question: *how do I behave like a Pro Controller?* Someone reading
it should not need to know anything about Splatoon or any other game.

## System

```text
                 home LAN (protocol v1, TCP 47800)
Mac (switchcap) ───────────────────────────────────► Pi Zero W (this repo)
      ▲                                                  │ USB (HID gadget)
      │ HDMI capture                                     ▼
      └──────────────────────────────────────────── Nintendo Switch
```

The Mac owns capture, vision, game/screen state, automation, closed-loop
control (SCLC = Switch Closed-Loop Controller), recovery and orchestration.
The Pi owns everything below the controller abstraction.

## Responsibilities

In scope here:

- USB gadget setup (`add_procon_gadget.sh`, `procon.service`)
- the wired Pro Controller USB protocol: `0x80`/`0x81` handshake, `0x01`
  subcommands / `0x21` replies, SPI blobs, ~125 Hz `0x30` input reports
- controller state: buttons, d-pad, sticks, synthetic IMU (pitch orientation,
  angular velocity, gravity vector)
- the Mac↔Pi protocol (`protocol_v1.py`, `PROTOCOL.md`, `protocol/v1/`)
- safety: neutral on session loss and on idle while inputs are held; one
  serializing lock for every command source (see "Failsafe" below)
- deployment (`deploy.sh`)

Out of scope, and must not be added: game screens, menus, replay codes, visual
state, goals, HDMI capture, timing scripts that assume what the game is doing.
`legacy/` holds historical code that crossed this line; it is not deployed.

## Runtime

```text
protocol v1 client (Mac) ──TCP──► protocol_v1.V1Server ─┐
procon_send.py / ProconClient ──/run/procon.sock──► ControlServer ─┤ pad.command_lock
interactive TTY (manual start only) ───────────────────────────────┘
                                                        ▼
                                           procon.ProController
                                 state (buttons, sticks, SyntheticImu)
                                                        ▼
                               /dev/hidg0  (sole owner: procon.py)
                                                        ▼
                                                  Nintendo Switch
```

`procon.py` deliberately stays one module. It is verified on hardware
(tags `baseline-working`, `step2-hardware-accepted`); splitting it into a
package for appearance alone is not worth the regression risk. Split it only
when a change needs it, with tests covering the moved code.

## Failsafe

The Pi's failsafe protects only against lost communication with the Mac:

- idle neutral: a control session that holds a button, d-pad direction or
  off-centre stick, has sent nothing (not even `ping`) for 2000 ms, and has
  nothing executing or queued is neutralized (`watchdog_neutral`);
- closing the control session always neutralizes (`session_closed_neutral`);
- one control session at a time (`busy`); `takeover` replaces it without
  neutralizing.

It does not count a non-zero IMU orientation as held input, cannot detect a
half-open TCP connection beyond the 2 s idle rule, and knows nothing about
the game. It is not, and must not become, the safety mechanism of Mac-side
automation. The Mac's UI session ownership and future SCLC ownership are
separate; see `switch-capture-automation/docs/LIVENESS_AND_OWNERSHIP.md`.

## Levels of command success

The protocol distinguishes, and clients must not collapse:

1. **Acknowledged** — the `ack` arrived: the Pi received and accepted the
   command for execution. Nothing about the Switch.
2. **Transmitted** — the `result` has `status: "ok"` and
   `first_report_seq`/`last_report_seq`: the input was present in `0x30`
   reports written while the Switch was polling. Not proof that the game reacted.
3. **Visually verified** — fresh HDMI evidence shows the expected effect. Only
   the Mac can establish this; the Pi never claims it.

## Protocol ownership

This repository is the single owner of protocol v1. `PROTOCOL.md`,
`protocol/v1/VERSION` and `protocol/v1/fixtures/` are authoritative; the Mac
vendors a pinned copy and must not extend it independently. See
[PROTOCOL.md § Versioning](../PROTOCOL.md#versioning).

## Pre-SCLC cleanup (Phase 0)

The inventory and report covering both repositories live in the Mac
repository: `switch-capture-automation/PRE_SCLC_CLEANUP_INVENTORY.md` and
`PRE_SCLC_CLEANUP_REPORT.md` (pre-Phase-0 baseline here: `9c150ab`).
