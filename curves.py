"""Strict curve-only input. No hardware, service, or privilege operations."""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CURVES_PATH = ROOT / "curves.json"
LIMITS = {"CPU": 90, "GPU": 80}
ROW_KEYS = {"UpThreshold", "DownThreshold", "FanSpeed"}
MAX_ROWS = 16


def _number(value, low, high, label):
    # Check bounds before isfinite so even enormous JSON integers fail cleanly.
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value)):
        raise ValueError(f"{label}: expected finite number in {low}..{high}, not bool")
    return value


def validate_curves(data):
    """Return a fresh validated copy; never accept arbitrary NBFC/Lua fields.

    Pinned NBFC moves FROM row i when temperature >= row i's UpThreshold,
    and moves back FROM row i when temperature <= row i's DownThreshold.
    Thus Down[i] must be strictly below Up[i-1] to avoid oscillation.
    """
    if type(data) is not dict or set(data) != set(LIMITS):
        raise ValueError("curves must contain exactly CPU and GPU")
    result = {}
    for name, limit in LIMITS.items():
        rows = data[name]
        if type(rows) is not list or not 2 <= len(rows) <= MAX_ROWS:
            raise ValueError(f"{name}: expected 2..{MAX_ROWS} threshold rows")
        clean = []
        for i, row in enumerate(rows):
            label = f"{name}[{i + 1}]"
            if type(row) is not dict or set(row) != ROW_KEYS:
                raise ValueError(f"{label}: exact keys required: {sorted(ROW_KEYS)}")
            up = _number(row["UpThreshold"], 0, limit, label + " UpThreshold")
            down = _number(row["DownThreshold"], 0, limit, label + " DownThreshold")
            speed = _number(row["FanSpeed"], 40, 100, label + " FanSpeed")
            if down >= up:
                raise ValueError(f"{label}: DownThreshold must be below UpThreshold")
            if i == 0:
                if down != 0:
                    raise ValueError(f"{label}: first DownThreshold must be zero")
            else:
                previous = clean[-1]
                if up <= previous["UpThreshold"] or down <= previous["DownThreshold"]:
                    raise ValueError(f"{label}: both temperature columns must strictly increase")
                if down >= previous["UpThreshold"]:
                    raise ValueError(f"{label}: DownThreshold must be below previous UpThreshold")
                if speed < previous["FanSpeed"]:
                    raise ValueError(f"{label}: speeds must be monotonic nondecreasing")
            clean.append({"UpThreshold": up, "DownThreshold": down, "FanSpeed": speed})
        if clean[-1]["FanSpeed"] != 100:
            raise ValueError(f"{name}: last speed must be 100%")
        result[name] = clean
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def load_curves(path=CURVES_PATH):
    with Path(path).open("r", encoding="utf-8") as stream:
        text = stream.read(65537)
    if len(text) > 65536:
        raise ValueError("Curve input exceeds 64 KiB text limit")
    return validate_curves(json.loads(text, object_pairs_hook=_unique_object,
                                     parse_constant=_invalid_constant))
