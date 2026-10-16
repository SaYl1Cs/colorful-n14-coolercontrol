# Python/C IPC integration — design and engineering history

> **Installed and live-verified on 2026-10-16.**
> [DEPLOYMENT-VERIFICATION.md](DEPLOYMENT-VERIFICATION.md) records the current
> installation. [QUIET-LIFECYCLE.md](QUIET-LIFECYCLE.md) supersedes the unconditional
> full-duty handover described below. Earlier staged/deployment statements and
> test counts are historical; use [README.md](README.md) for current requirements.

The integration described here superseded the older staged-consumer/blocker
notes in [EC-MIGRATION.md](EC-MIGRATION.md) and
[PRODUCER-CONTRACT.md](patches/PRODUCER-CONTRACT.md), without changing the producer
patches or release artifacts. That integration-only task did not install or start
anything; the completed later deployment is recorded above. The requirements and
verification sections below preserve the state at that earlier task boundary.

## Wiring implemented and exercised offline

1. [sensor_cache.py](sensor_cache.py) reads CPU EC 0x56 and, only for confirmed
   runtime active + PCI D0, GPU EC 0x58. It reads temperature bytes read-only.
   Confirmed runtime suspended + D3cold has no GPU number. No CPU substitution,
   zero sentinel, NVIDIA API, LACT, hwmon fallback, or temperature EC write exists.
2. Each sampler process gets a UUID-v4 session. Sample IDs increase per
   observation; generations increase on active/suspended/error changes and
   recovery. Errors never reuse old values. Unix milliseconds and **CLOCK_BOOTTIME
   nanoseconds** describe the start of I/O. The producer field is named
   `sampled_at_monotonic_ns`, but its agreed clock here is BOOTTIME, including suspend.
3. [adapter_client.c](src/adapter_client.c) sends bounded one-line Unix RPC to
   the serial [adapter_server.py](adapter_server.py). The provenance argument is
   retained literally, including spaces: at most 65,536 UTF-8 bytes, plus bounded
   command framing. Partial sends/receives, response bounds and deadlines are
   handled. No Python process is launched per plugin read/write.
4. Temperature commands **temp-cpu/json** and **temp-gpu/json** return Celsius and
   the original paired sample object. Hyphen aliases **temp-cpu-json** and
   **temp-gpu-json** are equivalent. Legacy numeric endpoints remain diagnostics
   in millidegrees. Sleeping/invalid GPU JSON requests fail; they return no number.
5. The patched plugin transports those sample objects into daemon history; the
   patched daemon emits immutable Graph/Max-Mix assignment/calculation envelopes.
   Its **set_duty_provenance** command prefix receives decimal duty and one JSON
   argument. The C command is **set-duty-provenance**. Plain **set-duty** accepts
   only 100; fixed/manual lower duties cannot bypass the fence.
6. [profile_selector_service.py](profile_selector_service.py) uses the already
   enrolled token via [authorize_coolercontrol.py](authorize_coolercontrol.py).
   The token's installed path is defined by that module's TOKEN_FILE constant;
   neither token contents nor credential-bearing exception messages are logged.
   The API is fixed to loopback port 11987, bearer-authenticated, with no proxy or
   redirect support. Credentials and bindings must be private root-owned files;
   symlink components are rejected. Core dumps are disabled for the selector.
7. [profile_selector.py](profile_selector.py) reads live profiles, functions,
   devices, global poll interval, target assignment and daemon health. Only the
   target's profile assignment is changed; graphs/functions/device definitions
   are never created or edited. **CPU Graph** is selected in suspended/D3cold;
   **Maximum(CPU Graph, GPU Graph)** in active/D0.
8. Adapter RPCs **fence-close-full**, **install-assignment**, **assignment-status**
   and **renew-assignment** implement the handover. **fence-close** invalidates
   without writing; **reset** closes and requests full. A close UUID prevents a
   delayed install callback from undoing a later close. Full confirmation means
   the guarded firmware command completed, **not proven fan rotation**.

## State machine and timing

The persistent fence has closed, awaiting-first-calculation and active phases.
An adapter restart discards the old assignment while holding the existing sole
hardware-owner lock. The selector also has a single-instance lock.

- On startup, transition, sampler restart/error recovery, valid live policy edit,
  changed target assignment, or daemon restart hint: close, request full, PUT the
  selected profile, verify its returned identity and recheck the live catalog,
  then install the exact envelope bound to the current sampler epoch.
- HTTP 200 only enters awaiting. It **cannot** permit an old CPU command after
  wake. The first accepted command must match the new producer session, exact
  assignment generation/profile/target/timestamp, required Graph/Mix structure,
  bound sources/functions and current sampler session/generation/power mode.
