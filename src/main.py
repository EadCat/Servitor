"""Servitor - a dashboard that monitors NVIDIA GPUs on several servers over SSH."""

from __future__ import annotations

import collections
import json
import math
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import threading
import tkinter as tk
import uuid
from tkinter import filedialog, font as tkfont
from urllib.parse import unquote

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw, ImageFont

import collector
from collector import ServerCollector, forget_host_key, test_connection
from i18n import LANGUAGES, language, set_language, t

APP_NAME = "servitor"
POLL_MS = 200
# How long a resize must be still before names are cut to fit it. Long
# enough that dragging a window edge does not refit at every step of the drag.
SETTLE_MS = 150
# How long after a hidden server's first process list its table is built.
WARM_MS = 1000
HISTORY = 60
MIN_INTERVAL = 0.2
MAX_INTERVAL = 60.0
STAT_KEYS = ("mem_util", "fan", "pstate", "clock_core", "clock_mem", "power",
             "slow", "pcie", "ecc")
SIDEBAR_W = 268
# Wider than the sidebar: its scrollbar is always there, and sixteen GPUs'
# columns still have to leave the summary beside them whole.
MINI_W = 300
DRAG_SLACK = 5            # pixels of wobble a click may have before it drags
# A GPU counts as occupied on its own terms, never as a share of the server.
BUSY_MEM = 0.30
BUSY_UTIL = 10
# Both are replaced by the user's settings at startup.
TEMP_WARN, TEMP_DANGER = 80, 90
IDLE_MINUTES = 4          # holding memory at 0% this long is worth reporting
MAX_FAULTS = 3            # keep a card short; the rest collapses into a count
STALL_FACTOR = 5          # missed samples before a server counts as stalled
LOCAL_ID = "__local__"
UI_FONT = "Pretendard"    # bundled in assets/fonts; see THIRD-PARTY-NOTICES.md
# Wide enough that the process table's command column keeps a useful width.
DEFAULT_WINDOW = (1280, 800)
# Narrower, the value columns no longer fit beside the sidebar and the right
# edge of the view is cut off; the mini sidebar is the small view.
MIN_WINDOW = (960, 520)
# The "font size" setting names the body text size; everything else keeps its
# proportion to it. It is the user's to choose, so there is no upper limit.
FONT_BASE = 12
# In the order the close dialog and Settings offer them: quitting is what the
# X button plainly means, so it comes first and the tray last.
CLOSE_ACTIONS = ("quit", "mini", "tray")
# Alerts that find_faults reports; the rest come from the connection state.
GPU_KINDS = ("gone", "temp", "na", "idle", "slow", "power", "pcie", "ecc")
WARN_KINDS = ("idle", "reconnect", "power", "swap")
# Alerts about the server itself, from the CPU and memory reading.
HOST_KINDS = ("hostmem", "swap")
HOST_MEM_DANGER = 0.95    # share of RAM in use
SWAP_WARN_GB = 0.1        # less is the kernel tidying up, not memory pressure
REMIND_CAP_MIN = 8 * 60
TOAST_GAP = 60            # seconds between popups; what arrives meanwhile is merged
DOCK_DESKTOP_ID = "servitor.desktop"   # the name --install registers it under
ALERT_DEFAULTS = {
    "alert_enabled": True,
    "alert_taskbar": True, "alert_tray": True, "alert_popup": False,
    "alert_gone": True, "alert_temp": True, "alert_na": True, "alert_stalled": True,
    "alert_fatal": True, "alert_reconnect": True, "alert_idle": False,
    # A training job at full tilt sits on the power cap as a matter of course.
    "alert_slow": True, "alert_power": False, "alert_pcie": True, "alert_ecc": True,
    "alert_hostmem": True, "alert_swap": False,
    "temp_warn": 80, "temp_danger": 90, "alert_clear_temp": 80, "alert_clear_min": 2,
    "alert_reconnect_n": 10,
    "alert_remind": "grow", "alert_remind_min": 30,
    "alert_toast_timer": True, "alert_toast_sec": 3,
}
# Minimums only: the process column carries the grid weight and takes whatever
# space is left, so these must add up to less than a narrow window.
DEFAULT_PROC_WIDTHS = {
    "gpu": 44, "pid": 56, "user": 56, "container": 96, "type": 40, "name": 64,
    "sm": 60, "mem": 64, "cpu": 48, "rss": 60, "age": 70,
}

# PyInstaller unpacks bundled data files under sys._MEIPASS; in a source
# checkout the assets sit next to src/ instead.
ASSETS_DIR = os.path.join(
    getattr(sys, "_MEIPASS",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)),
    "assets",
)

# The VERSION file at the repository root, which the bundle carries alongside.
with open(os.path.join(ASSETS_DIR, os.pardir, "VERSION"), encoding="utf-8") as _fp:
    APP_VERSION = _fp.read().strip()

ACCENT = "#76b900"          # NVIDIA green
ACCENT_HOVER = "#5e9400"
ACCENT_TEXT = ("#4a7a00", "#9ad62e")   # readable as text in either theme
BUTTON_TEXT = "#0d1a00"     # dark text: white on #76b900 is only 2.4:1
OK = "#76b900"
WARN = "#f59e0b"
DANGER = "#ef4444"

TEXT_MUTED = ("#6b7280", "#8b8f98")
PANEL_BG = ("#ffffff", "#1c1d21")
BORDER = ("#e5e7eb", "#2b2d33")
INNER_BG = ("#f4f5f7", "#232529")
TRACK = ("#e5e7eb", "#33363d")
# Tinted backgrounds for the issue chips, the warning colours at low strength.
CHIP_DANGER = ("#fde4e4", "#3a2024")
CHIP_WARN = ("#fdf0d8", "#3a2e17")
CHIP_ON = ("#e8f5d0", "#26331a")       # a toggle that is on, tinted with the accent
TAB_ACTIVE = ("#e8eaee", "#2b2d33")

STATUS_COLORS = {
    "connected": OK,
    "connecting": WARN,
    "retrying": WARN,
    "auth_failed": DANGER,
    "error": DANGER,
    "offline": DANGER,
    "stalled": DANGER,
}


# --- config ----------------------------------------------------------------


def config_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return os.path.join(base, APP_NAME, "config.json")


collector.host_keys_path = os.path.join(os.path.dirname(config_path()), "known_hosts")


def local_server() -> dict:
    """The always-present entry for the machine the app runs on."""
    return {"id": LOCAL_ID, "name": "", "local": True}


def display_name(server: dict) -> str:
    return t("local_machine") if server.get("local") else server["name"]


def load_config() -> dict:
    cfg = {
        "interval_sec": 2.0,
        "appearance": "dark",
        "language": "en",
        "servers": [local_server()],
        "open": [LOCAL_ID],
        "offline": [],
        "window": list(DEFAULT_WINDOW),
        "sidebar_side": "left",
        "col_widths": dict(DEFAULT_PROC_WIDTHS),
        "close_action": "ask",
        "font_size": FONT_BASE,
        "host_stats": True,
        "mini_topmost": True,
        **ALERT_DEFAULTS,
    }
    try:
        with open(config_path(), encoding="utf-8") as fp:
            cfg.update(json.load(fp))
    except (OSError, ValueError):
        return cfg
    for server in cfg["servers"]:
        server.setdefault("id", uuid.uuid4().hex)
    if not any(s.get("id") == LOCAL_ID for s in cfg["servers"]):
        cfg["servers"].insert(0, local_server())
    known = {s["id"] for s in cfg["servers"]}
    cfg["open"] = [sid for sid in cfg.get("open", []) if sid in known]
    # Paused servers are a subset of the open ones, so a card closed while
    # paused cannot come back paused later with nothing on screen to say why.
    cfg["offline"] = [sid for sid in cfg.get("offline", []) if sid in cfg["open"]]
    if cfg.get("sidebar_side") not in ("left", "right"):
        cfg["sidebar_side"] = "left"
    if cfg.get("close_action") not in ("ask",) + CLOSE_ACTIONS:
        cfg["close_action"] = "ask"
    for key, value in ALERT_DEFAULTS.items():
        if type(cfg.get(key)) is not type(value):
            cfg[key] = value
    if not isinstance(cfg.get("host_stats"), bool):
        cfg["host_stats"] = True
    if not isinstance(cfg.get("mini_topmost"), bool):
        cfg["mini_topmost"] = True
    if not isinstance(cfg.get("font_size"), int) or cfg["font_size"] < 1:
        cfg["font_size"] = FONT_BASE
    if cfg["alert_remind"] not in ("grow", "fixed", "off"):
        cfg["alert_remind"] = "grow"
    widths = dict(DEFAULT_PROC_WIDTHS)
    # Renamed from proc_widths when the columns were narrowed and reordered,
    # so widths dragged for the old layout start over rather than overflow.
    cfg.pop("proc_widths", None)
    widths.update({k: v for k, v in (cfg.get("col_widths") or {}).items()
                   if k in DEFAULT_PROC_WIDTHS and isinstance(v, int)})
    cfg["col_widths"] = widths
    return cfg


def save_config(cfg: dict):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
    if sys.platform != "win32":
        # The file holds plaintext passwords, so restrict it to the owner.
        os.chmod(path, 0o600)


# --- color helpers ----------------------------------------------------------


def mode_color(color):
    if isinstance(color, (tuple, list)):
        return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]
    return color


def rgb(color) -> tuple:
    value = mode_color(color)
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def hex_row(pixels) -> str:
    return "{" + " ".join("#%02x%02x%02x" % p for p in pixels) + "}"


# tkinter's Canvas has no antialiasing, so arcs and lines come out jagged.
# Painting the pixels ourselves and handing them over as one PhotoImage costs
# about a millisecond and looks smooth; results are cached because a rounded
# percentage only has a hundred or so distinct states.
_IMAGE_CACHE = {}


def ring_image(size, frac, fill, track, back, ink):
    # Key on the resolved colours, not the palette entries: one entry is two
    # different colours, one per appearance mode, so keying on the entry served
    # light-mode rings back in dark mode.
    fill_c, track_c, back_c, ink_c = rgb(fill), rgb(track), rgb(back), rgb(ink)
    key = ("ring", size, round(frac * 100), fill_c, track_c, back_c, ink_c)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    centre = size / 2.0
    # Proportions of the original 44 px ring, so a scaled one keeps its look.
    outer, inner = size / 2.0 - size * 3 / 44, size / 2.0 - size * 8 / 44
    mid, half = (outer + inner) / 2.0, (outer - inner) / 2.0
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = x + 0.5 - centre, y + 0.5 - centre
            cover = half + 0.5 - abs(math.hypot(dx, dy) - mid)
            if cover <= 0:
                row.append(back_c)
                continue
            cover = min(1.0, cover)
            angle = (math.degrees(math.atan2(-dy, dx)) - 90) % 360
            edge = fill_c if (360 - angle) / 360 <= frac else track_c
            row.append(tuple(round(e * cover + b * (1 - cover))
                             for e, b in zip(edge, back_c)))
        rows.extend(row)
    # The percentage is drawn into the image rather than laid over it as label
    # text: a label centres the font's line box, ascender and descender
    # included, which leaves digits sitting off the middle. Here the digits'
    # own outline is centred, shrunk if needed to clear the ring.
    canvas = Image.new("RGB", (size, size))
    canvas.putdata(rows)
    draw = ImageDraw.Draw(canvas)
    text = f"{frac * 100:.0f}%"
    room = (inner - size * 2 / 44) * 2
    px = round(size * 9 / 44)
    while True:
        face = ImageFont.truetype(os.path.join(ASSETS_DIR, "fonts", "Pretendard-Bold.otf"), px)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
        if right - left <= room or px <= 6:
            break
        px -= 1
    draw.text((centre - (left + right) / 2, centre - (top + bottom) / 2), text,
              font=face, fill=ink_c)
    # From the raw bytes: getdata() is deprecated and goes in Pillow 14.
    raw = canvas.tobytes()
    pixels = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    image = tk.PhotoImage(width=size, height=size)
    image.put(" ".join(hex_row(pixels[y * size:(y + 1) * size]) for y in range(size)))
    _IMAGE_CACHE[key] = image
    return image


def spark_image(width, height, values, line, back):
    """Antialiased sparkline: one interpolated y per column, feathered."""
    line_c, back_c = rgb(line), rgb(back)
    area_c = tuple(round(l * 0.22 + b * 0.78) for l, b in zip(line_c, back_c))
    pad = 4.0
    span = max(1, len(values) - 1)
    step = (width - 1) / span
    heights = []
    for x in range(width):
        pos = min(span, x / step)
        low = int(pos)
        high = min(span, low + 1)
        blend_t = pos - low
        value = values[low] * (1 - blend_t) + values[high] * blend_t
        heights.append(height - pad - (value / 100.0) * (height - 2 * pad))
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            top = heights[x]
            distance = abs(y + 0.5 - top)
            if distance <= 1.6:                      # the stroke itself
                cover = min(1.0, 1.6 - distance)
                row.append(tuple(round(l * cover + a * (1 - cover))
                                 for l, a in zip(line_c, area_c if y + 0.5 > top else back_c)))
            elif y + 0.5 > top:                      # filled area below
                row.append(area_c)
            else:
                row.append(back_c)
        rows.append(hex_row(row))
    image = tk.PhotoImage(width=width, height=height)
    image.put(" ".join(rows))
    return image


def load_color(value) -> str:
    if value is None or value < 50:
        return ACCENT
    return WARN if value < 85 else DANGER


_FONTS = {}
_font_size = FONT_BASE


def _font_px(size):
    return max(1, round(size * _font_size / FONT_BASE))


def font(size: int, weight: str = "normal"):
    """A font shared by every widget asking for this size, so the font size
    setting can resize them all at once: CustomTkinter redraws a widget when
    the CTkFont it was given is reconfigured."""
    key = (size, weight)
    if key not in _FONTS:
        _FONTS[key] = ctk.CTkFont(size=_font_px(size), weight=weight)
    return _FONTS[key]


def set_font_size(size):
    global _font_size
    _font_size = size
    for (base, _weight), shared in _FONTS.items():
        shared.configure(size=_font_px(base))


def scaled(widget, pixels):
    """Pixels for a plain Tk widget, which CustomTkinter does not scale itself.

    The factor is the text size setting times, on Windows, the display scaling.
    """
    return max(1, round(pixels * ctk.ScalingTracker.get_widget_scaling(widget)))


def tk_font(widget, size, weight="normal"):
    """A font for a plain Tk widget, sized the way CustomTkinter sizes its own."""
    return (ctk.ThemeManager.theme["CTkFont"]["family"], -scaled(widget, _font_px(size)),
            weight)


_MEASURE = {}


def measure_text(widget, size, weight="normal"):
    """The width-in-pixels function of a tk_font, made once per font.

    Building a tkfont.Font is the expensive part; one per measurement made
    the first showing of a server freeze for most of a second.
    """
    spec = tk_font(widget, size, weight)
    if spec not in _MEASURE:
        raw = tkfont.Font(root=widget, font=spec).measure
        widths = {}

        # Each measurement is a round trip to Tk costing about a millisecond,
        # and a window being resized asks for the same strings over and over.
        def measure(text):
            if text not in widths:
                if len(widths) > 20000:
                    widths.clear()
                widths[text] = raw(text)
            return widths[text]

        _MEASURE[spec] = measure
    return _MEASURE[spec]


def fit_text(text, width, measure, keep=0):
    """text, or as much of it as fits in width followed by an ellipsis.

    Starts where the text's average character width says the cut falls and
    steps from there: two or three measurements rather than one per
    character, which on a first showing added up to a visible freeze.
    """
    full = measure(text)
    if full <= width:
        return text
    cut = max(keep, min(len(text) - 1, int(len(text) * width / full)))
    while cut > keep and measure(text[:cut] + "\u2026") > width:
        cut -= 1
    while cut + 1 < len(text) and measure(text[:cut + 1] + "\u2026") <= width:
        cut += 1
    return text[:cut].rstrip() + "\u2026"


def fmt_gb(mib):
    return "—" if mib is None else f"{mib / 1024:.1f}"


def fmt_pct(value):
    return "—" if value is None else f"{value:.0f}%"


def fmt_mhz(value):
    return "—" if value is None else f"{value:.0f} MHz"


def load_text_color(value):
    # ACCENT is tuned for fills; as text on a white panel it is too light.
    if value is None or value < 50:
        return ACCENT_TEXT
    return WARN if value < 85 else DANGER


def temp_color(celsius):
    if celsius is None:
        return TEXT_MUTED
    if celsius >= TEMP_DANGER:
        return DANGER
    if celsius >= TEMP_WARN:
        return WARN
    return TEXT_MUTED


def is_busy(gpu) -> bool:
    """Occupied if it holds memory or is computing, judged per GPU."""
    used, total = gpu.get("mem_used"), gpu.get("mem_total")
    if used is not None and total:
        if used / total >= BUSY_MEM:
            return True
    return (gpu.get("util") or 0) >= BUSY_UTIL


# clocks_throttle_reasons bits, from nvml.h. Idle, application clocks and
# sync boost are left out: they are the GPU working as configured.
_SLOW_BITS = (
    (0x40, "issue_hw_thermal"),
    (0x80, "issue_brake"),
    (0x20, "issue_sw_thermal"),
    (0x08, "issue_hw"),        # the umbrella of 0x40 and 0x80; named alone only
)
_POWER_CAP_BIT = 0x04


def gpu_issues(gpu):
    """What the GPU reports wrong with itself: [(severity, kind, text)], worst first.

    Read from the driver's own flags and counters, not inferred from
    temperature or power. Missing fields (an older driver) mean no issue.
    """
    issues = []
    bits = gpu.get("throttle") or 0
    for bit, key in _SLOW_BITS:
        if bits & bit and not (bit == 0x08 and bits & 0xC0):
            issues.append(("danger", "slow", t(key)))
    width, width_max = gpu.get("pcie_width"), gpu.get("pcie_width_max")
    # Only the width counts: the link generation drops on its own when idle.
    if width and width_max and width < width_max:
        issues.append(("danger", "pcie", t("issue_pcie", cur=f"{width:.0f}",
                                              max=f"{width_max:.0f}")))
    if gpu.get("mem_repair"):
        issues.append(("danger", "ecc", t("issue_repair")))
    elif gpu.get("ecc") and gpu.get("ecc_errors"):
        issues.append(("danger", "ecc", t("issue_ecc", n=f"{gpu['ecc_errors']:.0f}")))
    if bits & _POWER_CAP_BIT:
        issues.append(("warn", "power", t("issue_power")))
    return issues


def host_issues(host):
    """[(severity, kind, text)] for the server itself, from a host reading."""
    issues = []
    used, total = host.get("mem_used"), host.get("mem_total")
    if used is not None and total and used / total >= HOST_MEM_DANGER:
        issues.append(("danger", "hostmem", t("issue_hostmem", pct=f"{used / total * 100:.0f}")))
    if (host.get("swap_used") or 0) >= SWAP_WARN_GB:
        issues.append(("warn", "swap", t("issue_swap")))
    return issues


def find_faults(gpus, procs, idle_since, now, gone=()):
    """Return the card's [(severity, i18n key, args)], the same list uncut, and
    the updated idle bookkeeping.

    Only compute processes count towards the idle check: graphics ones sit on
    memory at low utilization all the time, which would flag every desktop.
    Alerts read the uncut list: a fault pushed into "N more" has not gone away.
    """
    faults = [("danger", "fault_gone", {"i": index}) for index in gone]
    fresh = {}
    limit = MAX_FAULTS
    # Hitting the power cap is how a busy GPU normally runs, so it may raise an
    # alert if asked for but never takes one of the card's few lines.
    quiet = []
    compute_mem = {}
    for proc in procs:
        if proc.get("type") == "C":
            compute_mem[proc["gpu"]] = compute_mem.get(proc["gpu"], 0) + (proc["mem"] or 0)

    for gpu in gpus:
        index = gpu["index"]
        temp = gpu.get("temp")
        if gpu.get("util") is None and temp is None:
            faults.append(("danger", "fault_na", {"i": index}))
            continue
        # Only the dangerous band gets its own line: the card header already
        # shows the hottest GPU in amber once anything crosses TEMP_WARN, and a
        # line per GPU turned an eight-card server into a wall of warnings.
        if temp is not None and temp >= TEMP_DANGER:
            faults.append(("danger", "fault_temp", {"i": index, "temp": f"{temp:.0f}"}))
        for severity, kind, text in gpu_issues(gpu):
            (quiet if kind == "power" else faults).append(
                (severity, "fault_" + kind, {"i": index, "what": text}))

        held = compute_mem.get(index, 0)
        if held > 0 and (gpu.get("util") or 0) < 1:
            started = idle_since.get(index, now)
            fresh[index] = started
            minutes = (now - started) / 60.0
            if minutes >= IDLE_MINUTES:
                faults.append(("warn", "fault_idle", {
                    "i": index, "mem": f"{held / 1024:.1f}", "mins": int(minutes)}))
    # Danger first, hottest first, and never more than a few lines; the rest is
    # summarised. By index, a cooler GPU 0 pushed the hottest one out of sight.
    faults.sort(key=lambda f: (0 if f[0] == "danger" else 1,
                               0 if f[1] == "fault_gone" else 1,
                               -float(f[2]["temp"]) if f[1] == "fault_temp" else 0))
    full = faults + quiet
    if len(faults) > limit:
        hidden = len(faults) - limit
        faults = faults[:limit] + [("warn", "fault_more", {"n": hidden})]
    return faults, full, fresh


# --- alert indicators -------------------------------------------------------


def tray_image(severity):
    """The app icon, with a dot in the corner while an alert is unseen."""
    image = Image.open(os.path.join(ASSETS_DIR, "icon.png")).convert("RGBA").resize((64, 64))
    if severity:
        ImageDraw.Draw(image).ellipse((34, 34, 63, 63), outline="#ffffff", width=3,
                                      fill=DANGER if severity == "danger" else WARN)
    return image


