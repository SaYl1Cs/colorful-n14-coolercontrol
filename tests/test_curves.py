"""Offline schema, builder, hysteresis, editor-model and artifact tests."""
import copy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_profile as builder
import curve_editor as editor
from curves import CURVES_PATH, LIMITS, load_curves, validate_curves


class CurveValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = load_curves()

    def invalid(self, mutate):
        data = copy.deepcopy(self.data)
        mutate(data)
        with self.assertRaises(ValueError):
            validate_curves(data)
        with self.assertRaises(ValueError):
            builder.build(data)

    def test_defaults_and_fresh_copy(self):
        self.assertEqual(validate_curves(self.data), self.data)
        clean = validate_curves(self.data)
        clean["CPU"][0]["FanSpeed"] = 99
        self.assertEqual(self.data["CPU"][0]["FanSpeed"], 40)

    def test_exact_top_level_keys_and_types(self):
        for data in (None, [], True, {}, {"CPU": self.data["CPU"]},
                     dict(self.data, LuaLibraries=["os"]), dict(self.data, cpu=[])):
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_curves(data)

    def test_exact_row_keys(self):
        for name in LIMITS:
            self.invalid(lambda d: d[name][0].update(ReadLuaCode=["evil"]))
            self.invalid(lambda d: d[name][0].pop("FanSpeed"))
            self.invalid(lambda d: d[name].__setitem__(0, [45, 0, 40]))

    def test_row_count_and_container_type(self):
        for rows in ([], self.data["CPU"][:1], self.data["CPU"] * 4, {}, (), None):
            self.invalid(lambda d: d.__setitem__("CPU", rows))

    def test_finite_numeric_types_excluding_bool(self):
        for name in LIMITS:
            for field in ("UpThreshold", "DownThreshold", "FanSpeed"):
                for value in (True, False, None, "45", [], {}, math.nan, math.inf, -math.inf, 10**400):
                    with self.subTest(name=name, field=field, value=value):
                        self.invalid(lambda d: d[name][1].__setitem__(field, value))

    def test_all_temperatures_bounded(self):
        for name, limit in LIMITS.items():
            for field in ("UpThreshold", "DownThreshold"):
                for value in (-0.01, limit + 0.01):
                    self.invalid(lambda d: d[name][-1].__setitem__(field, value))

    def test_increasing_temperatures(self):
        for field in ("UpThreshold", "DownThreshold"):
            for value in (self.data["CPU"][0][field], self.data["CPU"][0][field] - 1):
                self.invalid(lambda d: d["CPU"][1].__setitem__(field, value))

    def test_hysteresis_and_initial_floor(self):
        self.invalid(lambda d: d["CPU"][0].__setitem__("DownThreshold", 1))
        self.invalid(lambda d: d["CPU"][0].__setitem__("UpThreshold", 0))
        self.invalid(lambda d: d["CPU"][1].__setitem__("DownThreshold", 45))
        self.invalid(lambda d: d["CPU"][1].__setitem__("DownThreshold", 60))

    def test_speed_bounds_monotonic_and_final_full(self):
        for value in (0, 39.99, 100.01):
            self.invalid(lambda d: d["GPU"][0].__setitem__("FanSpeed", value))
        self.invalid(lambda d: d["GPU"][2].__setitem__("FanSpeed", 49))
        self.invalid(lambda d: d["GPU"][-1].__setitem__("FanSpeed", 99))

    def test_fractional_thresholds_and_equal_speeds_allowed(self):
        self.data["CPU"][0]["UpThreshold"] = 45.5
        self.data["CPU"][1]["FanSpeed"] = 40
        self.assertEqual(validate_curves(self.data), self.data)

    def test_json_duplicate_nonfinite_and_oversize_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "input.json"
            for text in ('{"CPU":[],"CPU":[],"GPU":[]}', '{"CPU":NaN,"GPU":[]}',
                         '{"CPU":Infinity,"GPU":[]}', ' ' * 65537):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_curves(path)

    def test_rising_and_falling_selection_semantics(self):
        for name, rows in self.data.items():
            selected = 0
            for temperature in range(101):
                while selected > 0 and temperature <= rows[selected]["DownThreshold"]:
                    selected -= 1
                while selected < len(rows) - 1 and temperature >= rows[selected]["UpThreshold"]:
                    selected += 1
                self.assertGreaterEqual(rows[selected]["FanSpeed"], 40)
                if temperature >= (80 if name == "CPU" else 72):
                    self.assertEqual(rows[selected]["FanSpeed"], 100)
            for temperature in range(100, -1, -1):
                while selected > 0 and temperature <= rows[selected]["DownThreshold"]:
                    selected -= 1
                after_down = selected
                while selected < len(rows) - 1 and temperature >= rows[selected]["UpThreshold"]:
                    selected += 1
                self.assertEqual(selected, after_down, "no hysteresis oscillation")
            self.assertEqual(selected, 0)


