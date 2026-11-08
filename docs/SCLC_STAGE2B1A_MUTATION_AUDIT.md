# S2B-1A controller-mutation audit

**Scope:** source audit for the common ownership gate. This document records
current mutation paths and the S2B-1A handling. It does not add a protocol-v2
listener, autonomous command, lease, credential, or scheduler.

## Gate rule

Every operation that can create or preserve non-neutral controller state calls
`ProController.ownership_gate.allow_mutation()`. Full `neutral()` is permitted
for trusted internal safety paths even during autonomous ownership. External
v1, Unix-socket, and stdin neutral requests are denied during that ownership;
the approved stop path must be used. Neutralization never clears the latch or
releases the reservation.

While no autonomous owner is reserved, legacy human paths keep their existing
behavior. A future v2-only authority reservation blocks all non-autonomous
mutation. S2B-1A contains no production caller able to reserve that authority.

## Audited paths

| Path | Mutation entry point | S2B-1A handling |
|---|---|---|
| Protocol v1 | `protocol_v1.execute()` | Runs inside the v1 source context; v1 control hello is refused as `busy` when autonomy is reserved. |
| Unix control socket | `ControlServer._handle_conn()` → `run_cli_command()` | Runs in the Unix-socket source context; mutators return the normal command error instead of changing state when blocked. |
| stdin CLI | `_serve_commands()` → `run_cli_command()` | Runs in the stdin source context and receives the same decision. |
| Internal callers | Public `ProController` mutation methods | Default source is `internal`; direct mutation cannot bypass an autonomous reservation. |
| Startup CLI flags/self-test | Public `ProController` mutation methods | Default internal source; normal disarmed startup remains human-compatible. |
| Session close/watchdog | `pad.neutral()` | Trusted internal safety neutralization remains available. |
| USB handshake/report packing | USB stream/lights/report state | Not input mutation; outside ownership gate. |
| Constructor initialization | Initial in-memory state | Before command service begins; startup then explicitly calls full neutral. |

Direct writes to private controller fields (for example, the button bytearray
or synthetic-IMU object) are implementation internals, not supported command
sources. The repository audit found no production caller that mutates those
fields outside `ProController`; test fakes use controlled subclasses only.
S2B-1A gates every public non-neutral mutator. Future reviews must retain that
property before exposing any additional mutation API.

The audited non-neutral public methods are `press`, `release`,
`release_all`, `tap`, `set_dpad`, `set_stick`, `center_sticks`,
`start_pitch_rotation`, and `finish_pitch_rotation`. `tap` and
`center_sticks` reach already-gated primitive methods.

## Compatibility

There is no deployed v2 reservation path in this slice, so normal v1, Unix,
stdin, and direct supervised-human controller use remain unchanged. The only
new behavior is observable in tests or a future v2 caller: legacy mutation is
denied while an autonomous reservation exists. Legacy `takeover=true` is
refused as `busy` in that state; it is never an autonomous preemption method.
