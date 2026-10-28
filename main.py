"""
Rogue Keybind Remapper
----------------------
Lightweight global key remapper with system-tray support.

Features
- Remap any key to any other key (global low-level hook).
- Window closes to the system tray (right-click tray icon to show/exit).
- Auto-disables all remaps while the Rogue Lineage Gate spell typing UI is on
  screen (OCR of the "Type the name of a gate" header in the focused window).
- Optional: only remap while Roblox is the focused window.
- Manual suspend hotkey (default F8).

Requires: keyboard, mss, pystray, Pillow, pytesseract (Tesseract-OCR is
bundled next to the app; run fetch_tesseract.ps1 when building from source)
"""

import ctypes
import ctypes.wintypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import keyboard
import mss
import pystray
import pytesseract
from PIL import Image, ImageChops, ImageDraw, ImageFilter

import macro_engine as mengine

# Make sure screen coordinates match physical pixels (for region capture).
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

APP_NAME = "Rogue Keybind Remapper"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "keybinds_config.json")

# Downscaled scan resolution for Gate-UI detection.
SAMPLE_W, SAMPLE_H = 40, 24

DEFAULT_CONFIG = {
    "bindings": [],  # [{"src": "1", "dst": "f", "enabled": True}, ...]
    "macros": [],    # [{"name": "", "trigger": "f5", "enabled": True, "script": "..."}, ...]
    "suspend_hotkey": "f8",
    "only_when_roblox_focused": True,
    "always_on_top": False,
    "gate_detection": {
        "enabled": True,
        "poll_ms": None,
    },
}


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Screen / window helpers
# --------------------------------------------------------------------------

_user32 = ctypes.windll.user32


def foreground_hwnd():
    try:
        return _user32.GetForegroundWindow()
    except Exception:
        return 0


def foreground_title():
    try:
        hwnd = _user32.GetForegroundWindow()
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def window_rect(hwnd):
    try:
        rect = ctypes.wintypes.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        pass
    return None


def find_roblox_hwnd():
    """Find the Roblox game window by exact title, regardless of focus."""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, lparam):
        try:
            if _user32.IsWindowVisible(hwnd):
                n = _user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value.strip().lower() == "roblox":
                        r = window_rect(hwnd)
                        if r and r[2] >= 300 and r[3] >= 200:
                            found.append(hwnd)
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(_cb)  # keep a reference alive during the enum
    try:
        _user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return found[0] if found else 0


_roblox_hwnd_cache = {"hwnd": 0, "checked": 0.0}


def get_roblox_hwnd():
    """Cached Roblox window lookup - re-enumerates at most once per 2s."""
    now = time.time()
    hwnd = _roblox_hwnd_cache["hwnd"]
    if hwnd and now - _roblox_hwnd_cache["checked"] < 2.0:
        try:
            if _user32.IsWindow(hwnd):
                return hwnd
        except Exception:
            pass
    hwnd = find_roblox_hwnd()
    _roblox_hwnd_cache["hwnd"] = hwnd
    _roblox_hwnd_cache["checked"] = now
    return hwnd


SCAN_W, SCAN_H = 320, 180   # resolution used for window captures


def _grab_window_gray(sct, rect, out_w, out_h):
    left, top, width, height = rect
    shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img.convert("L").resize((out_w, out_h), Image.BILINEAR)


def grab_roblox_frame(sct):
    """Capture the Roblox window as a small grayscale image, or None."""
    hwnd = get_roblox_hwnd()
    if not hwnd:
        return None
    rect = window_rect(hwnd)
    if not rect or rect[2] < 100 or rect[3] < 100:
        return None
    return _grab_window_gray(sct, rect, SCAN_W, SCAN_H)


# --- OCR-based typing UI detection ---------------------------------------

TESSERACT_PATHS = [
    # Bundled copy shipped next to the app (installer and portable zip).
    os.path.join(BASE_DIR, "tesseract", "tesseract.exe"),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]

GATE_UI_PHRASES = ("begin typing", "type the name", "name of a gate")


def _tesseract_path():
    for p in TESSERACT_PATHS:
        if os.path.exists(p):
            return p
    return "tesseract"


pytesseract.pytesseract.tesseract_cmd = _tesseract_path()


# Region of the "Gate / Type the name of a gate" header, as fractions of the
# Roblox client area. Measured from real frames; this header stays on screen
# while typing (the "Press '/'" line below it is replaced by typed text).
GATE_HEADER_BOX = (0.38, 0.20, 0.62, 0.30)
MIN_INK = 0.008          # below this the region has no bright text -> skip OCR
MISSES_TO_CLEAR = 2      # consecutive misses before the UI counts as closed
GATE_KEYWORDS = ("type", "name", "gate", "typing")


def focused_roblox_hwnd():
    try:
        hwnd = _user32.GetForegroundWindow()
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return hwnd if buf.value.strip().lower() == "roblox" else 0
    except Exception:
        return 0


def client_rect(hwnd):
    """Screen-space (left, top, width, height) of the window's client area."""
    try:
        r = ctypes.wintypes.RECT()
        if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
            return None
        pt = ctypes.wintypes.POINT(0, 0)
        _user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return (pt.x, pt.y, r.right, r.bottom)
    except Exception:
        return None


