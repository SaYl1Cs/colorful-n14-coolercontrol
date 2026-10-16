# Upstream producer provenance contract (schema 1)

The matching patched daemon/plugin are installed on the development laptop.
See [current verification](../DEPLOYMENT-VERIFICATION.md) and
[source build instructions](../BUILDING.md). Source-task and offline build
notes below are retained as engineering history, not current deployment status.
Labels alone do not authorize lowering fans: the adapter's validation is required.

## Integration contract

Plugin config command key is `set_duty_provenance` (snake_case), whose value is a trusted
command prefix, e.g. `/path/to/adapter set-duty-provenance`. Do not include `{duty}`,
JSON placeholders, pipelines or shell control operators. Plugin appends **two positional
arguments**: decimal device duty, then one literal compact JSON string. JSON is never
interpolated into shell source. Command exit zero is success; nonzero/spawn failure is
RPC failure. Existing `set_duty` is fallback only if the provenance command is not configured.
If configured, missing/invalid/oversized (>65536 bytes) provenance is rejected, never downgraded.
Consumer must parse and validate the entire schema; plugin only checks JSON object and size.

Daemon FixedDutyRequest protobuf field 4 is `string provenance_json`. Old clients send empty;
old plugins ignore this field, so both patched components and the opt-in command are required.

Successful PUT `/devices/{device_uid}/settings/{channel_name}/profile` now returns JSON:

```json
{"schema":1,"producer_session":"uuid-v4","device_uid":"daemon UID","channel_name":"both-fans","assignment":{"generation":12,"profile_uid":"selected profile UID","assigned_at_unix_ms":1792115000000}}
```

Unsupported profile paths return JSON null. Capture expected session, device, channel,
profile and generation from this response, under the parent's persistent closed fence.
HTTP success is still NOT a first-command acknowledgement or a cancellation barrier.
Assignment is captured immediately after scheduling, before persistence awaits. A failed
save can leave the setting active; keep the fence closed on every failure.

The command JSON has the same envelope plus `calculation`:

```json
{"schema":1,"producer_session":"uuid-v4","device_uid":"daemon UID","channel_name":"both-fans","assignment":{"generation":12,"profile_uid":"cpu-profile","assigned_at_unix_ms":1792115000000},"calculation":{"calculation_id":13,"calculated_at_unix_ms":1792115000100,"profile_uid":"cpu-profile","duty":42,"inputs":{"kind":"graph","source_device_uid":"sensor device UID","source_temp_name":"cpu","function_uid":"standard-function","status_observations_newest_first":[{"status_unix_ms":1792115000050,"temperature":63.0,"sensor_sample":null}],"sensor_sample":null,"freshness_scope":"daemon_status_not_sensor_sample","stateful_function":true}}}
```

This sampleless example is intentionally rejected by the consumer. The sensor-input
contract below describes how actual sampler metadata replaces the null fields.

Generation and calculation IDs share a checked, increasing u64 counter within the producer
session. They are NOT contiguous, global across restarts, or EC power-state generations.
Same-profile reassignment receives a new generation; calculations predating it cannot be
labelled for it. Process restart creates a new session. All timestamps are Unix milliseconds,
not monotonic time. Reject future timestamps and treat clock discontinuities conservatively.

Mix calculation `inputs` is `{ "kind":"mix", "members":[...] }`. Each member has
`profile_uid`, `kind` (`fixed` or `calculated`). Fixed members have `duty`. Calculated members
have `retained` and `calculation` (recursive shape above, or null when unknown). Retained
members retain their OLD calculation ID, timestamp, source observations and duty. A new Mix
calculation timestamp does not refresh its members. Consumer must recursively validate
all required members and reject unknown/null calculations. Fixed members are not samples.

Graph source observations are captured synchronously with the actual function/graph
calculation, before any async queueing. They describe the newest daemon status history
window (up to the configured response-delay stack size), including null temperatures for
missing source data. This is observation provenance, NOT a complete trace of hysteresis
state. Standard functions retain previous accepted temperatures and duty state; the list
is not a claim that every internal state contributor is fresh. The original algorithm,
curves and function behavior are unchanged.

Queued commands own their original serialized labels; dispatch does not look up current
profile/session/generation. A dropped RPC waiter does not cancel the hardware command.
The parent must fence at the actual write boundary, checking exact expected assignment,
command age, member/source ages and its independent live safety conditions.

## Sensor-input contract (implemented, parent supplies sensor command)

Set `provenance: true` on each plugin temperature entry. Its existing `command` must return
one bounded (<=16384 bytes) JSON object, atomically pairing Celsius with source metadata:

```json
{"temperature":63.0,"sample":{"session":"sampler-session","generation":7,"sample_id":101,"sampled_at_unix_ms":1792115000050,"sampled_at_monotonic_ns":123456789,"sensor":"cpu","power_state":"active"}}
```

`temperature` is finite Celsius, not millidegrees (no coercion on this path). `sample` must
be an object. The listed sample fields are the agreed recommended consumer shape, not
plugin-generated values: the parent must supply and strictly validate the identities,
clock domains, types and freshness semantics. The plugin transports the sample object
without manufacturing any session, generation, ID or timestamp. Malformed/failed reads
omit the sensor; they never fall back to plain numeric parsing.

Status protobuf field 6 is `string sample_provenance_json`. The daemon maps it alongside
the exact returned temperature and keeps it aligned with the status timestamp in a bounded
sidecar history. Each graph observation has `sensor_sample` (the original object, or null).
Numeric grace-cache reuse after missing/failed sensor reads does NOT refresh the identity:
missing sensors lose metadata immediately and whole-read failure clears it. Repeated reads
of the same underlying sample retain its original ID and timestamp. Synthetic startup
history, custom aggregate sensors and unmodified numeric sensor commands have no sample
metadata. The consumer must reject those nulls, not substitute local current samples.

For compatibility with the current Python fence, Graph inputs also carry a graph-level
`sensor_sample`: an exact copy of the **newest** observation's sample object, only when
that observation has a finite temperature. It is null if unavailable or malformed. It
never searches older observations for a replacement and never substitutes the consumer's
local sample. The per-observation history remains authoritative for the complete window;
the graph-level copy does not assert freshness of every historical or internal contributor.

## Explicit deployment blockers / limitations

* The new optional sensor-input path carries original EC sample metadata only when the
  parent implements the JSON sensor command and configures it. Legacy plain temperatures
  still have no sample identity. `status_unix_ms` remains an observation timestamp, not
  firmware freshness. **Do not unlock based on daemon timestamps alone.**
* Standard-function retained internal temperature/duty state is not fully traced by the
  observation-window metadata. This patch does not prove that every state contributor
  belongs to a new thermal generation. That remains a blocker for claiming safe transition
  completion solely from these labels; the parent must not hide this limitation.
* No atomic GPU power-state lease is established. No calibration of temperatures is implied.
* Provenance is provided for software Graph and Mix assignments. Overlay, fixed/manual,
  hardware-curve and calibrated channels are unsupported and send unlabelled duties;
  the opt-in plugin rejects these. Calibration's deferred target can change independently
  of the original request, so attaching its old label would be incorrect.
* Labels are not authenticated capabilities. Protect the local plugin, command configuration,
  transport and consumer socket. A caller with access can forge strings.
* No freshness timeout is invented by the producer. The consumer decides limits and rejects
  missing observations, nulls, retained stale members and clock anomalies. Dedup/Standard
  suppression can leave long intervals without commands; watchdog policy must account for
  that without treating absence as fresh proof.
* The early consumer used for the producer-only tests did not yet strictly validate all
  contract fields. The later [Python/C integration](../IPC-INTEGRATION.md) added ordering,
  sampler identity/clock, observation-history and recursive member checks in the
  [Python fence](../provenance_fence.py). Passing an envelope is still not certification
  of complete hardware safety, an atomic power-state lease or temperature calibration.
* RPC success means the configured command exited zero, not independently verified hardware
  state. Async profile scheduling logs write failures; it cannot retroactively fail a prior
  profile PUT. Late process completion after transport timeout remains possible.

## Reproduction and verification — rebuilt 2026-10-16

These results supersede the pre-deletion claims. Both pinned commits were checked again:

* CoolerControl 5.0.1: `d9201247d6f61a5ddf8e2af8faa3aba10641e340`.
* Custom-device 0.2.0: `4e7268179a521f625ef00ab2c7f3de34d2e81c74`
  (annotated tag object `2fd1924108ac70f40734a87ec06fc305cfad166f`). The plugin was freshly
  cloned from GitLab into a new, non-destructive project build directory.

Apply [`coolercontrold-producer-provenance.patch`](coolercontrold-producer-provenance.patch)
and [`custom-device-producer-provenance.patch`](custom-device-producer-provenance.patch)
only to their respective clean pinned source trees, never to a live install. The daemon
patch now includes the entire new producer module, all previous tests, eight added Rust
tests, and the regenerated OpenAPI document. The plugin patch preserves its four existing
tests and adds a complete command-to-status-protobuf sample transport test.

