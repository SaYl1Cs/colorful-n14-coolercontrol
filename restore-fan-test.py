#!/usr/bin/env python3
"""Restore the commands saved by hold-fan-zero.py."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("test-fan-stop-restart.py")
STATE = Path("/run/colorful-n14-fan-stop-test.json")
spec = importlib.util.spec_from_file_location("n14_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise SystemExit("Cannot load supervised test module")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

mod.ensure_platform()
try:
    state = json.loads(STATE.read_text(encoding="ascii"))
    values = {int(k): int(v) for k, v in state.items()}
    if set(values) != {mod.MODE, mod.CPU, mod.GPU} or any(not 0 <= v <= mod.MAX_RAW for v in values.values()):
        raise ValueError("invalid saved state")
except (OSError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Cannot restore saved command: {exc}")

try:
    mod.write(mod.MODE, 2)
    mod.write(mod.CPU, values[mod.CPU])
    mod.write(mod.GPU, values[mod.GPU])
    print(f"Restored mode={mod.acpi(mod.MODE)}, CPU command={mod.acpi(mod.CPU)}, GPU command={mod.acpi(mod.GPU)}")
    STATE.unlink()
except BaseException:
    try:
        mod.both(mod.MAX_RAW)
        print("Restore failed; both fans set to 100%.")
    except BaseException as failsafe:
        print(f"RESTORE AND FAILSAFE FAILED: {failsafe}")
    raise
