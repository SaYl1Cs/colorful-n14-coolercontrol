# COLORFUL N14 24 — EC cooling with CoolerControl

**Current error policy: no automatic emergency fan override.** Sensor/control
errors revoke command authority without setting any replacement speed. A stopped
fan can remain stopped during a prolonged failure. This is not a thermal-safety
guarantee. See [exact behavior and risks](NO-EMERGENCY-OVERRIDE.md).

Experimental, model-specific fan control that reads CPU/GPU temperatures from the
embedded controller (EC), without periodically waking the NVIDIA GPU through NVML.

**Installed and verified on the development laptop on 2026-10-16.** With the
cooling services running, the RTX remained in **D3cold for a continuous 60-second
check**, while CPU-only cooling stayed active. This is not a guarantee for other
hardware, sustained workloads, or firmware-controlled fan behavior during boot.
See [deployment verification and rollback](DEPLOYMENT-VERIFICATION.md).

## Supported hardware

The firmware interface is pinned to:

- COLORFUL N14 24;
- BIOS 2.07 and EC firmware 2.0;
- controller identity 55:71 and the validated DSDT hash;
- CPU CPUT at EC offset **0x56**, GPU GPUT at **0x58**;
- guarded fan-control ACPI interface `\_SB.AMW0.WMBA`, index transport 0x302.

**Do not bypass identity checks or use these addresses on another laptop.** The
standard EC temperature offsets are not interchangeable with the indexed
fan-control register addresses.

## Cooling behavior

| Validated condition | Selected existing CoolerControl profile |
| --- | --- |
| PCI D3cold **and** runtime suspended | CPU graph only |
| PCI D0 **and** runtime active, valid EC readings | Maximum of CPU and GPU graph outputs |
| GPU asleep | No GPU temperature measurement is published |
| Brief sensor loss | Bounded hold of the last validated fan setting |
| Valid high temperature | Continue the existing curve, including its configured 100% output |
| Persistent sensor loss or control failure | Reject/revoke invalid control; no replacement fan command |

CPU is never substituted as GPU temperature. A sleeping GPU is not reported as
0 degrees C. Its temperature endpoint returns `GPU_ASLEEP`; the plugin omits the
reading, and the adapter does not log this expected read result as an error.
Invalid readings while active, stale metadata and failed control writes remain
errors.

The selector changes only the assigned CPU/Mix profile. Existing curves and
Standard functions remain editable through CoolerControl. The producer and
adapter validate command origins, sample freshness and power-mode generations
so delayed CPU-only commands cannot be accepted as current GPU-load commands.

The logical `both_fans` channel controls both physical fans:

- UI 0% maps to raw 1; raw 0 is never written.
- UI 100% maps to raw 70.
- GPU fan RPM is unknown. Command readback is not measured fan rotation.

### Quiet lifecycle and safety limits

Normal handover can preserve validated individual fan targets for **up to 10
seconds**, without restarting that deadline on retries. A thermal-only sensor gap
can retain authority only while the required readings have a **total age
under 5 seconds**. After expiry the assignment is closed without changing speed;
zero/low fan settings may therefore remain indefinitely until valid control resumes.

Confirmed orderly system shutdown is distinguished from a manual stop/crash.
Neither now forces full fan duty. Overtemperature, failed writes, invalid control
data and expired holds revoke authority without an emergency speed override.
Actual reboot acoustics and firmware behavior before Linux starts have **not**
been validated. Details: [current policy](NO-EMERGENCY-OVERRIDE.md).

## Components