def _text_mask(img):
    """White = bright, sharp strokes (UI text); independent of scene lighting."""
    g = img.convert("L")
    g = g.resize((g.width * 2, g.height * 2), Image.BILINEAR)
    hp = ImageChops.subtract(g, g.filter(ImageFilter.GaussianBlur(5)))
    return ImageChops.multiply(hp.point(lambda v: 255 if v > 14 else 0),
                               g.point(lambda v: 255 if v > 110 else 0))


def _is_gate_text(text):
    norm = " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())
    if "type the name" in norm or "name of a" in norm or "of a gate" in norm:
        return True
    return sum(1 for k in GATE_KEYWORDS if k in norm.split()) >= 2


def read_gate_header(sct, hwnd):
    """Returns (found, ocr_text). ocr_text is None if OCR was skipped."""
    rect = client_rect(hwnd)
    if not rect or rect[2] < 200 or rect[3] < 200:
        return False, None
    left, top, w, h = rect
    x0, y0, x1, y1 = GATE_HEADER_BOX
    shot = sct.grab({"left": left + int(w * x0), "top": top + int(h * y0),
                     "width": int(w * (x1 - x0)), "height": int(h * (y1 - y0))})
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    mask = _text_mask(img)
    if mask.histogram()[255] / float(mask.width * mask.height) < MIN_INK:
        return False, None
    try:
        text = pytesseract.image_to_string(ImageChops.invert(mask), config="--psm 6")
    except Exception:
        return False, None
    return _is_gate_text(text), text.strip()


# --------------------------------------------------------------------------
# Core: hooks + gate detection
# --------------------------------------------------------------------------

