# Installed EC control: verification on 2026-10-16

## Current deployment

The EC sampler, CPU/Mix selector, guarded adapter, patched CoolerControl daemon
and custom-device plugin are installed and running. Distro daemon binaries have
not been overwritten. Global `ec_sys.write_support` remains `Y`.

## Rollback on the installed machine

The installer retains private, backup-specific recovery bundles under
`/var/backups/colorful-n14-control/` and prints the exact restore command before
stopping services. These local backups and their machine-specific paths are not
part of the public repository. Record the command from your own installation;
do not use another machine's backup identifier or guess the newest directory.

After selecting and verifying the intended backup, restore it with its own
recovery script (replace the example path with that recorded backup):

```sh
BACKUP=/var/backups/colorful-n14-control/YOUR-RECORDED-BACKUP
sudo bash "$BACKUP/recovery/install.sh" --restore "$BACKUP"
```

The development machine retains both a preceding EC-version backup and a separate
original pre-EC backup. Use the recovery code belonging to the selected backup,
not a different version of the checkout. Restoring a pre-EC backup returns the
previous NVML sampler and its settings; do not combine manifests from different
backups. No rollback was performed as part of publication.

## Expected sleeping-temperature result

`temp-gpu`, `temp-gpu/json` and `temp-gpu-json` return the non-numeric response
`ERR GPU_ASLEEP: D3cold; temperature unavailable` only after validation of a
fresh suspended/D3cold snapshot. The client exits unsuccessfully so the plugin
does not publish a fake GPU temperature, but the adapter does not log this
expected read result as a failure. CPU readings remain independent.

Malformed/stale metadata, active GPU temperature zero, transport failures and
fan-command validation errors remain errors. A GPU-asleep exception cannot
bypass protection on a control request. Request-error logs now include only
allowlisted action names and sanitized reasons, not payloads or credentials.

## Observed live behavior

Seven snapshots at ten-second intervals, spanning 60 seconds:

- PCI power state was `D3cold` and runtime state was `suspended` throughout.
- Runtime active time stayed at 99,629 ms.
- Runtime suspended time increased from 4,083,611 to 4,143,624 ms.
- CPU temperature from EC was 59–64 degrees C.
- GPU temperature was absent, not zero and not replaced by CPU temperature.
- CPU-only profile generation remained 18 for the entire observation.
- Fan command duty ranged from 0 to 22 percent, according to the existing CPU
  profile. The adapter's EC command readback matched the recorded command.
  Command readback is not an independent measurement of both fan RPMs.
- Sensor, adapter, selector, CoolerControl and plugin services were active.
- No new adapter-rejection, watchdog or profile-validation errors were logged
  during the observation.

No NVIDIA telemetry, direct ACPI calls, register writes or reset requests were
used by the final verification collector. It queried the existing adapter socket
for status/temperature/readback and read PCI power-state sysfs files. D3cold and
runtime suspension are power-state observations, **not a direct measurement of
0 W GPU consumption**. Publication did not repeat this live check.

## Tests and lifecycle limitations

282 offline tests passed during the deployment work. Headless installer
preflight runs the non-GUI tests and skips the GTK widget class when no display
is available. All safety and control-protocol tests remain enabled.

The installed quiet lifecycle implementation preserves validated existing targets
through bounded handover (at most 10 seconds) and through brief sensor loss
(up to 5 seconds total age of the last valid required readings). It does not
freeze low fan settings indefinitely. Overtemperature, failed hardware writes,
invalid control commands, expired holds and prolonged sensor loss retain full-duty
protection. See [QUIET-LIFECYCLE.md](QUIET-LIFECYCLE.md).

Reboot/shutdown detection and sensor-gap behavior have offline test coverage.
An actual end-to-end reboot, firmware fan behavior before Linux starts, and
sustained gaming temperature calibration have not been validated by this final
check. A quiet reboot cannot yet be claimed as a hardware-tested result.

## Publication-only verification (2026-10-16)

The publication review did not reinstall or operate cooling services, access EC
registers, query NVIDIA telemetry, change GPU settings, or reboot the machine.
Its fresh checks were:

- **282 offline tests passed with no skips** in the working tree, then **282 passed
  with no skips** in an isolated copy containing only intended publication files.
- Python AST validation for 35 files, JSON parsing for 6 files, shell syntax for
  4 scripts, shell-example syntax, and publication-document link checks passed.
- The C11 client compiled with warnings as errors, fortified source and stack
  protection. Offline systemd verification passed for all four checked-in units.
- Both patch checks passed forward/reverse application and byte-for-byte source
  comparison: 18 daemon files and 5 plugin files. The plugin was checked against
  its pinned upstream commit. The original temporary daemon checkout was no longer
  available, so its check used the preserved final verification snapshot and the
  patch's original/patched Git blob hashes, not a fresh upstream checkout.
- Both patch and local release-binary hashes matched the existing checksum manifest.
  Existing successful producer build/test receipts were inspected, **not regenerated
  or claimed as freshly rerun Rust builds/tests**.
- A source-only checkout correctly refused release verification without the locally
  built executables. No build receipts were fabricated to bypass this requirement.

Source, tests, documentation, configuration and textual patches are published.
Credentials, private runtime snapshots, local backup bundles, test/build logs,
executables and upstream source/build trees remain excluded. These offline checks
are not additional live idle, load, reboot, acoustic or wattage measurements.