- [sensor_cache.py](sensor_cache.py): persistent read-only EC sampler and atomic snapshots.
- [thermal_state.py](thermal_state.py): suspend-aware freshness and temperature validation.
- [adapter_server.py](adapter_server.py): serial root-only control socket and watchdog.
- [src/adapter_client.c](src/adapter_client.c): small socket client; no Python startup per plugin poll.
- [coolercontrol_adapter.py](coolercontrol_adapter.py): guarded fan, keyboard-light and lid firmware access.
- [profile_selector_service.py](profile_selector_service.py): authenticated CPU/Mix selection.
- [provenance_fence.py](provenance_fence.py): rejection of stale/mismatched control commands.
- [colorful-n14-acpi.system-sleep](colorful-n14-acpi.system-sleep): pre/post-sleep coordination through the adapter socket.
- [patches](patches): matching CoolerControl 5.0.1/custom-device 0.2.0 producer patches.
- [install.sh](install.sh): preflight, backup, timed recovery, installation and rollback.

### Keyboard backlight

The indexed EC protocol uses register 0x5B on the pinned N14 24 / BIOS 2.07 /
55:71 controller. Brightness 0..100 maps to raw values 175..0. Physical tests on
2026-10-18 confirmed that manual fan mode can prevent brightness application
although register readback changes. Temporarily selecting automatic fan mode
and waiting **20 ms before the lighting write**, with no intentional post-write
wait, made the physical transitions work. This is a tested setting, not a proven
minimum or a timing guarantee on other firmware.

The transaction runs inside the existing serial root-only adapter. It restores
both individual fan targets only while fresh thermal/provenance checks still
authorize them; otherwise it fences old commands without an emergency write and
leaves the already selected automatic firmware mode alone. Curves, leases,
sample clocks and input validation are not relaxed.
See [sleep coordination and validation](SLEEP-COORDINATION.md).

The installed client supports:

```sh
sudo /usr/local/libexec/colorful-n14-coolercontrol keylight-get
sudo /usr/local/libexec/colorful-n14-coolercontrol keylight-set 50
```

The system-sleep hook saves the exact pre-sleep brightness byte, including off,
and requests a one-shot restore after resume. It first refreshes the lid cache
from the controller using the validated firmware handler, waits one second for
post-S3 firmware/EC settling, restores lighting, waits one more second, and
performs one bounded reapply. A failed first restore remains pending for this
retry; a successful restore is not replayed by duplicate sleep notifications.
It then re-polls a delayed lid update within a bounded five-second budget. It
never forces a physically closed lid open. Errors are logged; all firmware
requests use the same adapter socket, with no direct ACPI/EC access in the hook
or dependency on a user-session D-Bus service.

No sleep-mode default is changed. Register readback is not a measurement of
physical light output: the integrated deep-resume behavior must be checked on
the actual laptop, separately from the successful isolated brightness tests.

The temperature sampler uses no NVIDIA API, GPU enumeration API, raw EC scan or
EC write. Global `ec_sys.write_support` is **left unchanged**. Existing fan
commands still use the guarded ACPI writer. Do not run another writer against the
same fan-control registers; this is not a universal broker for unrelated EC tools.

## Installation scope

This repository publishes **source and patches, not prebuilt release binaries**.
The current installer is a transactional **upgrade of the existing running N14
integration**, not a one-command bootstrap for an arbitrary laptop.

Get the source without running any hardware-control commands:

```sh
git clone https://github.com/SaYl1Cs/colorful-n14-coolercontrol.git
cd colorful-n14-coolercontrol
```

Prerequisites include:

1. Exact matching hardware/firmware and the prior running N14 preparation,
   sensor, adapter and CoolerControl services; NBFC must be stopped.
2. Readable EC debugfs interface, mounted debugfs, and suitable `acpi_call` and
   `ec_sys` modules. The installer does not disable global EC writes.
3. Matched patched daemon/plugin builds, build/test receipts and checksums.
   See [BUILDING.md](BUILDING.md). A fresh clone intentionally refuses installation
   without these artifacts. Do not bypass that verification.