_TASKBAR = None


def windows_taskbar(hwnd, severity):
    """Fill the taskbar button yellow for a warning, red for danger, or clear it.

    ITaskbarList3 is a COM interface, called through its vtable with ctypes so
    that no extra package is needed.
    """
    global _TASKBAR
    import ctypes
    from ctypes import wintypes
    if _TASKBAR is None:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        clsid, iid = (ctypes.c_byte * 16)(), (ctypes.c_byte * 16)()
        ole32.CLSIDFromString("{56FDF344-FD6D-11d0-958A-006097C9A090}", ctypes.byref(clsid))
        ole32.CLSIDFromString("{ea1afb91-9e28-4b86-90e9-9e9f8a5eee6f}", ctypes.byref(iid))
        ptr = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid),
                                  ctypes.byref(ptr)):
            raise OSError("ITaskbarList3 is unavailable")
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtable[3])(ptr)   # HrInit
        _TASKBAR = (
            ptr,
            ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND,
                               ctypes.c_ulonglong, ctypes.c_ulonglong)(vtable[9]),
            ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND,
                               ctypes.c_int)(vtable[10]),
        )
    ptr, set_value, set_state = _TASKBAR
    # TBPF_PAUSED draws yellow, TBPF_ERROR red; a full bar makes the colour show.
    set_state(ptr, hwnd, {"warn": 8, "danger": 4}.get(severity, 0))
    if severity:
        set_value(ptr, hwnd, 100, 100)


def dbus_message(serial, kind, fields, update=None):
    """Marshal one D-Bus message: a method call (1) or a signal (4).

    Only what Servitor sends is supported: Hello, and the Unity LauncherEntry
    Update signal whose body is (s a{sv}) with a count and two flags.
    """
    buf = bytearray(b"l" + bytes([kind, 0, 1]) + bytes(8))

    def pad(n):
        buf.extend(bytes(-len(buf) % n))

    def u32(value):
        pad(4)
        buf.extend(struct.pack("<I", value))

    def text(value):
        data = value.encode()
        u32(len(data))
        buf.extend(data + b"\0")

    def sig(value):
        buf.append(len(value))
        buf.extend(value.encode() + b"\0")

    def array(entries, write):
        u32(0)
        at = len(buf) - 4
        pad(8)
        start = len(buf)
        for entry in entries:
            pad(8)
            write(*entry)
        struct.pack_into("<I", buf, at, len(buf) - start)

    def field(code, kind_sig, value):
        buf.append(code)
        sig(kind_sig)
        text(value) if kind_sig in "so" else sig(value)

    if update:
        fields = fields + [(8, "g", "sa{sv}")]
    array(fields, field)
    pad(8)
    body = len(buf)
    if update:
        uri, count, urgent = update

        def prop(name, code, value):
            text(name)
            sig(code)
            if code == "x":
                pad(8)
                buf.extend(struct.pack("<q", value))
            else:
                u32(int(value))

        text(uri)
        array([("count", "x", count), ("count-visible", "b", count > 0),
               ("urgent", "b", urgent)], prop)
    struct.pack_into("<II", buf, 4, len(buf) - body, serial)
    return bytes(buf)


def dbus_connect():
    """Open the session bus and say Hello, or return None if there is none.

    The dock drops a launcher badge the moment its sender leaves the bus, so
    the connection has to stay open for as long as the badge should show.
    """
    for part in os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        transport, _, params = part.partition(":")
        opts = dict(p.split("=", 1) for p in params.split(",") if "=" in p)
        if transport != "unix" or not ({"path", "abstract"} & opts.keys()):
            continue
        target = (unquote(opts["path"]) if "path" in opts
                  else "\0" + unquote(opts["abstract"]))
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(target)
            sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode()
                         + b"\r\n")
            if not sock.recv(256).startswith(b"OK"):
                raise OSError("D-Bus refused the login")
            sock.sendall(b"BEGIN\r\n" + dbus_message(1, 1, [
                (1, "o", "/org/freedesktop/DBus"), (2, "s", "org.freedesktop.DBus"),
                (3, "s", "Hello"), (6, "s", "org.freedesktop.DBus")]))
        except OSError:
            sock.close()
            continue
        return sock
    return None


# --- widgets ----------------------------------------------------------------


class Sparkline(tk.Canvas):
    """A small graph of recent utilization history."""

    def __init__(self, master, width=190, height=44):
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0)
        # Not _w/_h: tkinter.Misc already uses _w for the widget's Tcl path name.
        self._px_w, self._px_h = width, height
        self._image = None
        self.values = collections.deque(maxlen=HISTORY)
        self.refresh_theme()

    def push(self, value):
        # Only recorded: GpuRow draws when the graph is on screen. Drawing every
        # sample, mostly into folded rows and other servers' views, was a
        # quarter of each refresh once several servers were open.
        self.values.append(min(100.0, max(0.0, value or 0.0)))

    def refresh_theme(self):
        self.configure(bg=mode_color(INNER_BG))
        self._draw()

    def _draw(self):
        self.delete("all")
        width, height = scaled(self, self._px_w), scaled(self, self._px_h)
        self.configure(width=width, height=height)
        if len(self.values) < 2:
            return
        self._image = spark_image(width, height, list(self.values),
                                  load_color(self.values[-1]), INNER_BG)
        self.create_image(0, 0, anchor="nw", image=self._image)


class Donut(tk.Label):
    """Ring showing how much of a server's total VRAM is taken."""

    def __init__(self, master, size=44):
        super().__init__(master, bd=0, highlightthickness=0)
        self._size = size
        self._frac = 0.0
        self._bg = INNER_BG
        self._image = None
        self.refresh_theme()

    def set_backdrop(self, color):
        self._bg = color
        self.refresh_theme()

    def set_fraction(self, frac):
        self._frac = max(0.0, min(1.0, frac))
        self._draw()

    def refresh_theme(self):
        self.configure(bg=mode_color(self._bg))
        self._draw()

    def _draw(self):
        # Keep a reference: Tk drops images it does not see referenced.
        self._image = ring_image(scaled(self, self._size), self._frac,
                                 load_color(self._frac * 100), TRACK, self._bg, TEXT_MUTED)
        self.configure(image=self._image)


class ServerCard(ctk.CTkFrame):
    """Sidebar card: one server at a glance, expandable to its GPUs."""

    def __init__(self, master, server, on_select, on_close, on_drag, on_drop):
        super().__init__(master, fg_color=INNER_BG, corner_radius=10,
                         border_width=1, border_color=BORDER)
        self.server = server
        self.on_select = on_select
        self.on_drag = on_drag
        self.on_drop = on_drop
        self._press_y = 0
        self._grab_dy = 0
        self._pressed = False
        self._dragging = False
        self._gpus = []
        self._faults = []
        self._expanded = False
        self._rows = []
        self._fault_lines = []
        self._layout = None
        self.grid_columnconfigure(1, weight=1)

        self.dot = ctk.CTkLabel(self, text="●", font=font(10), text_color=WARN, width=8)
        self.dot.grid(row=0, column=0, padx=(10, 5), pady=(9, 0), sticky="n")
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=1, sticky="ew", pady=(7, 0))
        self.name = ctk.CTkLabel(head, text=display_name(server), font=font(13, "bold"),
                                 anchor="w")
        self.name.grid(row=0, column=0, sticky="w")
        self._name_color = self.name.cget("text_color")
        self.summary = ctk.CTkLabel(head, text="", font=font(10), text_color=TEXT_MUTED,
                                    anchor="w")
        self.summary.grid(row=1, column=0, sticky="w", pady=(1, 8))
        self.host_line = ctk.CTkLabel(head, text="", font=font(10), text_color=TEXT_MUTED,
                                      anchor="w")

        # Close sits in the corner with the donut tucked under it, both flush
        # to the right edge. The button stays put whether or not the card is
        # selected, so the donut never shifts.
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, sticky="ne", padx=(4, 7), pady=(5, 0))
        self.close_button = ctk.CTkButton(
            right, text="✕", width=18, height=18, font=font(10), fg_color="transparent",
            hover_color=TRACK, text_color=TEXT_MUTED, command=on_close,
        )
        self.close_button.grid(row=0, column=0, sticky="e")
        self.donut = Donut(right)
        self.donut.grid(row=1, column=0, pady=(1, 0))
        self.vram_label = ctk.CTkLabel(right, text=t("vram_label"), font=font(9),
                                       text_color=TEXT_MUTED)
        self.vram_label.grid(row=2, column=0, pady=(1, 6))
        self.faults_box = ctk.CTkFrame(self, fg_color="transparent")
        self.toggle = ctk.CTkButton(
            self, text="", height=22, font=font(10), anchor="w", fg_color="transparent",
            hover_color=TRACK, text_color=TEXT_MUTED, command=self._toggle,
        )
        self.gpu_box = ctk.CTkFrame(self, fg_color=TRACK, corner_radius=6)
        self.gpu_box.grid_columnconfigure(1, weight=1)

        self._make_draggable(self, head, right, self.name, self.summary, self.dot,
                             self.donut, self.vram_label, self.faults_box, self.gpu_box)

    # --- drag to reorder ---

    def _press(self, event):
        self._press_y = event.y_root
        # Where the card was taken hold of, so it hangs under the pointer at the
        # spot it was grabbed rather than jumping its top edge to the cursor.
        self._grab_dy = event.y_root - self.winfo_rooty()
        self._pressed = True
        self._dragging = False

    def _motion(self, event):
        if not self._pressed:
            return
        if not self._dragging and abs(event.y_root - self._press_y) < DRAG_SLACK:
            return
        self._dragging = True
        self.on_drag(event.y_root, self._grab_dy)

    def _release(self, _event):
        if not self._pressed:
            return
        self._pressed = False
        if self._dragging:
            self._dragging = False
            self.on_drop()
        else:
            self.on_select()

    def _make_draggable(self, *widgets):
        """Let these parts of the card carry the whole card.

        Everything on a card drags it except the two buttons that have a job of
        their own, so a widget the cursor promises is draggable really is one.
        """
        for widget in widgets:
            widget.configure(cursor="fleur")
            widget.bind("<Button-1>", self._press)
            widget.bind("<B1-Motion>", self._motion)
            widget.bind("<ButtonRelease-1>", self._release)

    # --- layout ---

    def _relayout(self):
        # Every sample calls this. grid_forget unmaps, so re-gridding an
        # unchanged layout blanked the GPU toggle for a frame each time.
        layout = (bool(self._faults), bool(self._gpus), self._expanded)
        if layout == self._layout:
            return
        self._layout = layout
        row = 1
        self.faults_box.grid_forget(); self.toggle.grid_forget(); self.gpu_box.grid_forget()
        if self._faults:
            self.faults_box.grid(row=row, column=0, columnspan=3, sticky="ew",
                                 padx=8, pady=(0, 2))
            row += 1
        if self._gpus:
            self.toggle.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8,
                             pady=(3, 6 if not self._expanded else 3))
            row += 1
            if self._expanded:
                self.gpu_box.grid(row=row, column=0, columnspan=3, sticky="ew",
                                  padx=8, pady=(0, 8))

    def _toggle(self):
        self._expanded = not self._expanded
        self.toggle.configure(text=t("gpu_collapse") if self._expanded
                              else t("gpu_expand", n=len(self._gpus)))
        if self._expanded:
            self._fill_gpus(self._gpus)
        self._relayout()

    def set_carried(self, carried):
        """Fade into a slot while a copy of the card is being carried.

        Colour only: hiding anything would unmap it, and the window holding the
        pointer grab is somewhere in here.
        """
        self.name.configure(text_color=TEXT_MUTED if carried else self._name_color)
        if carried:
            self.configure(fg_color=PANEL_BG, border_color=BORDER)

    def set_active(self, active):
        self.configure(fg_color=TAB_ACTIVE if active else INNER_BG,
                       border_color=ACCENT if active else BORDER)
        self.donut.set_backdrop(TAB_ACTIVE if active else INNER_BG)

    # --- data ---

    def set_status(self, status, detail_key="", detail_args=None):
        self.dot.configure(text_color=STATUS_COLORS.get(status, WARN))
        if status not in ("connected", "offline"):
            self._gpus = []
            self._faults = []
            text = t("status_" + status) if status != "stalled" else t("status_stalled")
            if detail_key:
                text = t(detail_key, **(detail_args or {}))
            self.summary.configure(text=text, text_color=STATUS_COLORS.get(status, WARN))
            self.donut.grid_remove(); self.vram_label.grid_remove()
            self.set_host(None)
            self._relayout()

    def set_host(self, host):
        """One line of the server's CPU and memory under the summary, or none."""
        if host is None:
            self.host_line.grid_remove()
            self.summary.grid_configure(pady=(1, 8))
            return
        cpu = "—" if host["cpu"] is None else f"{host['cpu']:.0f}%"
        ram = host["mem_used"] / host["mem_total"] * 100 if host["mem_total"] else 0
        self.host_line.configure(text=t("card_host", cpu=cpu, ram=f"{ram:.0f}"),
                                 text_color=DANGER if ram >= HOST_MEM_DANGER * 100 else TEXT_MUTED)
        if not self.host_line.winfo_manager():
            self.summary.grid_configure(pady=(1, 0))
            self.host_line.grid(row=2, column=0, sticky="w", pady=(0, 8))
            self._make_draggable(self.host_line)

    def update_gpus(self, gpus, faults):
        self._gpus, self._faults = gpus, faults
        self.donut.grid(); self.vram_label.grid()

        temps = [g["temp"] for g in gpus if g["temp"] is not None]
        busy = sum(1 for g in gpus if is_busy(g))
        parts = [t("card_summary", busy=busy, total=len(gpus))]
        if temps:
            parts.append(f"{t('card_avg')} {sum(temps)/len(temps):.0f}°")
            parts.append(f"{t('card_max')} {max(temps):.0f}°")
        self.summary.configure(text="  ".join(parts), text_color=TEXT_MUTED)

        used = sum(g["mem_used"] or 0 for g in gpus)
        total = sum(g["mem_total"] or 0 for g in gpus)
        self.donut.set_fraction(used / total if total else 0)

        # Relabel the lines already there; they are rebuilt only when their
        # number changes. Destroying and recreating them every sample left the
        # space blank for a frame, so a standing warning blinked every refresh.
        if len(self._fault_lines) != len(faults):
            for child in self.faults_box.winfo_children():
                child.destroy()
            self._fault_lines = []
            self.faults_box.grid_columnconfigure(0, weight=1)
            for i in range(len(faults)):
                line = ctk.CTkFrame(self.faults_box, fg_color=TRACK, corner_radius=6)
                line.grid(row=i, column=0, sticky="ew", pady=(3, 0))
                line.grid_columnconfigure(1, weight=1)
                icon = ctk.CTkLabel(line, text="", font=font(10))
                icon.grid(row=0, column=0, padx=(7, 4), pady=4)
                note = ctk.CTkLabel(line, text="", font=font(10), justify="left",
                                    wraplength=SIDEBAR_W - 92, anchor="w")
                note.grid(row=0, column=1, sticky="w", pady=4)
                self._make_draggable(line, icon, note)
                self._fault_lines.append((icon, note))
        for (icon, note), (severity, key, args) in zip(self._fault_lines, faults):
            color = DANGER if severity == "danger" else WARN
            icon.configure(text="▲" if severity == "danger" else "⚠", text_color=color)
            note.configure(text=t(key, **args),
                           text_color=color if severity == "danger" else self._name_color)

        if not self._expanded:
            self.toggle.configure(text=t("gpu_expand", n=len(gpus)))
        else:                     # folded, the rows are out of sight; _toggle fills them
            self._fill_gpus(gpus)
        self._relayout()

    def _fill_gpus(self, gpus):
        if len(self._rows) != len(gpus):
            for child in self.gpu_box.winfo_children():
                child.destroy()
            self._rows = []
            for i in range(len(gpus)):
                cells = {}
                cells["i"] = ctk.CTkLabel(self.gpu_box, text="", font=font(10), width=10,
                                          text_color=TEXT_MUTED)
                cells["i"].grid(row=i, column=0, padx=(8, 4), pady=1)
                cells["bar"] = ctk.CTkProgressBar(self.gpu_box, width=46, height=6,
                                                  corner_radius=3, fg_color=PANEL_BG)
                cells["bar"].grid(row=i, column=1, sticky="w")
                cells["util"] = ctk.CTkLabel(self.gpu_box, text="", font=font(10),
                                             width=30, anchor="e")
                cells["util"].grid(row=i, column=2, padx=(4, 0))
                cells["mem"] = ctk.CTkLabel(self.gpu_box, text="", font=font(10), width=38,
                                            anchor="e", text_color=TEXT_MUTED)
                cells["mem"].grid(row=i, column=3)
                cells["temp"] = ctk.CTkLabel(self.gpu_box, text="", font=font(10), width=26,
                                             anchor="e")
                cells["temp"].grid(row=i, column=4, padx=(2, 8))
                self._make_draggable(*cells.values())
                self._rows.append(cells)
        for cells, gpu in zip(self._rows, gpus):
            util = gpu["util"] or 0
            cells["i"].configure(text=gpu["index"])
            cells["bar"].configure(progress_color=load_color(util)); cells["bar"].set(util / 100)
            cells["util"].configure(text=f"{util:.0f}%", text_color=load_text_color(util))
            cells["mem"].configure(text=f"{(gpu['mem_used'] or 0)/1024:.1f}G")
            cells["temp"].configure(text="—" if gpu["temp"] is None else f"{gpu['temp']:.0f}°",
                                    text_color=temp_color(gpu["temp"]))

    def retranslate(self):
        self.name.configure(text=display_name(self.server))
        self.vram_label.configure(text=t("vram_label"))
        self.toggle.configure(text=t("gpu_collapse") if self._expanded
                              else t("gpu_expand", n=len(self._gpus)))
        if self._gpus:
            self.update_gpus(self._gpus, self._faults)

    def refresh_theme(self):
        self.donut.refresh_theme()