Both patches were generated using Git diffs restricted to named files, with intent-to-add
for the new producer file. Verification extracted the original versions from the pinned
commits into clean isolated directories, checked/applied/reverse-checked the patches, and
compared **every resulting file byte-for-byte** against the build source: 18 daemon files
and 5 plugin files. UI assets, build outputs and unrelated checkout changes are excluded.
This establishes patch/source reproduction, not bit-for-bit reproducibility of binaries
across different toolchains or embedded UI asset timestamps.

Toolchain: Arch Linux Rust 1.99.0 (`b940084d7`), Cargo 1.99.0 (`5f94df478`),
protobuf compiler 36.1. All Cargo commands used the pinned lockfiles and two build jobs.
Targets were placed on disk in the project's build-verification directory, not on the
memory-backed temporary filesystem. The existing prebuilt daemon UI assets were reused;
UI tests/build and hardware deployment were outside this repair.

### Repeating the build

Use the checkout-relative commands in [BUILDING.md](../BUILDING.md), including
real UI assets, pinned lockfiles, explicit binary targets and two Cargo jobs.
The original temporary source-tree names are not part of the public interface.
The daemon has no library target. Build products, logs and success receipts stay
local under the ignored build-verification directory; none are downloadable
artifacts or evidence files in this repository.

The first daemon test run found a stale OpenAPI document (1983 passed, 1 failed, 2 ignored).
It was regenerated via the offline CLI's OpenAPI subcommand, without starting a service,
and included in the patch. The test was preserved, not bypassed.

### Verified tests

* Full daemon binary-target suite (recorded local build log):
  **1984 passed, 0 failed, 2 ignored**, no filters. This includes 13 producer-specific tests
  covering identity, ordering/reassignment, queue immutability, complete sample fields,
  retained Mix inputs, failure propagation, protobuf mapping and stale cache metadata removal.
* The two ignored upstream tests require a live power-profile D-Bus daemon or a real block
  device plus sudo. They were deliberately not enabled. The optional gated-tests feature
  (hardware/performance/timing-dependent tests) was not enabled either.
* Plugin binary-target suite (recorded local build log):
  **5 passed, 0 failed, 0 ignored**. Includes literal JSON arguments, command failure/RPC
  propagation, missing/invalid provenance rejection, bounded sample parsing, full original
  sample metadata through actual protobuf encoding/decoding, repeated samples without
  retimestamping, and omission of failed/malformed sensor reads without numeric fallback.
* Cross-language engine-envelope checks (recorded local test log):
  **6 passed** against the unchanged Python consumer: accept actual engine output, reject
  missing sample, stale sample, stale calculation, wrong producer session and wrong generation.
  The Rust engine test can export its actual envelope when given a test fixture output path
  through the CC_PRODUCER_TEST_FIXTURE environment variable. Sampler data is explicitly
  simulated; freshness was evaluated at the recorded calculation time, not claimed as live.
* Early Python fence tests (recorded local test log): **5 passed**. Later consumer
  validation and its expanded suite supersede that compatibility-only result.

### Release artifacts

Both release builds completed successfully with two jobs. The binaries were kept on
persistent disk and were not installed during that producer-only build task. They
were subsequently deployed as recorded in [deployment verification](../DEPLOYMENT-VERIFICATION.md).
They and their build logs are deliberately excluded from the public repository.

| Local artifact | Size | Recorded result |
| --- | ---: | --- |
| coolercontrold daemon | 49,198,848 bytes | Release build passed; offline OpenAPI export matches the patched document byte-for-byte |
| custom-device plugin | 2,271,128 bytes | Release build passed after the expanded five-test suite |

Both are host-built x86-64 Linux dynamically linked ELF executables; the daemon is
unstripped and the plugin is stripped per its upstream release profile.

The [SHA-256 manifest](BUILD-SHA256SUMS) records both patches and both binaries with paths
relative to the Desktop workspace. Verify it there with:

```sh
sha256sum -c nbfc-n14-control/patches/BUILD-SHA256SUMS
```

The daemon release has one non-fatal dead-code warning for the legacy ordinary-duty
wrapper, which remains exercised by tests. Stable Rustfmt also reports that two upstream
nightly-only formatting options are ignored; the targeted format checks passed. Neither
warning was hidden or converted into a skipped test.

Rustfmt was run on the new/edited Rust source. Generated tonic/prost source is regenerated
by Cargo and is not stored in patches. No test was deleted or disabled for that repair.
The producer-only build task did not commit/push, operate live services, deploy, change
system configuration or edit the Python integration/installer. Those are historical
scope statements, not the current installation or publication status.
