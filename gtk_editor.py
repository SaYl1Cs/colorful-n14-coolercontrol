"""Offline GTK4 fallback for systems whose optional Tk/Qt runtime is absent."""
from pathlib import Path
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from build_profile import PROFILE_ID, PROFILE_PATH, export_profile, save_curves_and_build
from curves import LIMITS, MAX_ROWS, load_curves


class GtkCurveEditor(Gtk.Window):
    def __init__(self, parse_tables, warning, columns, loop):
        super().__init__(title="N14 temperature curves — OFFLINE ONLY")
        self.parse_tables = parse_tables
        self.columns = columns
        self.loop = loop
        self.rows = {name: [] for name in LIMITS}
        self.grids = {}
        self.dirty = False
        self.set_default_size(900, 620)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(12)
        self.set_child(box)
        box.append(Gtk.Label(label=PROFILE_ID, xalign=0))
        notice = Gtk.Label(label=warning, xalign=0, wrap=True)
        notice.add_css_class("error")
        box.append(notice)
        box.append(Gtk.Label(label=(
            "Up leaves this row for the NEXT speed; Down leaves it for the PREVIOUS speed.\n"
            "Final Up is not a transition. Down must be below the previous row's Up.\n"
            "Only threshold tables are editable; sensors, limits, Lua and permissions are fixed."
        ), xalign=0, wrap=True))
        notebook = Gtk.Notebook(vexpand=True)
        box.append(notebook)
        for name, limit in LIMITS.items():
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            page.append(Gtk.Label(label=f"@{name}: Max temperature; fixed critical {limit} °C", xalign=0))
            grid = Gtk.Grid(column_spacing=12, row_spacing=6)
            self.grids[name] = grid
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_child(grid)
            page.append(scroller)
            buttons = Gtk.Box(spacing=8)
            for title, callback in (("Add row", self.add_row), ("Remove last row", self.remove_row)):
                button = Gtk.Button(label=title)
                button.connect("clicked", lambda _, n=name, c=callback: c(n))
                buttons.append(button)
            page.append(buttons)
            notebook.append_page(page, Gtk.Label(label=f"{name}: thresholds ≤ {limit} °C"))
        buttons = Gtk.Box(spacing=8)
        for label, callback in (("Validate only", self.validate), ("Save curves + rebuild locally", self.save),
                                ("Export generated profile…", self.export)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _, c=callback: c())
            buttons.append(button)
        box.append(buttons)
        self.status = Gtk.Label(label="Offline. Nothing applied. Installation/application is outside this editor.", xalign=0, wrap=True)
        box.append(self.status)
        for name, rows in load_curves().items():
            for row in rows:
                self.add_row(name, [str(row[key]) for key in columns])
        self.dirty = False
        self.status.set_text("Loaded local curves. Nothing saved, installed or applied.")
        self.connect("close-request", self.close_requested)

    def changed(self, *_):
        self.dirty = True
        self.status.set_text("Unsaved edits. No hardware access or automatic application.")

    def redraw(self, name):
        grid = self.grids[name]
        while child := grid.get_first_child():
            grid.remove(child)
        for column, title in enumerate(("Row", "Up → next (°C)", "Down → previous (°C)", "Command (%)")):
            grid.attach(Gtk.Label(label=title), column, 0, 1, 1)
        for index, entries in enumerate(self.rows[name], 1):
            grid.attach(Gtk.Label(label=str(index)), 0, index, 1, 1)
            for column, entry in enumerate(entries, 1):
                grid.attach(entry, column, index, 1, 1)

    def add_row(self, name, values=None):
        if len(self.rows[name]) >= MAX_ROWS:
            self.status.set_text(f"At most {MAX_ROWS} rows. Nothing saved.")
            return
        entries = [Gtk.Entry(text=value) for value in (values or ("", "", "100"))]
        for entry in entries:
            entry.connect("changed", self.changed)
        self.rows[name].append(entries)
        self.redraw(name)
        self.changed()

    def remove_row(self, name):
        if len(self.rows[name]) <= 2:
            self.status.set_text("At least two rows required. Nothing saved.")
            return
        self.rows[name].pop()
        self.redraw(name)
        self.changed()

    def collect(self):
        return self.parse_tables({name: [[entry.get_text() for entry in row] for row in rows]
                                  for name, rows in self.rows.items()})

    def validate(self):
        try:
            self.collect()
            self.status.set_text("Thresholds valid. GPU RPM UNKNOWN. Nothing saved or applied.")
        except ValueError as exc:
            self.status.set_text(f"INVALID: {exc}. Nothing saved or applied.")

    def save(self):
        try:
            save_curves_and_build(self.collect())
            self.dirty = False
            self.status.set_text("Saved curves.json + rebuilt local profile. NOT installed or applied.")
        except (ValueError, OSError) as exc:
            self.status.set_text(f"NOT APPLIED: {exc}. After disk errors rebuild before future application.")

    def export(self):
        try:
            data = self.collect()
        except ValueError as exc:
            self.status.set_text(f"INVALID: {exc}. Nothing exported.")
            return
        chooser = Gtk.FileChooserNative.new("Export only — never applies", self,
                                            Gtk.FileChooserAction.SAVE, "Export", "Cancel")
        chooser.set_current_name(PROFILE_PATH.name)
        file_filter = Gtk.FileFilter()
        file_filter.set_name("JSON profile")
        file_filter.add_pattern("*.json")
        chooser.add_filter(file_filter)
        def response(dialog, response_id):
            try:
                if response_id == Gtk.ResponseType.ACCEPT:
                    selected = dialog.get_file()
                    path = selected.get_path() if selected else None
                    if not path:
                        raise ValueError("Only local file export is supported")
                    export_profile(data, Path(path))
                    self.status.set_text("Exported generated profile. Curve input unchanged; NOT installed or applied.")
            except (ValueError, OSError) as exc:
                self.status.set_text(f"Export failed: {exc}. Nothing applied.")
            finally:
                dialog.destroy()
        chooser.connect("response", response)
        chooser.show()

    def close_requested(self, *_):
        if not self.dirty:
            self.loop.quit()
            return False
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, buttons=Gtk.ButtonsType.YES_NO,
                                   text="Discard unsaved edits? Nothing will be applied.")
        def response(prompt, response_id):
            prompt.destroy()
            if response_id == Gtk.ResponseType.YES:
                self.dirty = False
                self.close()
        dialog.connect("response", response)
        dialog.present()
        return True


def run_editor(parse_tables, warning, columns):
    if not Gtk.init_check():
        raise RuntimeError("GTK cannot connect to a graphical display")
    loop = GLib.MainLoop()
    window = GtkCurveEditor(parse_tables, warning, columns, loop)
    window.present()
    loop.run()
    return 0
