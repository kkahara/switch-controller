# Protocol v1 fixtures

Each `*.json` file is one connection: `setup` describes the controller
(`"stream": true` = the Switch is polling input), and `exchanges` lists what
the client sends and the exact sequence of messages it must receive.

Expected messages match as a **subset** (extra fields are allowed). Placeholder
strings: `"$int"` any integer, `"$str"` any string, `"$any"` any value present.
The token is always `fixture-token-0123456789`.

Both the server tests (`tests/test_protocol_v1.py`) and every client's fake
server must pass these.
