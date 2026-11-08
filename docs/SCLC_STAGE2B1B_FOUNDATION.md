# S2B-1B local authentication foundation

S2B-2A adds a local stop coordinator to the daemon integration; see
[S2B-2A emergency-stop verification](SCLC_STAGE2B2A_EMERGENCY_STOP.md).
The authentication-foundation behavior below describes instances without
that coordinator. The listener remains opt-in and rearm remains disabled.

The v2 listener is disabled unless `--v2-listen` is explicitly supplied along
with `--v2-cert`, `--v2-key`, `--v2-ca`, and `--v2-roles`. The bind requires an
explicit IPv4 address and port; non-loopback addresses additionally require
`--v2-allow-network`. The intended port is 47801. The existing service remains
unchanged.

TLS 1.3 requires client certificates signed by the configured CA. The JSON
role registry maps lowercase SHA-256 hashes of complete DER certificates to
one of `executor`, `stop`, or `human`; the hello role must match that identity.
Provisioning and secure access to these files are outside this slice. Do not
use untrusted or writable configuration paths, or deploy this foundation.
Production clients must validate the server certificate chain and the expected
DNS name or IP subject alternative name; disabling hostname verification is
prohibited. Certificate-to-role mapping is a startup snapshot: removing a
fingerprint from the file does not revoke an already running server or
connection. Safe reload/revocation must be implemented before deployment.

Each authenticated connection has its own worker and does not claim the pad.
A stop identity can therefore connect while an executor connection is idle;
the `estop` operation responds `not_implemented` and provides no stopping
guarantee. Human `rearm` always returns `rearm_disabled`, without modifying
the persistent marker or ownership. All controller, lease, arm, and other
requests are rejected. This is not a full protocol-v2 implementation.

The listener limits active connections to 8, concurrent handshakes to 4,
authenticated non-stop sessions to 2, and stop sessions to 2. Handshakes and
each message have a 2-second deadline; messages are at most 8192 bytes and
connections accept at most 32 requests. Stalled authenticated executors cannot
occupy stop-role slots. However, an unauthenticated flood can temporarily fill
the shared handshake slots or kernel backlog: independent stop availability
under hostile load is not guaranteed. Resource bounds do not turn this into an
operational emergency stop.

## Latch-integrity limitation and proposed future design

S2B-1A stores a plain SHA-256 checksum over each marker payload. This detects
accidental corruption, but anyone able to alter the marker can recompute the
checksum and forge a valid clear or older state. File permissions and dual
slots are not cryptographic authentication or rollback protection. The
current implementation does not provide an authenticated rearm path.

Proposed future design, requiring separate approval and tests: generate an
independent random HMAC-SHA-256 key during local physical-presence provisioning,
store it in a root-owned mode-0600 file in a root-owned mode-0700 directory,
separate from TLS credentials, and authenticate a canonical versioned marker
including key ID, generation, latch and cause. Never export keys to the Mac,
logs or evidence. Rotate only with both old and new keys present under a
crash-safe, audited, latched migration; do not clear the latch as part of key
rotation. Missing, unreadable, corrupt, unknown-key or invalid-MAC records
must boot neutral and require human recovery. Lost keys require local-console
and physical-presence reprovisioning, preserving the latched state until a
separately authenticated human rearm.

An HMAC key on the same filesystem does not protect against root compromise,
offline theft of both key and records, or rollback of both slots and key. A
hardware-protected monotonic counter or equivalent independent trusted anchor
would be required to detect whole-storage rollback. The mechanism and threat
model are proposals, not implemented guarantees.

The listener is not a safety protection yet. It lacks priority stop, lease,
rearm, production credential provisioning, secure Keychain/launchd isolation,
revocation/rotation, adversarial availability and the approved recovery
procedure. All remain prerequisites before hardware deployment or autonomy.
Temporary TLS keys in local tests are generated only inside temporary folders.
