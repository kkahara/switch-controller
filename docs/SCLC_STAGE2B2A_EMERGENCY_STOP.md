# SCLC S2B-2A local emergency-stop primitive

Status: local implementation and fake-Pi verification only. No autonomous
execution, leases, human rearm, production credential provisioning, physical
recovery or hardware deployment is implemented or authorized by this file.

## Stop outcomes and independence

The opt-in v2 listener in the existing daemon receives authenticated stop-role
estop requests. A bounded coordinator owns one independent stop worker and one
persistence worker. Concurrent requests share the active transaction. Work
continues if the requesting TLS connection closes. No ordinary command queue,
executor callback, or command_lock is required.

The receipt ack means only accepted. The terminal result independently records:

- activated: the transmission fence and authority revocation completed;
- internal_neutral: buttons/D-pad cleared, sticks centered, synthetic IMU reset;
- latch_durable: marker file sync, atomic replacement, directory sync and
  read-back completed;
- neutral_hid_transmitted: a successful 0x30 write carried the exact stop state
  generation; includes report sequence, state generation, epoch and timestamp.

Status ok requires all four. Missing HID streaming or write confirmation,
storage failure, or a fence timeout produces degraded. A 1-second network
response wait may return stop_pending without canceling the transaction.
Rejected human_owner_active preserves a verified unrelated human session.
USB write success proves Pi transmission only, never Switch acceptance.

A server instance constructed without a coordinator still reports
not_implemented. The daemon's explicitly enabled v2 listener attaches the
coordinator. Arm, controller commands, lease operations and rearm stay disabled.
There is no network or production path issuing autonomous mutation grants.

## Atomic mutation and ownership

All public non-neutral mutators validate authority under a guard held through
the pad-state update. An autonomous source label alone is insufficient: test
workers need the exact opaque grant object issued by this gate instance and
current generation. Stop invalidates that grant before neutralization.

A mutation committed before activation is erased by neutralization. A mutation
waiting for the guard, queued fake command, old callback, or subsequent step of
a compound operation fails with a revoked grant. No guard spans a sleep,
filesystem operation, network operation, USB operation or executor callback.
Private trusted Python memory is not a sandbox against malicious in-process code.

V1 control sessions, Unix-socket sessions, and active stdin are recorded under
the same gate. V1 human claims and the stop decision are atomic. Only a
consistent human record with no autonomous owner, orphan grant, pending stop,
or prior activation suppresses the stop. Human+autonomous inconsistencies
enforce the stop. Ordinary supervised-human paths retain their legacy behavior
outside autonomous ownership or a pending/degraded stop.

After all stop guarantees succeed, ownership becomes none; no held state or
connection is transferred. The persistent autonomy latch remains set. If stop
is degraded, non-neutral mutation stays blocked until another successful stop
or daemon restart; restart still requires human rearm for autonomy while
allowing supervised human operation. Neutral remains available to internal
safety paths.

## HID linearization and lock order

Input reports (0x30 and 0x21) are immutable bytes tagged at pack time with a
transmission epoch and state generation. Packing takes a consistent pad
snapshot. The writer takes the transmission lock, rejects untagged/stale input
reports, then begins its device write. Only actual successful 0x30 writes
advance report evidence. Test writers override the low-level device seam,
so fake writes still traverse the production fence.

The successful stop linearization boundary is the authority transition while
holding the transmission lock. The stop increments the transmission epoch
and clears pad state before releasing that lock. Consequently, any previously
packed input report attempting to begin transmission after that boundary is
rejected. A write already inside the device/kernel path cannot necessarily be
recalled; activation waits for it to finish, up to a 100 ms fence-acquisition
deadline.

If that deadline expires, the gate is revoked and pad state neutralized anyway,
and the epoch advances to reject subsequent stale reports. activated=false and
hid_fence_timeout explicitly mean a fully linearized HID fence was not
verified. An already admitted write may still complete; no success is claimed.

Lock order:

1. HID writer: transmission lock, brief pad snapshot, then USB I/O holding only
   the transmission lock.
2. Successful stop: transmission lock, authority gate, pad-state lock. Release
   all three before persistence or report waiting.
3. Mutators: authority gate, pad-state lock.
4. Persistence worker: store lock only; never authority, pad or transmission.
5. V1 ownership: authority gate then v1 session lock. Session cleanup releases
   the session lock before touching the gate.

A permanently frozen trusted state lock or daemon cannot be preempted by
another Python thread. This is a documented failure limit, not a process-
independent stop mechanism. The shared TLS handshake pool also retains the
S2B-1B hostile-flooding limitation.