class GpuRow(ctk.CTkFrame):
    """One GPU: a single summary line that expands into meters and history."""

    def __init__(self, master):
        super().__init__(master, fg_color=INNER_BG, corner_radius=10)
        self._open = False
        self._span = 0
        self.grid_columnconfigure(1, weight=1)

        self.toggle = ctk.CTkButton(
            self, text="＋", width=24, height=24, font=font(13),
            fg_color="transparent", hover_color=TRACK, text_color=TEXT_MUTED,
            command=self._toggle,
        )
        self.toggle.grid(row=0, column=0, padx=(10, 6), pady=9)
        self.title = ctk.CTkLabel(self, text="", font=font(13, "bold"), anchor="w")
        self.title.grid(row=0, column=1, sticky="w", pady=9)
        # The slack goes after the chips, so they stay next to the name.
        self.grid_columnconfigure(1, weight=0)
        self.grid_columnconfigure(2, weight=1)
        # Issue chips sit after the name, as many as fit; the rest fold into "+N".
        # Size 1: an empty CTkFrame otherwise asks for 200 by 200 and props the row open.
        self.chip_box = ctk.CTkFrame(self, fg_color="transparent", width=1, height=1)
        self.chip_box.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self._chips = []
        self._issues = []
        self._chip_state = None
        self._fit_key = None
        self._title_full = ""

        # Each value carries its name, as the header is read without the detail.
        self.heads = {}
        for key, column in (("util", 3), ("mem", 6), ("power", 8), ("temp", 10)):
            # Fixed width and right-aligned, so the server row's values line up.
            self.heads[key] = ctk.CTkLabel(self, text=t("h_" + key), font=font(10), width=40,
                                           anchor="e", text_color=TEXT_MUTED)
            self.heads[key].grid(row=0, column=column, padx=(8, 0))
        self.mini = ctk.CTkProgressBar(
            self, width=44, height=6, corner_radius=3, fg_color=TRACK, progress_color=ACCENT
        )
        self.mini.set(0)
        self.mini.grid(row=0, column=4, padx=(6, 6))
        self.util_value = ctk.CTkLabel(self, text="—", font=font(12), width=40, anchor="e")
        self.util_value.grid(row=0, column=5)
        # Fixed widths, so the columns stay put as the numbers change length.
        self.mem_value = ctk.CTkLabel(self, text="—", font=font(12), width=96, anchor="e")
        self.mem_value.grid(row=0, column=7, padx=(6, 0))
        self.power_value = ctk.CTkLabel(self, text="—", font=font(12), width=48, anchor="e")
        self.power_value.grid(row=0, column=9, padx=(6, 0))
        self.temp_value = ctk.CTkLabel(
            self, text="—", font=font(12), width=46, anchor="e", text_color=TEXT_MUTED
        )
        self.temp_value.grid(row=0, column=11, padx=(6, 12))
        # Unfolded, the bars below carry utilization and memory with their values.
        self._folding = (self.heads["util"], self.mini, self.util_value,
                         self.heads["mem"], self.mem_value)
        self.bind("<Configure>", lambda _e: self._fit_soon())

        self.detail = ctk.CTkFrame(self, fg_color="transparent")
        self.detail.grid_columnconfigure(1, weight=1)
        self.util_bar, self.util_big = self._meter("m_util", 0)
        self.mem_bar, self.mem_big = self._meter("m_mem", 1)
        spark_box = ctk.CTkFrame(self.detail, fg_color="transparent")
        spark_box.grid(row=0, column=3, rowspan=3, sticky="ne", padx=(16, 12), pady=(0, 10))
        self.spark_caption = ctk.CTkLabel(
            spark_box, text="", font=font(10), text_color=TEXT_MUTED, anchor="w"
        )
        self.spark_caption.grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.spark = Sparkline(spark_box)
        self.spark.grid(row=1, column=0)

        stats = ctk.CTkFrame(self.detail, fg_color="transparent")
        stats.grid(row=2, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(4, 12))
        self.stat_names = {}
        self.stat_values = {}
        for i, key in enumerate(STAT_KEYS):
            row, col = divmod(i, 3)
            name = ctk.CTkLabel(
                stats, text=t("d_" + key), font=font(11), text_color=TEXT_MUTED, anchor="w"
            )
            name.grid(row=row, column=col * 2, sticky="w", pady=1)
            value = ctk.CTkLabel(stats, text="—", font=font(12), anchor="w", width=104,
                                 justify="left")
            value.grid(row=row, column=col * 2 + 1, sticky="w", padx=(8, 22), pady=1)
            self.stat_names[key] = name
            self.stat_values[key] = value
        self._ecc_shown = True

    def _meter(self, key, row):
        name = ctk.CTkLabel(
            self.detail, text=t(key), font=font(11, "bold"), text_color=TEXT_MUTED, anchor="w",
        )
        name.grid(row=row, column=0, sticky="w", padx=(10, 10), pady=(0, 8))
        self.meter_names = getattr(self, "meter_names", []) + [(key, name)]
        bar = ctk.CTkProgressBar(
            self.detail, height=10, corner_radius=5, fg_color=TRACK, progress_color=ACCENT
        )
        bar.set(0)
        bar.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        value = ctk.CTkLabel(self.detail, text="—", font=font(12), anchor="w")
        value.grid(row=row, column=2, sticky="w", padx=(12, 0), pady=(0, 8))
        return bar, value

    def _toggle(self):
        self._open = not self._open
        self.toggle.configure(text="−" if self._open else "＋")
        if self._open:
            for widget in self._folding:
                widget.grid_remove()
            self.detail.grid(row=1, column=0, columnspan=12, sticky="ew")
            self.spark._draw()
        else:
            self.detail.grid_forget()
            for widget in self._folding:
                widget.grid()
        self._fit_header()

    def _fit_soon(self):
        # A first layout resizes the row several times over a few idle rounds;
        # fitting once it has been still for a moment does the work of all.
        if getattr(self, "_fit_timer", None):
            self.after_cancel(self._fit_timer)
        self._fit_timer = self.after(SETTLE_MS, self._fit_run)

    def _fit_run(self):
        self._fit_timer = None
        if self.winfo_exists():
            self._fit_header()

    def _fit_header(self):
        """Fit the name and the issue chips into what the values leave.

        The values never give way. Then at least one chip keeps its words, the
        rest folding into "+N" (the detail and the sidebar card list them all),
        and last the GPU name is cut short with an ellipsis.
        """
        if not self._title_full:
            return
        # Called on every resize of the row; with nothing changed, the answer is too.
        key = (self.winfo_width(), self._title_full, tuple(self._issues),
               getattr(self, "_open", None), _font_size)
        if key == self._fit_key:
            return
        self._fit_key = key
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        values = 0
        for w in self.grid_slaves(row=0):
            # Grid paddings count too; reqwidth leaves them out.
            pad = w.grid_info().get("padx", 0)
            values += sum(pad) if isinstance(pad, tuple) else 2 * int(pad)
            if w not in (self.title, self.chip_box):
                values += w.winfo_reqwidth()
        room = self.winfo_width() - values
        measure = measure_text(self, 10, "bold")
        cost = lambda text: measure(text) + round(22 * scale)
        title_measure = measure_text(self, 13, "bold")
        title_w = title_measure(self._title_full)
        texts = [text for _sev, _kind, text in self._issues]

        def chips_for(shown):
            chips = [(sev, text) for sev, _kind, text in self._issues[:shown]]
            if shown < len(texts):
                rest = self._issues[shown:]
                chips.append(("danger" if any(i[0] == "danger" for i in rest) else "warn",
                              f"+{len(rest)}"))
            return chips

        shown = len(texts)
        while shown > 1 and title_w + sum(cost(c[1]) for c in chips_for(shown)) > room:
            shown -= 1
        title = self._title_full
        short = title_measure(title[:title.index(" ·")] + "\u2026") if " ·" in title else title_w
        # Too narrow for even one named chip beside "GPU n…": a count alone.
        if shown and short + sum(cost(c[1]) for c in chips_for(shown)) > room:
            shown = 0
        chips = chips_for(shown) if texts else []
        # Not even "+N" beside "GPU n…": the detail and the card still list them.
        if chips and short + sum(cost(c[1]) for c in chips) > room:
            chips = []
        if title_w + sum(cost(c[1]) for c in chips) > room:
            title = self._ellipsize(title, room - sum(cost(c[1]) for c in chips),
                                    title_measure)
        if self.title.cget("text") != title:
            self.title.configure(text=title)
        if chips == self._chip_state:
            return
        self._chip_state = chips
        while len(self._chips) < len(chips):
            self._chips.append(ctk.CTkLabel(self.chip_box, text="", font=font(10, "bold"),
                                            corner_radius=9, height=20))
        for i, chip in enumerate(self._chips):
            if i < len(chips):
                severity, text = chips[i]
                chip.configure(text=text, text_color=DANGER if severity == "danger" else WARN,
                               fg_color=CHIP_DANGER if severity == "danger" else CHIP_WARN)
                chip.grid(row=0, column=i, padx=(0, 6), ipadx=4)
            else:
                chip.grid_remove()

    @staticmethod
    def _ellipsize(text, width, measure):
        # Never shorter than "GPU n", which is what tells the rows apart.
        return fit_text(text, width, measure, text.index(" ·") if " ·" in text else len(text))

    def set_span(self, seconds):
        # Kept so retranslate() can redraw the caption in the other language.
        self._span = seconds
        self.spark_caption.configure(text=t("spark_caption", seconds=int(seconds)))

    def set_expanded(self, expanded):
        if expanded != self._open:
            self._toggle()

    def is_expanded(self):
        return self._open

    def update_gpu(self, gpu: dict, push=True):
        # Every card here is an NVIDIA one; the prefix only took room.
        name = gpu["name"].removeprefix("NVIDIA ")
        title = f"GPU {gpu['index']} · {name}"
        if title != self._title_full:
            self._title_full = title
            self._fit_header()

        util = gpu["util"]
        color = load_color(util)
        for bar in (self.mini, self.util_bar):
            bar.configure(progress_color=color)
            bar.set((util or 0) / 100.0)
        self.util_value.configure(
            text="—" if util is None else f"{util:.0f}%",
            text_color=load_text_color(util),
        )
        if push:                  # catch_up() replays values the graph already has
            self.spark.push(util)
        # Decided from what the app knows rather than by asking Tk, whose
        # "viewable" lags a switch: a graph drawn too early stayed blank.
        if self._open and self.master.shown:
            self.spark._draw()

        self.util_big.configure(text="—" if util is None else f"{util:.0f}%",
                                text_color=load_text_color(util))
        used, total = gpu["mem_used"], gpu["mem_total"]
        ratio = used / total if used is not None and total else 0.0
        self.mem_bar.configure(progress_color=load_color(ratio * 100))
        self.mem_bar.set(ratio)
        self.mem_value.configure(text=f"{fmt_gb(used)} / {fmt_gb(total)} GB")
        self.mem_big.configure(text=f"{fmt_gb(used)} / {fmt_gb(total)} GB  ({ratio * 100:.0f}%)")

        self.temp_value.configure(
            text="—" if gpu["temp"] is None else f"{gpu['temp']:.0f}°C",
            text_color=temp_color(gpu["temp"]),
        )
        self.power_value.configure(
            text="—" if gpu["power"] is None else f"{gpu['power']:.0f} W"
        )

        core, core_max = gpu.get("clock_core"), gpu.get("clock_core_max")
        self.stat_values["mem_util"].configure(text=fmt_pct(gpu.get("mem_util")))
        self.stat_values["fan"].configure(text=fmt_pct(gpu.get("fan")))
        self.stat_values["pstate"].configure(text=gpu.get("pstate") or "—")
        self.stat_values["clock_core"].configure(
            text=f"{core:.0f} / {core_max:.0f} MHz" if core is not None and core_max
            else fmt_mhz(core)
        )
        self.stat_values["clock_mem"].configure(text=fmt_mhz(gpu.get("clock_mem")))
        if gpu["power"] is None:
            power = "—"
        elif gpu["power_limit"]:
            power = f"{gpu['power']:.0f} / {gpu['power_limit']:.0f} W"
        else:
            power = f"{gpu['power']:.0f} W"
        self.stat_values["power"].configure(text=power)

        issues = gpu_issues(gpu)
        if issues != self._issues:
            self._issues = issues
            self._fit_header()
        slow = [(sev, text) for sev, kind, text in issues if kind in ("slow", "power")]
        self.stat_values["slow"].configure(
            text=" · ".join(text for _sev, text in slow) or t("d_none"),
            text_color=(DANGER if any(sev == "danger" for sev, _ in slow)
                        else WARN if slow else self.stat_values["fan"].cget("text_color")))
        width, width_max = gpu.get("pcie_width"), gpu.get("pcie_width_max")
        narrow = any(kind == "pcie" for _sev, kind, _text in issues)
        self.stat_values["pcie"].configure(
            text=next((text for _s, kind, text in issues if kind == "pcie"),
                      f"x{width:.0f}" if width else "—"),
            text_color=DANGER if narrow else self.stat_values["fan"].cget("text_color"))
        # Only GPUs that count memory errors get the line; on the rest it is noise.
        counted = bool(gpu.get("ecc") or gpu.get("mem_repair"))
        if counted != self._ecc_shown:
            self._ecc_shown = counted
            for widget in (self.stat_names["ecc"], self.stat_values["ecc"]):
                widget.grid() if counted else widget.grid_remove()
        bad = next((text for _s, kind, text in issues if kind == "ecc"), None)
        self.stat_values["ecc"].configure(
            text=bad or t("d_none"),
            text_color=DANGER if bad else self.stat_values["fan"].cget("text_color"))

    def retranslate(self):
        for key, label in self.stat_names.items():
            label.configure(text=t("d_" + key))
        for key, label in self.heads.items():
            label.configure(text=t("h_" + key))
        for key, label in self.meter_names:
            label.configure(text=t(key))
        self.set_span(self._span)

    def refresh_theme(self):
        self.spark.refresh_theme()


class HostRow(ctk.CTkFrame):
    """The server's own CPU and memory, laid out like a GPU row so the values
    line up with the GPU ones underneath."""

    # Same header rule as a GPU row: values stay, chips fold, the name shortens.
    _fit_header = GpuRow._fit_header
    _fit_soon = GpuRow._fit_soon
    _fit_run = GpuRow._fit_run
    _ellipsize = staticmethod(GpuRow._ellipsize)

    def __init__(self, master):
        super().__init__(master, fg_color=INNER_BG, corner_radius=10)
        self._host = None
        self._title_full = ""
        self._issues = []
        self._chip_state = None
        self._fit_key = None
        self.grid_columnconfigure(2, weight=1)
        # Stands in for the GPU rows' fold button, which keeps the columns aligned.
        ctk.CTkLabel(self, text="", width=24).grid(row=0, column=0, padx=(10, 6), pady=9)
        self.title = ctk.CTkLabel(self, text="", font=font(13, "bold"), anchor="w")
        self.title.grid(row=0, column=1, sticky="w")
        self.chip_box = ctk.CTkFrame(self, fg_color="transparent", width=1, height=1)
        self.chip_box.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self._chips = []
        self.heads = {}
        for key, column in (("cpu", 3), ("ram", 6), ("swap", 8), ("load", 10)):
            self.heads[key] = ctk.CTkLabel(self, text=t("h_" + key), font=font(10), width=40,
                                           anchor="e", text_color=TEXT_MUTED)
            self.heads[key].grid(row=0, column=column, padx=(8, 0))
        self.mini = ctk.CTkProgressBar(self, width=44, height=6, corner_radius=3,
                                       fg_color=TRACK, progress_color=ACCENT)
        self.mini.set(0)
        self.mini.grid(row=0, column=4, padx=(6, 6))
        self.cpu_value = ctk.CTkLabel(self, text="—", font=font(12), width=40, anchor="e")
        self.cpu_value.grid(row=0, column=5)
        self.ram_value = ctk.CTkLabel(self, text="—", font=font(12), width=96, anchor="e",
                                      text_color=TEXT_MUTED)
        self.ram_value.grid(row=0, column=7, padx=(6, 0))
        self.swap_value = ctk.CTkLabel(self, text="—", font=font(12), width=48, anchor="e")
        self.swap_value.grid(row=0, column=9, padx=(6, 0))
        self.load_value = ctk.CTkLabel(self, text="—", font=font(12), width=46, anchor="e")
        self.load_value.grid(row=0, column=11, padx=(6, 12))
        self.bind("<Configure>", lambda _e: self._fit_soon())

    def update_host(self, host):
        self._host = host
        self._title_full = t("host_title", cores=host["cores"], mem=f"{host['mem_total']:.0f}")
        cpu = host["cpu"]
        self.mini.configure(progress_color=load_color(cpu))
        self.mini.set((cpu or 0) / 100)
        self.cpu_value.configure(text="—" if cpu is None else f"{cpu:.0f}%",
                                 text_color=load_text_color(cpu))
        self.ram_value.configure(text=f"{fmt_gb(host['mem_used'] * 1024)} / "
                                      f"{fmt_gb(host['mem_total'] * 1024)} GB")
        self.swap_value.configure(text=f"{host['swap_used']:.1f} GB")
        # Load past the core count means work is queueing for a CPU.
        self.load_value.configure(text="—" if host["load"] is None else f"{host['load']:.2f}",
                                  text_color=WARN if (host["load"] or 0) > host["cores"]
                                  else self.swap_value.cget("text_color"))
        self._issues = host_issues(host)
        self._fit_header()

    def retranslate(self):
        for key, label in self.heads.items():
            label.configure(text=t("h_" + key))
        if self._host:
            self.update_host(self._host)


class ProcessSection(ctk.CTkFrame):
    """Collapsible process table with a user filter and draggable columns."""

    ALL_USERS = "\u0000all"

    def __init__(self, master, widths, on_widths_changed):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(1, weight=1)
        self.widths = widths
        self.on_widths_changed = on_widths_changed
        self._open = True
        self._structure = None
        self._count = 0
        self._last = ([], [], "")
        self._user = self.ALL_USERS
        self._name_cells = []          # (pid, label) for the command column
        self._full_names = {}          # pid -> full command line
        self._rows = []                # {column key: widget} per table row
        self._row_pids = []
        self._row_buttons = {}
        self._empty = None
        self._expanded = set()         # pids whose command line is unfolded
        self._visible = set()
        self._name_col = None
        self._drag = None
        self._name_font = font(12)
        self.host_cols = True
        self._cut = {}                 # label -> (full name, width) it was cut for
        self._heads = {}               # column key -> its title label
        self._names_timer = None

        self.toggle = ctk.CTkButton(
            self, text=t("proc_toggle", arrow="\u25be", n=0), font=font(12), anchor="w",
            height=26, width=140, fg_color="transparent", hover_color=INNER_BG,
            text_color=TEXT_MUTED, command=self._toggle,
        )
        self.toggle.grid(row=0, column=0, sticky="w")
        self.expand_button = ctk.CTkButton(
            self, text=t("expand_all"), width=88, height=26, font=font(12),
            fg_color="transparent", border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, hover_color=INNER_BG, command=self.toggle_all,
        )
        self.expand_button.grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.user_menu = ctk.CTkOptionMenu(
            self, values=[t("filter_all")], width=150, height=26, font=font(12),
            fg_color=INNER_BG, button_color=INNER_BG, button_hover_color=TRACK,
            text_color=TEXT_MUTED, dropdown_font=font(12), command=self._pick_user,
        )
        self.user_menu.grid(row=0, column=3, sticky="e")

        self.notice = ctk.CTkLabel(
            self, text="", font=font(11), text_color=WARN, anchor="w", justify="left"
        )
        self.body = ctk.CTkFrame(self, fg_color=INNER_BG, corner_radius=10)
        self.body.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        self.body.bind("<Configure>", lambda _event: self._names_soon())

    # --- collapsing ---

    def _toggle(self):
        self._open = not self._open
        if self._open:
            self.body.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        else:
            self.body.grid_forget()
        self._update_toggle_text()

    def _update_toggle_text(self):
        arrow = "\u25be" if self._open else "\u25b8"
        self.toggle.configure(text=t("proc_toggle", arrow=arrow, n=self._count))

    # --- user filter ---

    def _pick_user(self, _choice=None):
        selected = self.user_menu.get()
        self._user = self.ALL_USERS if selected == t("filter_all") else selected
        self.update_procs(*self._last)

    def _sync_user_menu(self, procs):
        users = sorted({p.get("user") for p in procs if p.get("user")})
        values = [t("filter_all")] + users
        if self.user_menu.cget("values") != values:
            self.user_menu.configure(values=values)
        # The chosen user can disappear when their job ends.
        if self._user != self.ALL_USERS and self._user not in users:
            self._user = self.ALL_USERS
        self.user_menu.set(
            t("filter_all") if self._user == self.ALL_USERS else self._user
        )

    # --- column resizing ---

    def _grip(self, parent, key):
        grip = ctk.CTkFrame(parent, width=5, height=18, fg_color="transparent",
                            cursor="sb_h_double_arrow")
        grip.grid(row=0, column=1, sticky="e")
        grip.bind("<Button-1>", lambda event, k=key: self._drag_start(event, k))
        grip.bind("<B1-Motion>", self._drag_move)
        grip.bind("<ButtonRelease-1>", self._drag_end)
        return grip

    def _drag_start(self, event, key):
        self._drag = (key, event.x_root, self.widths.get(key, 100))

    def _drag_move(self, event):
        if self._drag is None or self._name_col is None:
            return
        key, origin, start = self._drag
        moved = (event.x_root - origin) / ctk.ScalingTracker.get_widget_scaling(self)
        self.widths[key] = max(40, min(900, round(start + moved)))
        self._apply_widths()

    def _drag_end(self, _event):
        if self._drag is not None:
            self._drag = None
            self.on_widths_changed()

    def _apply_widths(self):
        # Widths are kept at 100 % and scaled here, so a column keeps its share
        # when the text size setting changes.
        for col, (_title, key) in enumerate(self._columns):
            minsize = 34 if key == "_toggle" else self.widths.get(key, 100)
            self.body.grid_columnconfigure(
                col, minsize=scaled(self.body, minsize),
                weight=1 if col == self._name_col else 0,
            )
        self._names_soon()

    def _names_soon(self):
        # The first layout of a table sends a burst of resizes; one pass once
        # it has been still for a moment does the work of all of them.
        if self._names_timer:
            self.after_cancel(self._names_timer)
        self._names_timer = self.after(SETTLE_MS, self._names_now)

    def _names_now(self):
        self._names_timer = None
        self._apply_names()

    @staticmethod
    def _ellipsize(text, width, measure):
        return fit_text(text, width, measure)

    def _apply_names(self):
        """One line by default, cut to the column; the whole line when unfolded."""
        if self._name_col is None or not self._name_cells:
            return
        box = self.body.grid_bbox(column=self._name_col, row=1)
        width = max(40, (box[2] if box else 0) - 24)
        # Measured at the size the label is drawn at, which on Windows includes
        # display scaling that the shared CTkFont itself does not carry.
        measure = measure_text(self, 12)
        for pid, label in self._name_cells:
            if not label.winfo_exists():
                continue
            full = self._full_names.get(pid, "")
            opened = pid in self._expanded
            # Cutting a name is a search of many measurements; a name already
            # cut for this width is left alone.
            if not opened and self._cut.get(label) == (full, width):
                continue
            text, wrap = (full, width) if opened else (self._ellipsize(full, width, measure), 0)
            self._cut[label] = None if opened else (full, width)
            if (label.cget("text"), int(str(label.cget("wraplength")))) != (text, wrap):
                label.configure(text=text, wraplength=wrap)
            button = self._row_buttons.get(pid)
            sign = "\u2212" if opened else "\uff0b"
            if button is not None and button.cget("text") != sign:
                button.configure(text=sign)

    def toggle_all(self):
        if not self._visible:
            return
        self._expanded = set() if self._expanded >= self._visible else set(self._visible)
        self._apply_names()
        self._sync_expand_button()

    def _toggle_row(self, pid):
        self._expanded.symmetric_difference_update({pid})
        self._apply_names()
        self._sync_expand_button()

    def _sync_expand_button(self):
        opened = bool(self._visible) and self._expanded >= self._visible
        self.expand_button.configure(
            text=t("collapse_all") if opened else t("expand_all")
        )

    # --- rendering ---

    def retranslate(self):
        # Column titles rewritten in place; rebuilding the table blanked it.
        for title, key in self._layout(self._last[0]):
            head = self._heads.get(key)
            if head is not None and head.cget("text") != title:
                head.configure(text=title)
        self.update_procs(*self._last)

    def refresh_theme(self):
        # The cells are plain Tk labels with colours and a font of their own.
        self._structure = None
        self.update_procs(*self._last)

    def set_host_cols(self, shown):
        self.host_cols = shown
        self._structure = None
        self.update_procs(*self._last)

    def update_procs(self, procs, gpus, notice=""):
        self._last = (procs, gpus, notice)
        if notice:
            self.notice.configure(text=t(notice))
            self.notice.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        else:
            self.notice.grid_forget()
        self._sync_user_menu(procs)
        if self._user != self.ALL_USERS:
            procs = [p for p in procs if p.get("user") == self._user]
        self._count = len(procs)
        self._update_toggle_text()

        columns = self._layout(procs)
        # Rows are reused rather than rebuilt: recreating them showed a frame of
        # empty cells whenever a process came or went, so only a change of
        # columns rebuilds the table.
        structure = (tuple(key for _title, key in columns), bool(procs))
        if structure != self._structure:
            self._structure = structure
            self._build(columns)
        self._fill(procs, gpus)

    def _layout(self, procs):
        # Who and what first, then what it uses on the GPU, then on the host,
        # and how long it has run last.
        columns = [("", "_toggle"), (t("col_gpu"), "gpu"), ("PID", "pid"),
                   (t("col_user"), "user")]
        # The container column is only worth its width when something is in one.
        if any(proc.get("container") for proc in procs):
            columns.append((t("col_container"), "container"))
        columns += [(t("col_type"), "type"), (t("col_process"), "name"),
                    (t("col_sm"), "sm"), (t("col_memory"), "mem")]
        if self.host_cols:
            columns += [(t("col_cpu"), "cpu"), (t("col_rss"), "rss")]
        columns.append((t("col_age"), "age"))
        return columns

    def _fill(self, procs, gpus):
        self._full_names = {proc["pid"]: proc["name"] for proc in procs}
        self._visible = set(self._full_names)
        self._expanded &= self._visible
        if not procs:
            # Memory in use with nothing listed is the confusing case; say why.
            busy = (self._user == self.ALL_USERS
                    and any((gpu.get("mem_used") or 0) > 300 for gpu in gpus))
            text = t("proc_hidden") if busy else t("proc_empty")
            if self._empty.cget("text") != text:
                self._empty.configure(text=text)
            self._sync_expand_button()
            return

        self.body.grid_rowconfigure(len(self._rows), pad=0)
        while len(self._rows) > len(procs):
            for widget in self._rows.pop().values():
                widget.destroy()
        for row, proc in enumerate(procs, start=1):
            if row > len(self._rows):
                self._rows.append(self._make_row(row))
            for key, label in self._rows[row - 1].items():
                if key in ("_toggle", "name"):
                    continue
                text = self._cell_text(proc, key)
                if label.cget("text") != text:
                    label.configure(text=text)
                if key == "sm":
                    color = mode_color(WARN if self._idle(proc) else TEXT_MUTED)
                    if label.cget("fg") != color:
                        label.configure(fg=color)
        self.body.grid_rowconfigure(len(procs), pad=8)
        self._row_pids = [proc["pid"] for proc in procs]
        self._name_cells = [(pid, cells["name"]) for pid, cells in zip(self._row_pids, self._rows)]
        self._row_buttons = {pid: cells["_toggle"] for pid, cells in zip(self._row_pids, self._rows)}
        # At once, so a row taken over by another process never shows the old
        # name; a resize still refits them through _names_soon.
        self._apply_names()
        self._sync_expand_button()

    @staticmethod
    def _idle(proc):
        # A compute job holding memory with nothing running: often a forgotten
        # notebook kernel or a job stuck waiting.
        return proc.get("sm") == 0 and proc.get("type") == "C" and (proc["mem"] or 0) > 300

    @staticmethod
    def _cell_text(proc, key):
        if key == "mem":
            # Windows does not report it per process on GeForce cards.
            return "\u2014" if proc["mem"] is None else f"{fmt_gb(proc['mem'])} GB"
        if key == "sm":
            # The share of the GPU's compute this process used, not its memory.
            if proc.get("sm") is None:
                return "\u2014"
            pct = f"{proc['sm']:.0f}%"
            return t("only_mem", pct=pct) if ProcessSection._idle(proc) else pct
        if key == "cpu":
            # 100% is one core, as top counts it; a busy loader can pass it.
            return "\u2014" if proc.get("cpu") is None else f"{proc['cpu']:.0f}%"
        if key == "rss":
            return "\u2014" if proc.get("rss") is None else f"{proc['rss']:.1f} GB"
        if key == "age":
            if proc.get("started") is None:
                return "\u2014"
            secs = max(0, int(time.time() - proc["started"]))
            d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
            return (t("age_dh", d=d, h=h) if d else t("age_hm", h=h, m=m) if h
                    else t("age_m", m=m))
        return proc.get(key) or "\u2014"

    def _build(self, columns):
        for child in self.body.winfo_children():
            child.destroy()
        self._name_cells = []
        self._rows = []
        self._cut = {}
        self._heads = {}
        self._row_buttons = {}
        if not self._structure[1]:
            self._name_col = None
            self._empty = ctk.CTkLabel(
                self.body, text="", font=font(12), text_color=TEXT_MUTED, justify="left",
            )
            self._empty.grid(row=0, column=0, columnspan=8, sticky="w", padx=14, pady=10)
            return

        self._columns = columns
        self._name_col = [key for _title, key in columns].index("name")
        for col, (title, key) in enumerate(columns):
            if key == "_toggle":
                continue
            head = ctk.CTkFrame(self.body, fg_color="transparent")
            last = col == len(columns) - 1
            # The last column keeps clear of the view's scrollbar.
            head.grid(row=0, column=col, sticky="ew", padx=(10, 12 if last else 0), pady=(9, 2))
            head.grid_columnconfigure(0, weight=1)
            self._heads[key] = ctk.CTkLabel(
                head, text=title, font=font(11, "bold"), text_color=TEXT_MUTED,
                anchor="e" if col == len(columns) - 1 else "w",
            )
            self._heads[key].grid(row=0, column=0, sticky="ew")
            if col < len(columns) - 1:
                self._grip(head, key)
        self._apply_widths()

    def _make_row(self, row):
        cells = {}
        for col, (_title, key) in enumerate(self._columns):
            if key == "_toggle":
                # Looked up at click time, since the row may show another process by then.
                button = ctk.CTkButton(
                    self.body, text="\uff0b", width=22, height=20, font=font(11),
                    fg_color="transparent", hover_color=TRACK, text_color=TEXT_MUTED,
                    command=lambda index=row - 1: self._toggle_row(self._row_pids[index]),
                )
                button.grid(row=row, column=col, padx=(10, 0), pady=1)
                cells[key] = button
                continue
            # A plain Tk label, not a CTkLabel: the cells have no rounded
            # corners to draw, and a CTkLabel is three Tk widgets redrawn on
            # every resize. With a long process list, dragging the window edge
            # on Windows slowed to a crawl. Theme and font changes rebuild them.
            # The command column is filled by _apply_names once widths settle.
            label = tk.Label(
                self.body, text="", font=tk_font(self, 12), justify="left", bd=0,
                # pady keeps the rows as tall as the CTkLabels they replace.
                padx=0, pady=scaled(self, 5),
                anchor="e" if col == len(self._columns) - 1 else "w",
                bg=mode_color(INNER_BG),
                fg=mode_color(ctk.ThemeManager.theme["CTkLabel"]["text_color"]
                              if key == "name" else TEXT_MUTED),
            )
            label.grid(row=row, column=col, sticky="ew",
                       padx=(10, 12 if col == len(self._columns) - 1 else 4), pady=1)
            cells[key] = label
        return cells


