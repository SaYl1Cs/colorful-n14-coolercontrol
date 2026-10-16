"""Offline GTK widget smoke tests; no fan/service code, no automatic file saves."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import curve_editor
from curves import load_curves


class GtkWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            raise unittest.SkipTest('No graphical display in this environment')
        try:
            import gtk_editor
        except (ImportError, ValueError) as exc:
            raise unittest.SkipTest(f"GTK not available: {exc}") from exc
        cls.module = gtk_editor
        initialized = gtk_editor.Gtk.init_check()
        # GTK3 GI returns (success, argv); GTK4 returns a bool.
        if isinstance(initialized, tuple):
            initialized = initialized[0]
        if not initialized:
            raise unittest.SkipTest("No graphical display for GTK widget smoke tests")

    def setUp(self):
        self.save_patch = patch.object(self.module, "save_curves_and_build")
        self.save = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)
        self.window = self.module.GtkCurveEditor(curve_editor.parse_tables, curve_editor.WARNING,
                                                curve_editor.COLUMNS, Mock())
        self.addCleanup(self.window.destroy)

    def test_widgets_load_curves_without_writes(self):
        self.assertEqual(self.window.collect(), load_curves())
        self.assertFalse(self.window.dirty)
        self.save.assert_not_called()

    def test_invalid_edit_cannot_save(self):
        self.window.rows["GPU"][0][2].set_text("0")
        self.window.save()
        self.save.assert_not_called()
        self.assertIn("NOT APPLIED", self.window.status.get_text())

    def test_valid_edit_saves_only_through_shared_builder(self):
        self.window.rows["CPU"][0][0].set_text("46")
        expected = load_curves()
        expected["CPU"][0]["UpThreshold"] = 46
        self.assertTrue(self.window.dirty)
        self.window.save()
        self.save.assert_called_once_with(expected)
        self.assertFalse(self.window.dirty)
        self.assertIn("NOT installed or applied", self.window.status.get_text())

    def test_add_remove_and_validation_only(self):
        self.window.add_row("CPU")
        self.window.validate()
        self.assertIn("INVALID", self.window.status.get_text())
        self.window.remove_row("CPU")
        self.window.validate()
        self.assertEqual(self.window.collect(), load_curves())
        self.save.assert_not_called()

    def test_clean_close_never_saves(self):
        self.assertFalse(self.window.close_requested())
        self.window.loop.quit.assert_called_once()
        self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
