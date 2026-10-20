# N14 sleep, keyboard lighting and cooling coordination

> **Emergency fan-speed overrides are now removed.** The current failure policy
> is documented in [NO-EMERGENCY-OVERRIDE.md](NO-EMERGENCY-OVERRIDE.md).

## Supported platform and confirmed observations

Only the platform already guarded by [coolercontrol_adapter.py](coolercontrol_adapter.py)
is supported: COLORFUL N14 24, BIOS 2.07, EC identity 55:71 and the exact validated
DSDT hash. Do not extrapolate register addresses to another laptop or BIOS.

Physical, user-observed isolation tests on 2026-10-18 established:

| Software state / firmware fan mode | Brightness result |
| --- | --- |
| Cooling consumers stopped, automatic mode | Maximum, off and original level all applied physically |
| Same consumers stopped, manual mode with full targets | Register changed, physical illumination did not |
| Automatic temporarily; 1 s before / 1 s after write; return to manual | All physical transitions worked |
| Automatic temporarily; 1 s before / no intentional wait after | All physical transitions worked |
| Automatic temporarily; 20 ms before / no intentional wait after | All physical transitions worked |

The measured pre-write windows for the 20 ms test were approximately 20.48–20.60
ms, including software/ACPI overhead. This is a physically tested setting, not a
proven lower bound, hard-real-time guarantee, or evidence of stability over an
arbitrary number of suspend cycles. A readback alone must not be called a
physical brightness measurement. Earlier speculation that a native USB/HID
keyboard initialization was necessarily required was not established and is not
used by this implementation.

## One firmware owner

The system [sleep hook](colorful-n14-acpi.system-sleep) sends only three
allowlisted root-only socket requests to [adapter_server.py](adapter_server.py).
It never opens the ACPI-call or EC-debugfs interfaces, invokes another writer,
or changes the selected sleep mode. The adapter handles each request serially,
so fan-control requests, lighting transactions and lid refreshes cannot interleave
within the vendor firmware transaction.

This is not a system-wide broker for arbitrary root ACPI utilities. Do not run
independent raw ACPI/indexed-I/O writers or NBFC concurrently. The read-only EC
temperature sampler continues to use the kernel EC interface, not the shared
ACPI-call response buffer.

## Lighting transaction and cooling return

The indexed-I/O lighting register is 0x302:0x5B. In manual cooling mode the
adapter caches both individual fan targets, selects firmware automatic cooling,
waits 20 ms, confirms the selected mode, writes the lighting byte, and returns
to guarded manual cooling without an intentional post-write wait.

Returning a cached lower fan target requires fresh valid sensor data, unchanged
PCI power state, current provenance/authority and matching last applied targets.
Individual bounded holds retain their original deadlines and must satisfy their
existing thermal/policy checks. A required transition rise, expired hold,
critical temperature, stale sample, uncertain last duty or unconfirmed GPU
power state cannot be hidden by a lighting operation. Unverifiable targets close
the previous command fence without restoring stale targets or issuing a full-duty
command. The already selected firmware automatic mode is left alone. No lease,
producer calculation ID or sensor timestamp is refreshed by this failure path.

A partial firmware/write failure invalidates authority without emergency retries.
The backend remains the owner; a timed-out hook client must not kill it. Firmware
calls themselves are not hard-real-time operations. Saved curves and firmware
protections are not modified, but software emergency cooling is no longer provided.

## Exact one-shot brightness state

Before sleep, the adapter invalidates any older pending record, reads and saves
the exact lighting byte, and arms a one-shot restore in a root-private runtime
file. Both off (raw 175) and maximum (raw 0) are valid. The displayed percentage
is not round-tripped back into a different byte.

After resume, a validated pending record is claimed before hardware work and
applied once. A duplicate post is a no-op. Missing state is a no-op; malformed,
legacy, inconsistent and out-of-range state is rejected. Failed saves do not
leave a previous pending restore armed; failed restore attempts are reported,
not silently replayed during a later cycle. Explicit manual lighting commands
cancel an older pending restore.

## Lid repair

The firmware's cached lid value can lag behind the real controller status after
S3. The adapter reads both values. On mismatch it invokes the pinned firmware
handler, which rereads the controller, updates the cache and notifies Linux.
It then checks convergence. It does not write an unconditional open value or
change the lid-close policy.

The earlier implementation incorrectly escaped the ACPI root prefix and added
literal backslash/newline characters to direct method requests. The corrected
transport has regression tests for the exact request bytes.

The hook first reconciles the lid, waits one second for post-S3 firmware/EC
settling, then requests lighting restoration and performs one reapply after a
second bounded wait. If the lid is not yet reported open, it makes bounded later
re-polls outside the adapter's firmware transaction, allowing the fan watchdog
and RPC loop to run between requests. Total post work is bounded to
approximately five seconds subject to OS scheduling; individual socket requests
use sub-second deadlines.
It never contacts a possibly frozen user-session D-Bus service. One operation's
failure does not skip the other, and failures are logged rather than preventing
system sleep.

## Tests, deployment and remaining live verification

The regression suite covers exact ACPI path bytes, mode/delay/write ordering,
fan-target preservation and fallback, expired authority, raw lighting state,
off-state restoration, duplicate/failing pre/post actions, delayed lid updates,
RPC framing/peer identity and installer asset/backup coverage. These mocked
tests do not prove physical cooling performance or S3 behavior.

Install with the existing transactional [installer](install.sh), not by copying
modules over live services. Preflight does not stop cooling; installation
creates a complete backup and timed recovery, coordinates all cooling services,
and verifies their health. The printed backup-specific rollback restores the
previous hook and backend together. No BIOS setting, default sleep mode, module
write policy, user graph or API credential is changed by this feature.

Live validation must separately confirm:

1. Physical maximum/off/original transitions with the ordinary services running.
2. An actual kernel-confirmed deep/S3 cycle with the lid open, without manually
   pressing the lighting key after resume.
3. Restoration of the previous physical level, keyboard/touchpad operation and
   healthy cooling services.
4. A separate lid-close/open S3 cycle: controller, cached ACPI state and UPower
   must agree, without repeated unsolicited suspend.

Both integrated deep scenarios were physically validated on the installed retry
version: open-lid S3 and a close/open lid cycle restored the keyboard illumination
without pressing its key, and the lid state converged without a repeated suspend
loop. The default sleep mode remains unchanged (s2idle).
