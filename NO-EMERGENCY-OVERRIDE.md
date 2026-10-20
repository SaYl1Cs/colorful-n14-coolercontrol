# No automatic emergency fan override

## Current policy

Automatic emergency speed substitution in the N14 adapter is disabled. No
replacement 100%, 50%, 5% or 0% command is sent on sensor errors, stale snapshots,
invalid commands, expired authority, failed fan
writes, service teardown, or startup/resume validation failure.

The adapter still rejects invalid commands and revokes the old assignment.
Revocation is a state-file operation, not a fan write. It does not confirm a
known speed or invent a temperature. The previous fan setting can therefore
remain unchanged **indefinitely**, including a stopped-fan setting. After a
partial firmware write, the two targets or mode may be different/unknown;
there are no automatic recovery writes or retries.

**Risk:** software no longer forces cooling when temperature observability is
lost or a critical temperature is detected. Hardware/firmware protections are
not changed by this patch, but their existence does not prove this policy safe
under all workloads. A nonzero low command does not guarantee either fan rotates.

## Normal control and recovery

- Hardware identity, permission, root-only socket access, strict framing,
  sensor freshness, assignment identity and replay checks remain intact.
- Fresh CPU/Max profile commands continue to control both fans. Saved curves and
  Standard functions are not edited. A normal curve or an explicit operator
  request can still request 100%; this is not an emergency override.
- Valid high readings (including CPU 100 degrees C) are published as measurements,
  not rejected as sensor errors. They do not close the assignment or block the
  curve's 100% commands. No additional full-duty threshold or hysteresis is added.
- After a failure, a fresh authenticated assignment can enter awaiting state
  without a forced-full handshake. Its current commanded duty is unknown until
  a successful validated write is recorded. New observations/lease renewals alone
  do not fabricate duty confirmation.
- A valid first command can apply normally, including zero if the user's curve
  asks for zero. No emergency minimum is inserted.
- Existing short handover/sensor holds and curve-based transition floors remain
  normal-control behavior. Expiry now revokes authority **without** changing duty.
- A failed keyboard-light fan return leaves the already selected firmware
  automatic mode alone; it neither restores invalid cached targets nor forces
  both targets full.

## Compatibility and scope

The existing `reset`, `lifecycle-stop` and legacy `fence-close-full` RPC names now
revoke authority without forcing full duty. The historical name is retained so
existing clients and the selector remain wire-compatible. Explicit `set-duty 100`
is still a full-duty request. This change does not introduce a new fixed/manual
GUI protocol or remove the original provenance requirements for lower duties.

The temperature curves, producer/plugin binaries, legacy NBFC experiment and
firmware are separate from this adapter error policy. In particular, this is not
an attempt to disable BIOS/EC thermal shutdown or stop every possible source of
a legitimate 100% command.

## Verification and deployment

The offline suite includes [no-override regressions](tests/test_no_emergency_override.py):
missing/stale sensors, critical readings, malformed/replayed commands, expired
lease, API/storage failures, partial writes, lighting failure, startup/deep-resume
clock gaps, fresh-command recovery, and explicit/normal full-duty requests.
Firmware writes are mocked; these tests are not physical thermal validation.

Deploy all changed Python modules together with the [transactional installer](install.sh).
Its backup and timed rollback remain enabled. The live health check verifies fresh
snapshots, current assignment, selector renewal and a valid awaiting/active state;
it no longer requires a historical full-duty confirmation. No live suspend test
is performed automatically by installation.
