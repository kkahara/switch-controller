# Protocol v1 fixtures

Each `*.json` file is one scenario: `setup` describes the controller and
`exchanges` lists, in order, what a client sends and the exact sequence of
messages it must receive.

`setup`:

- `"stream": true` — the Switch is polling input.
- `"reports": false` (optional, default `true`) — the Switch is polling but no
  0x30 report gets written (the USB write fails), so report waits time out.

Each exchange:

- `"conn"` (optional, default `"main"`) — which client connection it uses.
  A connection is opened the first time its name appears, so one scenario can
  involve several concurrent clients (busy, takeover, observers).
- `"send"` (optional) — the message to send. Omit it to only check what that
  connection receives.
- `"expect"` — messages to read on that connection, in order.
- `"then_closed": true` — afterwards the server has closed that connection.

Expected messages match as a **subset** (extra fields are allowed). Placeholder
strings: `"$int"` any integer, `"$str"` any string, `"$any"` any value present.
The token is always `fixture-token-0123456789`.

Both the server tests (`tests/test_protocol_v1.py`) and every client's fake
server must pass these.