- Stable polls renew a 15-second selector lease. They do **not** close, write full,
  or PUT again. The selector's own restart deliberately gets a new assignment.
  Daemon PID/uptime changes are restart hints only, never write authorization.
- Every lower-duty command validates a current EC snapshot and reads current PCI
  runtime/power state. PM is read again after persistent validation, immediately
  before fan writes. Replay/out-of-order IDs, incorrect duty types or arguments,
  null inputs, unknown members and wrong sources are rejected. A failed lower
  write attempts full duty and invalidates the assignment.
- The watchdog checks every **one second**, independently of selector HTTP work.
  Socket clients are nonblocking, bounded to 32 concurrent connections and one
  second of framing time; a partial/slow client cannot monopolize the watchdog.
  Invalid/absent/stale active GPU data, live PM mismatch, sampler epoch changes,
  critical temperatures or lease failure close the fence and request full.
- Current sample age limit: **5 seconds**, checked in both Unix and BOOTTIME
  domains; clock disagreement over one second is rejected. Detection after age
  expiry occurs on the next watchdog check. Critical thresholds remain actual
  CPU 90°C and actual active GPU 80°C.
- Queued **top-level commands must be no older than 2 seconds**. Same-mode
  retained Graph calculations may be up to **65 seconds** old, allowing the
  producer's 30–60 second Standard safety latch. Every observation retains its
  own clock, sensor and epoch validation; older response-delay history may extend
  to the configured window plus that retention allowance. Null synthetic history
  never counts as original data. Historical inputs do not replace the separate
  fresh local CPU/GPU check.
- A first accepted command and subsequent commands for **max(response_delay)+65
  seconds** receive a conservative floor from the freshly read actual curve
  demand. The interpolation/normalization follows the inspected producer; the
  saved curve and Standard function are not altered. During that interval the
  watchdog requests full if fresh demand rises above the held duty. Afterwards
  incoming producer duties pass unchanged, preserving Standard smoothing. If the
  producer suppresses unchanged output, a previously applied higher floor can
  remain until the next valid output; it is not lowered by replaying an old label.
- Mix dedup counts **produced outputs**, not polls. Standard can emit only once
  per 60 seconds, with Mix suppressing 30 equal outputs: a legitimate interval can
  reach 1,860 seconds. Output absence is therefore bounded at **1,900 seconds**,
  while fresh sensors, live PM, catalog and API health are still checked normally.
  First-calculation waiting is separately bounded by transition duration +100
  seconds; until then fans remain full. This avoids periodic forced-full cycles
  during long stable operation.

These are pragmatic software bounds, **not an atomic power lease**, proof of EC
firmware freshness, or a full trace of Standard's retained internal state. A
power transition after the final sysfs read can race a fan command until the
next watchdog check. Scheduler stalls, blocked kernel/ACPI I/O, unavailable
hardware, process death and competing privileged writers prevent an absolute
one-second physical guarantee. Root-accessible labels are not cryptographic
capabilities. All installed producer/plugin/config/adapter files must remain
trusted and no other controller may own the fans.

## Parent deployment requirements — not performed here

### Existing IDs and live mutable curves

The previously observed profile UIDs are embedded as discovery defaults in
[capture_profile_bindings.py](capture_profile_bindings.py):

| Role | Existing profile UID |
| --- | --- |
| CPU | 240b496f-027a-4e71-a978-47703a14c9ee |
| GPU | f8997ee4-df30-4f2b-8b44-7eb08e5d0398 |
| Max Mix | 5732965b-667c-4f05-84f4-d0ead7cc0c43 |

Run the read-only discovery helper under the parent's authorized root context.
It prints only an identity binding, never a credential, and writes no files.
It resolves the unique CustomDevice with internal sensor names **cpu_temp** and
**gpu_temp** and channel **both_fans**. It checks the saved graphs actually use
those exact sources and the Mix has exactly those two members with Max. Ambiguous,
missing or changed inputs fail; the helper does not rewrite them. A root-only
live configuration read was unavailable during this task (sudo required a
password), so the actual daemon device UID must be resolved on deployment,
not guessed from the Rust test device UID.

The parent stores the resulting object at BINDINGS_PATH from
[profile_selector_service.py](profile_selector_service.py), root-owned mode 0600
under the same private 0700 directory as the enrolled token. The file has three
members: profiles (role-to-UID), sources (role-to-device/temp pair), target
(device/channel). **It contains no saved curve/function copies.** Valid curve,
Standard parameter, Graph function UID and display-name edits remain live from
CoolerControl and trigger a fresh fenced assignment. Sensor rebinding, unsupported
function type, deleted profiles, invalid curve values or changed Mix inputs fail
closed. Mix's unused default Identity function UID 0 is correctly allowed.

