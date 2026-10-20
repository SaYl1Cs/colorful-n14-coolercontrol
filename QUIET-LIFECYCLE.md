# Quiet lifecycle handover and bounded sensor-loss grace

> **Current behavior supersedes the historical full-duty fallbacks below.**
> Emergency fan overrides are removed: errors and expired holds only revoke
> authority, without changing fan speed. Fresh control can recover without a
> full-duty handshake. See [no-override policy and risks](NO-EMERGENCY-OVERRIDE.md).
> The original timing rules and earlier deployment observations below are retained
> as engineering history, not a claim that emergency cooling is still enabled.

**Installed on the development laptop on 2026-10-16.** Live idle verification
confirmed D3cold with CPU-only control and clean logs; an actual reboot/acoustic
check remains outstanding. See [DEPLOYMENT-VERIFICATION.md](DEPLOYMENT-VERIFICATION.md)
for the current deployment and rollback. This document supersedes the earlier
unconditional full-duty lifecycle behavior, not the command-provenance or
hardware-identity requirements. The offline task notes below are historical.

## Exact limits and behavior

| Situation | Behavior and limit |
| --- | --- |
| Adapter startup | After platform/EC/control-permission guards, require a valid current paired snapshot, matching live PCI power state, manual fan mode, and **both actual target bytes in 1–70**. Preserve them individually without writes. Raw zero, unknown mode/duty, invalid/stale/hot data or failed guards cannot establish a quiet hold. |
| Startup / profile assignment / valid mode transition | A separate closed-fence hold lasts **at most 10 seconds**, including assignment installation and waiting for the first valid producer command. Repeated close/assignment/renewal/restart attempts carry the original deadline, not another ten seconds. |
| Fresh curve demand during handover | Use the existing authenticated live policy, without editing it. Increase each target only if it is below current EC curve demand; do not lower the stronger fan to match its peer. Startup without a live policy may only preserve existing targets during the short cool-state hold. |
| Transient thermal availability failure | Hold the last known successful setting only until the oldest required safe measurement reaches **5 seconds total age**. Original BOOTTIME and wall-clock ages both count. This is **not five extra seconds after the normal freshness limit**. |
| Repeated errors, missing files or rereading the same snapshot | No timer renewal, timestamp rewriting, synthetic temperature or new sensor provenance. Only a genuinely newer validated safe snapshot can supply a newer deadline. |
| Cool-state eligibility for any hold | CPU **below 80°C**; when GPU is active, GPU **below 70°C**. These extra margins govern permission to hold, not the saved curves or normal producer duty. D3cold has no GPU temperature requirement or fabricated value. |
| Emergency thresholds | CPU **90°C or above**, or active GPU **80°C or above**, immediately request full duty. A critical available sensor also overrides failure of the other sensor. |
| Unknown duty, malformed/replayed/stale command, policy/API validation error, expired authority or failed actual write | No thermal grace. Close the assignment and request full duty. Failed/partial writes attempt emergency writes to both fans, preserve uncertainty on failure, and cannot establish successful full confirmation. |
| Ordinary service stop, restart or exception | Full duty remains the default. Signal delivery alone never authorizes quiet shutdown. Startup/socket setup failures after establishing a hold also execute full-duty cleanup. |

The existing one-second watchdog enforces running-process deadlines. Expiry is
checked at the next watchdog tick (normally within one second), and also at
assignment/control boundaries. Firmware/sysfs I/O and operating-system scheduling
are not hard real-time. Existing selector leases, producer silence limits,
transition floors, command-age limits, replay fences and critical protections
remain in force. If a producer's response history takes longer than the ten-second
quiet assignment allowance, full duty is the intentional fallback until normal
validated control is available.

A command received during thermal loss is **not successfully applied**. It can
leave duty unchanged only after validation against the unchanged original safe
snapshot and current fence. Its calculation ID is consumed without renewing
output/lease timestamps. The reply remains an error; replaying it or sending a
malformed/stale command still forces full. Normal recovery may require a new
assignment because the sampler's generation changes across error/recovery.

