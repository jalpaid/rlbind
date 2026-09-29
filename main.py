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

Requires: keyboard, mss, pystray, Pillow, pytesseract + Tesseract-OCR
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
from tkinter import messagebox, ttk

import keyboard
import mss
import pystray
import pytesseract
from PIL import Image, ImageChops, ImageDraw, ImageFilter

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
