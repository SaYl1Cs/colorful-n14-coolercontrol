"""Validate N14 platform and explicitly prepare an experimental NBFC session.

Default 'check' does not invoke firmware. 'probe' and 'arm' issue diagnostic
indexed reads (index-port writes, but no fan setting writes). This script never
installs software, writes fan settings, or enables a service. ONLY the explicit
--launch option used by the hardware launchers executes the pinned NBFC binary.
Do not run this script during offline editing/testing.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys

from build_profile import PROFILE_ID, build

ROOT = Path(__file__).resolve().parent
DSDT_SHA256 = "4aab3c89fdaac8ab9e20d31cff3fe0a18b83877442520eb294429ff9237bef06"
GUID = "3161c7c3-489f-4074-82ae-538101cce1c2"
METHOD = r"\_SB.AMW0.WMBA"
PERMISSION_DIR = Path("/run/colorful-n14-control")
NBFC_SERVICE = "/usr/local/bin/nbfc_service"
EXPECTED_EC_ID = (0x55, 0x71)
SMBIOS_RAW = Path("/sys/firmware/dmi/entries/0-0/raw")


def smbios_ec_version(data):
    # SMBIOS Type 0 (BIOS Information), EC firmware release at offsets 22/23.
    if len(data) < 24 or data[0] != 0 or not 24 <= data[1] <= len(data):
        raise RuntimeError("Missing/truncated SMBIOS Type 0 EC firmware release")
    version = tuple(data[22:24])
    if version != (2, 0):
        raise RuntimeError(f"SMBIOS EC firmware must be 2.0, got {version}")
    return "2.0"


def matches_wmi_name(name):
    return re.fullmatch(re.escape(GUID) + r"(?:[-:][0-9]+)?", name.lower()) is not None


def ensure_runtime_dir():
    PERMISSION_DIR.mkdir(mode=0o700, exist_ok=True)
    info = PERMISSION_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("Permission directory must be root-owned and not a symlink")
    os.chmod(PERMISSION_DIR, 0o700)


@contextmanager
def session_lock():
    """Serialize new preflights; launcher retains lock across foreground exec.

    All legacy NBFC processes are additionally rejected by the global PID/process
    check. This cannot coordinate arbitrary other ACPI utilities or modified
    legacy launchers: never run those concurrently.
    """
    ensure_runtime_dir()
    fd = os.open(PERMISSION_DIR / "session.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise RuntimeError("Invalid session lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another curves preparation/session is already running") from exc
        yield fd
    finally:
        os.close(fd)


def packet(address, index=None):
    data = bytearray(24)
    data[:4] = b"BYDL"
    struct.pack_into("<I", data, 4, address)
    data[14] = 8
    if index is not None:
        data[12] = 1
        data[15] = index
    return bytes(data)


def parse_result(text):
    text = text.rstrip("\x00\r\n ")
    if not re.fullmatch(r"0x[0-9a-fA-F]+", text):
        raise RuntimeError(f"Unexpected ACPI result: {text!r}")
    value = int(text, 16)
    if not 0 <= value <= 255:
        raise RuntimeError(f"Firmware error or non-byte diagnostic result: {text}")
    return value


def read_byte(data):
    if len(data) != 24 or data[13] != 0:
        raise RuntimeError("Only 24-byte diagnostic read requests are allowed")
    request = f"{METHOD} 0 2 b{data.hex()}".encode("ascii")
    fd = os.open("/proc/acpi/call", os.O_WRONLY | os.O_CLOEXEC)
    try:
        if os.write(fd, request) != len(request):
            raise RuntimeError("Short ACPI request write")
    finally:
        os.close(fd)
    fd = os.open("/proc/acpi/call", os.O_RDONLY | os.O_CLOEXEC)
    try:
        text = os.read(fd, 4096).decode("ascii")
    finally:
        os.close(fd)
    return parse_result(text)


def assert_service_stopped():
    pidfile = Path("/run/nbfc_service.pid")
    if pidfile.exists():
        # Refuse even stale files: resolve service state manually, never kill blindly.
        raise RuntimeError("NBFC PID file exists. Stop NBFC and resolve stale state before preparation.")
    # Also catch foreground service instances with another PID file.
    for p in Path("/proc").iterdir():
        if not p.name.isdecimal():
            continue
        try:
            if (p / "comm").read_text().strip() == "nbfc_service":
                raise RuntimeError("An nbfc_service process is already running")
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError as exc:
            raise RuntimeError("Cannot verify absence of other NBFC processes") from exc


def check_platform():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Run this preparation script as root on Linux, not Windows")
    dmi = Path("/sys/class/dmi/id")
    expected = {"sys_vendor": "COLORFUL", "product_name": "N14 24", "bios_version": "2.07"}
    actual = {key: (dmi / key).read_text().strip() for key in expected}
    if actual != expected:
        raise RuntimeError(f"Unsupported platform: {actual}")
    ec_version = smbios_ec_version(SMBIOS_RAW.read_bytes())
    data = Path("/sys/firmware/acpi/tables/DSDT").read_bytes()
    if hashlib.sha256(data).hexdigest() != DSDT_SHA256:
        raise RuntimeError("DSDT hash mismatch: no override is provided; inspect the new firmware first")
    devices = Path("/sys/bus/wmi/devices")
    if not devices.is_dir() or not any(matches_wmi_name(p.name) for p in devices.iterdir()):
        raise RuntimeError("Expected WMI interface is absent")
    profile = ROOT / "configs" / (PROFILE_ID + ".json")
    if not profile.is_file():
        raise RuntimeError("Generated profile missing; run build_profile.py first")
    if json.loads(profile.read_text(encoding="utf-8")) != build():
        raise RuntimeError("Profile differs from validated local build; rebuild before preparation")
    print(json.dumps({"platform": actual, "dsdt_sha256": DSDT_SHA256,
                      "nbfc_service": NBFC_SERVICE,
                      "acpi_call_loaded": Path("/proc/acpi/call").exists(),
                      "smbios_ec_version": ec_version,
                      "gpu_feedback": "command readback only; RPM UNKNOWN"}, indent=2))
    return profile


def parse_brand(text):
    if not re.fullmatch(r"[0-9a-fA-F]{2}:[0-9a-fA-F]{2}", text):
        raise argparse.ArgumentTypeError("Expected two hex bytes HH:LL")
    return tuple(int(part, 16) for part in text.split(":"))


def write_permission(brand, control, profile):
    if brand != EXPECTED_EC_ID:
        raise RuntimeError("Only observed controller ID 55:71 is supported")
    ensure_runtime_dir()
    access = "control" if control else "read"
    text = f"{DSDT_SHA256} {brand[0]:02x} {brand[1]:02x} {access}\n"
    tmp = PERMISSION_DIR / "permission.new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(text)
    os.replace(tmp, PERMISSION_DIR / "permission")
    config = {
        "SelectedConfigId": str(profile.resolve()),
        "EmbeddedControllerType": "dummy",
        "TargetFanSpeeds": [-1, -1],
        "Comment": "Experimental foreground session. Dummy disables only standard EC; ACPI Lua calls are REAL. TargetFanSpeeds resets inherited fixed speeds and is migrated by NBFC.",
    }
    service_file = PERMISSION_DIR / "service.json"
    fd = os.open(service_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print(f"Permission: {access}; expires on reboot. Service config: {service_file}")
    print("No NBFC service was started. No fan setting was written.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("check", "probe", "arm"), default="check")
    parser.add_argument("--acknowledge-risk", action="store_true")
    parser.add_argument("--sensors-verified", action="store_true", help="Confirm BOTH correct CPU and NVIDIA GPU sources worked in read-only mode")
    parser.add_argument("--expected-ec-id", type=parse_brand, help="Exact controller ID printed by prior probe")
    parser.add_argument("--ec-version", help="User-confirmed EC firmware version; must be 2.0")
    parser.add_argument("--launch", choices=(NBFC_SERVICE,),
                        help="Hardware launcher only: explicitly exec pinned foreground service")
    args = parser.parse_args(argv)
    if args.launch and args.action == "check":
        parser.error("check cannot launch NBFC")
    if args.action != "check" and not args.acknowledge_risk:
        parser.error("Diagnostic indexed reads require --acknowledge-risk; stop other EC/ACPI tools first")
    if args.action == "arm" and (not args.sensors_verified or args.expected_ec_id != EXPECTED_EC_ID or args.ec_version != "2.0"):
        parser.error("arm requires --sensors-verified, --expected-ec-id 55:71 from prior probe, and --ec-version 2.0")
    try:
        profile = check_platform()
        if args.action == "check":
            return 0
        with session_lock() as lock_fd:
            assert_service_stopped()  # Includes old profile and foreground instances.
            if not Path("/proc/acpi/call").exists():
                raise RuntimeError("Load a compatible acpi_call module before diagnostic reads")
            # Invalidate any old permission before starting a new diagnostic session.
            old = PERMISSION_DIR / "permission"
            old.unlink(missing_ok=True)
            brand = (read_byte(packet(0x2E, 0x20)), read_byte(packet(0x2E, 0x21)))
            if brand != EXPECTED_EC_ID:
                raise RuntimeError("Controller ID is not pinned observed 55:71; no permission written")
            if args.action == "arm" and brand != args.expected_ec_id:
                raise RuntimeError("Controller ID differs from prior probe; no control permission written")
            cpu_tach = read_byte(packet(0x302, 0x5C))
            gpu_command = read_byte(packet(0x302, 0x55))
            if not 0 <= cpu_tach <= 70:
                raise RuntimeError("CPU tachometer outside experimental 0..7000 RPM range")
            if not 0 <= gpu_command <= 70:
                raise RuntimeError("GPU command outside 0..70; RPM UNKNOWN")
            print(json.dumps({"ec_id": "%02X:%02X" % brand,
                              "transport": "indexed-io", "cpu_rpm_nominal": cpu_tach * 100,
                              "gpu_command": gpu_command,
                              "gpu_command_percent": gpu_command * 100 / 70,
                              "gpu_rpm": None, "gpu_feedback": "RPM UNKNOWN; rotation not verified"}, indent=2))
            write_permission(brand, args.action == "arm", profile)
            if args.launch:
                assert_service_stopped()  # Recheck immediately before exec.
                command = [NBFC_SERVICE]
                if args.action == "probe":
                    command.append("--read-only")
                command.extend(["--config-file", str(PERMISSION_DIR / "service.json")])
                print("Explicit hardware launch. GPU RPM UNKNOWN. Control cleanup requests BOTH fans FULL.", flush=True)
                os.set_inheritable(lock_fd, True)
                os.execv(NBFC_SERVICE, command)  # No shell, PATH search, sudo, or service manager.
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
