# EC migration — historical engineering notes

> **Current status: installed and verified on the development laptop.**
> See [README.md](README.md) and [DEPLOYMENT-VERIFICATION.md](DEPLOYMENT-VERIFICATION.md).
> The blocked/staged status entries below describe earlier work, not the current
> installation. Global EC write support remains enabled; no broker migration
> or boot-parameter change is required by the installed read-only sampler.

> **Current Python/C integration:** see [IPC-INTEGRATION.md](IPC-INTEGRATION.md)
> for the implemented selector service, mutable live policy, strict provenance
> fence, C/socket wiring, offline tests and finite timing limits. The entries
> below are historical observations and superseded blockers. The producer builds
> are recorded in [PRODUCER-CONTRACT.md](patches/PRODUCER-CONTRACT.md). That earlier
> integration-only task did not deploy; the later transactional installer did.
> Globally disabling EC writes is not a requirement of the read-only sampler.

## Engineering verification (2026-10-16): deployment blocked, protocol staged

Added [staged selector](profile_selector.py) and
[20 offline tests](tests/test_profile_selector.py). It validates explicitly bound
existing CPU/GPU graphs, their exact Standard functions, temperature sources and
Maximum Mix membership. It selects CPU for suspended/D3cold and Mix for active/D0
using the supported bearer-authenticated PUT endpoint, without editing curves or
functions. It requires an externally confirmed persistent close-and-full-fan
fence before selection and again before PUT. **There is no unlock operation.**
No production fence, CLI or service integration is provided. This is not a
completed control backend and must not be manually deployed.

### Producer protocol patch status

Installed daemon 5.0.1-1 and custom-device plugin 0.2.0 were inspected read-only.
The running plugin executable matched its on-disk SHA-256:
816af8a57d9aa0d02cfc484f4897a17d8b4360318e049cb079cd5f919ccc56ab.
Source tag 5.0.1 was inspected at commit
 d9201247d6f61a5ddf8e2af8faa3aba10641e340 (not a reproducible-build attestation).
Relevant daemon source paths under coolercontrold/daemon:

- resources/proto/coolercontrol/device_service/v1/fixed_duty.proto:8–14:
  only device ID, channel and duty; no profile, generation or sample IDs.
- src/repositories/service_plugin/client_proxy.rs:28–70,292–301,351–357:
  queued requests are dispatched into tasks; cancellation of the reply receiver
  does not establish cancellation of the hardware command.
- src/engine/main.rs:325–345,458–481: profile scheduling replacement does not
  establish a generation fence for previously calculated/queued duties.
- src/api/actor/device.rs:281–300: HTTP success confirms setting application,
  not first fresh command from the new profile.
- src/engine/commanders/graph.rs:218–225 and mix.rs:329–337: both produce the
  same unlabelled downstream duty operation.

The installed plugin command substitutes only duty into the adapter invocation.
A generation added by a plugin/wrapper on receipt would relabel old queued work
as new. Waiting, discarding the first command, checking the current API profile,
PID checks and fresh local sensor data do not recover calculation provenance.
Public plugin 0.2.0 source additionally does not reliably propagate unsuccessful
shell execution as an unsuccessful duty RPC. Merely changing the plugin is
therefore insufficient: an end-to-end producer-side protocol change and its
verification are required before permitting lower fan commands. Full-fan-only
operation is not presented as completion of the requested profile control.

### Actual read-only live verification

All four existing prepare/sensor/adapter/CoolerControl units remained active;
NBFC was inactive. No service was stopped and no live profile was reassigned.
At observation time GPU 0000:01:00.0 was active/D0 (its audio function was
suspended/D3hot, which is NOT GPU D3cold). Read-only single-byte EC reads returned
CPU 63°C and GPU 54°C. The installed legacy snapshot reported CPU 64°C, GPU 56°C,
active, no errors, and still contained the legacy substituted-input field.
These asynchronous readings are not calibration or load verification.
Existing Maximum Mix UID 5732965b-667c-4f05-84f4-d0ead7cc0c43 remained assigned to
the both-fans channel; CPU UID 240b496f-027a-4e71-a978-47703a14c9ee and GPU UID
f8997ee4-df30-4f2b-8b44-7eb08e5d0398 and both Standard functions were unchanged.