class ServerView(ctk.CTkScrollableFrame):
    """The panel shown for the selected tab."""

    def __init__(self, master, server, on_edit, on_toggle_active,
                 widths, on_widths_changed, interval):
        super().__init__(master, fg_color="transparent")
        self.server = server
        self._interval = interval
        self.rows = {}
        self._gpus = []
        self._stale = False
        self._procs = self._procs_shown = None
        self._warmed = False
        # Set by App._select. Asking Tk whether the view is viewable lagged a
        # switch: the frame maps a moment later, and a sample arriving in that
        # gap was filed as hidden, leaving the new view one sample behind.
        self.shown = False
        self._online = True
        self._all_expanded = False
        self._status_state = ("connecting", "", {}, 0)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(3, weight=1)
        self.address = ctk.CTkLabel(
            header, text=self._address(), font=font(12), text_color=TEXT_MUTED
        )
        self.address.grid(row=0, column=0, sticky="w")
        # A long host name, or a narrow window, shortens the address rather
        # than pushing the buttons on the right out of sight.
        header.bind("<Configure>", lambda _e: self._address_soon())
        self._versions = None
        self.versions = ctk.CTkLabel(header, text="", font=font(11), text_color=TEXT_MUTED,
                                     fg_color=INNER_BG, corner_radius=6)
        self.expand_button = ctk.CTkButton(
            header, text=t("expand_all"), width=88, height=26, font=font(12),
            fg_color="transparent", border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, hover_color=INNER_BG, command=self.toggle_all,
        )
        self.expand_button.grid(row=0, column=2, sticky="w", padx=(14, 0))
        self.power_button = ctk.CTkButton(
            header, text=t("btn_online"), width=92, height=26, font=font(12),
            fg_color="transparent", border_width=1, border_color=OK, text_color=OK,
            hover_color=INNER_BG, command=lambda: on_toggle_active(self.server),
        )
        self.power_button.grid(row=0, column=4, sticky="e", padx=(8, 8))
        self.status = ctk.CTkLabel(
            header, text=t("status_connecting"), font=font(12), text_color=WARN
        )
        self.status.grid(row=0, column=5, sticky="e", padx=(0, 8))
        self.edit_button = ctk.CTkButton(
            header, text=t("btn_edit"), width=56, height=26, font=font(12),
            fg_color="transparent", border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, command=lambda: on_edit(self.server),
        )
        self.edit_button.grid(row=0, column=6, sticky="e")

        self.placeholder = ctk.CTkLabel(
            self, text=t("waiting_gpu"), font=font(12), text_color=TEXT_MUTED
        )
        self.placeholder.grid(row=2, column=0, sticky="w", pady=6)
        self.host = HostRow(self)   # gridded at row 1 once a reading arrives
        self.processes = ProcessSection(self, widths, on_widths_changed)

    def set_versions(self, driver, cuda):
        self._versions = (driver, cuda)
        self.versions.configure(text=t("versions", driver=driver, cuda=cuda))
        self.versions.grid(row=0, column=1, sticky="w", padx=(10, 0), ipadx=6)
        self._fit_address()

    def _address_soon(self):
        # As with the rows: not at every step of a window being dragged.
        if getattr(self, "_address_timer", None):
            self.after_cancel(self._address_timer)
        self._address_timer = self.after(SETTLE_MS, self._fit_address)

    def _fit_address(self):
        self._address_timer = None
        header = self.address.master
        others = 0
        for w in header.grid_slaves(row=0):
            pad = w.grid_info().get("padx", 0)
            others += sum(pad) if isinstance(pad, tuple) else 2 * int(pad)
            if w is not self.address:
                others += w.winfo_reqwidth()
        text = ProcessSection._ellipsize(self._address(), header.winfo_width() - others,
                                         measure_text(self, 12))
        if self.address.cget("text") != text:
            self.address.configure(text=text)

    def _address(self):
        s = self.server
        if s.get("local"):
            return t("local_address")
        return f"{s['username']}@{s['host']}:{s.get('port', 22)}"

    def set_online(self, online):
        self._online = online
        color = OK if online else DANGER
        self.power_button.configure(
            text=t("btn_online") if online else t("btn_offline"),
            border_color=color, text_color=color,
        )
        self.after_idle(self._fit_address)
        # The button already says "offline", so the status label would repeat it.
        if not online:
            self.status.configure(text="")
            if not self.rows:
                self.placeholder.configure(text=t("offline_hint"))
                self.placeholder.grid()

    def set_status(self, status, detail_key="", detail_args=None, attempt=0):
        self._status_state = (status, detail_key, detail_args or {}, attempt)
        color = STATUS_COLORS.get(status, WARN)
        if status == "retrying" and attempt:
            text = t("status_retrying_n", n=attempt)
        else:
            text = t("status_" + status)
        # Every sample says "connected"; only a change of wording moves the
        # buttons beside it, so only that refits the address.
        if self.status.cget("text") != text:
            self.status.configure(text=text)
            self.after_idle(self._fit_address)
        self.status.configure(text_color=color)
        if detail_key and status in ("retrying", "auth_failed", "error"):
            self.placeholder.configure(text=t(detail_key, **(detail_args or {})))
            if not self.rows:
                self.placeholder.grid()

    def set_interval(self, interval):
        self._interval = interval
        for row in self.rows.values():
            row.set_span(HISTORY * interval)

    def toggle_all(self):
        if not self.rows:
            return
        self._all_expanded = not all(row.is_expanded() for row in self.rows.values())
        for row in self.rows.values():
            row.set_expanded(self._all_expanded)
        self._sync_expand_button()

    def _sync_expand_button(self):
        # Individual rows can be toggled on their own, so derive the label.
        self._all_expanded = bool(self.rows) and all(
            row.is_expanded() for row in self.rows.values()
        )
        self.expand_button.configure(
            text=t("collapse_all") if self._all_expanded else t("expand_all")
        )

    def update_gpus(self, gpus):
        self._gpus = gpus
        # Out of sight only the graphs' history is kept; the numbers wait for
        # catch_up() when the view is shown. With several servers open, the
        # hidden views were most of the work done on every refresh.
        if not self.shown and {g["index"] for g in gpus} == set(self.rows):
            for gpu in gpus:
                self.rows[gpu["index"]].spark.push(gpu["util"])
            self._stale = True
            return
        self._stale = False
        self.placeholder.grid_remove()
        for i, gpu in enumerate(gpus):
            row = self.rows.get(gpu["index"])
            if row is None:
                row = GpuRow(self)
                row.set_span(HISTORY * self._interval)
                row.set_expanded(self._all_expanded)
                row.grid(row=2 + i, column=0, sticky="ew", pady=(0, 8))
                self.rows[gpu["index"]] = row
            row.update_gpu(gpu)
        self._sync_expand_button()
        self.processes.grid(row=2 + len(gpus), column=0, sticky="ew", pady=(4, 0))

    def update_host(self, host):
        self.host.update_host(host)
        if not self.host.winfo_manager():
            self.host.grid(row=1, column=0, sticky="ew", pady=(0, 8))

    def set_host_enabled(self, enabled):
        if not enabled:
            self.host.grid_remove()
        self.processes.set_host_cols(enabled)

    def update_procs(self, procs, notice=""):
        self._procs = (procs, notice)
        if self.shown:
            self._show_procs()
        elif not self._warmed:
            # Built once while out of sight, a moment after its first list, so
            # that creating the table's cells is not what a click waits for.
            self._warmed = True
            self.after(WARM_MS, self._show_procs)

    def _show_procs(self):
        # The same list arrives every sample while nothing changes on the
        # server; redrawing the table for it cost a third of each refresh.
        if self._procs is not None and self._procs != self._procs_shown:
            self._procs_shown = self._procs
            procs, notice = self._procs
            self.processes.update_procs(procs, self._gpus, notice)

    def catch_up(self):
        """Bring a view that was out of sight up to date, graphs included."""
        if self._stale:
            self._stale = False
            for gpu in self._gpus:
                self.rows[gpu["index"]].update_gpu(gpu, push=False)
        self._show_procs()
        for row in self.rows.values():
            if row.is_expanded():
                row.spark._draw()

    def retranslate(self):
        self.address.configure(text=self._address())
        self._fit_address()
        self.edit_button.configure(text=t("btn_edit"))
        if self._versions:
            self.set_versions(*self._versions)
        self.set_status(*self._status_state)
        self.set_online(self._online)
        if self._online and not self._status_state[1]:
            self.placeholder.configure(text=t("waiting_gpu"))
        for row in self.rows.values():
            row.retranslate()
        self.host.retranslate()
        self._sync_expand_button()
        self.processes.retranslate()

    def refresh_theme(self):
        for row in self.rows.values():
            row.refresh_theme()
        self.processes.refresh_theme()


class ServerDialog(ctk.CTkToplevel):
    """Dialog for adding or editing a server."""

    def __init__(self, master, server, on_save):
        super().__init__(master)
        # Owned by the main window, so Windows keeps it above it; see CloseDialog.
        self.transient(master)
        self.on_save = on_save
        self.server = server
        self.title(t("dlg_edit") if server else t("dlg_add"))
        self.geometry("460x470")
        self.resizable(False, False)
        self.grid_columnconfigure(0, weight=1)

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=24, pady=20)
        form.grid_columnconfigure(1, weight=1)

        self.fields = {}
        rows = (
            ("name", t("f_name"), t("ph_name")),
            ("host", t("f_host"), t("ph_host")),
            ("port", t("f_port"), t("ph_port")),
            ("username", t("f_username"), t("ph_username")),
            ("password", t("f_password"), t("ph_password")),
        )
        for row, (key, label, placeholder) in enumerate(rows):
            ctk.CTkLabel(form, text=label, font=font(12), anchor="w").grid(
                row=row, column=0, sticky="w", pady=7, padx=(0, 12)
            )
            entry = ctk.CTkEntry(form, placeholder_text=placeholder, height=34, font=font(13))
            if key == "password":
                entry.configure(show="•")
            entry.grid(row=row, column=1, sticky="ew", pady=7)
            self.fields[key] = entry

        ctk.CTkLabel(form, text=t("f_key"), font=font(12), anchor="w").grid(
            row=5, column=0, sticky="w", pady=7, padx=(0, 12)
        )
        key_row = ctk.CTkFrame(form, fg_color="transparent")
        key_row.grid(row=5, column=1, sticky="ew", pady=7)
        key_row.grid_columnconfigure(0, weight=1)
        self.fields["key_path"] = ctk.CTkEntry(
            key_row, placeholder_text=t("ph_key"), height=34, font=font(13)
        )
        self.fields["key_path"].grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            key_row, text=t("btn_browse"), width=52, height=34, font=font(13),
            command=self._browse
        ).grid(row=0, column=1, padx=(6, 0))

        self.ask = ctk.CTkCheckBox(form, text=t("f_ask"), font=font(12), fg_color=ACCENT,
                                   hover_color=ACCENT_HOVER, checkmark_color=BUTTON_TEXT)
        self.ask.grid(row=6, column=1, sticky="w", pady=(4, 0))
        if server and server.get("ask_password"):
            self.ask.select()

        self.message = ctk.CTkLabel(
            form, text="", font=font(12), text_color=TEXT_MUTED, wraplength=400
        )
        self.message.grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = ctk.CTkFrame(form, fg_color="transparent")
        buttons.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        buttons.grid_columnconfigure(0, weight=1)
        self.test_button = ctk.CTkButton(
            buttons, text=t("btn_test"), width=100, height=36, font=font(13), fg_color="transparent",
            border_width=1, border_color=BORDER, text_color=TEXT_MUTED, command=self._test,
        )
        self.test_button.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            buttons, text=t("btn_cancel"), width=80, height=36, font=font(13), fg_color="transparent",
            border_width=1, border_color=BORDER, text_color=TEXT_MUTED, command=self.destroy,
        ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            buttons, text=t("btn_save"), width=90, height=36, font=font(13), command=self._save
        ).grid(row=0, column=2)

        if server:
            for key, entry in self.fields.items():
                # Inserting an empty string would hide the placeholder text.
                if server.get(key):
                    entry.insert(0, str(server[key]))
        else:
            self.fields["port"].insert(0, "22")

        self.after(120, self._focus)

    def _focus(self):
        self.grab_set()
        self.fields["name"].focus_set()

    def _browse(self):
        path = filedialog.askopenfilename(
            parent=self, title=t("pick_key"), initialdir=os.path.expanduser("~/.ssh")
        )
        if path:
            self.fields["key_path"].delete(0, "end")
            self.fields["key_path"].insert(0, path)

    def _collect(self):
        values = {key: entry.get().strip() for key, entry in self.fields.items()}
        if not values["host"] or not values["username"]:
            self.message.configure(text=t("err_required"), text_color=DANGER)
            return None
        try:
            port = int(values["port"] or 22)
        except ValueError:
            self.message.configure(text=t("err_port"), text_color=DANGER)
            return None
        return {
            "id": self.server["id"] if self.server else uuid.uuid4().hex,
            "name": values["name"] or values["host"],
            "host": values["host"],
            "port": port,
            "username": values["username"],
            "password": values["password"],
            "key_path": values["key_path"],
            "ask_password": bool(self.ask.get()),
        }

    def _test(self):
        server = self._collect()
        if server is None:
            return
        self.test_button.configure(state="disabled", text=t("btn_testing"))
        self.message.configure(text=t("msg_testing"), text_color=TEXT_MUTED)

        def worker():
            ok, key, args = test_connection(server)
            self.after(0, lambda: self._test_done(ok, key, args))

        threading.Thread(target=worker, daemon=True).start()

    def _test_done(self, ok, key, args):
        if not self.winfo_exists():
            return
        self.test_button.configure(state="normal", text=t("btn_test"))
        self.message.configure(text=t(key, **args), text_color=OK if ok else DANGER)

    def _save(self):
        server = self._collect()
        if server is None:
            return
        secret = None
        if server["ask_password"]:
            # Never written to the config; the app keeps it until it quits.
            secret, server["password"] = server["password"], ""
        self.on_save(server, secret)
        self.destroy()


class FavoritesWindow(ctk.CTkToplevel):
    """Saved servers: open one in a tab, or edit and delete the entry."""

    def __init__(self, master, app):
        super().__init__(master)
        self.transient(master)    # kept above the main window, as the dialogs are
        self.app = app
        self.title(t("favorites"))
        self.geometry("470x430")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.grid(row=0, column=0, sticky="nsew", padx=16, pady=(16, 8))
        self.list_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            self, text=t("fav_add"), height=38, font=font(13), command=self.app.add_server
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))

        self.refresh()
        self.after(120, self.grab_set)

    def refresh(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        servers = self.app.cfg["servers"]
        if not servers:
            ctk.CTkLabel(
                self.list_frame, text=t("fav_empty"), font=font(13),
                text_color=TEXT_MUTED, justify="left",
            ).grid(row=0, column=0, sticky="w", pady=20)
            return
        for row, server in enumerate(servers):
            self._row(row, server)

    def _row(self, row, server):
        frame = ctk.CTkFrame(self.list_frame, fg_color=INNER_BG, corner_radius=10)
        frame.grid(row=row, column=0, sticky="ew", pady=4)
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text=display_name(server), font=font(13, "bold"),
                     anchor="w").grid(row=0, column=0, sticky="w", padx=12, pady=(9, 0))
        address = (t("local_address") if server.get("local")
                   else f"{server['username']}@{server['host']}:{server.get('port', 22)}")
        ctk.CTkLabel(
            frame, text=address, font=font(11), text_color=TEXT_MUTED, anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 9))

        # Opening is the primary action, so it gets the filled button; closing
        # is destructive-ish and only offered while the server is open.
        if server["id"] in self.app.views:
            toggle = ctk.CTkButton(
                frame, text=t("fav_close"), width=58, height=28, font=font(12),
                fg_color="transparent", border_width=1, border_color=BORDER,
                text_color=TEXT_MUTED, hover_color=TRACK,
                command=lambda s=server: self.app.close_server(s["id"]),
            )
        else:
            toggle = ctk.CTkButton(
                frame, text=t("fav_open"), width=58, height=28, font=font(12),
                command=lambda s=server: self.app.open_server(s),
            )
        toggle.grid(row=0, column=1, rowspan=2, padx=(0, 6))
        if server.get("local"):
            # Nothing to edit and nothing to remove: it is this machine.
            ctk.CTkFrame(frame, width=68, height=1, fg_color="transparent").grid(
                row=0, column=2, columnspan=2, rowspan=2, padx=(0, 8)
            )
            return
        ctk.CTkButton(
            frame, text="✎", width=30, height=28, font=font(13), fg_color="transparent",
            hover_color=TRACK, text_color=TEXT_MUTED,
            command=lambda s=server: self.app.edit_server(s),
        ).grid(row=0, column=2, rowspan=2)
        ctk.CTkButton(
            frame, text="✕", width=30, height=28, font=font(13), fg_color="transparent",
            hover_color=TRACK, text_color=TEXT_MUTED,
            command=lambda s=server: self.app.delete_server(s),
        ).grid(row=0, column=3, rowspan=2, padx=(0, 8))