## Persistence and restart

Every daemon startup loads marker state, derives a new in-memory generation,
forces requires_human_rearm=true, and neutralizes before starting HID threads
or listeners. Valid historical clear records never authorize autonomy on
restart. If storage cannot establish a generation, startup remains latched;
continuity is not claimed. Gate-instance identity prevents an old grant from
being valid in a new process.

Stop sets runtime state before storage begins. The persistence worker handles
short writes, file fsync, atomic rename, directory fsync and read-back outside
the mutation guard. Any failure leaves the runtime latch set. Storage timeout
does not create an unbounded number of workers: one worker can remain blocked,
and the single pending slot is bounded. The terminal durability claim remains
false if completion was not observed by the deadline.

Missing, partial, corrupt or conflicting marker recovery is fail-closed.
Restarts latch even if a crash restored older clear records. Plain SHA-256
checksums still do not authenticate markers or prevent offline rollback/root
tampering; the HMAC proposal and its limitations remain deferred. No clear-
latch operation exists in this slice.

## Verification methodology and scope

tests/test_emergency_stop.py uses event/barrier-controlled schedules and fake
device writes. Each applicable concurrency campaign runs 100 repetitions:
delayed compound mutations, blocked ordinary queue, packed HID reports,
human/autonomous inconsistency, human claim versus stop, in-flight HID writes,
guard linearization and coalesced stop/persistence requests. The TLS suite
adds 100 independent authenticated stop transactions with idle executor
connections and a fake HID stream.

Injected faults cover persistence writes, short writes, file sync, rename,
directory sync, read-back, stalled storage, missing/corrupt/conflicting markers,
simulated daemon restarts, and a blocked HID transmission fence. Restart tests
reconstruct daemon safety state locally; they do not prove SD-card power-loss
durability.

Latency samples use host monotonic timestamps from validated local receipt
(or the stop primitive call) to internal full neutralization, including worker
scheduling and lock acquisition. Nearest-rank p99 and maximum must be at most
100 ms and 150 ms respectively. Storage and HID outcomes are separately
asserted; blocked-kernel-write degradation is not counted as successful
activation. Fake-Pi timings are not measurements on the real Pi.

The complete suite also retains v1 golden fixtures, S2B-1A ownership tests,
S2B-1B TLS/resource tests and permanently disabled rearm checks.

## Local review result

Pre-acceptance local run: 65 tests passed in 9.940 seconds. Each row below contains 100
repetitions. Values are milliseconds on the Mac host running the fake Pi,
not measurements on Raspberry Pi hardware. All observed successful-activation
samples pass p99 <= 100 ms and maximum <= 150 ms.

| Scenario | p99 ms | Maximum ms |
|---|---:|---:|
| Coalesced requests / persistence worker | 0.036208 | 0.068167 |
| Ordinary command lock blocked | 0.011667 | 0.016125 |
| Inconsistent human/autonomous ownership | 0.004459 | 0.011292 |
| In-flight write released before activation | 0.079500 | 0.120792 |
| Mutation guard linearization | 0.054750 | 0.076083 |
| Previously packed input reports | 0.003916 | 0.004417 |
| Delayed/compound generation fence | 0.007083 | 0.019542 |
| Independent authenticated stop | 0.039750 | 0.084375 |

A separate 100-way-ordering campaign (100 two-thread trials) verified atomic
human-claim versus stop decisions: 35 stop wins and 65 preserved human claims.
No unexpected state transition or stale mutation was observed. The blocked
HID-fence and storage-failure cases correctly returned degraded outcomes;
these are not counted as successful activation timings. The test log is a
local diagnostic artifact, not sealed real-hardware evidence.

## Acceptance review correction

The final source review added explicit matching of HID evidence to the stop's
state generation, transmission epoch, positive report sequence, and a write
timestamp after internal neutralization. Mismatched or missing evidence cannot
produce status ok. Completion also checks the authority generation, so an old
transaction cannot release a newer stop. Persistence uses the transaction's
generation rather than a later global snapshot.

The corrected full suite passed 67 tests in 10.072 seconds. All eight measured
campaigns again ran 100 repetitions, plus 100 human-claim races. The largest
observed per-scenario p99 was 0.067209 ms; the largest maximum was 0.109916 ms.
These remain Mac fake-Pi measurements. The original 65-test log is retained;
the corrected run is recorded separately as sclc-s2b2a-acceptance-review.log.