class ArtifactTests(unittest.TestCase):
    def test_artifact_exactly_matches_validated_builder(self):
        self.assertEqual(builder.PROFILE_PATH.read_text(), builder.profile_text())
        self.assertLess(builder.PROFILE_PATH.stat().st_size, 32768)

    def test_pinned_identity_and_honest_labels(self):
        profile = builder.build()
        self.assertEqual(builder.PROFILE_ID, "Colorful N14 24 - Temperature Curves - GPU Command Readback")
        self.assertEqual(profile["FanConfigurations"][1]["FanDisplayName"], "GPU - command readback, RPM UNKNOWN")
        self.assertIn(builder.UPSTREAM, profile["Comment"])
        self.assertIn("WITHOUT verified GPU rotation feedback", profile["Comment"])
        self.assertIn("55:71", profile["Comment"])

    def test_fixed_sensors_critical_limits_and_no_raw_ec(self):
        profile = builder.build()
        self.assertEqual(profile["RegisterWriteConfigurations"], [])
        self.assertEqual(profile["EcPollInterval"], 1000)
        self.assertFalse(profile["LegacyTemperatureThresholdsBehaviour"])
        self.assertEqual(profile["LuaLibraries"], ["base", "math", "string", "io"])
        for fan, name in zip(profile["FanConfigurations"], LIMITS):
            self.assertEqual(fan["Sensors"], ["@" + name])
            self.assertEqual(fan["TemperatureAlgorithmType"], "Max")
            self.assertEqual(fan["CriticalTemperature"], LIMITS[name])
            self.assertEqual(fan["CriticalTemperatureOffset"], 5)
            self.assertEqual((fan["MinSpeedValue"], fan["MaxSpeedValue"]), (0, 70))
            self.assertTrue(fan["ResetRequired"])
            for key in ("ReadRegister", "WriteRegister"):
                self.assertNotIn(key, fan)
            text = "\n".join(fan["ReadLuaCode"])
            self.assertNotIn("0x5d", text.lower())
            self.assertNotIn("ec_read(", text)
            self.assertNotIn("ec_write(", text)

    def test_input_changes_only_threshold_tables(self):
        original = builder.build()
        data = load_curves()
        data["CPU"][1]["UpThreshold"] = 61
        changed = builder.build(data)
        for a, b in zip(original["FanConfigurations"], changed["FanConfigurations"]):
            a.pop("TemperatureThresholds")
            b.pop("TemperatureThresholds")
        self.assertEqual(original, changed)

    def test_invalid_save_or_export_writes_nothing(self):
        data = load_curves()
        data["GPU"][0]["FanSpeed"] = 0
        with patch.object(builder, "atomic_write") as write:
            with self.assertRaises(ValueError):
                builder.save_curves_and_build(data)
            with self.assertRaises(ValueError):
                builder.export_profile(data, builder.PROFILE_PATH)
            write.assert_not_called()

    def test_save_writes_only_curve_input_and_generated_profile(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            curve = Path(tmp) / "curves.json"
            profile = Path(tmp) / "profile.json"
            with patch.object(builder, "CURVES_PATH", curve), patch.object(builder, "PROFILE_PATH", profile):
                builder.save_curves_and_build(load_curves())
            self.assertEqual(set(p.name for p in Path(tmp).iterdir()), {"curves.json", "profile.json"})
            self.assertEqual(json.loads(curve.read_text()), load_curves())
            self.assertEqual(json.loads(profile.read_text()), builder.build())

    def test_export_does_not_overwrite_sources(self):
        for path in (CURVES_PATH, ROOT / "prepare.py", ROOT / "provenance" / "profile.original.json"):
            with self.assertRaises(ValueError):
                builder.export_profile(load_curves(), path)

    def test_export_only_generated_profile(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            destination = Path(tmp) / "export.json"
            with patch.object(builder, "PROFILE_PATH", destination):
                builder.export_profile(load_curves(), destination)
            self.assertEqual(json.loads(destination.read_text()), builder.build())

    def test_atomic_output_refuses_symlinks(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            target = Path(tmp) / "target"
            target.write_text("unchanged")
            link = Path(tmp) / "link"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                builder.atomic_write(link, "changed")
            self.assertEqual(target.read_text(), "unchanged")


class EditorModelTests(unittest.TestCase):
    def tables(self):
        return {name: [[str(row[key]) for key in editor.COLUMNS] for row in rows]
                for name, rows in load_curves().items()}

    def test_table_round_trip(self):
        self.assertEqual(editor.parse_tables(self.tables()), load_curves())

    def test_editor_uses_same_validation(self):
        for value in ("nan", "inf", "True", "", "0", "39", "101", "__import__('os')"):
            tables = self.tables()
            tables["GPU"][0][2] = value
            with self.assertRaises(ValueError):
                editor.parse_tables(tables)

    def test_warning_is_honest(self):
        for phrase in ("NOT RPM", "RPM UNKNOWN", "minimum 40%", "0% is not verified", "BOTH fans FULL", "does NOT apply"):
            self.assertIn(phrase, editor.WARNING)

    def test_gui_actions_call_only_offline_builder(self):
        instance = editor.Editor.__new__(editor.Editor)
        instance.collect = Mock(return_value=load_curves())
        instance.status = Mock()
        instance.dirty = True
        instance.window = Mock()
        with patch.object(editor, "save_curves_and_build", return_value=builder.PROFILE_PATH) as save:
            instance.save()
            save.assert_called_once_with(load_curves())
            self.assertFalse(instance.dirty)
        dialog = Mock()
        dialog.asksaveasfilename.return_value = "export.json"
        with patch.object(editor, "filedialog", dialog), patch.object(editor, "export_profile") as export:
            instance.export()
            export.assert_called_once_with(load_curves(), Path("export.json"))

    def test_root_editor_refused_without_window(self):
        toolkit = Mock()
        with patch.object(editor.os, "geteuid", return_value=0), patch.object(editor, "tk", toolkit):
            self.assertEqual(editor.main(), 1)
            toolkit.Tk.assert_not_called()

    def test_missing_tk_selects_existing_gtk_fallback(self):
        import gtk_editor
        with patch.object(editor.os, "geteuid", return_value=1000), patch.object(editor, "tk", None), \
             patch.object(gtk_editor, "run_editor", return_value=0) as run:
            self.assertEqual(editor.main(), 0)
            run.assert_called_once_with(editor.parse_tables, editor.WARNING, editor.COLUMNS)


if __name__ == "__main__":
    unittest.main()