class ConfirmDialog(ctk.CTkToplevel):
    """A question with a destructive answer, drawn like the rest of the app.

    Tk's own message box ignored the theme and the language: grey in dark mode,
    and its Yes/No stayed English. Read .result after wait_window().
    """

    def __init__(self, master, title, question, detail, confirm):
        super().__init__(master)
        self.transient(master)    # kept above the main window, as the dialogs are
        self.result = False
        self.title(title)
        self.resizable(False, False)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)
        ctk.CTkLabel(body, text=question, font=font(14, "bold"), anchor="w",
                     justify="left").pack(fill="x")
        ctk.CTkLabel(body, text=detail, font=font(12), text_color=TEXT_MUTED, anchor="w",
                     justify="left").pack(fill="x", pady=(4, 0))
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(anchor="e", pady=(18, 0))
        ctk.CTkButton(buttons, text=t("btn_cancel"), width=80, height=34, font=font(12),
                      fg_color="transparent", border_width=1, border_color=BORDER,
                      text_color=TEXT_MUTED, hover_color=TRACK,
                      command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(buttons, text=confirm, width=80, height=34, font=font(12, "bold"),
                      fg_color=DANGER, hover_color="#dc2626", text_color="#ffffff",
                      command=self._confirm).grid(row=0, column=1)
        # Escape backs out; Enter is left unbound so deleting takes a deliberate click.
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(120, self._focus)

    def _focus(self):
        self.grab_set()
        self.focus_set()

    def _confirm(self):
        self.result = True
        self.destroy()


class SecretDialog(ctk.CTkToplevel):
    """Ask for a server's password, or its key's passphrase, for this session.

    Read .result after wait_window(): the text, or None when cancelled.
    """

    def __init__(self, master, server, locked):
        super().__init__(master)
        self.transient(master)
        self.result = None
        self.title(t("secret_title"))
        self.resizable(False, False)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)
        name = display_name(server)
        ctk.CTkLabel(body, text=t("secret_passphrase" if locked else "secret_password",
                                  name=name),
                     font=font(14, "bold"), anchor="w").pack(fill="x")
        self.entry = ctk.CTkEntry(body, show="•", width=300, height=34, font=font(13))
        self.entry.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(body, text=t("secret_note"), font=font(11), text_color=TEXT_MUTED,
                     anchor="w").pack(fill="x", pady=(6, 0))
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(anchor="e", pady=(16, 0))
        ctk.CTkButton(buttons, text=t("btn_cancel"), width=80, height=34, font=font(12),
                      fg_color="transparent", border_width=1, border_color=BORDER,
                      text_color=TEXT_MUTED, hover_color=TRACK,
                      command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(buttons, text=t("btn_connect"), width=80, height=34,
                      font=font(12, "bold"), command=self._ok).grid(row=0, column=1)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(120, self._focus)

    def _focus(self):
        self.grab_set()
        self.entry.focus_set()

    def _ok(self):
        if self.entry.get():
            self.result = self.entry.get()
            self.destroy()


class CloseDialog(ctk.CTkToplevel):
    """Ask what closing the main window should do, and optionally remember it."""

    def __init__(self, master, on_choose):
        super().__init__(master)
        # Owned by the main window so it stays above it. On Windows the click on
        # the X hands focus back to the main window, which otherwise covered it.
        self.transient(master)
        self.on_choose = on_choose
        self.title(t("close_title"))
        self.resizable(False, False)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(body, text=t("close_prompt"), font=font(14, "bold"),
                     anchor="w").pack(fill="x")
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(14, 0))
        for i, action in enumerate(CLOSE_ACTIONS):        # the first one filled
            # The others in the ordinary text colour: muted, they read as disabled.
            style = {} if i == 0 else dict(
                fg_color="transparent", border_width=1, border_color=BORDER,
                text_color=ctk.ThemeManager.theme["CTkLabel"]["text_color"], hover_color=TRACK)
            ctk.CTkButton(buttons, text=t("close_" + action), height=36, font=font(12),
                          command=lambda a=action: self._choose(a), **style).grid(
                row=0, column=i, padx=(0 if i == 0 else 8, 0))
        self.remember = ctk.CTkCheckBox(body, text=t("close_remember"), font=font(12),
                                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                        checkmark_color=BUTTON_TEXT)
        self.remember.pack(anchor="w", pady=(16, 0))
        # Opened under the pointer, which is on the X button, so the answer is
        # a short move away. Kept from running past the window's left edge
        # rather than the screen's: Tk only knows the primary monitor's size.
        self.update_idletasks()
        px, py = self.winfo_pointerxy()
        x = max(master.winfo_rootx(), px - self.winfo_reqwidth() + 40)
        self.geometry(f"+{x}+{py + 16}")
        self.after(120, self.grab_set)

    def _choose(self, action):
        remember = bool(self.remember.get())
        self.destroy()
        self.on_choose(action, remember)


class SettingsWindow(ctk.CTkToplevel):
    """The close button's default, theme, language and alerts."""

    def __init__(self, master, app):
        super().__init__(master)
        # Kept above the main window like the dialogs, but only while that is
        # on screen: opened from the tray with it hidden, an owned window
        # would stay hidden with it.
        if master.state() == "normal":
            self.transient(master)
        self.app = app
        self.resizable(False, False)
        self.body = None
        # Measured unfolded while hidden, then placed so that unfolding later
        # only grows the window. Placed folded, the window manager often put it
        # low enough that unfolding pushed the bottom off screen, and lifting
        # it back afterwards was a second jump that read as a flicker.
        self.withdraw()
        self.details = True
        # As wide as the widest language from the start, so switching language
        # rewrites the text without the window jumping in size.
        self.refresh()
        shown, widest = language(), 0
        for lang in LANGUAGES + (shown,):
            set_language(lang)
            self.retranslate()
            self.update_idletasks()
            widest = max(widest, self.winfo_reqwidth())
        self.minsize(round(widest / ctk.ScalingTracker.get_window_scaling(self)), 0)
        self.update_idletasks()
        full = self.winfo_reqheight()
        self.details = False
        self._show_details()
        mini = app.mini
        if mini is not None and mini.winfo_exists() and master.state() != "normal":
            # Beside the mini sidebar, which stays on top and would cover it.
            width = self.winfo_reqwidth()
            x = mini.winfo_rootx() + mini.winfo_width() + 12
            if x + width > self.winfo_screenwidth():
                x = max(0, mini.winfo_rootx() - width - 12)
            y = mini.winfo_rooty()
            # The window manager keeps a window on one monitor, which Tk cannot
            # see the edges of; pushed back inside, it can land on the mini.
            self.after(150, self._clear_of_mini)
        else:
            x = master.winfo_rootx() + 60
            y = master.winfo_rooty() + 40
        y = max(0, min(y, self.winfo_screenheight() - full - 80))
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        # Not modal: on Windows a theme change hides and re-shows every window to
        # recolour its title bar, and a grab held by a window that failed to
        # come back left the whole app unclickable.

    def _clear_of_mini(self):
        mini = self.app.mini
        if mini is None or not mini.winfo_exists() or not self.winfo_exists():
            return
        left, right = mini.winfo_rootx(), mini.winfo_rootx() + mini.winfo_width()
        if self.winfo_rootx() < right and self.winfo_rootx() + self.winfo_width() > left:
            x = left - self.winfo_width() - 12
            if x >= 0:
                self.geometry(f"+{x}+{self.winfo_rooty()}")

    def _toggle_details(self):
        self.details = not self.details
        self._show_details()

    def _show_details(self):
        # Shown and hidden rather than rebuilt: rebuilding blanked the window.
        if not self._detail_widgets:
            return                # alerts are off, so there is nothing to fold
        for widget in self._detail_widgets:
            widget.grid() if self.details else widget.grid_remove()
        self._fold_button.configure(text=t("alert_less" if self.details else "alert_more"))

    def refresh(self):
        # Rebuilt rather than relabelled, so a language change reaches every row.
        # The new rows are built before the old ones go, and swapped in at once:
        # destroying first left the window blank for a moment.
        cfg = self.app.cfg
        old = self.body
        self.title(t("settings"))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        rows = self._rows()
        fold = None
        self._worded = []          # widgets with text, in _wording() order
        self._vars = {}            # radio row key -> its variable
        for row, item in enumerate(rows):
            if item is None:
                fold = row
                self._fold_button = ctk.CTkButton(
                    self.body, text=t("alert_less" if self.details else "alert_more"),
                    height=26, font=font(12), anchor="w", fg_color="transparent",
                    hover_color=TRACK, text_color=TEXT_MUTED, command=self._toggle_details)
                self._fold_button.grid(row=row, column=1, sticky="w", pady=(0, 4))
                continue
            if len(item) == 2:
                self._line(row, *item)
                continue
            key, choices, current, apply = item
            name = ctk.CTkLabel(self.body, text=t(key), font=font(12, "bold"), anchor="w")
            name.grid(row=row, column=0, sticky="w", padx=(0, 18), pady=7)
            self._worded.append(name)
            var = self._vars[key] = tk.StringVar(value=current)
            options = ctk.CTkFrame(self.body, fg_color="transparent")
            options.grid(row=row, column=1, sticky="w", pady=7)
            for i, (value, label) in enumerate(choices):
                radio = ctk.CTkRadioButton(
                    options, text=label, value=value, variable=var, font=font(12), width=0,
                    fg_color=ACCENT, hover_color=ACCENT_HOVER,
                    command=lambda v=var, f=apply: f(v.get()),
                )
                radio.grid(row=0, column=i, padx=(0, 14))
                self._worded.append(radio)
        self._detail_widgets = [] if fold is None else [
            w for w in self.body.grid_slaves() if int(w.grid_info()["row"]) > fold]
        self._show_details()
        self.body.pack(fill="both", expand=True, padx=22, pady=18)
        if old is not None:
            old.destroy()

    def _rows(self):
        cfg = self.app.cfg
        # In screen order. Four items make a row of radio buttons, two a row of
        # checkboxes and number boxes for _line(), and None the fold button.
        rows = [
            ("set_close", [("ask", t("close_ask"))]
             + [(a, t("close_" + a)) for a in CLOSE_ACTIONS],
             cfg["close_action"], self.app.set_close_action),
            ("set_theme", [("dark", t("theme_dark")), ("light", t("theme_light"))],
             ctk.get_appearance_mode().lower(), self.app.choose_theme),
            ("set_language", [("ko", "한국어"), ("en", "English")],
             language(), self.app.choose_language),
            ("set_font", [("font_size", 1, math.inf), "unit_font"]),
            ("set_temp", ["temp_warn_lbl", ("temp_warn", 30, 109), "unit_warn",
                          "temp_danger_lbl", ("temp_danger", 50, 110), "unit_danger"]),
            ("set_host", [("on", t("opt_on")), ("off", t("opt_off"))],
             "on" if cfg["host_stats"] else "off",
             lambda v: self.app.set_option("host_stats", v == "on")),
            ("set_alerts", [("on", t("opt_on")), ("off", t("opt_off"))],
             "on" if cfg["alert_enabled"] else "off",
             lambda v: self.app.set_option("alert_enabled", v == "on")),
        ]
        if cfg["alert_enabled"]:
            rows.append(None)
            rows += [
                ("set_channels", [("alert_taskbar", "ch_taskbar"), ("alert_tray", "ch_tray"),
                                  ("alert_popup", "ch_popup")]),
                ("set_kinds", [("alert_" + k, "kind_" + k)
                               for k in ("gone", "temp", "na", "stalled", "fatal")]),
                ("", [("alert_" + k, "kind_" + k) for k in ("reconnect", "idle")]),
                ("", [("alert_" + k, "kind_" + k) for k in ("slow", "power")]),
                ("", [("alert_" + k, "kind_" + k) for k in ("pcie", "ecc")]),
                ("", [("alert_" + k, "kind_" + k) for k in HOST_KINDS]),
                ("set_clear", [("alert_clear_min", 1, 60), "unit_held"]),
                ("", ["clear_temp_lbl", ("alert_clear_temp", 30, 109), "unit_below"]),
                ("set_reconnect", [("alert_reconnect_n", 1, 1000), "unit_tries"]),
                ("set_remind", [("grow", t("remind_grow")), ("fixed", t("remind_fixed")),
                                ("off", t("remind_off"))], cfg["alert_remind"],
                 lambda v: self.app.set_option("alert_remind", v)),
                ("set_remind_min", [("alert_remind_min", 1, REMIND_CAP_MIN), "unit_minutes"]),
                ("set_toast", [("alert_toast_timer", "toast_auto"),
                               ("alert_toast_sec", 1, 60), "unit_seconds"]),
            ]
        return rows

    @staticmethod
    def _wording(rows):
        """The text of every worded widget the rows make, in the order made."""
        for item in rows:
            if item is None:
                continue
            if len(item) == 2:
                label, parts = item
                if label:
                    yield t(label)
                for part in parts:
                    if isinstance(part, str):
                        yield t(part)
                    elif len(part) == 2:
                        yield t(part[1])
                continue
            key, choices, _current, _apply = item
            yield t(key)
            for _value, text in choices:
                yield text

    def retranslate(self):
        """Rewrite the text in place: rebuilding blanked the window for a moment."""
        self.title(t("settings"))
        rows = self._rows()
        for widget, text in zip(self._worded, self._wording(rows)):
            if widget.cget("text") != text:
                widget.configure(text=text)
        # A change made elsewhere, such as the toolbar's language button.
        for item in rows:
            if item is not None and len(item) == 4 and item[0] in self._vars:
                self._vars[item[0]].set(item[2])
        if self._detail_widgets:
            self._fold_button.configure(text=t("alert_less" if self.details else "alert_more"))

    def _line(self, row, label, parts):
        """One settings row of checkboxes, number boxes and the words between.

        A part is a (config key, label key) checkbox, a (config key, lowest,
        highest) number box, or an i18n key for plain text.
        """
        cfg = self.app.cfg
        if label:
            name = ctk.CTkLabel(self.body, text=t(label), font=font(12, "bold"), anchor="w")
            name.grid(row=row, column=0, sticky="w", padx=(0, 18), pady=7)
            self._worded.append(name)
        line = ctk.CTkFrame(self.body, fg_color="transparent")
        line.grid(row=row, column=1, sticky="w", pady=7)
        for i, part in enumerate(parts):
            if isinstance(part, str):
                widget = ctk.CTkLabel(line, text=t(part), font=font(12), text_color=TEXT_MUTED)
                self._worded.append(widget)
            elif len(part) == 2:
                key = part[0]
                widget = ctk.CTkCheckBox(line, text=t(part[1]), font=font(12), width=0,
                                         fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                         checkmark_color=BUTTON_TEXT)
                widget.configure(command=lambda k=key, w=widget:
                                 self.app.set_option(k, bool(w.get())))
                self._worded.append(widget)
                if cfg[key]:
                    widget.select()
            else:
                key, low, high = part
                widget = ctk.CTkEntry(line, width=52, height=28, justify="center", font=font(13))
                widget.insert(0, str(cfg[key]))

                def apply(_event=None, k=key, w=widget, lo=low, hi=high):
                    try:
                        value = int(w.get().strip())
                    except ValueError:
                        value = cfg[k]
                    value = min(hi, max(lo, value))
                    w.delete(0, "end")
                    w.insert(0, str(value))
                    if value != cfg[k]:
                        self.app.set_option(k, value)

                widget.bind("<Return>", apply)
                widget.bind("<FocusOut>", apply)
            widget.grid(row=0, column=i, padx=(0, 6 if isinstance(part, tuple) and
                                               len(part) == 3 else 12))


