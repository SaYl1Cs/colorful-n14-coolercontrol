"""Offline Tk editor with existing-GTK fallback; never starts NBFC."""
import os
from pathlib import Path
import sys
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    TK_IMPORT_ERROR = None
except ImportError as exc:
    tk = filedialog = messagebox = ttk = None
    TK_IMPORT_ERROR = str(exc)

from build_profile import PROFILE_ID, PROFILE_PATH, export_profile, save_curves_and_build
from curves import LIMITS, MAX_ROWS, load_curves, validate_curves

COLUMNS = ("UpThreshold", "DownThreshold", "FanSpeed")
WARNING = (
    "GPU percentage = controller command / 70, NOT RPM. GPU RPM UNKNOWN.\n"
    "Temperature control WITHOUT verified GPU rotation feedback.\n"
    "First stage: minimum 40% for BOTH fans. 0% is not verified or enabled.\n"
    "Control cleanup requests BOTH fans FULL. Saving/exporting does NOT apply anything."
)


def parse_tables(tables):
    """Parse only table cells; the builder repeats the same strict validation."""
    parsed = {}
    for name, rows in tables.items():
        parsed[name] = []
        for row in rows:
            if len(row) != len(COLUMNS):
                raise ValueError("Every row needs Up, Down, and percent")
            try:
                values = [float(value.strip()) for value in row]
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"{name}: each cell must contain a number") from exc
            parsed[name].append(dict(zip(COLUMNS, values)))
    return validate_curves(parsed)


class Editor:
    def __init__(self, window):
        self.window = window
        self.rows = {}
        self.frames = {}
        self.dirty = False
        window.title("N14 temperature curves — OFFLINE ONLY")
        window.minsize(820, 580)
        ttk.Label(window, text=PROFILE_ID, font=("TkDefaultFont", 12, "bold")).pack(padx=12, pady=8)
        tk.Label(window, text=WARNING, justify="left", fg="#9b1c1c", wraplength=850).pack(padx=12, anchor="w")
        ttk.Label(window, text=(
            "Pinned NBFC semantics: Up leaves the current row for the NEXT speed;\n"
            "Down leaves the current row for the PREVIOUS speed. Final Up is not a transition.\n"
            "Cooling thresholds must be below the previous row's heating threshold."
        ), justify="left").pack(padx=12, pady=8, anchor="w")
        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=12)
        for name, limit in LIMITS.items():
            page = ttk.Frame(notebook, padding=10)
            notebook.add(page, text=f"{name}: thresholds ≤ {limit} °C")
            ttk.Label(page, text=f"{name} / {'@CPU' if name == 'CPU' else '@GPU'} — Max temperature; fixed critical {limit} °C").pack(anchor="w")
            canvas = tk.Canvas(page, height=220, highlightthickness=0)
            scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(fill="both", expand=True)
            table = ttk.Frame(canvas)
            canvas.create_window((0, 0), window=table, anchor="nw")
            table.bind("<Configure>", lambda event, c=canvas: c.configure(scrollregion=c.bbox("all")))
            self.frames[name] = table
            self.rows[name] = []
            buttons = ttk.Frame(page)
            buttons.pack(anchor="w", pady=6)
            ttk.Button(buttons, text="Add row", command=lambda n=name: self.add_row(n)).pack(side="left")
            ttk.Button(buttons, text="Remove last row", command=lambda n=name: self.remove_row(n)).pack(side="left", padx=8)
        buttons = ttk.Frame(window, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Validate only", command=self.validate).pack(side="left")
        ttk.Button(buttons, text="Save curves + rebuild locally", command=self.save).pack(side="left", padx=8)
        ttk.Button(buttons, text="Export generated profile…", command=self.export).pack(side="left")
        self.status = tk.StringVar(value="Offline. Nothing applied. Privileged installation/application is outside this editor.")
        ttk.Label(window, textvariable=self.status, wraplength=850).pack(padx=12, pady=8, anchor="w")
        data = load_curves()
        for name, rows in data.items():
            for row in rows:
                self.add_row(name, [str(row[key]) for key in COLUMNS])
        self.dirty = False
        self.status.set("Loaded local curves. Nothing saved, installed or applied.")
        window.protocol("WM_DELETE_WINDOW", self.close)

    def changed(self, *_):
        self.dirty = True
        self.status.set("Unsaved edits. No hardware access; no automatic application.")

    def redraw(self, name):
        frame = self.frames[name]
        for widget in frame.winfo_children():
            widget.destroy()
        for column, heading in enumerate(("Row", "Up → next row (°C)", "Down → previous row (°C)", "Command (%)")):
            ttk.Label(frame, text=heading).grid(row=0, column=column, padx=8, pady=4)
        for index, variables in enumerate(self.rows[name], 1):
            ttk.Label(frame, text=str(index)).grid(row=index, column=0)
            for column, variable in enumerate(variables, 1):
                ttk.Entry(frame, textvariable=variable, width=18).grid(row=index, column=column, padx=8, pady=3)

    def add_row(self, name, values=None):
        if len(self.rows[name]) >= MAX_ROWS:
            messagebox.showerror("Row limit", f"At most {MAX_ROWS} rows", parent=self.window)
            return
        variables = [tk.StringVar(value=value) for value in (values or ("", "", "100"))]
        for variable in variables:
            variable.trace_add("write", self.changed)
        self.rows[name].append(variables)
        self.redraw(name)
        self.changed()

    def remove_row(self, name):
        if len(self.rows[name]) <= 2:
            messagebox.showerror("Row limit", "At least two rows are required", parent=self.window)
            return
        self.rows[name].pop()
        self.redraw(name)
        self.changed()

    def collect(self):
        return parse_tables({name: [[cell.get() for cell in row] for row in rows]
                             for name, rows in self.rows.items()})

    def error(self, exc):
        messagebox.showerror("Not saved / not applied", str(exc), parent=self.window)

    def validate(self):
        try:
            self.collect()
            self.status.set("Thresholds valid. GPU RPM still UNKNOWN. Nothing saved or applied.")
        except (ValueError, OSError) as exc:
            self.error(exc)

    def save(self):
        try:
            target = save_curves_and_build(self.collect())
        except (ValueError, OSError) as exc:
            self.error(f"{exc}\nIf a disk write failed, rebuild before any future application.")
            return
        self.dirty = False
        self.status.set(f"Saved curves.json and rebuilt {target.name}. NOT installed or applied.")

    def export(self):
        try:
            data = self.collect()
            target = filedialog.asksaveasfilename(parent=self.window, title="Export only — never applies",
                                                initialfile=PROFILE_PATH.name,
                                                defaultextension=".json", filetypes=[("JSON profile", "*.json")])
            if target:
                export_profile(data, Path(target))
                self.status.set("Exported validated profile. Curve input unchanged; NOT installed or applied.")
        except (ValueError, OSError) as exc:
            self.error(exc)

    def close(self):
        if self.dirty and not messagebox.askyesno("Discard unsaved edits?", "Close without saving? Nothing will be applied.", parent=self.window):
            return
        self.window.destroy()


def main():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Do not run the offline editor as root.", file=sys.stderr)
        return 1
    if tk is None:
        try:
            from gtk_editor import run_editor
            return run_editor(parse_tables, WARNING, COLUMNS)
        except (ImportError, ValueError, OSError, RuntimeError) as exc:
            print(f"No usable GUI runtime. Tk: {TK_IMPORT_ERROR}; GTK: {exc}", file=sys.stderr)
            return 1
    try:
        window = tk.Tk()
        Editor(window)
        window.mainloop()
    except (tk.TclError, ValueError, OSError) as exc:
        print(f"Editor unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
