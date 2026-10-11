#!/usr/bin/env python3
"""Set both N14 fan commands to zero and hold until restore-fan-test.py is run."""
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
cpu_temp, gpu_temp = mod.temperatures()
if cpu_temp >= 65 or gpu_temp >= 60:
    raise SystemExit(f"STOP: temperatures too high: CPU {cpu_temp:.0f} C, GPU {gpu_temp:.0f} C")
before = {str(index): mod.acpi(index) for index in (mod.MODE, mod.CPU, mod.GPU)}
print(
    f"Before: mode={before[str(mod.MODE)]}, CPU command={before[str(mod.CPU)]}, "
    f"GPU command={before[str(mod.GPU)]}, CPU={cpu_temp:.0f}C GPU={gpu_temp:.0f}C",
    flush=True,
)
try:
    STATE.write_text(json.dumps(before) + "\n", encoding="ascii")
    mod.both(0)
    print(
        f"ZERO COMMAND SET: mode={mod.acpi(mod.MODE)}, CPU command={mod.acpi(mod.CPU)}, "
        f"GPU command={mod.acpi(mod.GPU)}",
        flush=True,
    )
    print("Waiting 10 seconds before the first tachometer sample...", flush=True)
    import time
    time.sleep(10)
    print(
        f"After 10s: CPU tach raw={mod.acpi(mod.CPU_TACH)}; "
        f"GPU tach raw={mod.acpi(mod.GPU_TACH)} (GPU tach is untrusted)",
        flush=True,
    )
    print("Both fans remain at zero command until restore-fan-test.py is run.", flush=True)
except BaseException:
    try:
        mod.both(mod.MAX_RAW)
        print("FAILSAFE: both fans set to 100%.", flush=True)
    except BaseException as failsafe:
        print(f"FAILSAFE WRITE FAILED: {failsafe}", flush=True)
    raise

STATE.chmod(0o600)
STATE.write_text(json.dumps(before) + "\n", encoding="ascii")
print(f"Saved previous commands in {STATE}", flush=True)