def pin_image(on):
    """The mini sidebar's pin: filled green when it stays on top, hollow when not."""
    big = 96                  # drawn large and shrunk, for smooth edges
    points = [(15, 3), (21, 9), (18, 10), (14, 14), (15, 19), (13, 21), (9, 17),
              (4, 22), (3, 21), (8, 16), (4, 12), (6, 10), (11, 11), (15, 7)]
    points = [(x * big / 24, y * big / 24) for x, y in points]
    images = []
    for mode in (0, 1):
        image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if on:
            draw.polygon(points, fill=ACCENT)
        else:
            draw.line(points + points[:1], fill=TEXT_MUTED[mode], width=big // 16, joint="curve")
        images.append(image.resize((32, 32), Image.LANCZOS))
    return ctk.CTkImage(light_image=images[0], dark_image=images[1], size=(16, 16))


# The id of the "Always on top" item added to the mini sidebar's window menu
# on Windows: any multiple of 16 below the system's own 0xF000 range.
SC_TOPMOST = 0x0010


class MiniWindow(ctk.CTkToplevel):
    """The sidebar shrunk to the servers that are connected, kept on top.

    Each server is one line, a small column per GPU for its utilisation, and
    a click on it unfolds it into a bar per GPU with memory and temperature.
    Both are drawn on a canvas: a widget per GPU per server is what made the
    sidebar slow. The button on top brings the main window back.
    """

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Servitor")
        self.attributes("-topmost", app.cfg["mini_topmost"])
        # Closing the small window puts the app in the tray; the button on top
        # is the way back to the main window.
        self.protocol("WM_DELETE_WINDOW", app._mini_to_tray)
        self.rows = {}
        self._order = []
        self._open = set()
        self._hwnd = self._old_proc = None
        self._ready = False           # set once placed; refits wait for that
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=(8, 4), pady=(8, 0))
        self.open_button = ctk.CTkButton(top, text=t("mini_open"), height=30, font=font(12),
                                         command=app.restore)
        self.open_button.pack(side="left", fill="x", expand=True)
        self._pins = (pin_image(False), pin_image(True))
        self.pin = ctk.CTkButton(top, text="", width=30, height=30, border_width=1,
                                 hover_color=TRACK, command=self._toggle_topmost)
        self.pin.pack(side="left", padx=(6, 0))
        ctk.CTkButton(top, text="⚙", width=30, height=30, font=font(13), border_width=1,
                      fg_color="transparent", border_color=BORDER, text_color=TEXT_MUTED,
                      hover_color=TRACK, command=app.show_settings).pack(side="left", padx=(6, 0))
        self.pin.bind("<Enter>", lambda _e: self._tip_soon())
        self.pin.bind("<Leave>", lambda _e: self._tip_hide())
        self._tip = self._tip_job = None
        self._show_pin()
        if sys.platform == "win32":
            # After CustomTkinter's own title-bar work, which hides and shows it.
            self.after(400, self._add_window_menu)
        # Scrolls once the window is dragged smaller than what it holds.
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True)
        self.empty = ctk.CTkLabel(self.body, text="", font=font(12), text_color=TEXT_MUTED)
        self.refresh()
        if app._mini_place:
            # Where it was last closed this session, at the width it was dragged
            # to; the height follows what it holds now, which may have grown.
            width, _height, x, y = re.match(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)",
                                            app._mini_place).groups()
            self.geometry(f"{x}{y}")
            self._fit(int(width))
            self._ready = True
            return
        # Open where the main window's sidebar was, on the same side of it.
        self.update_idletasks()
        x = master.winfo_rootx()
        if app.cfg["sidebar_side"] == "right":
            x += master.winfo_width() - scaled(self, MINI_W)
        self.geometry(f"+{x}+{master.winfo_rooty()}")
        self._fit(MINI_W)
        self._ready = True

    def _show_pin(self):
        on = self.app.cfg["mini_topmost"]
        self.pin.configure(image=self._pins[on], border_color=ACCENT if on else BORDER,
                           fg_color=CHIP_ON if on else "transparent")

    def _toggle_topmost(self):
        on = not self.app.cfg["mini_topmost"]
        self.app.cfg["mini_topmost"] = on
        save_config(self.app.cfg)
        self.attributes("-topmost", on)
        self._show_pin()
        if self._hwnd:
            import ctypes
            user32 = ctypes.windll.user32
            user32.CheckMenuItem(user32.GetSystemMenu(self._hwnd, False), SC_TOPMOST,
                                 0x0008 if on else 0x0000)       # MF_CHECKED

    def _tip_soon(self):
        self._tip_job = self.after(500, self._tip_show)

    def _tip_show(self):
        self._tip_job = None
        self._tip = tk.Toplevel(self)
        self._tip.overrideredirect(True)
        self._tip.attributes("-topmost", True)
        tk.Label(self._tip, text=t("mini_pin"), font=tk_font(self, 11), bd=0, padx=8, pady=4,
                 bg=mode_color(INNER_BG), fg=mode_color(ctk.ThemeManager.theme["CTkLabel"]["text_color"])
                 ).pack()
        self._tip.update_idletasks()
        # Right-aligned with the pin, which sits at the window's right edge.
        x = self.pin.winfo_rootx() + self.pin.winfo_width() - self._tip.winfo_reqwidth()
        self._tip.geometry(f"+{x}+{self.pin.winfo_rooty() + self.pin.winfo_height() + 4}")

    def _tip_hide(self):
        if self._tip_job is not None:
            self.after_cancel(self._tip_job)
            self._tip_job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _add_window_menu(self):
        """Put "Always on top" in the title bar's menu, as Windows has no such item.

        Tk offers no hook for that menu, so the item is added with the Win32
        menu calls and its click caught by wrapping the window procedure. Any
        failure leaves the window as it was: the pin button does the same job.
        """
        if not self.winfo_exists():
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            lresult = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]
            user32.CallWindowProcW.restype = lresult
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.GetSystemMenu.restype = wintypes.HMENU
            user32.GetSystemMenu.argtypes = [wintypes.HWND, wintypes.BOOL]
            user32.InsertMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT,
                                           ctypes.c_size_t, wintypes.LPCWSTR]
            user32.CheckMenuItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
            hwnd = int(self.wm_frame(), 16)
            menu = user32.GetSystemMenu(hwnd, False)
            # Before Close (SC_CLOSE), with a separator of its own above Close.
            user32.InsertMenuW(menu, 0xF060, 0x0000, SC_TOPMOST, t("mini_pin"))     # MF_STRING
            user32.InsertMenuW(menu, 0xF060, 0x0800, 0, None)                       # MF_SEPARATOR
            user32.CheckMenuItem(menu, SC_TOPMOST, 0x0008 if self.app.cfg["mini_topmost"] else 0)

            def procedure(handle, message, wparam, lparam):
                if message == 0x0112 and (wparam & 0xFFF0) == SC_TOPMOST:  # WM_SYSCOMMAND
                    self.after(0, self._toggle_topmost)
                    return 0
                return user32.CallWindowProcW(self._old_proc, handle, message, wparam, lparam)

            # Kept on the instance: a callback ctypes no longer references
            # would be freed while Windows still calls it.
            self._procedure = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT,
                                                 wintypes.WPARAM, wintypes.LPARAM)(procedure)
            self._old_proc = user32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(
                self._procedure, ctypes.c_void_p))                          # GWLP_WNDPROC
            self._hwnd = hwnd
        except Exception:
            self._hwnd = None

    def destroy(self):
        self.app._mini_place = self.geometry()
        self._tip_hide()
        if self._hwnd and self._old_proc:
            # The window procedure handed back before the Python one goes away.
            import ctypes
            ctypes.windll.user32.SetWindowLongPtrW(self._hwnd, -4, self._old_proc)
            self._hwnd = None
        super().destroy()

    def _fit(self, width=None):
        """Tall enough for every server, unless that would run off the screen.

        The size goes in at 100 % and the position stays put, as for the toast.
        Without a width, the one the user dragged to is kept.
        """
        self.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(self)
        width = width or round(self.winfo_width() / scale)
        # Summed from the cards shown: the scrollable frame's own requested
        # height did not come down when cards were taken out of it.
        if self._order:
            content = (sum(self.rows[sid]["frame"].winfo_reqheight() for sid in self._order)
                       + scaled(self, 8) * (len(self._order) + 1))
        else:
            content = self.empty.winfo_reqheight() + scaled(self, 36)
        height = min(content + self.open_button.winfo_reqheight() + scaled(self, 32),
                     self.winfo_screenheight() - self.winfo_rooty() - 60)
        self.geometry(f"{width}x{round(height / scale)}")
        # A resize asked for while the window manager is busy with the window
        # can be dropped, and Tk does not ask again; check, and ask once more.
        self.after(SETTLE_MS, lambda: self.winfo_exists()
                   and abs(self.winfo_height() - height) > 2
                   and self.wm_geometry(f"{self.winfo_width()}x{height}"))

    def _toggle(self, sid):
        self._open.symmetric_difference_update({sid})
        self.refresh()
        self._fit()

    def refresh_theme(self):
        for sid, row in self.rows.items():
            row["donut"].refresh_theme()
            for canvas in (row["cols"], row["bars"]):
                canvas.configure(bg=mode_color(INNER_BG if canvas is row["cols"] else TRACK))
            self._draw(sid)

    def refresh(self):
        app = self.app
        self.open_button.configure(text=t("mini_open"))
        # The window menu keeps the wording it was added with until reopened.
        shown = [sid for sid in app.cfg["open"] if app.statuses.get(sid) == "connected"]
        for sid in list(self.rows):
            if sid not in shown:
                self.rows.pop(sid)["frame"].destroy()
        if shown:
            self.empty.pack_forget()
        else:
            self.empty.configure(text=t("mini_empty"))
            self.empty.pack(padx=22, pady=18)
        for sid in shown:
            if sid not in self.rows:
                frame = ctk.CTkFrame(self.body, fg_color=INNER_BG, corner_radius=10,
                                     border_width=1, border_color=BORDER)
                frame.grid_columnconfigure(1, weight=1)
                donut = Donut(frame, size=36)
                donut.grid(row=0, column=0, rowspan=2, padx=(8, 6), pady=6)
                name = ctk.CTkLabel(frame, text="", font=font(12, "bold"), anchor="w")
                name.grid(row=0, column=1, sticky="sw", pady=(6, 0))
                summary = ctk.CTkLabel(frame, text="", font=font(10), text_color=TEXT_MUTED,
                                       anchor="w")
                summary.grid(row=1, column=1, sticky="nw", pady=(0, 6))
                cols = tk.Canvas(frame, bd=0, highlightthickness=0, bg=mode_color(INNER_BG))
                cols.grid(row=0, column=2, rowspan=2, padx=(4, 10))
                # The canvas sits inset in a rounded box; drawn to the edge, its
                # square corners would cover the rounding.
                box = ctk.CTkFrame(frame, fg_color=TRACK, corner_radius=6)
                bars = tk.Canvas(box, bd=0, highlightthickness=0, bg=mode_color(TRACK))
                bars.pack(fill="x", padx=3, pady=3)
                bars.bind("<Configure>", lambda _e, s=sid: self._draw(s))
                # The whole card folds and unfolds; a button for it was too small.
                for widget in (frame, donut, name, summary, cols, box, bars):
                    widget.configure(cursor="hand2")
                    widget.bind("<Button-1>", lambda _e, s=sid: self._toggle(s))
                self.rows[sid] = dict(frame=frame, donut=donut, name=name, summary=summary,
                                      cols=cols, box=box, bars=bars, gpus=[])
        # Repack only when the order changes; doing it every sample flickers.
        if shown != self._order:
            self._order = shown
            for sid in shown:
                self.rows[sid]["frame"].pack_forget()
            for i, sid in enumerate(shown):
                self.rows[sid]["frame"].pack(fill="x", padx=(8, 4),
                                             pady=(8 if i == 0 else 0, 8))
            # A server coming online or going away changes how tall it must be.
            if self._ready:
                self.after(SETTLE_MS, self._fit)
        for sid in shown:
            row = self.rows[sid]
            gpus = row["gpus"] = app.cards[sid]._gpus
            temps = [g["temp"] for g in gpus if g["temp"] is not None]
            used = sum(g["mem_used"] or 0 for g in gpus)
            total = sum(g["mem_total"] or 0 for g in gpus)
            parts = [t("card_summary", busy=sum(1 for g in gpus if is_busy(g)),
                       total=len(gpus))]
            if temps:
                parts.append(f"{t('card_max')} {max(temps):.0f}°")
            row["name"].configure(text=display_name(app.cards[sid].server))
            kinds = {kind for server_id, kind in app.alerts if server_id == sid}
            row["frame"].configure(border_color=BORDER if not kinds else
                                   WARN if kinds <= set(WARN_KINDS) else DANGER)
            row["summary"].configure(text="  ".join(parts))
            row["donut"].set_fraction(used / total if total else 0)
            opened = sid in self._open
            # Folded, the columns say it all; unfolded, the bars below repeat them.
            if opened:
                row["cols"].grid_remove()
                row["box"].grid(row=2, column=0, columnspan=4, sticky="ew",
                                padx=8, pady=(0, 8))
            else:
                row["box"].grid_remove()
                row["cols"].grid()
            self._draw(sid)

    def _draw(self, sid):
        row = self.rows[sid]
        gpus = row["gpus"]
        track = mode_color(TRACK)
        if sid not in self._open:
            # At most 64 px, so the summary beside it keeps its room: eight GPUs
            # get 8 px each, more share the width, sixteen get 4 px.
            canvas, n = row["cols"], max(1, len(gpus))
            pitch = 8 if n <= 8 else 64 / n
            width, height = scaled(canvas, pitch * n), scaled(canvas, 30)
            canvas.configure(width=width, height=height)
            canvas.delete("all")
            step = width / n
            for i, gpu in enumerate(gpus):
                util = gpu["util"] or 0
                x0, x1 = round(i * step), round((i + 1) * step) - max(1, round(step / 4))
                canvas.create_rectangle(x0, 0, x1, height, fill=track, width=0)
                top = height - max(2, round(height * util / 100))
                canvas.create_rectangle(x0, top, x1, height, fill=load_color(util), width=0)
            return
        canvas = row["bars"]
        line, pad = scaled(canvas, _font_px(10) + 8), scaled(canvas, 5)
        canvas.configure(height=line * len(gpus) + 2 * pad)
        canvas.delete("all")
        width = canvas.winfo_width()
        text = tk_font(canvas, 10)
        bold = tk_font(canvas, 9, "bold")
        muted = mode_color(TEXT_MUTED)
        # Right-hand columns as wide as the 100 % layout gave them in the mockup.
        temp_x = width - scaled(canvas, 8)
        power_x = temp_x - scaled(canvas, 30)
        mem_x = power_x - scaled(canvas, 38)
        bar_x0, bar_x1 = scaled(canvas, 22), mem_x - scaled(canvas, 40)
        for i, gpu in enumerate(gpus):
            util = gpu["util"] or 0
            y = pad + i * line + line / 2
            half = scaled(canvas, 7)
            canvas.create_text(scaled(canvas, 10), y, text=gpu["index"], fill=muted,
                               font=text, anchor="w")
            canvas.create_rectangle(bar_x0, y - half, bar_x1, y + half,
                                    fill=mode_color(PANEL_BG), width=0)
            fill_x = bar_x0 + (bar_x1 - bar_x0) * util / 100
            if util:
                canvas.create_rectangle(bar_x0, y - half, fill_x, y + half,
                                        fill=load_color(util), width=0)
            # Inside the bar once it is long enough to hold the number, after it
            # otherwise, where the number would straddle the fill's edge.
            inside = fill_x - bar_x0 >= scaled(canvas, 34)
            canvas.create_text((bar_x0 if inside else fill_x) + scaled(canvas, 5), y,
                               text=f"{util:.0f}%", fill="#ffffff" if inside else muted,
                               font=bold, anchor="w")
            canvas.create_text(mem_x, y, text=f"{(gpu['mem_used'] or 0) / 1024:.1f}G",
                               fill=muted, font=text, anchor="e")
            canvas.create_text(power_x, y, fill=muted, font=text, anchor="e",
                               text="—" if gpu["power"] is None else f"{gpu['power']:.0f}W")
            canvas.create_text(temp_x, y, fill=mode_color(temp_color(gpu["temp"])), font=text,
                               anchor="e",
                               text="—" if gpu["temp"] is None else f"{gpu['temp']:.0f}°")


class Toast(ctk.CTkToplevel):
    """A notice in the corner of the screen that leaves by itself unless hovered."""

    def __init__(self, master, title, lines, seconds, on_open):
        super().__init__(master)
        self.overrideredirect(True)       # a notice, not a window: no title bar
        self.attributes("-topmost", True)
        self.configure(fg_color=BORDER)
        self.on_open = on_open
        self.seconds = self.left = seconds
        worst = DANGER if any(sev == "danger" for sev, _, _ in lines) else WARN
        # Its own fonts, not the shared ones from font(): the toast keeps its size
        # whatever the font size setting says, as it sits over other programs.

        def f(size, weight="normal"):
            return ctk.CTkFont(size=size, weight=weight)

        card = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=0)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        # Height 1: it is stretched to the card anyway, and CustomTkinter's
        # default of 200 would otherwise set the toast's height on its own.
        ctk.CTkFrame(card, fg_color=worst, width=4, height=1, corner_radius=0).pack(
            side="left", fill="y")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=(12, 8), pady=(10, 0))
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text="⚡ Servitor", font=f(11, "bold"),
                     text_color=TEXT_MUTED).pack(side="left")
        self.close_button = ctk.CTkButton(
            head, text="✕", width=20, height=20, font=f(11), fg_color="transparent",
            hover_color=TRACK, text_color=TEXT_MUTED, command=self.destroy)
        self.close_button.pack(side="right")
        ctk.CTkLabel(body, text=title, font=f(14, "bold"), anchor="w").pack(
            fill="x", pady=(4, 6))
        for severity, server, text in lines:
            line = ctk.CTkFrame(body, fg_color=INNER_BG, corner_radius=6)
            line.pack(fill="x", pady=2)
            ctk.CTkLabel(line, text="▲" if severity == "danger" else "⚠", font=f(10),
                         text_color=DANGER if severity == "danger" else WARN).pack(
                side="left", padx=(8, 6), pady=5)
            ctk.CTkLabel(line, text=server, font=f(11, "bold")).pack(side="left")
            ctk.CTkLabel(line, text=text, font=f(11), text_color=TEXT_MUTED).pack(
                side="left", padx=8)
        ctk.CTkLabel(body, text=t("toast_hint"), font=f(10), text_color=TEXT_MUTED,
                     anchor="w").pack(fill="x", pady=(6, 6))
        self.bar = None
        if seconds:
            self.bar = ctk.CTkProgressBar(card, height=6, corner_radius=0, border_width=0,
                                          fg_color=TRACK, progress_color=worst)
            self.bar.place(relx=0, rely=1.0, anchor="sw", relwidth=1)
            self.bar.set(1)
        self.bind("<ButtonRelease-1>", self._click)
        self.update_idletasks()
        # CustomTkinter scales the size given to geometry() but not the
        # position, so the size goes in at 100 % and the position in real pixels.
        scale = ctk.ScalingTracker.get_window_scaling(self)
        width, height = 380, round(self.winfo_reqheight() / scale)
        self.geometry(f"{width}x{height}"
                      f"+{self.winfo_screenwidth() - round(width * scale) - 24}"
                      f"+{self.winfo_screenheight() - self.winfo_reqheight() - 60}")
        self.alpha = 0.0
        # Only a pointer that moves onto the toast holds it. One that was
        # already resting in that corner would otherwise keep it up for good.
        self._pointer = self.winfo_pointerxy()
        self.after(16, self._tick)

    def _click(self, event):
        # The toplevel binding sees every click inside, the close button's too.
        if self.winfo_exists() and not str(event.widget).startswith(str(self.close_button)):
            self.destroy()
            self.on_open()

    def _tick(self):
        if not self.winfo_exists():
            return
        if self.alpha < 0.97:
            self.alpha = min(0.97, self.alpha + 0.12)
            self.attributes("-alpha", self.alpha)
        elif self.seconds:
            x, y = self.winfo_pointerxy()
            hovered = ((x, y) != self._pointer
                       and self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                       and self.winfo_rooty() <= y < self.winfo_rooty() + self.winfo_height())
            if not hovered:
                self.left -= 0.05
                self.bar.set(max(0.0, self.left / self.seconds))
            if self.left <= 0:
                self.destroy()
                return
        self.after(50, self._tick)