## Planned shutdown / reboot

The adapter checks systemd **only on lifecycle reset/finalization**, not on every
watchdog or temperature read. It accepts either the system manager's exact
stopping state or a queued/running start job for shutdown, reboot, poweroff or
halt. The observation uses no shell, has a **one-second total subprocess budget**,
and caches positive evidence for **at most ten seconds**. Timeout, failure,
unknown state or an ordinary service-stop job does not confirm system shutdown.
There is no caller-supplied shutdown flag or unauthenticated bypass; the existing
root-only socket/peer protections remain unchanged.

The plugin's existing reset command, the selector's orderly finalization and the
adapter's orderly finalization share the same bounded hold deadline. They can
preserve a verified cool known setting only with confirmed system shutdown.
Critical/invalid data, unknown duty or an expired hold still requests full.
The explicit close-and-full command is unconditional, including during shutdown;
the ordinary reset command also remains full outside confirmed system shutdown.
The watchdog and exception paths use unconditional full, never the shutdown
exception. No Rust rebuild or plugin command/configuration change is required.

**Boundary of control:** while the adapter remains running, its watchdog enforces
the hold deadline. On confirmed orderly system shutdown it exits without an
unnecessary full-duty write if the hold is still valid. There is no software
watchdog after that process has exited, and no hardware lease is claimed. Firmware
and the remaining shutdown/boot sequence own that interval. BIOS/EC fan behavior
before userspace starts, after final adapter exit, or during a stalled firmware
reboot cannot be guaranteed quiet or bounded by this Python process. This change
does not disable firmware protections or change an EC global flag.

## Parent deployment scope

Deploy the changed runtime Python modules together using the parent's existing
controlled transaction, not by overwriting them beneath running services:

- [adapter_server.py](adapter_server.py): bounded lifecycle/reset dispatch,
  systemd observation, watchdog and exception cleanup.
- [coolercontrol_adapter.py](coolercontrol_adapter.py): validated individual
  startup targets, last-safe sample accounting, local raise-only handover and
  failed-write protection.
- [provenance_fence.py](provenance_fence.py): distinct expiring hold marker,
  assignment acceptance, authority checks and consumed rejected-command IDs.
- [thermal_state.py](thermal_state.py): typed thermal unavailability without
  confusing malformed provenance/clocks with a recoverable sensor failure.
- [sensor_cache.py](sensor_cache.py): consistent paired error-mode metadata when
  CPU fails but a GPU measurement is still available.
- [profile_selector.py](profile_selector.py) and
  [profile_selector_service.py](profile_selector_service.py): bounded handover
  handshake, thermal-only safety requests and adapter-verified orderly stop.

No new runtime module, service-unit change, credential, installer operation,
curve/function edit, NVIDIA query, EC global flag change or Rust binary change
was needed. The existing [C client](src/adapter_client.c) remains compatible;
new selector-local RPCs use its existing Python socket transport. Full-duty
confirmation is recorded only after successful actual full-duty commands; a
preserved target is never presented as that confirmation or as measured rotation.

## Historical offline verification

**275 tests passed in two consecutive full-suite runs** at the lifecycle task
boundary, including the existing 236-test baseline and 39 added lifecycle/clock/socket
cases. Python AST validation passed for 33 files. The C11 client compiled with
warnings as errors, fortified source and stack protection; Git whitespace validation
passed. Both run logs remain local and are deliberately excluded from Git.
See [README.md](README.md) for the expanded suite and reproduction commands.

- [New lifecycle and systemd-observation tests](tests/test_quiet_lifecycle.py)
- [Compiled C/socket integration tests](tests/test_integration_protocol.py)
- [Temporary-state and mocked-EC harness](tests/protocol_fixtures.py)

Tests use temporary snapshots/state/sockets and mocked EC writes/systemd
observations. No live service was stopped, no actual fan write or reboot was
performed, and nothing was deployed by that offline task. The later deployment
does not turn these offline results into hardware rotation, acoustic or live
reboot validation.