**Additional prerequisite conflict:** live ec_sys write_support was Y, configured
by /etc/modprobe.d/isw-ec_sys.conf (write_support=1), with module loading in
/etc/modules-load.d/isw-ec_sys.conf. No EC writes were performed. Do not silently
override/remove unrelated ISW configuration or unload EC support under live
cooling. Resolve ownership and read-only module prerequisites before installation.
The exact legacy GPU polling drop-in is
/etc/systemd/system/colorful-n14-sensors.service.d/60-active-gpu-temperature.conf;
it was inspected, not removed.

Full offline suite: **144 tests passed**, including 20 selector tests. From the
Desktop workspace use `PYTHONPATH=nbfc-n14-control python3 -B -m unittest discover
-s nbfc-n14-control/tests -p 'test_*.py' -v`. An initial invocation without the
module search path failed to import the pre-existing authorization test; the
corrected invocation passed. No live API/EC was used by tests.

Installer guard remains intact. Transactional installer/rollback and persistent
prerequisites are **not completed**. No deployment backup was created because
no live replacement was attempted; no recovery claim is made for the old
installer behind the guard. NVIDIA/LACT, credentials and authentication settings
were untouched. Idle/D3cold and game-load validation remain unperformed.

## Local authorization completed (2026-10-16)

- Added `authorize_coolercontrol.py`: desktop password dialog, supported
  `POST /login`, session-authenticated `POST /tokens`, then `POST /logout`.
  The password is not persisted or printed. Python cannot guarantee secret
  zeroization in process memory; core dumps are disabled by the helper.
- The user authorized and completed local enrollment. A dedicated write-enabled
  token labelled **Colorful N14 EC profile selector** is stored at
  `/etc/colorful-n14-control/coolercontrol-token.json`, owned by root, mode 0600,
  inside a root-only directory. Do not print this file or include it in reports.
- Verified a bearer-authenticated read of `/profiles`: HTTP 200. No profile
  assignments were changed. Password settings, existing tokens and authentication
  enforcement were not changed. There is no supported root-only login/token CLI
  in the inspected installed 5.0.1 interface.
- The helper refuses proxies, redirects and credential-file replacement. On
  storage failure it attempts to revoke only the newly created token. Polkit
  and the password dialog have no imposed interaction timeout.
- Eight isolated authorization tests pass; the complete offline suite now
  passes **124 tests**. These tests do not establish transition safety or prove
  a completed EC deployment.
- Enrollment rollback: revoke **Colorful N14 EC profile selector** in the
  authenticated CoolerControl token UI, then remove its dedicated local token
  file with root authorization. No cooling-service backup was created in this
  step because no cooling configuration or installed backend was replaced.

**Remaining:** authenticated profile-selection integration, a proven command
transition interlock, persistent read-only EC prerequisites, transactional
installation/rollback and live verification. The installer guard remains in
place; possession of a valid token does not make the partial backend deployable.

## What is implemented in the working tree

- [EC sampler](sensor_cache.py): CPU CPUT 0x56 and GPU GPUT 0x58 only,
  single-byte read-only reads of EC debugfs ec0/io. No scans or writes.
- Platform validation before sampling: existing DMI, BIOS, SMBIOS EC version,
  exact DSDT, WMI and generated-profile checks remain in effect.
- GPU sleep requires runtime suspended **and** PCI D3cold. Active requires
  runtime active **and** PCI D0. Repeated observations bracket the GPU read.
- Sleeping GPU has no measurement; invalid active readings are errors. No CPU
  substitution, old-temperature reuse or fabricated zero measurement.
- [Snapshot validation](thermal_state.py) rejects legacy schema, stale data,
  unknown/transitional/error states and contradictory PM metadata.
- [Adapter](coolercontrol_adapter.py) refuses to output a sleeping GPU number.
  Its full-fan override and the [watchdog](adapter_server.py) use separate CPU
  90°C and active-GPU 80°C thresholds. Existing ACPI safety checks and
  best-effort full-fan handling remain unchanged.
- Missing EC debugfs produces invalid snapshots and retries; it never silently
  switches to another sensor. A blocking read cannot refresh the old timestamp.

This is **not** a complete control implementation. The pure curve-request helper
in [thermal_state.py](thermal_state.py) does not assign CoolerControl profiles.

## Staged provenance consumer

[provenance_fence.py](provenance_fence.py) starts closed, atomically records only the authenticated daemon assignment envelope, invalidates on adapter-server restart, and rejects producer labels whose session, generation, profile, calculation age, or EC sample identity/freshness does not match. [coolercontrol_adapter.py](coolercontrol_adapter.py) accepts labelled writes only through `set-duty-provenance`; ordinary `set-duty` remains unlabelled. [profile_selector.py](profile_selector.py) can pass the authenticated PUT response to an assignment sink. No unlock operation exists.