class App(ctk.CTk):
    def __init__(self, cfg):
        # The class names the window to the desktop: GNOME matches WM_CLASS to
        # servitor.desktop, and only then does the dock badge land on its icon.
        super().__init__(className=APP_NAME)
        # Windows registers the font privately and non-enumerably, so families()
        # never lists it however well it loaded; the name has to be taken on
        # trust there, which is what CustomTkinter does for its own Roboto.
        # Elsewhere the face is a file copied into ~/.fonts, which a read-only
        # home or a stale fontconfig cache can defeat, so the check still earns
        # its place: it degrades to the previous font instead of breaking.
        if sys.platform.startswith("win") or UI_FONT in tkfont.families():
            ctk.ThemeManager.theme["CTkFont"]["family"] = UI_FONT
            # Dialogs Tk draws itself, the delete confirmation and the key
            # picker on Linux, use these named fonts rather than CustomTkinter's.
            # Windows shows native dialogs there, which keep the system font.
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                         "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
                tkfont.nametofont(name, root=self).configure(family=UI_FONT)
        self.cfg = cfg
        self.queue = queue.Queue()
        self.collectors = {}
        # Passwords and key passphrases typed this session, never written to
        # disk; quitting forgets them. _locked holds servers whose key turned
        # out to need a passphrase that was not saved.
        self.secrets = {}
        self._locked = set()
        # Servers whose key no longer matches the saved one, with the new
        # key's fingerprint; going online asks whether to trust it.
        self._rekeyed = {}
        # Where the main window and the mini sidebar were, so switching between
        # them puts each back in its place. Kept for the session only.
        self._main_place = None
        self._mini_place = None
        self.views = {}
        self.statuses = {}
        self.cards = {}
        self.last_sample = {}
        self.idle_since = {}
        self.known_gpus = {}
        self.procs = {}
        self.active = None
        self.favorites = None
        self.settings = None
        self.tray = None
        self.mini = None
        self.alerts = {}          # (server id, kind) -> args, last seen, next reminder
        self.unseen = set()       # alerts raised since the window was last looked at
        self.pending = set()      # alerts waiting for the next popup
        self.toast = None
        self._toast_at = -TOAST_GAP
        self._dock = None
        self._dock_serial = 1
        self._indicated = None

        self.title(t("app_title"))
        size = cfg.get("window") or DEFAULT_WINDOW
        try:
            width, height = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            width, height = DEFAULT_WINDOW
        width, height = max(MIN_WINDOW[0], width), max(MIN_WINDOW[1], height)
        # Size only: the window manager decides where the window lands.
        self.geometry(f"{width}x{height}")
        self._normal_size = [width, height]
        self._size_job = None
        self.bind("<Configure>", self._remember_size)
        self._frozen = False
        self._freeze_job = None
        self._seen_size = None
        self.bind("<Configure>", self._hold_layout, add="+")
        self.minsize(*MIN_WINDOW)
        # Tk keeps no reference to the image, so hold one on the instance.
        self._icon = tk.PhotoImage(file=os.path.join(ASSETS_DIR, "icon.png"))
        self.iconphoto(True, self._icon)

        self._build_toolbar()
        self._build_sidebar()
        # In the margin under the content panel, out of the way.
        ctk.CTkLabel(self, text=f"v{APP_VERSION}", font=font(9), text_color=TEXT_MUTED,
                     height=14).place(relx=1.0, rely=1.0, anchor="se", x=-20, y=-1)

        for sid in list(self.cfg["open"]):
            server = self._find(sid)
            if server:
                self._open(server)
        self._relayout_sidebar()
        self._select(self.cfg["open"][0] if self.cfg["open"] else None)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<FocusIn>", self._alerts_seen, add="+")
        self.after(POLL_MS, self._poll)

    # --- construction ---

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=22, pady=(16, 10))
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bar, text="⚡ Servitor", font=font(22, "bold")).grid(
            row=0, column=0, sticky="w"
        )

        self.interval_label = ctk.CTkLabel(
            bar, text=t("interval_label"), font=font(12), text_color=TEXT_MUTED
        )
        self.interval_label.grid(row=0, column=2, padx=(0, 6))
        self.interval_entry = ctk.CTkEntry(bar, width=56, height=34, justify="center", font=font(13))
        self.interval_entry.insert(0, f"{float(self.cfg['interval_sec']):g}")
        self.interval_entry.grid(row=0, column=3)
        self.interval_entry.bind("<Return>", self._apply_interval)
        self.interval_entry.bind("<FocusOut>", self._apply_interval)
        self.interval_unit = ctk.CTkLabel(
            bar, text=t("interval_unit"), font=font(12), text_color=TEXT_MUTED
        )
        self.interval_unit.grid(row=0, column=4, padx=(6, 16))

        # The way back is the mini sidebar's own "open main window" button.
        self.mini_button = ctk.CTkButton(
            bar, text=t("btn_mini"), width=38, height=34, font=font(12),
            fg_color="transparent", border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, command=lambda: self._close_as("mini", False),
        )
        self.mini_button.grid(row=0, column=5, padx=(0, 8))
        self.lang_button = ctk.CTkButton(
            bar, text="EN" if language() == "ko" else "한", width=38, height=34,
            font=font(12, "bold"), fg_color="transparent", border_width=1,
            border_color=BORDER, text_color=TEXT_MUTED, command=self._toggle_language,
        )
        self.lang_button.grid(row=0, column=6, padx=(0, 8))
        self.theme_button = ctk.CTkButton(
            bar, text="☀" if ctk.get_appearance_mode() == "Dark" else "☾", width=38, font=font(13),
            height=34, fg_color="transparent", border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, command=self._toggle_theme,
        )
        self.theme_button.grid(row=0, column=7)
        ctk.CTkButton(
            bar, text="⚙", width=38, height=34, font=font(13), fg_color="transparent",
            border_width=1,
            border_color=BORDER, text_color=TEXT_MUTED, command=self.show_settings,
        ).grid(row=0, column=8, padx=(8, 0))

    def _build_sidebar(self):
        # The holder follows the window; the panels inside are placed in it, so
        # that _hold_layout can pin them at a fixed size during a resize. The
        # packer would still shrink a pinned frame to fit; the placer does not.
        self.holder = ctk.CTkFrame(self, fg_color="transparent")
        self.holder.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.split = ctk.CTkFrame(self.holder, fg_color="transparent")
        self.split.place(x=0, y=0, relwidth=1, relheight=1)

        # The sidebar is a card of its own, so the server cards read as sitting on
        # it instead of floating on the window, and it is plainly one object to
        # pick up. Its own empty space is what carries it to the other side.
        self.side_panel = ctk.CTkFrame(self.split, fg_color=PANEL_BG, corner_radius=12,
                                       width=SIDEBAR_W, border_width=1,
                                       border_color=BORDER)
        self.side_panel.pack_propagate(False)
        self.sidebar = ctk.CTkScrollableFrame(self.side_panel, fg_color="transparent")
        self.sidebar.pack(fill="both", expand=True, padx=8, pady=(13, 0))
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.open_button = ctk.CTkButton(self.side_panel, text=t("open_server"), height=32,
                                         font=font(12), command=self.show_favorites)
        self.open_button.pack(fill="x", padx=8, pady=8)

        # The blank strip under the last card belongs to the scrollable frame's
        # canvas rather than to the frame the cards are gridded in, so the canvas
        # has to answer for it as well as the panel itself.
        self._dock_x = None
        self._dock_dx = 0
        self._dock_armed = False
        self._dock_ghost = None
        self._drag_sid = None
        self._ghost = None
        for area in (self.side_panel, self.sidebar._parent_canvas):
            area.configure(cursor="fleur")
            area.bind("<Button-1>", self._dock_press)
            area.bind("<B1-Motion>", self._dock_motion)
            area.bind("<ButtonRelease-1>", self._dock_release)

        self.content = ctk.CTkFrame(self.split, fg_color=PANEL_BG, corner_radius=12)
        self.empty = ctk.CTkLabel(self.content, text=t("empty_hint"), font=font(14),
                                  text_color=TEXT_MUTED, justify="center")
        self._apply_sidebar_side()

    # --- moving the whole sidebar ---

    def _apply_sidebar_side(self):
        """Repack in place: the widgets keep their state, only the side changes."""
        side = self.cfg["sidebar_side"]
        for widget in (self.side_panel, self.content):
            widget.pack_forget()
        self.side_panel.pack(side=side, fill="y")
        self.content.pack(side=side, fill="both", expand=True,
                          padx=(10, 0) if side == "left" else (0, 10))

    def _dock_press(self, event):
        self._dock_x = event.x_root
        self._dock_dx = event.x_root - self.side_panel.winfo_rootx()
        self._dock_armed = False

    def _dock_motion(self, event):
        """Carry an outline of the panel, and light it once it is far enough.

        Measuring the drag rather than which half of the window the pointer ends
        in is what keeps the gesture reachable: the sidebar sits against an edge,
        so the midpoint is most of a window away.
        """
        if self._dock_x is None:
            return
        travel = event.x_root - self._dock_x
        if self._dock_ghost is None:
            if abs(travel) < DRAG_SLACK:
                return
            self._lift_sidebar()
        x = event.x_root - self.split.winfo_rootx() - self._dock_dx
        self._dock_ghost.place(x=max(0, min(x, self.split.winfo_width() - SIDEBAR_W)),
                               y=0, relheight=1)
        self._dock_ghost.lift()
        armed = (abs(travel) >= SIDEBAR_W / 2
                 and ("right" if travel > 0 else "left") != self.cfg["sidebar_side"])
        if armed != self._dock_armed:
            self._dock_armed = armed
            self._dock_ghost.configure(border_color=ACCENT if armed else BORDER)

    def _lift_sidebar(self):
        """Raise an outline to carry, never the panel itself.

        Repacking the panel would unmap it, and the window holding the pointer
        grab is inside it: X11 takes the grab back the instant that window stops
        being viewable, which would end the drag on its first step.
        """
        # Painted like the panel rather than left hollow: "transparent" here means
        # the window's own colour, which reads as a hole punched in the content.
        self._dock_ghost = ctk.CTkFrame(self.split, fg_color=PANEL_BG,
                                        width=SIDEBAR_W, corner_radius=12,
                                        border_width=2, border_color=BORDER)

    def _dock_release(self, event):
        if self._dock_x is None:
            return
        travel = event.x_root - self._dock_x
        self._dock_x = None
        if self._dock_ghost is not None:
            self._dock_ghost.destroy()
            self._dock_ghost = None
        if not self._dock_armed:
            return
        self._dock_armed = False
        self.cfg["sidebar_side"] = "right" if travel > 0 else "left"
        self._apply_sidebar_side()
        save_config(self.cfg)

    # --- reordering the cards ---

    def _drag_card(self, server_id, y_root, grab_dy):
        """Carry a copy of the card under the pointer; the card marks the slot."""
        if server_id not in self.cfg["open"]:
            return
        if self._drag_sid != server_id:
            self._lift_card(server_id)
        inner = self.sidebar
        top = max(0, min(y_root - inner.winfo_rooty() - grab_dy,
                         inner.winfo_height() - self._ghost.cget("height")))
        self._ghost.place(x=0, y=top, relwidth=1)
        self._ghost.lift()
        self._retarget(server_id, y_root)

    def _lift_card(self, server_id):
        """Raise something to carry, and hollow the card out into a slot.

        What follows the pointer is a new widget, never the card itself. Moving
        the card to place() would unmap it, and the window holding the pointer
        grab lives inside it: X11 hands the grab back the moment that window
        stops being viewable, which ends the drag on the spot. Re-gridding and
        recolouring leave it mapped throughout.
        """
        card = self.cards[server_id]
        self._drag_sid = server_id
        self._ghost = ctk.CTkFrame(self.sidebar, fg_color=INNER_BG, corner_radius=10,
                                   border_width=1, border_color=ACCENT,
                                   height=card.winfo_height())
        ctk.CTkLabel(self._ghost, text=display_name(card.server), font=font(13, "bold"),
                     anchor="w").place(x=14, rely=0.5, anchor="w")
        card.set_carried(True)

    def _drop_card(self):
        server_id, self._drag_sid = self._drag_sid, None
        if server_id is None:
            return
        self._ghost.destroy()
        self._ghost = None
        card = self.cards[server_id]
        card.set_carried(False)
        card.set_active(server_id == self.active)
        save_config(self.cfg)

    def _retarget(self, server_id, y_root):
        """Move the empty slot once the pointer passes a neighbour's middle.

        Comparing against the neighbour rather than the pointer's own offset keeps
        the order stable when an expanded card is far taller than the one it passes.
        """
        order = self.cfg["open"]
        i = target = order.index(server_id)
        if i > 0:
            above = self.cards[order[i - 1]]
            if y_root < above.winfo_rooty() + above.winfo_height() / 2:
                target = i - 1
        if i < len(order) - 1:
            below = self.cards[order[i + 1]]
            if y_root > below.winfo_rooty() + below.winfo_height() / 2:
                target = i + 1
        if target != i:
            order.insert(target, order.pop(i))
            self._relayout_sidebar()

    def _relayout_sidebar(self):
        # Re-grid in place rather than forgetting first: grid_forget unmaps the
        # card at once, and X11 drops the implicit pointer grab when the window
        # holding it becomes unviewable, which would end a drag at its first swap.
        for i, sid in enumerate(self.cfg["open"]):
            card = self.cards.get(sid)
            if card is not None:
                card.grid(row=i, column=0, sticky="ew", pady=4)

    # --- tabs ---

    def _find(self, server_id):
        return next((s for s in self.cfg["servers"] if s["id"] == server_id), None)

    def _open(self, server):
        sid = server["id"]
        self.views[sid] = ServerView(
            self.content, server, self.edit_server, self.toggle_active,
            self.cfg["col_widths"], self._save_widths,
            float(self.cfg["interval_sec"]),
        )
        self.views[sid].processes.host_cols = self.cfg["host_stats"]
        self.cards[sid] = ServerCard(
            self.sidebar, server,
            on_select=lambda s=sid: self._select(s),
            on_close=lambda s=sid: self.close_server(s),
            on_drag=lambda y, dy, s=sid: self._drag_card(s, y, dy),
            on_drop=self._drop_card,
        )
        self.last_sample[sid] = None
        self.idle_since[sid] = {}
        # Left paused last time, so do not connect; the view says why on screen.
        if sid in self.cfg["offline"]:
            self.views[sid].set_online(False)
            self._note_status(sid, "offline")
            return
        self.statuses[sid] = "connecting"
        if self._needs_secret(server):
            self._hold_for_secret(sid, "status_need_secret")
            return
        collector = ServerCollector(self._creds(server), self.queue,
                                    float(self.cfg["interval_sec"]), self.cfg["host_stats"])
        self.collectors[sid] = collector
        collector.start()

    def _select(self, server_id):
        self.active = server_id
        self.empty.pack_forget()
        for view in self.views.values():
            view.pack_forget()
            view.shown = False
        for sid, card in self.cards.items():
            card.set_active(sid == server_id)
        if server_id is None:
            self.empty.pack(expand=True)
        else:
            view = self.views[server_id]
            view.pack(fill="both", expand=True, padx=14, pady=14)
            # Hidden, it kept only graph history; see ServerView.update_gpus.
            view.shown = True
            view.catch_up()

    def open_server(self, server):
        sid = server["id"]
        if sid not in self.views:
            self._open(server)
            self.cfg["open"].append(sid)
            save_config(self.cfg)
        self._relayout_sidebar()
        self._select(sid)
        if self.favorites is not None and self.favorites.winfo_exists():
            self.favorites.refresh()

    def close_server(self, server_id):
        collector = self.collectors.pop(server_id, None)
        if collector:
            collector.stop()
        view = self.views.pop(server_id, None)
        if view:
            view.destroy()
        card = self.cards.pop(server_id, None)
        if card:
            card.destroy()
        self.statuses.pop(server_id, None)
        self.last_sample.pop(server_id, None)
        self.idle_since.pop(server_id, None)
        self.known_gpus.pop(server_id, None)
        for key in [k for k in self.alerts if k[0] == server_id]:
            del self.alerts[key]
        self._alert_indicate()
        self.procs.pop(server_id, None)
        if server_id in self.cfg["offline"]:
            self.cfg["offline"].remove(server_id)
        if server_id in self.cfg["open"]:
            self.cfg["open"].remove(server_id)
            save_config(self.cfg)
        if self.active == server_id:
            self._select(self.cfg["open"][-1] if self.cfg["open"] else None)
        if self.favorites is not None and self.favorites.winfo_exists():
            self.favorites.refresh()
        if self.mini is not None:
            self.mini.refresh()

    # --- favorites ---

    def show_favorites(self):
        if self.favorites is not None and self.favorites.winfo_exists():
            self.favorites.refresh()
            self.favorites.focus()
            return
        self.favorites = FavoritesWindow(self, self)

    def add_server(self):
        ServerDialog(self, None, self._on_saved)

    def edit_server(self, server):
        ServerDialog(self, server, self._on_saved)

    def _on_saved(self, server, secret=None):
        sid = server["id"]
        # A new key or a new setting starts over; what was typed now is kept
        # for this session only.
        self._locked.discard(sid)
        self._rekeyed.pop(sid, None)
        if secret:
            self.secrets[sid] = secret
        elif not server.get("ask_password"):
            self.secrets.pop(sid, None)
        servers = self.cfg["servers"]
        for i, existing in enumerate(servers):
            if existing["id"] == server["id"]:
                servers[i] = server
                break
        else:
            servers.append(server)
        save_config(self.cfg)

        if server["id"] in self.views:
            # Connection details may have changed, so reconnect with the new ones.
            self.close_server(server["id"])
            self.open_server(server)
        if self.favorites is not None and self.favorites.winfo_exists():
            self.favorites.refresh()

    def delete_server(self, server):
        dialog = ConfirmDialog(self, t("delete_title"),
                               t("delete_confirm", name=display_name(server)),
                               t("delete_detail"), t("btn_delete"))
        self.wait_window(dialog)
        if not dialog.result:
            return
        self.close_server(server["id"])
        self.cfg["servers"] = [s for s in self.cfg["servers"] if s["id"] != server["id"]]
        save_config(self.cfg)
        if self.favorites is not None and self.favorites.winfo_exists():
            self.favorites.refresh()

    # --- passwords asked for, not saved ---

    def _needs_secret(self, server):
        sid = server["id"]
        return sid not in self.secrets and (server.get("ask_password") or sid in self._locked)

    def _creds(self, server):
        """The server as the collector should see it, with this session's password."""
        secret = self.secrets.get(server["id"])
        return server if secret is None else {**server, "password": secret}

    def _hold_for_secret(self, sid, key):
        """Leave the server offline, saying that it waits for a password."""
        view = self.views[sid]
        view.set_online(False)
        self._note_status(sid, "offline")
        view.status.configure(text=t(key), text_color=WARN)
        # The card would otherwise keep the error, worded for the edit dialog.
        self.cards[sid].summary.configure(text=t(key), text_color=WARN)
        if not view.rows:
            view.placeholder.configure(text=t("hint_need_secret"))
            view.placeholder.grid()

    def _ask_secret(self, server):
        dialog = SecretDialog(self, server, locked=server["id"] in self._locked
                              or bool(server.get("key_path")))
        self.wait_window(dialog)
        if dialog.result is None:
            return False
        self.secrets[server["id"]] = dialog.result
        return True

    def _secret_refused(self, sid, status, key):
        """A typed password that failed, or a key found locked: ask again."""
        server = self._find(sid)
        wrong = (status == "auth_failed" and server.get("ask_password")
                 or key == "err_bad_passphrase")
        if not (wrong or key == "err_need_passphrase"):
            return
        self.secrets.pop(sid, None)
        if key in ("err_need_passphrase", "err_bad_passphrase"):
            self._locked.add(sid)
        # The collector gave up on its own; it only has to leave the list.
        self.collectors.pop(sid, None)
        self._hold_for_secret(sid, "status_wrong_secret" if wrong else "status_need_secret")

    def toggle_active(self, server):
        """Pause or resume monitoring without giving up the open connection slot."""
        sid = server["id"]
        view = self.views.get(sid)
        if view is None:
            return
        collector = self.collectors.pop(sid, None)
        if collector is not None:
            collector.stop()
            view.set_online(False)
            self._note_status(sid, "offline")
            if sid not in self.cfg["offline"]:
                self.cfg["offline"].append(sid)
                save_config(self.cfg)
            return
        if sid in self._rekeyed:
            dialog = ConfirmDialog(self, t("hostkey_title"),
                                   t("hostkey_confirm", name=display_name(server)),
                                   t("hostkey_detail", fingerprint=self._rekeyed[sid]),
                                   t("btn_trust"))
            self.wait_window(dialog)
            if not dialog.result:
                return
            forget_host_key(server)
            del self._rekeyed[sid]
        if self._needs_secret(server) and not self._ask_secret(server):
            return
        fresh = ServerCollector(self._creds(server), self.queue,
                                float(self.cfg["interval_sec"]), self.cfg["host_stats"])
        self.collectors[sid] = fresh
        fresh.start()
        view.set_online(True)
        view.set_status("connecting")
        self._note_status(sid, "connecting")
        if sid in self.cfg["offline"]:
            self.cfg["offline"].remove(sid)
            save_config(self.cfg)

    # --- settings ---

    def show_settings(self):
        if self.settings is not None and self.settings.winfo_exists():
            self._raise_settings()
            return
        self.settings = SettingsWindow(self, self)

    def _raise_settings(self):
        # Shown again even if hidden: see _toggle_theme.
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.deiconify()
            self.settings.lift()
            self.settings.focus_force()

    def set_close_action(self, action):
        self.cfg["close_action"] = action
        save_config(self.cfg)

    def choose_theme(self, mode):
        if mode != ctk.get_appearance_mode().lower():
            self._toggle_theme()

    def choose_language(self, lang):
        if lang != language():
            self._toggle_language()

    def _apply_interval(self, _event=None):
        current = float(self.cfg["interval_sec"])
        try:
            value = float(self.interval_entry.get().strip().replace(",", "."))
        except ValueError:
            value = current
        value = min(MAX_INTERVAL, max(MIN_INTERVAL, value))
        self.interval_entry.delete(0, "end")
        self.interval_entry.insert(0, f"{value:g}")
        if value == current:
            return
        self.cfg["interval_sec"] = value
        save_config(self.cfg)
        # The interval is baked into the remote command, so reconnect to apply it.
        for sid, collector in list(self.collectors.items()):
            collector.stop()
            fresh = ServerCollector(self._creds(self._find(sid)), self.queue, value,
                                    self.cfg["host_stats"])
            self.collectors[sid] = fresh
            fresh.start()
        for view in self.views.values():
            view.set_interval(value)

    def _toggle_theme(self):
        dark = ctk.get_appearance_mode() == "Dark"
        ctk.set_appearance_mode("light" if dark else "dark")
        self.theme_button.configure(text="☾" if dark else "☀")
        self.cfg["appearance"] = "light" if dark else "dark"
        save_config(self.cfg)
        for view in self.views.values():
            view.refresh_theme()
        for card in self.cards.values():
            card.refresh_theme()
        if self.mini is not None:
            self.mini.refresh_theme()
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.refresh()
            # On Windows CustomTkinter hides and re-shows each window to recolour
            # its title bar, and the settings window did not always come back.
            # Its own re-show runs a few dozen ms in; this follows after it.
            self.after(400, self._raise_settings)

    def _toggle_language(self):
        set_language("en" if language() == "ko" else "ko")
        self.cfg["language"] = language()
        save_config(self.cfg)

        self.lang_button.configure(text="EN" if language() == "ko" else "한")
        self._indicated = None
        self._alert_indicate()
        self.interval_label.configure(text=t("interval_label"))
        self.interval_unit.configure(text=t("interval_unit"))
        self.empty.configure(text=t("empty_hint"))
        self.open_button.configure(text=t("open_server"))
        self.mini_button.configure(text=t("btn_mini"))
        for card in self.cards.values():
            card.retranslate()
        for view in self.views.values():
            view.retranslate()
        if self.favorites is not None and self.favorites.winfo_exists():
            self.favorites.title(t("favorites"))
            self.favorites.refresh()
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.retranslate()
        if self.mini is not None:
            self.mini.refresh()

    # --- runtime ---

    def _save_widths(self):
        save_config(self.cfg)

    def _note_status(self, server_id, status, key="", args=None, attempt=0):
        self.statuses[server_id] = status
        self._alert_status(server_id, status, key, args, attempt)
        card = self.cards.get(server_id)
        if card is not None:
            card.set_status(status, key, args)
        if self.mini is not None:
            self.mini.refresh()

    def _poll(self):
        now = time.monotonic()
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            sid = message["server_id"]
            view = self.views.get(sid)
            # A paused server's collector is stopped, but a sample it had already
            # queued still arrives; taken in, it turned the view back to
            # "connected" and, with nothing after it, to "not responding".
            if view is None or sid not in self.collectors:
                continue
            kind = message["type"]
            if kind == "gpus":
                view.set_status("connected")
                view.update_gpus(message["gpus"])
                self.last_sample[sid] = now
                self._refresh_card(sid, message["gpus"], now)
            elif kind == "procs":
                self.procs[sid] = message["procs"]
                view.update_procs(message["procs"], message.get("notice", ""))
            elif kind == "versions":
                view.set_versions(message["driver"], message["cuda"])
            elif kind == "host":
                # A reading already queued when the setting was turned off.
                if not self.cfg["host_stats"]:
                    continue
                view.update_host(message)
                card = self.cards.get(sid)
                if card is not None:
                    card.set_host(message)
                for _severity, host_kind, text in host_issues(message):
                    self._alert_raise(sid, host_kind, {"what": text}, now)
            else:
                if message["detail_key"] == "err_host_key":
                    # The collector gave up on its own. Shown offline, the
                    # button is the way back in, and it asks about the key.
                    self._rekeyed[sid] = message["detail_args"]["fingerprint"]
                    self.collectors.pop(sid, None)
                    view.set_online(False)
                view.set_status(message["status"], message["detail_key"],
                                message["detail_args"], message["attempt"])
                self.last_sample[sid] = None
                self._note_status(sid, message["status"], message["detail_key"],
                                  message["detail_args"], message["attempt"])
                self._secret_refused(sid, message["status"], message["detail_key"])
        self._check_stalled(now)
        self._alert_tick(now)
        self.after(POLL_MS, self._poll)

    def _refresh_card(self, server_id, gpus, now):
        card = self.cards.get(server_id)
        if card is None:
            return
        # A GPU vanishing from the list is worth saying out loud. Known by UUID:
        # nvidia-smi numbers GPUs in enumeration order, so an index can move to
        # another card and the wrong one would be reported missing.
        seen = {gpu["uuid"]: gpu["index"] for gpu in gpus}
        known = self.known_gpus.get(server_id, {})
        gone = sorted((i for u, i in known.items() if u not in seen), key=int)
        self.known_gpus[server_id] = {**known, **seen}
        faults, full, idle = find_faults(gpus, self.procs.get(server_id, []),
                                         self.idle_since.get(server_id, {}), now, gone)
        self.idle_since[server_id] = idle
        self.statuses[server_id] = "connected"
        self._alert_status(server_id, "connected")
        self._alert_gpus(server_id, gpus, full, now)
        card.set_active(server_id == self.active)
        card.dot.configure(text_color=OK)
        card.update_gpus(gpus, faults)
        if self.mini is not None:
            self.mini.refresh()

    def _check_stalled(self, now):
        """nvidia-smi can block on a wedged GPU while SSH stays up."""
        limit = max(6.0, float(self.cfg["interval_sec"]) * STALL_FACTOR)
        for sid, stamp in self.last_sample.items():
            if stamp is None or self.statuses.get(sid) == "offline":
                continue
            gap = now - stamp
            if gap < limit:
                continue
            self._note_status(sid, "stalled", "stalled_detail", {"secs": int(gap)})
            view = self.views.get(sid)
            if view is not None:
                view.set_status("stalled", "stalled_detail", {"secs": int(gap)})

    # --- alerts ---

    def set_option(self, key, value):
        global TEMP_WARN, TEMP_DANGER
        self.cfg[key] = value
        if key == "font_size":
            set_font_size(value)      # text only; the window keeps its dragged size
            # Process cells are plain Tk labels, which the shared fonts do not reach.
            for view in self.views.values():
                view.processes.refresh_theme()
        if key == "host_stats":
            # Taken up at the next process sample; off, the command is not sent.
            for collector in self.collectors.values():
                collector.host_stats = value
            for view in self.views.values():
                view.set_host_enabled(value)
            if not value:
                for card in self.cards.values():
                    card.set_host(None)
                for alert in [k for k in self.alerts if k[1] in HOST_KINDS]:
                    del self.alerts[alert]
        # Yellow has to start below red, and resolving at or above the trigger
        # would clear an alert the moment it fires.
        for lower in ("temp_warn", "alert_clear_temp"):
            if self.cfg[lower] >= self.cfg["temp_danger"]:
                self.cfg[lower] = self.cfg["temp_danger"] - 1
        TEMP_WARN, TEMP_DANGER = self.cfg["temp_warn"], self.cfg["temp_danger"]
        save_config(self.cfg)
        for alert in [k for k in self.alerts if not self._alert_wanted(k[1])]:
            del self.alerts[alert]
        self._indicated = None
        self._alert_indicate()
        if key in ("alert_enabled", "temp_warn", "temp_danger", "alert_clear_temp"):
            if self.settings is not None and self.settings.winfo_exists():
                self.settings.refresh()

    def _alert_wanted(self, kind):
        return self.cfg["alert_enabled"] and self.cfg["alert_" + kind]

    def _alert_gpus(self, server_id, gpus, full, now):
        groups = {}
        for _severity, key, args in full:
            groups.setdefault(key[len("fault_"):], []).append(args["i"])
        # Once raised, heat counts until it drops below the clear temperature,
        # so a GPU hovering at the trigger does not raise and clear by turns.
        if (server_id, "temp") in self.alerts:
            clear = self.cfg["alert_clear_temp"]
            groups["temp"] = [g["index"] for g in gpus
                              if g["temp"] is not None and g["temp"] >= clear]
        for kind, indices in groups.items():
            if not indices:
                continue
            # One GPU can report several slowdown reasons at once.
            args = {"gpus": ", ".join(dict.fromkeys(indices))}
            if kind == "temp":
                args["temp"] = f"{max(g['temp'] for g in gpus if g['index'] in indices):.0f}"
            self._alert_raise(server_id, kind, args, now)

    def _alert_status(self, server_id, status, key="", args=None, attempt=0):
        now = time.monotonic()
        present = {
            "stalled": status == "stalled",
            "fatal": status in ("auth_failed", "error"),
            # Between tries the collector reports "connecting" with the same
            # count; treating that as recovered would re-raise on every retry.
            "reconnect": (status in ("retrying", "connecting")
                          and attempt >= self.cfg["alert_reconnect_n"]),
        }
        for kind, on in present.items():
            if on:
                self._alert_raise(server_id, kind, {"key": key, "args": args or {},
                                                     "n": attempt}, now)
            # The others wait out the hold in _alert_tick. An auth failure or a
            # fatal error stops retrying, so it cannot flap and ends at once.
            elif kind == "fatal" and self.alerts.pop((server_id, kind), None) is not None:
                self._alert_indicate()

    def _alert_raise(self, server_id, kind, args, now):
        if not self._alert_wanted(kind):
            return
        alert = self.alerts.get((server_id, kind))
        if alert is not None:
            alert["args"], alert["seen"] = args, now
            return
        self.alerts[(server_id, kind)] = {"args": args, "seen": now, "step": 0,
                                          "next": self._alert_next(now, 0)}
        self._alert_fire((server_id, kind))

    def _alert_next(self, now, step):
        mode, minutes = self.cfg["alert_remind"], self.cfg["alert_remind_min"]
        if mode == "off":
            return None
        if mode == "grow":
            minutes = min(REMIND_CAP_MIN, minutes * 2 ** step)
        return now + minutes * 60

    def _alert_fire(self, key):
        """Light the indicators and queue a popup, unless the window is in use."""
        try:
            looking = self.state() == "normal" and self.focus_displayof() is not None
        except KeyError:          # focus is in a widget tkinter did not create
            looking = True
        if looking:
            self._alert_indicate()    # nothing lights, but the tray status line moves
            return
        self.unseen.add(key)
        if self.cfg["alert_popup"]:
            self.pending.add(key)
        self._alert_indicate()

    def _alert_tick(self, now):
        # An alert ends once its problem has been absent for the hold time, so
        # a GPU that stalls and recovers by turns stays one alert. The hold is
        # kept above a few refresh intervals, or a slow one would expire an
        # alert between two samples that both still show it.
        hold = max(self.cfg["alert_clear_min"] * 60, 3 * float(self.cfg["interval_sec"]))
        changed = False
        for key, alert in list(self.alerts.items()):
            status = self.statuses.get(key[0])
            # A reconnect alert stays alive for the whole outage, even across a
            # backoff longer than the hold; samples refresh the GPU ones.
            if ((key[1] == "stalled" and status == "stalled")
                    or (key[1] == "reconnect" and status in ("retrying", "connecting"))):
                alert["seen"] = now
            if key[1] != "fatal" and now - alert["seen"] >= hold:
                del self.alerts[key]
                changed = True
            elif alert["next"] is not None and now >= alert["next"]:
                alert["step"] += 1
                alert["next"] = self._alert_next(now, alert["step"])
                self._alert_fire(key)
        if changed:
            self._alert_indicate()
        self.pending &= self.alerts.keys()
        if self.pending and now - self._toast_at >= TOAST_GAP:
            self._toast_at = now
            self._alert_toast(sorted(self.pending))
            self.pending.clear()

    def _alert_text(self, kind, args):
        if kind in GPU_KINDS + HOST_KINDS:
            return t("alert_" + kind, **args)
        if kind == "reconnect":
            return t("alert_reconnect", n=args["n"])
        if kind == "stalled":
            return t("status_stalled")
        return t(args["key"], **args["args"]) if args["key"] else t("status_error")

    def _alert_toast(self, keys):
        if self.toast is not None and self.toast.winfo_exists():
            self.toast.destroy()
        keys.sort(key=lambda k: k[1] in WARN_KINDS)
        lines = [("warn" if kind in WARN_KINDS else "danger",
                  display_name(self._find(sid)), self._alert_text(kind, self.alerts[(sid, kind)]["args"]))
                 for sid, kind in keys]
        servers = len({sid for sid, _ in keys})
        title = (t("toast_title", n=len(keys)) if servers == 1
                 else t("toast_title_many", s=servers, n=len(keys)))
        seconds = self.cfg["alert_toast_sec"] if self.cfg["alert_toast_timer"] else 0
        self.toast = Toast(self, title, lines, seconds, lambda s=keys[0][0]: self.restore(s))

    def _alerts_seen(self, _event=None):
        if self.unseen:
            self.unseen.clear()
            self._alert_indicate()

    def _unseen_severity(self):
        self.unseen &= self.alerts.keys()
        if not self.unseen:
            return None
        return "warn" if all(kind in WARN_KINDS for _, kind in self.unseen) else "danger"

    def _alert_indicate(self):
        """Bring the title, taskbar or dock, tray and mini sidebar up to date."""
        severity = self._unseen_severity()
        count = len(self.unseen)
        state = (severity, count, self.cfg["alert_taskbar"], self.cfg["alert_tray"])
        if self.mini is not None:
            self.mini.refresh()
        if self.tray is not None:
            self.tray.update_menu()   # the status line on the native menu
        if state == self._indicated:
            return
        self._indicated = state
        prefix = f"{'▲' if severity == 'danger' else '⚠'} ({count}) " if severity else ""
        self.title(prefix + t("app_title"))
        taskbar = severity if self.cfg["alert_taskbar"] else None
        # Neither is touched until something is to be shown, so with alerts off
        # no COM object or bus connection is ever made.
        if sys.platform == "win32":
            if taskbar or _TASKBAR is not None:
                try:
                    windows_taskbar(int(self.wm_frame(), 16), taskbar)
                except OSError:
                    pass          # the colour is a nicety; never fail over it
        elif taskbar or self._dock is not None:
            if self._dock is None:
                self._dock = dbus_connect() or False
            if self._dock:
                self._dock_serial += 1
                try:
                    self._dock.sendall(dbus_message(
                        self._dock_serial, 4,
                        [(1, "o", "/io/github/eadcat/Servitor"),
                         (2, "s", "com.canonical.Unity.LauncherEntry"), (3, "s", "Update")],
                        ("application://" + DOCK_DESKTOP_ID, count if taskbar else 0,
                         bool(taskbar))))
                except OSError:
                    self._dock = False
        if self.tray is not None:
            self.tray.icon = tray_image(severity if self.cfg["alert_tray"] else None)

    def _hold_layout(self, event):
        """While the window is being resized, keep the content at its old size.

        Laying the panels out again at every step of a drag was what made
        resizing crawl on Windows. The content is pinned while the size keeps
        changing and laid out once when it has been still for SETTLE_MS: the
        frame moves freely during a drag and the content catches up after.
        """
        if event.widget is not self:
            return
        size = (event.width, event.height)
        if size == self._seen_size:
            return
        first, self._seen_size = self._seen_size is None, size
        if first:
            return                # the window appearing, not being resized
        if not self._frozen:
            self._frozen = True
            # Its own size, no longer the window's: the panels stop being laid
            # out, and the part outside the window is simply not shown. Tk's own
            # place, in real pixels: CustomTkinter's refuses a width and height.
            tk.Place.place_configure(self.split, relwidth=0, relheight=0,
                                     width=self.split.winfo_width(),
                                     height=self.split.winfo_height())
        if self._freeze_job is not None:
            self.after_cancel(self._freeze_job)
        self._freeze_job = self.after(SETTLE_MS, self._release_layout)

    def _release_layout(self):
        self._freeze_job = None
        self._frozen = False
        tk.Place.place_configure(self.split, relwidth=1, relheight=1, width=0, height=0)

    def _remember_size(self, event):
        """Note the size the window returns to, once it has stopped changing.

        Waiting matters: a window manager maximises in two steps, resizing first
        and marking the window zoomed only afterwards, so a size read during
        that gap is the whole screen with the flag still clear. Leaving
        maximised at close time is no good either -- the window manager answers
        that asynchronously, long after the handler has had to finish.
        """
        if event.widget is not self:
            return
        if self._size_job is not None:
            self.after_cancel(self._size_job)
        self._size_job = self.after(200, self._keep_size)

    def _keep_size(self):
        self._size_job = None
        if self.state() == "zoomed" or (sys.platform.startswith("linux")
                                        and self.attributes("-zoomed")):
            return
        # Read through geometry(), not winfo_width(): CustomTkinter scales the
        # string handed to geometry() by the display factor but leaves
        # winfo_width() in raw pixels, so saving one and feeding back the other
        # grew the window by that factor every launch. On a 150% display it hit
        # the screen edge within two runs and stayed there; at 100%, as on this
        # machine, the two agree and nothing looked wrong.
        size = self.geometry().split("+")[0].split("-")[0]
        self._normal_size = [int(n) for n in size.split("x")]

    def _on_close(self):
        if self.cfg["close_action"] == "ask":
            CloseDialog(self, self._close_as)
        else:
            self._close_as(self.cfg["close_action"], False)

    def _remember_main(self):
        if self.state() != "normal":
            return                # already hidden; what was noted still holds
        zoomed = self.state() == "zoomed" or (sys.platform.startswith("linux")
                                              and self.attributes("-zoomed"))
        self._main_place = "zoomed" if zoomed else self.geometry()

    def _close_as(self, action, remember):
        if remember:
            self.set_close_action(action)
        if action in ("tray", "mini"):
            self._remember_main()
        if action == "tray":
            # pystray calls back on its own thread; after() hands the work to Tk's.
            items = []
            for entry in self._tray_entries():
                if entry is None:
                    items.append(pystray.Menu.SEPARATOR)
                    continue
                label, action = entry
                # pystray passes as many arguments as the action declares,
                # defaults included, so the action must take none at all.
                items.append(pystray.MenuItem(
                    lambda _item, f=label: f(),
                    (lambda f: lambda: self.after(0, f))(action) if action else None,
                    default=action == self.restore, enabled=action is not None))
            self.tray = pystray.Icon(
                APP_NAME, tray_image(self._unseen_severity()
                                     if self.cfg["alert_tray"] else None), "Servitor",
                pystray.Menu(*items),
            )
            # The X11 backend has no menu and drops every button but the left,
            # so a right click is caught here and answered with a Tk menu. Other
            # backends, the Windows one included, show the native menu above.
            # Asked by backend, not by attribute: the Windows icon has its own
            # _message_handlers too, and Xlib does not exist there.
            if type(self.tray).__module__ == "pystray._xorg":
                import Xlib.X
                handlers = self.tray._message_handlers
                left = handlers[Xlib.X.ButtonPress]
                handlers[Xlib.X.ButtonPress] = lambda event: (
                    self.after(0, self._tray_popup) if event.detail == 3 else left(event))
            # Not run_detached(): on X11 it starts the setup thread twice, and the
            # orphan blocks stop() for five seconds and keeps the process alive.
            threading.Thread(target=self.tray.run, daemon=True).start()
            self.withdraw()
        elif action == "mini":
            # Built before withdrawing, while the main window still has a position.
            self.mini = MiniWindow(self, self)
            self.withdraw()
        else:
            self._quit()

    def _tray_entries(self):
        """The tray menu: (label function, action) pairs, None for a separator.

        One list feeds both the native menu and the Tk one, so the two match.
        The first line only reports, which is why it has no action.
        """
        return [
            (self._tray_status, None),
            None,
            (lambda: t("fav_open"), self.restore),
            (lambda: t("tray_mini"), self._tray_to_mini),
            (lambda: t("settings"), self.show_settings),
            None,
            (lambda: t("close_quit"), self._quit),
        ]

    def _tray_status(self):
        if not self.alerts:
            return t("tray_ok")
        keys = sorted(self.alerts, key=lambda k: k[1] in WARN_KINDS)
        sid, kind = keys[0]
        text = t("tray_status", n=len(keys), first=f"{display_name(self._find(sid))} "
                                                   f"{t('kind_' + kind)}")
        return text + (t("tray_more", n=len(keys) - 1) if len(keys) > 1 else "")

    def _tray_popup(self):
        menu = tk.Menu(self, tearoff=0, font=tk_font(self, 12), relief="flat", borderwidth=1,
                       bg=mode_color(PANEL_BG),
                       fg=mode_color(ctk.ThemeManager.theme["CTkLabel"]["text_color"]),
                       activebackground=ACCENT, activeforeground=BUTTON_TEXT,
                       disabledforeground=mode_color(TEXT_MUTED))
        for entry in self._tray_entries():
            if entry is None:
                menu.add_separator()
                continue
            label, action = entry
            menu.add_command(label=label(), command=action,
                             state="normal" if action else "disabled")
        x, y = self.winfo_pointerxy()
        menu.tk_popup(x, y)

    def _mini_to_tray(self):
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
        self._close_as("tray", False)

    def _tray_to_mini(self):
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.mini = MiniWindow(self, self)

    def restore(self, server_id=None):
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
        self.deiconify()
        if self._main_place == "zoomed":
            if sys.platform.startswith("linux"):
                self.attributes("-zoomed", True)
            else:
                self.state("zoomed")
        elif self._main_place:
            self.geometry(self._main_place)
        self.lift()
        self.focus_force()
        if server_id in self.views:
            self._select(server_id)

    def _quit(self):
        if self.tray is not None:
            self.tray.stop()
        if self._size_job is not None:
            self.after_cancel(self._size_job)
        self.cfg["window"] = self._normal_size
        save_config(self.cfg)
        for collector in self.collectors.values():
            collector.stop()
        self.destroy()


