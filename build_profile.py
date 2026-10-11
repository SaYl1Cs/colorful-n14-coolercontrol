"""Build ONLY the pinned N14 derivative from validated curves; entirely offline."""
import json
import os
from pathlib import Path
import tempfile

from curves import CURVES_PATH, load_curves, validate_curves

ROOT = Path(__file__).resolve().parent
PROFILE_ID = "Colorful N14 24 - Temperature Curves - GPU Command Readback"
PROFILE_PATH = ROOT / "configs" / (PROFILE_ID + ".json")
UPSTREAM = "59b4b4ffacf125cc79c8c7740ff2f2f8ff1343f7"


def build(curves=None):
    curves = load_curves() if curves is None else validate_curves(curves)
    common = (ROOT / "src" / "transport.lua").read_text(encoding="utf-8")

    def lua(expression, bootstrap=False, cleanup=False):
        if bootstrap:
            prefix = common
        elif cleanup:
            prefix = "if not N14 then return nil, 0 end"
        else:
            prefix = 'if not N14 then return "N14: read initialization missing", 0 end'
        return (prefix + "\nreturn " + expression + "\n").splitlines()

    def fan(name, sensor, reader, write_index, critical, rows):
        return {
            "FanDisplayName": name,
            "ReadLuaCode": lua(f"N14.{reader}()", bootstrap=True),
            "WriteLuaCode": lua(f"N14.write({write_index}, get_value())"),
            "ResetRequired": True,
            "ResetLuaCode": lua("N14.reset()", cleanup=True),
            "MinSpeedValue": 0,
            "MaxSpeedValue": 70,
            "ReadWords": False,
            "WriteWords": False,
            "CriticalTemperature": critical,
            "CriticalTemperatureOffset": 5,
            "TemperatureAlgorithmType": "Max",
            "Sensors": [sensor],
            "TemperatureThresholds": rows,
        }

    return {
        "NotebookModel": "N14 24",
        "Author": "Experimental community profile; hardware validation pending",
        "Comment": (
            "ONLY COLORFUL N14 24 BIOS 2.07, SMBIOS EC 2.0, controller 55:71; indexed I/O 0x302. "
            "Requires prepare.py permission and exact DSDT/WMI/DMI verification. "
            "GPU percentage is actual command register 0x55 / 70, NOT RPM or verified rotation. "
            "Temperature control WITHOUT verified GPU rotation feedback. GPU RPM UNKNOWN. "
            "Derivative not hardware-tested. Cleanup requests BOTH fans full, NOT Normal. "
            "First stage: minimum 40%; 0% unverified and disabled; no calibration. "
            f"ONLY nbfc-linux 0.5.3 commit {UPSTREAM}."
        ),
        "EcPollInterval": 1000,
        "CriticalTemperature": 85,
        "CriticalTemperatureOffset": 5,
        "ReadWriteWords": False,
        "LegacyTemperatureThresholdsBehaviour": False,
        "LuaLibraries": ["base", "math", "string", "io"],
        "FanConfigurations": [
            fan("CPU - nominal RPM/7000", "@CPU", "read_cpu_tach", 0x54, 90, curves["CPU"]),
            fan("GPU - command readback, RPM UNKNOWN", "@GPU", "read_gpu_command", 0x55, 80, curves["GPU"]),
        ],
        "RegisterWriteConfigurations": [],
    }


def profile_text(curves=None):
    text = json.dumps(build(curves), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) >= 32768:
        raise ValueError("Profile exceeds pinned nbfc-linux file size limit")
    return text


def atomic_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_curves_and_build(data):
    clean = validate_curves(data)
    text = profile_text(clean)  # All validation, including size, before any writes.
    atomic_write(CURVES_PATH, json.dumps(clean, indent=2, allow_nan=False) + "\n")
    atomic_write(PROFILE_PATH, text)
    return PROFILE_PATH


def export_profile(data, destination):
    # Export generated data only; never import arbitrary profile/Lua fields.
    destination = Path(destination)
    if destination.suffix.lower() != ".json":
        raise ValueError("Export must be a .json file")
    # Do not let an export overwrite curve input or any project source/provenance.
    if destination.resolve().is_relative_to(ROOT.resolve()) and destination.resolve() != PROFILE_PATH.resolve():
        raise ValueError("Export outside the project, or use the generated profile path")
    atomic_write(destination, profile_text(data))


def main():
    atomic_write(PROFILE_PATH, profile_text())
    print(PROFILE_PATH)


if __name__ == "__main__":
    main()