This remains staged: Standard-function retained state is not fully traced, daemon status history is not firmware sample provenance, and no atomic GPU power-state lease exists. The [installer](install.sh) and live services remain blocked until real sensor JSON sample production, complete selector/sink wiring, and recovery tests are reviewed.
Do not manually deploy individual changed modules under the existing Mix setup.

## Verified CoolerControl mechanism and blockers

Inspected upstream tag 5.0.1, commit
`d9201247d6f61a5ddf8e2af8faa3aba10641e340`, matching the installed package version.
The upstream [OpenAPI specification](https://gitlab.com/coolercontrol/coolercontrol/-/blob/5.0.1/openapi/openapi.json)
defines PUT on `/devices/{device_uid}/settings/{channel_name}/profile`, with a
JSON object containing the required string property `profile_uid`. It accepts
session-cookie or bearer authentication and returns HTTP 200 on success.
The local API returned HTTP 401 to an unauthenticated request. No credential
was supplied, no password was guessed, and authentication was not weakened.

The current installed configuration was inspected read-only using elevated
access. It has CPU and GPU Graph profiles, each with a nontrivial Standard
function, and a Maximum Mix profile assigned to the logical both-fans channel.
The existing graph points and function definitions have not been modified.

The upstream [Mix implementation](https://gitlab.com/coolercontrol/coolercontrol/-/blob/5.0.1/coolercontrold/daemon/src/engine/commanders/mix.rs)
retains previous member duties when a member has no new output. Therefore:

- returning no GPU measurement does not prove CPU-only control;
- sending CPU temperature through the GPU graph is not CPU-only control;
- simply implementing linear interpolation would not preserve the user's
  Standard functions, response delays and threshold behavior.

The daemon source checkout at `/tmp/n14-coolercontrol-source/coolercontrold` now contains the staged producer changes from
[`coolercontrold-producer-provenance.patch`](patches/coolercontrold-producer-provenance.patch): immutable fixed-duty provenance, producer session/generation/calculation labels, sample sidecars, queue-preserved labels, and plugin-RPC failure propagation. The checkout is deliberately not installed or committed. The patch file is a reproducible review artifact; `git apply --check` against that already-modified checkout is expected to fail because its hunks are already present. Rust compilation could not be repeated in this environment because `cargo` is not installed.

## Historical deployment gate (superseded)

The source-level producer contract, consumer fence, authenticated selector,
read-only prerequisite checker, and transactional backup/rollback are now
implemented and covered by offline tests. The producer patch remains staged in
`/tmp/n14-coolercontrol-source/coolercontrold` and has not been installed.

Deployment is intentionally blocked by two independently verified conditions:

1. The live system reports `ec_sys.write_support=Y`, configured by the foreign
   ISW configuration. The checker requires a persistent boot argument
   `ec_sys.write_support=0`; it does not delete that configuration or unload
   `ec_sys` under live cooling.
2. `cargo` is unavailable, so the patched daemon/plugin cannot be compiled and
   recovery-tested. The installer guard therefore remains active.

The suite passes **169 tests**. It includes fake API/EC provenance tests for
session and generation mismatch, stale samples, restart transitions, API
failures, queued-command provenance, sleep/wake races, prerequisites, and
transaction rollback. `bash -n`, Python AST/bytecode validation, and
`git diff --check` pass.

No services were stopped, restarted, or replaced; no backup was created because
no deployment transaction began. The four existing cooling services remained
active during the final check. The installed system still uses the old NVML
sampler and the known `60-active-gpu-temperature.conf` override. No EC writes,
profile reassignment, GPU power-state changes, NVIDIA/LACT changes, credential
changes, commit, or publication were performed.

Prior user-provided observations: read-only EC sampling preserved D3cold for
60 seconds; GPUT changed from 55 to 0 after sleep while CPU changed from 71 to
46. Active EC GPU was about 54–55°C versus NVML 57°C. These are preliminary
observations, **not accuracy calibration or validation under load**. No new load
or prolonged sleep test was performed here.

Errors can originate in missing kernel support, debugfs availability or access,
transport failures, malformed/stale snapshots, PM transitions or software bugs.
An error does not by itself mean hardware has been destroyed. Conversely, a
plausible byte does not prove freshness or accuracy of the firmware's sensor.

No GPU power-off, unbind/remove, driver/LACT changes, git commit or publication
was performed.