def apply_palette():
    """Point CustomTkinter's stock widgets at the NVIDIA-green palette."""
    theme = ctk.ThemeManager.theme
    theme["CTkButton"]["fg_color"] = ACCENT
    theme["CTkButton"]["hover_color"] = ACCENT_HOVER
    theme["CTkButton"]["text_color"] = BUTTON_TEXT
    theme["CTkProgressBar"]["progress_color"] = ACCENT
    theme["CTkEntry"]["border_color"] = BORDER


def install_font():
    """Register the bundled UI font with the OS.

    Tk resolves families through the platform rather than from files, so the
    faces have to be registered before any widget asks for one. CustomTkinter
    already carries the per-OS code for this and uses it for its own Roboto.
    """
    for name in ("Pretendard-Regular.otf", "Pretendard-Bold.otf"):
        ctk.FontManager.load_font(os.path.join(ASSETS_DIR, "fonts", name))


# What the AppImage installs for the user, and its own name for the dock.
INSTALL_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin")
INSTALL_FILES = (
    os.path.join(INSTALL_BIN, "Servitor"),
    os.path.join(INSTALL_BIN, "servitor"),        # the same, for the lowercase habit
    os.path.join(os.path.expanduser("~"), ".local", "share", "applications", DOCK_DESKTOP_ID),
    os.path.join(os.path.expanduser("~"), ".local", "share", "icons", "hicolor", "256x256",
                 "apps", "servitor.png"),
)
# Copied into ~/.fonts when Servitor runs on Linux: its own two and the three
# CustomTkinter brings. Only --remove --purge deletes them.
COPIED_FONTS = ("Pretendard-Regular.otf", "Pretendard-Bold.otf", "Roboto-Regular.ttf",
                "Roboto-Medium.ttf", "CustomTkinter_shapes_font.otf")


def _refresh_menus():
    # Harmless where the tools are missing; desktops without them rescan anyway.
    apps = os.path.dirname(INSTALL_FILES[2])
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", apps], capture_output=True)
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", os.path.join(
            os.path.expanduser("~"), ".local", "share", "icons", "hicolor")], capture_output=True)


def run_command_line(args):
    """Servitor --install, --remove and --remove --purge, for the Linux AppImage.

    An AppImage is one file, so it installs itself rather than shipping a
    script beside it: copied to ~/.local/bin as Servitor and servitor, with a
    menu entry and icon. Returns False when there is nothing to do here.
    """
    if not args:
        return False
    usage = "usage: Servitor [--install | --remove [--purge]]"
    if args[0] not in ("--install", "--remove") or len(args) > 2 or (
            len(args) == 2 and args != ["--remove", "--purge"]):
        print(usage, file=sys.stderr)
        sys.exit(2)
    if not sys.platform.startswith("linux"):
        print("--install and --remove are for the Linux AppImage. On Windows, install with "
              "the setup program and remove Servitor from Control Panel.", file=sys.stderr)
        sys.exit(2)
    program, alias, desktop, icon = INSTALL_FILES
    if args[0] == "--install":
        # Set by the AppImage runtime to the file it was started from.
        source = os.environ.get("APPIMAGE")
        if not source or not os.path.isfile(source):
            print("--install installs the AppImage, so run it from that file:\n"
                  "    ./Servitor-<version>-x86_64.AppImage --install", file=sys.stderr)
            sys.exit(1)
        for path in INSTALL_FILES:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.realpath(source) != os.path.realpath(program):
            # Through a temporary name, so a running copy is replaced, not overwritten.
            shutil.copyfile(source, program + ".new")
            os.chmod(program + ".new", 0o755)
            os.replace(program + ".new", program)
        if os.path.lexists(alias):
            os.remove(alias)
        os.symlink("Servitor", alias)
        shutil.copyfile(os.path.join(ASSETS_DIR, "icon.png"), icon)
        with open(os.path.join(ASSETS_DIR, "servitor.desktop"), encoding="utf-8") as fp:
            entry = re.sub(r"^Exec=.*$", "Exec=" + program, fp.read(), flags=re.M)
        with open(desktop, "w", encoding="utf-8") as fp:
            fp.write(entry)
        _refresh_menus()
        print("installed:")
        for path in INSTALL_FILES:
            print("  " + path)
        if INSTALL_BIN not in os.environ.get("PATH", "").split(os.pathsep):
            print("note: %s is not on PATH yet; log out and back in to run Servitor by name,"
                  " or start it from the applications menu." % INSTALL_BIN)
        print("remove it with:  Servitor --remove   (--remove --purge also deletes your settings)")
        return True

    purge = "--purge" in args
    removed = [path for path in INSTALL_FILES if os.path.lexists(path)]
    for path in removed:
        os.remove(path)
        print("removed " + path)
    if not removed:
        print("Servitor is not installed under ~/.local.")
    _refresh_menus()
    config = os.path.dirname(config_path())
    fonts = [os.path.join(os.path.expanduser("~"), ".fonts", name) for name in COPIED_FONTS]
    if purge:
        if os.path.isdir(config):
            shutil.rmtree(config)
            print("removed " + config)
        gone = [path for path in fonts if os.path.exists(path)]
        for path in gone:
            os.remove(path)
            print("removed " + path)
        if gone and shutil.which("fc-cache"):
            subprocess.run(["fc-cache", "-f", os.path.dirname(fonts[0])], capture_output=True)
    elif os.path.isdir(config):
        print("kept %s (saved servers and passwords); --remove --purge deletes it" % config)
    return True


def main():
    global TEMP_WARN, TEMP_DANGER
    if run_command_line(sys.argv[1:]):
        return
    cfg = load_config()
    TEMP_WARN, TEMP_DANGER = cfg["temp_warn"], cfg["temp_danger"]
    set_font_size(cfg["font_size"])
    set_language(cfg.get("language", "en"))
    ctk.set_appearance_mode(cfg.get("appearance", "dark"))
    ctk.set_default_color_theme("blue")
    apply_palette()
    if sys.platform == "win32":
        # CustomTkinter puts its own logo on every window 200 ms after it opens
        # unless iconbitmap() was called, and iconphoto() does not count; the
        # title bar and taskbar showed its logo while the shortcut showed ours.
        ico = os.path.join(ASSETS_DIR, "icon.ico")
        for cls in (ctk.CTk, ctk.CTkToplevel):
            cls._windows_set_titlebar_icon = lambda self, path=ico: self.iconbitmap(path)
    install_font()
    App(cfg).mainloop()


if __name__ == "__main__":
    main()