4. Root-only local CoolerControl API token and existing CPU/GPU/Max profile
   bindings. [authorize_coolercontrol.py](authorize_coolercontrol.py) enrolls a token
   through local authorization; passwords/tokens must never be committed.
   The binding-discovery defaults identify the development installation's profiles;
   they are not universal IDs or credentials. Different profile IDs fail closed,
   and the upgrade installer does not create replacement graphs/functions.
5. Python 3.11+, C compiler, Lua 5.4, systemd and the dependencies checked by preflight.
   To include the optional widget tests, provide PyGObject/GTK 4 and a display;
   otherwise that class is explicitly skipped, not reported as passed.
   Initial local token enrollment needs Zenity and a working Polkit agent.

Run from the project directory after reviewing the requirements:

```sh
sudo bash install.sh --preflight
sudo bash install.sh
```

Preflight does not stop cooling. Installation saves an explicit manifest backup,
coordinates service shutdown/startup, installs patched binaries alongside the
distribution binaries, and rolls back on failed post-start health checks. It
prints a backup-specific restore command. Credentials and unrelated EC/LACT/
NVIDIA settings are not replaced.

Do not copy individual new modules over live services: the sensor, consumer,
selector and producer protocols must match. Historical migration notes in
[EC-MIGRATION.md](EC-MIGRATION.md) are not current installation instructions.

## Diagnostics

```sh
systemctl --no-pager status colorful-n14-sensors.service
systemctl --no-pager status colorful-n14-adapter.service
systemctl --no-pager status colorful-n14-profile-selector.service
systemctl --no-pager status coolercontrold.service
journalctl -b -u colorful-n14-sensors -u colorful-n14-adapter -u colorful-n14-profile-selector
cat /sys/bus/pci/devices/0000:01:00.0/power_state
cat /sys/bus/pci/devices/0000:01:00.0/power/runtime_status
```

Use PCI power-state/sysfs observations rather than GPU telemetry when checking
idle power. Monitoring applications may themselves wake the GPU. D3cold is not
a direct measurement of 0 W GPU consumption.

Root-private snapshots live under `/run/colorful-n14-control`. BOOTTIME-based
age checks include time spent in system suspend. Fan commands, sampled
performance and thermal response are not hard real-time guarantees.

## Verification and limitations

- **282 Python tests passed** during the deployment work. Tests mock firmware,
  API and EC writes; they are not physical cooling validation.
- Producer build verification recorded **1,984 daemon tests passed**, 2 ignored
  hardware/integration tests, and **5 plugin tests passed**.
- Live idle check: D3cold persisted, CPU-only assignment stayed unchanged, EC CPU
  readings were distinct from absent GPU readings, and no new control errors were
  logged. See [DEPLOYMENT-VERIFICATION.md](DEPLOYMENT-VERIFICATION.md).
- EC GPU readings differed by about 2–3 degrees C from NVML in preliminary
  comparisons. Accuracy, lag and thresholds under sustained load remain to be
  validated. Existing user curves have not been recalibrated.
- No supported guarantee is made for another BIOS, GPU, laptop, fan firmware or
  CoolerControl version. Retain a working fallback installation.

Run offline tests from the checkout:

```sh
PYTHONPATH=.:tests python3 -B -m unittest discover -s tests -v
cc -std=c11 -O2 -Wall -Wextra -Werror src/adapter_client.c -o /tmp/colorful-n14-adapter-client
```

GTK widget tests require a desktop display and are explicitly skipped in the
headless root installer. All safety/protocol tests still run there.

## Legacy NBFC and provenance

NBFC profile generation and the offline curve editor are retained; the EC
CoolerControl installer does not start NBFC. Do not operate NBFC and the adapter
concurrently. Legacy NBFC shutdown behavior is separate from the new adapter's
quiet lifecycle policy.

Original source attribution and derivative details are recorded in
[provenance/README.original.md](provenance/README.original.md),
[provenance/manifest.json](provenance/manifest.json), and
[provenance/derivative.patch](provenance/derivative.patch). Upstream producer
patch headers retain their license and copyright notices.
