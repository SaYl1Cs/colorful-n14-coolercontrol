"""Execute actual generated Lua handlers using installed Lua 5.4, no Lupa."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_profile import PROFILE_PATH

HARNESS = (ROOT / "tests" / "lua_harness.lua").read_text(encoding="utf-8")
SCENARIOS = re.findall(r"^function tests\.([a-z_]+)\(\)", HARNESS, re.MULTILINE)


class LuaHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lua = shutil.which("lua5.4")
        if not cls.lua:
            raise RuntimeError("Lua 5.4 CLI required; no packages are installed by tests")
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        fans = []
        for fan in profile["FanConfigurations"]:
            handlers = []
            for operation in ("Read", "Write", "Reset"):
                code = "\n".join(fan[operation + "LuaCode"])
                handlers.append(f"{operation} = function()\n{code}\nend")
            fans.append("{\n" + ",\n".join(handlers) + "\n}")
        cls.handlers = "local handlers = {\n" + ",\n".join(fans) + "\n}\n"


def make_test(scenario):
    def test(self):
        script = f"local scenario = {json.dumps(scenario)}\n" + self.handlers + HARNESS
        result = subprocess.run([self.lua, "-"], input=script, text=True,
                                capture_output=True, timeout=5, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS " + scenario)
    return test


for _scenario in SCENARIOS:
    setattr(LuaHandlerTests, "test_" + _scenario, make_test(_scenario))


if __name__ == "__main__":
    unittest.main()