class RemapperCore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.running = True
        self.suspended = False
        self.recording = False        # True while the user records a key
        self.gate_active = False
        self.hooks_applied = False
        self.roblox_focused = False
        self.last_score_on = None
        self.last_score_off = None
        self.last_ocr_text = None
        self._dirty = True
        # Macros abort mid-run as soon as remaps are suspended (e.g. gating).
        self.macro_runner = mengine.MacroRunner(should_run=lambda: not (self.suspended or self.gate_active))

    # -- state ----------------------------------------------------------

    def mark_dirty(self):
        self._dirty = True

    def toggle_suspend(self):
        self.suspended = not self.suspended
        self.mark_dirty()

    def desired_state(self):
        if self.suspended or self.gate_active:
            return False
        if self.cfg.get("only_when_roblox_focused", True) and not self.roblox_focused:
            return False
        return True

    def status_text(self):
        if self.suspended:
            return "SUSPENDED (hotkey)"
        if self.gate_active:
            return "DISABLED (Gate UI on screen)"
        if self.hooks_applied:
            return "ACTIVE"
        if self.cfg.get("only_when_roblox_focused", True) and not self.roblox_focused:
            return "INACTIVE (Roblox not focused)"
        return "INACTIVE"

    # -- hooks ----------------------------------------------------------

    def apply_hooks(self, want_active):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        hotkey = (self.cfg.get("suspend_hotkey") or "").strip()
        if hotkey:
            try:
                keyboard.add_hotkey(hotkey, self.toggle_suspend, suppress=False)
            except Exception:
                pass
        if want_active:
            for b in self.cfg["bindings"]:
                src = (b.get("src") or "").strip()
                dst = (b.get("dst") or "").strip()
                if b.get("enabled", True) and src and dst and src != dst:
                    try:
                        keyboard.remap_key(src, dst)
                    except Exception:
                        pass
            for m in self.cfg.get("macros", []):
                trig = (m.get("trigger") or "").strip()
                if m.get("enabled", True) and trig and (m.get("script") or "").strip():
                    try:
                        keyboard.add_hotkey(
                            trig,
                            lambda mm=m: self.macro_runner.fire(mm),
                            suppress=False,
                        )
                    except Exception:
                        pass
        else:
            # going inactive (gating/suspended): stop any running macros
            self.macro_runner.stop_all()
        self.hooks_applied = bool(want_active)

    # -- monitor thread ---------------------------------------------------

    def monitor(self):
        sct = mss.mss()
        last_desired = None
        misses = 0
        try:
            while self.running:
                loop_start = time.time()
                try:
                    gd = self.cfg["gate_detection"]
                    hwnd = focused_roblox_hwnd()
                    self.roblox_focused = bool(hwnd)

                    # Only the focused Roblox window is on top, so only then
                    # does the screen capture show the game.
                    if gd.get("enabled") and hwnd:
                        found, text = read_gate_header(sct, hwnd)
                        if text is not None:
                            self.last_ocr_text = text
                        if found:
                            misses = 0
                            self.gate_active = True
                        else:
                            misses += 1
                            if misses >= MISSES_TO_CLEAR:
                                self.gate_active = False
                        self.last_score_on = 1.0 if self.gate_active else 0.0
                    else:
                        misses = MISSES_TO_CLEAR
                        self.gate_active = False
                        self.last_score_on = None

                    desired = self.desired_state()
                    if not self.recording and (desired != last_desired or self._dirty):
                        self.apply_hooks(desired)
                        last_desired = desired
                        self._dirty = False
                except Exception:
                    pass
                poll = 150
                try:
                    override = self.cfg["gate_detection"].get("poll_ms")
                    if override:
                        poll = max(60, int(override))
                except Exception:
                    pass
                elapsed_ms = (time.time() - loop_start) * 1000.0
                time.sleep(max(0.02, (poll - elapsed_ms) / 1000.0))
        finally:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            try:
                sct.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class BindingDialog(tk.Toplevel):
    def __init__(self, app, on_save):
        super().__init__(app.root)
        self.app = app
        self.on_save = on_save
        self.title("Add keybind")
        self.resizable(False, False)
        self.transient(app.root)

        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()

        pad = {"padx": 8, "pady": 4}
        ttk.Label(self, text="Key you press:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.src_var, width=14).grid(row=0, column=1, **pad)
        ttk.Button(self, text="Record", command=lambda: self._record(self.src_var)).grid(row=0, column=2, **pad)

        ttk.Label(self, text="Send instead:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.dst_var, width=14).grid(row=1, column=1, **pad)
        ttk.Button(self, text="Record", command=lambda: self._record(self.dst_var)).grid(row=1, column=2, **pad)

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=3, pady=8)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    def _record(self, var):
        self.app.core.recording = True

        def worker():
            try:
                key = keyboard.read_key(suppress=True)
                var.set(key)
            except Exception:
                pass
            finally:
                self.app.core.recording = False

        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        src = self.src_var.get().strip().lower()
        dst = self.dst_var.get().strip().lower()
        if not src or not dst:
            messagebox.showwarning("Missing key", "Both keys are required.", parent=self)
            return
        if src == dst:
            messagebox.showwarning("Invalid", "Source and target keys are the same.", parent=self)
            return
        self.on_save(src, dst)
        self.destroy()


class MacroDialog(tk.Toplevel):
    """Visual macro builder: build a list of actions, no scripting needed."""

    def __init__(self, app, macro, on_save):
        super().__init__(app.root)
        self.app = app
        self.on_save = on_save
        self.title("Macro editor")
        self.geometry("560x520")
        self.transient(app.root)

        # header: name / trigger / enabled
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Name:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=macro.get("name", ""))
        ttk.Entry(top, textvariable=self.name_var, width=22).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Trigger key:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.trig_var = tk.StringVar(value=macro.get("trigger", ""))
        ttk.Entry(top, textvariable=self.trig_var, width=10).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Button(top, text="Record", command=self._record_trigger).grid(row=0, column=4, padx=4)
        self.enabled_var = tk.BooleanVar(value=bool(macro.get("enabled", True)))
        ttk.Checkbutton(top, text="Enabled", variable=self.enabled_var).grid(row=0, column=5, padx=8)

        # action list
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.listbox = tk.Listbox(mid, font=("Segoe UI", 10), activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.listbox.bind("<Double-1>", lambda e: self._edit_selected())
        self.listbox.bind("<Delete>", lambda e: self._remove_selected())

        # load existing actions into the visual list
        self.nodes = []
        self.load_warning = None
        if (macro.get("script") or "").strip():
            try:
                self.nodes = mengine.script_to_nodes(macro["script"])
            except ValueError:
                self.nodes = []
            if not self.nodes:
                self.load_warning = ("The script converted, but no usable actions were found. "
                                     "It may contain only unsupported commands.")
        self._refresh_list()
        if self.load_warning:
            ttk.Label(self, text=self.load_warning, foreground="dark orange",
                      wraplength=540, justify="left").pack(fill="x", padx=8, pady=(0, 4))

        # add-action row
        add = ttk.LabelFrame(self, text="Add an action")
        add.pack(fill="x", padx=8, pady=4)
        self.add_type = ttk.Combobox(add, values=mengine.ACTION_TYPES, state="readonly", width=14)
        self.add_type.set("Press key")
        self.add_type.grid(row=0, column=0, padx=4, pady=6)
        self.add_type.bind("<<ComboboxSelected>>", lambda e: self._update_param())

        self.param_lbl = ttk.Label(add, text="Key:")
        self.param_lbl.grid(row=0, column=1, sticky="e")
        self.param_var = tk.StringVar()
        self.param_ent = ttk.Entry(add, textvariable=self.param_var, width=16)
        self.param_ent.grid(row=0, column=2, padx=4)
        self.param_btn = ttk.Button(add, text="Record", command=self._record_param)
        self.param_btn.grid(row=0, column=3, padx=2)
        self.param_extra = ttk.Combobox(add, values=["left", "right", "middle"], state="readonly", width=8)
        self.param_extra.set("left")
        ttk.Button(add, text="Add action", command=self._add_action).grid(row=0, column=5, padx=6)
        self._update_param()

        # list controls
        ctl = ttk.Frame(self)
        ctl.pack(fill="x", padx=8)
        ttk.Button(ctl, text="Edit", command=self._edit_selected).pack(side="left", padx=4)
        ttk.Button(ctl, text="Remove", command=self._remove_selected).pack(side="left", padx=4)
        ttk.Button(ctl, text="Up", command=lambda: self._move(-1)).pack(side="left", padx=4)
        ttk.Button(ctl, text="Down", command=lambda: self._move(1)).pack(side="left", padx=4)
        ttk.Button(ctl, text="Clear all", command=self._clear).pack(side="left", padx=4)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=8)
        ttk.Button(btns, text="Save macro", command=self._save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    # -- param input row adapts to the chosen action type -----------------

    def _update_param(self):
        t = self.add_type.get()
        for w in (self.param_lbl, self.param_ent, self.param_btn, self.param_extra):
            w.grid_remove()
        if t in ("Press key", "Hold key", "Release key"):
            self.param_lbl.config(text="Key:")
            self.param_lbl.grid(); self.param_ent.grid(); self.param_btn.grid()
        elif t == "Type text":
            self.param_lbl.config(text="Text:")
            self.param_lbl.grid(); self.param_ent.grid()
        elif t == "Wait":
            self.param_lbl.config(text="Milliseconds:")
            self.param_lbl.grid(); self.param_ent.grid()
        elif t in ("Mouse click", "Mouse down", "Mouse up"):
            self.param_lbl.config(text="Button:")
            self.param_lbl.grid(); self.param_extra.grid(row=0, column=2, padx=4)
        elif t == "Repeat":
            self.param_lbl.config(text="Times:")
            self.param_lbl.grid(); self.param_ent.grid()
        self.param_var.set("")

    def _record_param(self):
        self.app.core.recording = True

        def worker():
            try:
                key = keyboard.read_key(suppress=True)
                self.param_var.set(key)
            except Exception:
                pass
            finally:
                self.app.core.recording = False

        threading.Thread(target=worker, daemon=True).start()

    # -- list management --------------------------------------------------

    def _add_action(self):
        t = self.add_type.get()
        p = self.param_var.get().strip()
        node = {"type": t}
        try:
            if t in ("Press key", "Hold key", "Release key"):
                if not p:
                    raise ValueError("Pick a key (or use Record).")
                node["key"] = p.lower()
            elif t == "Type text":
                if not p:
                    raise ValueError("Enter the text to type.")
                node["text"] = p
            elif t == "Wait":
                node["ms"] = int(p) if p else 100
            elif t in ("Mouse click", "Mouse down", "Mouse up"):
                node["button"] = self.param_extra.get()
            elif t == "Repeat":
                node["count"] = int(p) if p else 2
                node["children"] = []
        except ValueError as exc:
            messagebox.showwarning("Invalid", str(exc), parent=self)
            return
        self.nodes.append(node)
        self._refresh_list()
        self.param_var.set("")

    def _refresh_list(self):
        self.listbox.delete(0, "end")
        for nd in self.nodes:
            self.listbox.insert("end", mengine.describe_node(nd))
            # show loop children indented
            for ch in nd.get("children", []):
                self.listbox.insert("end", "    \u21b3 " + mengine.describe_node(ch))

    def _sel(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        return self._top_index(sel[0])

    def _top_index(self, row):
        """Map a listbox row to a top-level node index (children rows -> None)."""
        top = 0
        for nd in self.nodes:
            if row == 0:
                return top
            row -= 1
            kids = nd.get("children", [])
            if row < len(kids):
                return None  # a child row
            row -= len(kids)
            top += 1
        return None

    def _edit_selected(self):
        idx = self._sel()
        if idx is None:
            return
        nd = self.nodes[idx]
        if nd["type"] == "Repeat":
            LoopDialog(self, nd, lambda: self._refresh_list())
            return
        # put values back into the add row for editing, then remove original
        self.add_type.set(nd["type"])
        self._update_param()
        if "key" in nd:
            self.param_var.set(nd["key"])
        elif "text" in nd:
            self.param_var.set(nd["text"])
        elif "ms" in nd:
            self.param_var.set(str(nd["ms"]))
        elif "button" in nd:
            self.param_extra.set(nd["button"])
        del self.nodes[idx]
        self._refresh_list()

    def _remove_selected(self):
        idx = self._sel()
        if idx is not None:
            del self.nodes[idx]
            self._refresh_list()

    def _move(self, delta):
        idx = self._sel()
        if idx is None:
            return
        j = idx + delta
        if 0 <= j < len(self.nodes):
            self.nodes[idx], self.nodes[j] = self.nodes[j], self.nodes[idx]
            self._refresh_list()

    def _clear(self):
        self.nodes = []
        self._refresh_list()

    def _record_trigger(self):
        self.app.core.recording = True

        def worker():
            try:
                key = keyboard.read_key(suppress=True)
                self.trig_var.set(key)
            except Exception:
                pass
            finally:
                self.app.core.recording = False

        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        name = self.name_var.get().strip()
        trig = self.trig_var.get().strip().lower()
        if not name:
            messagebox.showwarning("Missing", "Macro needs a name.", parent=self)
            return
        if not trig:
            messagebox.showwarning("Missing", "Set a trigger key.", parent=self)
            return
        if not self.nodes:
            messagebox.showwarning("Missing", "Add at least one action.", parent=self)
            return
        try:
            script = mengine.nodes_to_script(self.nodes)
        except ValueError as exc:
            messagebox.showerror("Invalid macro", str(exc), parent=self)
            return
        self.on_save({"name": name, "trigger": trig,
                      "enabled": bool(self.enabled_var.get()), "script": script})
        self.destroy()


class LoopDialog(tk.Toplevel):
    """Edit the actions inside a Repeat block."""

    def __init__(self, parent, node, on_change):
        super().__init__(parent)
        self.node = node
        self.on_change = on_change
        self.title("Repeat block contents")
        self.geometry("420x340")
        self.transient(parent)

        ttk.Label(self, text=f"This block repeats {node.get('count', 1)} times. "
                             "Add the actions to repeat:").pack(anchor="w", padx=8, pady=(8, 0))

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        self.listbox = tk.Listbox(mid)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        self.children = list(node.get("children", []))
        self._refresh()

        add = ttk.Frame(self)
        add.pack(fill="x", padx=8)
        self.add_type = ttk.Combobox(add, values=["Press key", "Type text", "Wait", "Mouse click"],
                                     state="readonly", width=12)
        self.add_type.set("Press key")
        self.add_type.grid(row=0, column=0, padx=4, pady=6)
        self.param_var = tk.StringVar()
        ttk.Entry(add, textvariable=self.param_var, width=14).grid(row=0, column=1, padx=4)
        ttk.Button(add, text="Add", command=self._add).grid(row=0, column=2, padx=4)

        ctl = ttk.Frame(self)
        ctl.pack(fill="x", padx=8, pady=8)
        ttk.Button(ctl, text="Remove", command=self._remove).pack(side="left", padx=4)
        ttk.Button(ctl, text="Done", command=self._done).pack(side="left", padx=4)

    def _refresh(self):
        self.listbox.delete(0, "end")
        for nd in self.children:
            self.listbox.insert("end", mengine.describe_node(nd))

    def _add(self):
        t = self.add_type.get()
        p = self.param_var.get().strip()
        node = {"type": t}
        try:
            if t == "Press key":
                if not p:
                    raise ValueError("Enter a key.")
                node["key"] = p.lower()
            elif t == "Type text":
                if not p:
                    raise ValueError("Enter text.")
                node["text"] = p
            elif t == "Wait":
                node["ms"] = int(p) if p else 100
            elif t == "Mouse click":
                node["button"] = "left"
        except ValueError as exc:
            messagebox.showwarning("Invalid", str(exc), parent=self)
            return
        self.children.append(node)
        self._refresh()
        self.param_var.set("")

    def _remove(self):
        sel = self.listbox.curselection()
        if sel:
            del self.children[sel[0]]
            self._refresh()

    def _done(self):
        self.node["children"] = self.children
        self.on_change()
        self.destroy()


class MacroRecorder:
    """Records keystrokes (with timing) into a macro script."""

    def __init__(self, app):
        self.app = app
        self.recording = False

    def start(self):
        if self.recording:
            return
        if not messagebox.askyesno(
            "Record macro",
            "Recording starts after you click OK.\n\n"
            "Every key you press is captured with its timing until you press ESC.\n"
            "The result opens in the macro editor.",
            parent=self.app.root,
        ):
            return
        self.recording = True
        self.app.core.recording = True  # suspend remaps while recording

        events = []
        t0 = [None]

        def on_key(e):
            if e.event_type != "down":
                return
            now = time.time()
            if t0[0] is None:
                t0[0] = now
            else:
                delay = int((now - t0[0]) * 1000)
                if delay > 5:
                    events.append(f"sleep {delay}")
            t0[0] = now
            if e.name == "esc":
                return
            events.append(f"press {e.name}")

        hook = keyboard.hook(on_key, suppress=False)

        def wait_stop():
            keyboard.wait("esc", suppress=False)
            try:
                keyboard.unhook(hook)
            except Exception:
                pass
            script = "\n".join(events)
            self.recording = False
            self.app.core.recording = False
            self.app.root.after(0, lambda: self._finish(script))

        threading.Thread(target=wait_stop, daemon=True).start()

    def _finish(self, script):
        if not script.strip():
            messagebox.showinfo("Record macro", "Nothing was recorded.", parent=self.app.root)
            return
        macro = {"name": "Recorded macro", "trigger": "", "enabled": True, "script": script}
        MacroDialog(self.app, macro, lambda m: self.app._save_macro(m, None))


class App:
    def __init__(self, core):
        self.core = core
        self.ui_queue = queue.Queue()
        self.icon = None
        self.root = tk.Tk()
        self._build_ui()
        self._build_tray()
        self._apply_topmost()
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)  # close -> tray
        self.root.bind("<Unmap>", self._on_unmap)  # minimize -> tray
        self.root.after(100, self._poll_queue)
        self.root.after(250, self._refresh_status)

    def _on_unmap(self, event):
        # Withdraw to the tray when the window is minimized.
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(10, self.root.withdraw)

    # -- UI construction ------------------------------------------------

    def _build_ui(self):
        r = self.root
        r.title(APP_NAME)
        r.geometry("620x560")
        r.minsize(560, 500)

        # --- bindings ---
        bf = ttk.LabelFrame(r, text="Keybinds (press key -> key sent to game)")
        bf.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        cols = ("src", "dst", "enabled")
        self.tree = ttk.Treeview(bf, columns=cols, show="headings", height=6)
        self.tree.heading("src", text="You press")
        self.tree.heading("dst", text="Game receives")
        self.tree.heading("enabled", text="Enabled")
        self.tree.column("src", width=140, anchor="center")
        self.tree.column("dst", width=140, anchor="center")
        self.tree.column("enabled", width=80, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(bf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.tree.bind("<Double-1>", lambda e: self.toggle_selected())

        brow = ttk.Frame(r)
        brow.pack(fill="x", padx=10)
        ttk.Button(brow, text="Add", command=self.add_binding).pack(side="left", padx=4)
        ttk.Button(brow, text="Remove", command=self.remove_selected).pack(side="left", padx=4)
        ttk.Button(brow, text="Enable/Disable", command=self.toggle_selected).pack(side="left", padx=4)

        # --- macros ---
        mf = ttk.LabelFrame(r, text="Macros (press trigger -> run actions; auto-disable with keybinds)")
        mf.pack(fill="both", expand=True, padx=10, pady=4)

        mcols = ("name", "trigger", "enabled")
        self.mtree = ttk.Treeview(mf, columns=mcols, show="headings", height=5)
        self.mtree.heading("name", text="Name")
        self.mtree.heading("trigger", text="Trigger")
        self.mtree.heading("enabled", text="Enabled")
        self.mtree.column("name", width=240, anchor="w")
        self.mtree.column("trigger", width=120, anchor="center")
        self.mtree.column("enabled", width=80, anchor="center")
        self.mtree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        msb = ttk.Scrollbar(mf, orient="vertical", command=self.mtree.yview)
        self.mtree.configure(yscrollcommand=msb.set)
        msb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.mtree.bind("<Double-1>", lambda e: self.edit_macro())

        mrow = ttk.Frame(r)
        mrow.pack(fill="x", padx=10)
        ttk.Button(mrow, text="New", command=lambda: self.edit_macro(new=True)).pack(side="left", padx=4)
        ttk.Button(mrow, text="Edit", command=self.edit_macro).pack(side="left", padx=4)
        ttk.Button(mrow, text="Remove", command=self.remove_macro).pack(side="left", padx=4)
        ttk.Button(mrow, text="Enable/Disable", command=self.toggle_macro).pack(side="left", padx=4)
        ttk.Button(mrow, text="Record", command=self.record_macro).pack(side="left", padx=4)
        ttk.Button(mrow, text="Import AHK", command=self.import_ahk).pack(side="left", padx=4)
        ttk.Button(mrow, text="Export", command=self.export_macro).pack(side="left", padx=4)
        ttk.Button(mrow, text="Import", command=self.import_macro).pack(side="left", padx=4)

        # --- gate detection ---
        gd = self.core.cfg["gate_detection"]
        gf = ttk.LabelFrame(r, text="Auto-disable when the Gate typing UI is on screen")
        gf.pack(fill="x", padx=10, pady=6)

        self.gate_enabled_var = tk.BooleanVar(value=bool(gd.get("enabled", True)))
        ttk.Checkbutton(
            gf, text="Enable Gate spell UI detection (reads the 'Type the name of a gate' header)",
            variable=self.gate_enabled_var, command=self._save_gate_settings,
        ).grid(row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(6, 2))

        ttk.Button(gf, text="Check status",
                   command=self._test_detection).grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=4)
        ttk.Button(gf, text="Show last text read",
                   command=self._show_ocr_text).grid(row=1, column=2, columnspan=2, sticky="ew", padx=8, pady=4)

        ttk.Label(
            gf,
            text="No calibration needed. While Roblox is focused, the app reads the Gate header "
                 "('Gate / Type the name of a gate') at the top-centre of the game. It stays visible while "
                 "you type, so keybinds stay suspended until the Gate UI closes.",
            wraplength=560, justify="left",
        ).grid(row=2, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 2))

        self.gate_status_var = tk.StringVar(value="Idle.")
        ttk.Label(gf, textvariable=self.gate_status_var, wraplength=560).grid(
            row=3, column=0, columnspan=6, sticky="w", padx=8, pady=(2, 6))

        # --- options ---
        of = ttk.LabelFrame(r, text="Options")
        of.pack(fill="x", padx=10, pady=4)

        self.only_roblox_var = tk.BooleanVar(value=bool(self.core.cfg.get("only_when_roblox_focused", True)))
        ttk.Checkbutton(
            of, text="Only remap while Roblox is the focused window",
            variable=self.only_roblox_var, command=self._save_options,
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        self.on_top_var = tk.BooleanVar(value=bool(self.core.cfg.get("always_on_top", False)))
        ttk.Checkbutton(
            of, text="Always on top (stay visible above other windows)",
            variable=self.on_top_var, command=self._save_options,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 2))

        ttk.Label(of, text="Suspend hotkey:").grid(row=1, column=0, sticky="e", padx=(8, 0), pady=6)
        self.hotkey_var = tk.StringVar(value=self.core.cfg.get("suspend_hotkey", "f8"))
        ttk.Entry(of, textvariable=self.hotkey_var, width=10).grid(row=1, column=1, sticky="w")
        ttk.Button(of, text="Record", command=self._record_hotkey).grid(row=1, column=2, padx=4)
        ttk.Button(of, text="Apply", command=self._save_options).grid(row=1, column=3, padx=4)

        # --- status bar ---
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(r, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            fill="x", side="bottom", padx=0, pady=0)

        self._reload_tree()
        self._reload_macros()

    # -- tray ------------------------------------------------------------

    def _make_icon_image(self):
        img = Image.new("RGB", (64, 64), (24, 24, 28))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([6, 6, 58, 58], radius=12, fill=(70, 130, 200))
        d.rectangle([20, 20, 44, 44], outline=(255, 255, 255), width=3)
        d.line([32, 20, 32, 44], fill=(255, 255, 255), width=3)
        return img

    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.ui_queue.put(("show", None)), default=True),
            pystray.MenuItem(
                lambda item: "Resume remaps" if self.core.suspended else "Suspend remaps",
                self._tray_toggle_suspend,
            ),
            pystray.MenuItem("Exit", lambda: self.ui_queue.put(("quit", None))),
        )
        self.icon = pystray.Icon("rogue-keybinds", self._make_icon_image(), APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _tray_toggle_suspend(self):
        self.core.toggle_suspend()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    # -- queue / status polling ------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                cmd, _ = self.ui_queue.get_nowait()
                if cmd == "show":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                elif cmd == "quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        if self.core.running:
            self.root.after(100, self._poll_queue)

    def _refresh_status(self):
        state = self.core.status_text()
        gd = self.core.cfg["gate_detection"]
        extra = ""
        if gd.get("enabled"):
            if self.core.last_score_on is not None:
                extra = "  |  Gate typing UI: detected" if self.core.gate_active else "  |  Gate typing UI: not on screen"
            else:
                extra = "  |  Gate: Roblox window not found"
        self.status_var.set(f"Remaps: {state}{extra}")
        if self.icon is not None:
            try:
                self.icon.title = f"{APP_NAME} - {state}"
            except Exception:
                pass
        if self.core.running:
            self.root.after(300, self._refresh_status)

    # -- bindings table ----------------------------------------------------

    def _reload_tree(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for idx, b in enumerate(self.core.cfg["bindings"]):
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(b.get("src", ""), b.get("dst", ""), "yes" if b.get("enabled", True) else "no"),
            )

    def add_binding(self):
        def on_save(src, dst):
            self.core.cfg["bindings"].append({"src": src, "dst": dst, "enabled": True})
            save_config(self.core.cfg)
            self.core.mark_dirty()
            self._reload_tree()

        BindingDialog(self, on_save)

    def _selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (ValueError, IndexError):
            return None

    def remove_selected(self):
        idx = self._selected_index()
        if idx is None:
            return
        del self.core.cfg["bindings"][idx]
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._reload_tree()

    def toggle_selected(self):
        idx = self._selected_index()
        if idx is None:
            return
        b = self.core.cfg["bindings"][idx]
        b["enabled"] = not b.get("enabled", True)
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._reload_tree()

    # -- macros -----------------------------------------------------------

    def _reload_macros(self):
        for i in self.mtree.get_children():
            self.mtree.delete(i)
        for idx, m in enumerate(self.core.cfg.get("macros", [])):
            self.mtree.insert(
                "", "end", iid=str(idx),
                values=(m.get("name", ""), m.get("trigger", ""),
                        "yes" if m.get("enabled", True) else "no"),
            )

    def _macro_index(self):
        sel = self.mtree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (ValueError, IndexError):
            return None

    def edit_macro(self, new=False):
        idx = None if new else self._macro_index()
        if not new and idx is None:
            return
        macro = {"name": "", "trigger": "", "enabled": True, "script": ""} if new \
            else self.core.cfg["macros"][idx]
        MacroDialog(self, macro, lambda m: self._save_macro(m, idx))

    def _save_macro(self, macro, idx):
        self.core.cfg.setdefault("macros", [])
        if idx is None:
            self.core.cfg["macros"].append(macro)
        else:
            self.core.cfg["macros"][idx] = macro
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._reload_macros()

    def remove_macro(self):
        idx = self._macro_index()
        if idx is None:
            return
        del self.core.cfg["macros"][idx]
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._reload_macros()

    def toggle_macro(self):
        idx = self._macro_index()
        if idx is None:
            return
        m = self.core.cfg["macros"][idx]
        m["enabled"] = not m.get("enabled", True)
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._reload_macros()

    def record_macro(self):
        if not hasattr(self, "_recorder"):
            self._recorder = MacroRecorder(self)
        self._recorder.start()

    def import_ahk(self):
        path = filedialog.askopenfilename(
            title="Import AutoHotkey script",
            filetypes=[("AutoHotkey", "*.ahk"), ("Text", "*.txt"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            script = mengine.ahk_to_script(text)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self.root)
            return
        # report what actually converted so it's never silently empty
        converted = [ln for ln in script.splitlines() if ln.strip() and not ln.strip().startswith(";")]
        skipped = [ln for ln in script.splitlines() if ln.strip().startswith("; unsupported:")]
        if not converted:
            messagebox.showwarning(
                "Nothing converted",
                "No usable actions were found in that AHK script.\n\n"
                + (f"{len(skipped)} line(s) were not supported, e.g.:\n"
                   + "\n".join(s.replace('; unsupported: ', '') for s in skipped[:6])
                   if skipped else "The file had no recognizable commands."),
                parent=self.root,
            )
            return
        name = os.path.splitext(os.path.basename(path))[0]
        # pull the hotkey trigger the converter recorded (e.g. "; trigger: r")
        trigger = ""
        for ln in script.splitlines():
            if ln.strip().lower().startswith("; trigger:"):
                trigger = ln.split(":", 1)[1].strip().lower()
                break
        macro = {"name": name, "trigger": trigger, "enabled": True, "script": script}
        MacroDialog(self, macro, lambda m: self._save_macro(m, None))

    def export_macro(self):
        idx = self._macro_index()
        if idx is None:
            return
        m = self.core.cfg["macros"][idx]
        path = filedialog.asksaveasfilename(
            title="Export macro", defaultextension=".rlmacro",
            initialfile=(m.get("name") or "macro") + ".rlmacro",
            filetypes=[("RL Macro", "*.rlmacro"), ("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(m, f, indent=2)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.root)

    def import_macro(self):
        path = filedialog.askopenfilename(
            title="Import macro",
            filetypes=[("RL Macro", "*.rlmacro"), ("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                m = json.load(f)
            mengine.parse_script(m.get("script", ""))  # validate
            macro = {
                "name": m.get("name", "Imported"),
                "trigger": m.get("trigger", ""),
                "enabled": bool(m.get("enabled", True)),
                "script": m.get("script", ""),
            }
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self.root)
            return
        self.core.cfg.setdefault("macros", []).append(macro)
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._reload_macros()

    # -- gate detection settings ------------------------------------------

    def _save_gate_settings(self):
        gd = self.core.cfg["gate_detection"]
        gd["enabled"] = bool(self.gate_enabled_var.get())
        save_config(self.core.cfg)
        self.core.mark_dirty()

    def _thr_changed(self):
        pass  # detection needs no tuning

    def _show_ocr_text(self):
        text = self.core.last_ocr_text
        if not text:
            self.gate_status_var.set("No text read yet - focus Roblox with the Gate typing UI open, then check again.")
        else:
            self.gate_status_var.set(f"Last text read: '{' '.join(text.split())[:120]}'")

    def _test_detection(self):
        # Detection only runs while Roblox is focused, so report the live result.
        if self.core.gate_active:
            self.gate_status_var.set("Gate typing UI DETECTED - keybinds suspended.")
        else:
            self.gate_status_var.set(
                "Gate typing UI not detected. Detection only runs while Roblox is the focused window."
            )

    # -- options -----------------------------------------------------------

    def _save_options(self):
        self.core.cfg["only_when_roblox_focused"] = bool(self.only_roblox_var.get())
        self.core.cfg["suspend_hotkey"] = self.hotkey_var.get().strip().lower()
        self.core.cfg["always_on_top"] = bool(self.on_top_var.get())
        save_config(self.core.cfg)
        self.core.mark_dirty()
        self._apply_topmost()

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.core.cfg.get("always_on_top", False)))
        except Exception:
            pass

    def _record_hotkey(self):
        self.core.recording = True

        def worker():
            try:
                key = keyboard.read_key(suppress=True)
                self.hotkey_var.set(key)
                self.core.cfg["suspend_hotkey"] = key
                save_config(self.core.cfg)
                self.core.mark_dirty()
            except Exception:
                pass
            finally:
                self.core.recording = False

        threading.Thread(target=worker, daemon=True).start()

    # -- lifecycle ----------------------------------------------------------

    def _quit(self):
        self.core.running = False
        save_config(self.core.cfg)
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------

def already_running():
    """
    Single-instance guard via a named mutex. A leftover mutex from a crashed
    process must not block startup, so we verify a live RogueKeybinds process
    actually exists before refusing to launch.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW(None, False, "RogueKeybindsRemapperMutex")
        if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return False
        # Mutex exists - but only block if a live process is really running.
        import subprocess
        out = subprocess.run(
            ["tasklist", "/fi", "imagename eq RogueKeybinds.exe", "/nh"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "RogueKeybinds.exe" in out
    except Exception:
        return False


def main():
    if already_running():
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo(
                APP_NAME,
                f"{APP_NAME} is already running.\nCheck the system tray.",
            )
            r.destroy()
        except Exception:
            pass
        return
    cfg = load_config()
    core = RemapperCore(cfg)
    threading.Thread(target=core.monitor, daemon=True).start()
    app = App(core)
    app.run()
    core.running = False


if __name__ == "__main__":
    main()