### Plugin options the parent must apply

At the integration-only task boundary the deployment configuration was still
unchanged. The current [coolercontrol-config.json](coolercontrol-config.json)
and transactional installer now include the options below. Preserve existing
device/sensor/channel IDs and unrelated plugin entries during an upgrade:

| Existing entry | Required option/value |
| --- | --- |
| both_fans commands | set_duty_provenance = installed C client followed by **set-duty-provenance** |
| cpu_temp | provenance = true; command = installed C client followed by **temp-cpu/json** |
| gpu_temp | provenance = true; command = installed C client followed by **temp-gpu/json** |
| GPU display label | Actual EC GPU temperature; unavailable during D3cold |

The provenance command is a trusted prefix with **no duty or JSON placeholders**;
plugin code appends both arguments literally. The installed client location is
already specified by the old configuration. Configure both patched components,
not one alone. No calibrated/manual/hardware/Overlay channel is supported by
this consumer. Keep the global CoolerControl poll interval at **1.0 second**;
the selector rejects other values rather than silently relying on wrong timing.

### Services and module set

Parent installs the C client and these modules together, never individually under
running services: [sensor_cache.py](sensor_cache.py), [thermal_state.py](thermal_state.py),
[coolercontrol_adapter.py](coolercontrol_adapter.py), [adapter_server.py](adapter_server.py),
[provenance_fence.py](provenance_fence.py), [control_policy.py](control_policy.py),
[profile_selector.py](profile_selector.py), [profile_selector_service.py](profile_selector_service.py),
plus the existing authorization and preparation modules they import.

The new [selector service](colorful-n14-profile-selector.service) starts after
and requires the existing adapter, sampler and CoolerControl services. It connects
to the existing local CoolerControl API; it does not launch another daemon.
The [sampler service](colorful-n14-sensors.service) no longer references an NVIDIA
persistence service or obsolete backend-selection environment variable. Existing
adapter/preparation ownership checks remain. No live unit was touched.

No installer, prerequisite-enforcement or transaction module was changed in this
integration task. Their pre-existing guards and restrictive read-only-module
requirements remain **parent work**; do not mistake them for a runtime requirement.
The new sampler only needs read access and does not globally disable EC writes,
unload a writable EC module, remove ISW settings, or require write_support=0.
Parent must handle backups, coordinated deployment/rollback, debugfs permissions,
old installed polling drop-ins and supervised live checks without changing
NVIDIA/LACT settings. Neither temperature calibration nor GPU fan rotation/load
validation is established by offline tests.

## Verification

**194 tests passed, three consecutive complete runs** at this earlier integration
stage (2026-10-16). The three local logs are excluded from Git, along with all
other build-verification output. See [README.md](README.md) for the later full-suite
count and [BUILDING.md](BUILDING.md) for checkout-relative reproduction commands.
The hardened C11 build, Python AST checks, Git whitespace check and offline
systemd unit verification also passed. No live sensor/ACPI access was used by
these tests. Large history windows can exceed the producer's 65,536-byte label
limit; those configurations fail closed rather than truncating provenance.

Run from the Desktop workspace:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=nbfc-n14-control:nbfc-n14-control/tests python3 -B -m unittest discover -s nbfc-n14-control/tests -p 'test_*.py'
cc -std=c11 -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -fstack-protector-strong nbfc-n14-control/src/adapter_client.c -o /tmp/n14-adapter-client
systemd-analyze --man=no verify nbfc-n14-control/colorful-n14-profile-selector.service nbfc-n14-control/colorful-n14-sensors.service nbfc-n14-control/colorful-n14-adapter.service
```

The suite uses temporary sensor snapshots, state files and Unix sockets; ACPI fan
writes are mocked. It exercises the compiled C client at the exact 65,536-byte
limit, assignment RPCs, malformed frames, root-peer enforcement, slow-client
watchdog progress, 400-second and 1,860-second stable operation, sleep/wake,
producer/sampler/selector/adapter restart, stale queued and retained calculations,
source changes, live curve edits, partial failures, replay and both temperature
JSON paths. Existing credential/HTTP/authorization and unrelated project tests
remain in the suite.

The saved [Rust engine envelope](tests/fixtures/producer-engine.json) is copied
unchanged from the producer verification output. Its synthetic/null startup
history is explicitly rejected; a second test keeps its actual assignment,
calculation/source IDs and envelope structure while supplying a simulated complete
same-mode sampler history. This is a consumer protocol test, not a claim that a
live Rust daemon was started or that its old synthetic fixture was safe. Rust
release builds and 1,984 daemon tests are the prior producer results documented
in [PRODUCER-CONTRACT.md](patches/PRODUCER-CONTRACT.md), not re-run here.
