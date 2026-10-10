#!/usr/bin/env python3
"""
Teamtrack desktop app (Windows + macOS) -- clock in / clock out.

The person opens the app and LOGS IN to start work, then clicks LOG OUT when done.
It does NOT start automatically with the computer. While logged in, it records
work/break time to your Teamtrack Google Sheet (system-wide activity, any app),
and it saves the exact login and logout times.

- Standard library only (tkinter for the window). No pip installs needed to run.
- Detects activity from the OS "time since last input". Never records what is typed.
- A break is counted after 5 minutes with no input; each break's start/end is saved.
- Today's totals survive a restart; a person can log out for lunch and back in later
  and the day's total keeps adding up.
"""

import json
import os
import re
import sys
import time
import threading
import platform
import subprocess
import urllib.request
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

# ----------------------------- settings -----------------------------------
BREAK_THRESHOLD = 5 * 60   # default; overridden by the admin setting fetched at login
LOOP_INTERVAL   = 2        # check activity every 2 seconds
SYNC_INTERVAL   = 300      # save to the sheet only every 5 min (plus login & logout)
DELTA_CLAMP     = 120
RESUME_WINDOW   = 6 * 3600 # resume an interrupted shift only if reopened within 6 hours

# --- anti-trick (nudge) detection ---
# A lone nudge (single click / tiny move) must NOT count as resuming work.
# Real work = at least MIN_SUSTAINED active samples within the last WINDOW_SAMPLES.
WINDOW_SAMPLES  = 10       # ~20 seconds of history (10 x 2s)
MIN_SUSTAINED   = 3        # need >=3 active samples in that window to count as "really working"
POPUP_COOLDOWN  = 120      # show the warning popup at most once every 2 minutes
STUCK_SECONDS   = 8        # same key(s) held + mouse not moving this long = fake (weight/long-press)
# "no variety" rule: over NOVAR_WINDOW, if input is basically just key-holds with
# the mouse barely moving and no different keys, it's fake even if nothing is "stuck".
NOVAR_WINDOW    = 45       # seconds of history to judge variety over
NOVAR_MIN_MOVES = 4        # need at least this many real mouse moves in that window
NOVAR_MIN_KEYS  = 3        # ...or at least this many DIFFERENT keys, to count as real work

# Admin: paste your Apps Script /exec URL here BEFORE building the app, so the
# team only enters ID + password. Leave "" to be asked once.
EMBEDDED_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxWVsZsix732bMqeR5Cs9bjVZIAPFxn8fjBU95avlHD7DmNulZRPR0igPT8FIrUo1tr6A/exec"

HOME        = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".teamtrack_agent.json")
STATE_PATH  = os.path.join(HOME, ".teamtrack_agent_state.json")

# brand colors
BG = "#0f1115"; PANEL = "#171a21"; TEXT = "#e7e9ee"; MUTED = "#9aa3b2"
ACCENT = "#4f8cff"; GOOD = "#37c26b"; WARN = "#f0a020"; LINE = "#2a2f3a"


# ----------------------------- OS idle time -------------------------------
def idle_seconds():
    s = platform.system()
    if s == "Windows":
        return _idle_windows()
    if s == "Darwin":
        return _idle_mac()
    return _idle_linux()


def _idle_windows():
    import ctypes

    class LI(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LI(); info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


def _idle_mac():
    try:
        out = subprocess.check_output(["ioreg", "-c", "IOHIDSystem"],
                                      stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1)) / 1_000_000_000.0
    except Exception:
        pass
    return 0.0


def _idle_linux():
    try:
        return int(subprocess.check_output(["xprintidle"], stderr=subprocess.DEVNULL).strip()) / 1000.0
    except Exception:
        return 0.0


# ------------------- stuck-key / frozen-mouse detection -------------------
# A weight on a key sends constant input, so idle time stays ~0 and it LOOKS
# like work. But a human never holds the exact same key(s) with the mouse
# perfectly still for long. So we read which keys are down + the cursor
# position; if that "fingerprint" doesn't change for STUCK_SECONDS, it's fake.

def input_fingerprint():
    """Return (held_keys_tuple, mouse_xy) for this instant, or None if unavailable."""
    s = platform.system()
    if s == "Windows":
        return _fingerprint_windows()
    if s == "Darwin":
        return _fingerprint_mac()
    return None


def _fingerprint_windows():
    try:
        import ctypes
        u = ctypes.windll.user32
        # 0x8000 = key is CURRENTLY down. We ignore the low 0x1 "was pressed since
        # last call" bit, which toggles on a held key and would look like change.
        held = tuple(vk for vk in range(8, 256)
                     if u.GetAsyncKeyState(vk) & 0x8000)

        class PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        p = PT()
        u.GetCursorPos(ctypes.byref(p))
        return (held, (p.x, p.y))
    except Exception:
        return None


def _fingerprint_mac():
    # mouse position via Quartz if available; key state needs the extra module.
    try:
        import Quartz  # type: ignore
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        mouse = (int(loc.x), int(loc.y))
        held = []
        for kc in range(0, 128):
            if Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateHIDSystemState, kc):
                held.append(kc)
        return (tuple(held), mouse)
    except Exception:
        return None


# ------------------- active application logging ---------------------------
# Records WHICH app is in the foreground (e.g. "Excel", "Chrome", "WhatsApp")
# so time can be rolled up per app. Reads the app/window name only -- never the
# screen contents, documents, messages, or what is typed.

def active_app():
    """Return a short, friendly name of the foreground app, or '' if unknown."""
    s = platform.system()
    try:
        if s == "Windows":
            return _active_app_windows()
        if s == "Darwin":
            return _active_app_mac()
    except Exception:
        pass
    return ""


def _active_app_windows():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    PROCESS_QUERY_LIMITED = 0x1000
    h = k.OpenProcess(PROCESS_QUERY_LIMITED, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        # QueryFullProcessImageNameW
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            exe = buf.value.rsplit("\\", 1)[-1]        # e.g. EXCEL.EXE
            return _friendly(exe)
    finally:
        k.CloseHandle(h)
    return ""


def _active_app_mac():
    out = subprocess.check_output(
        ["osascript", "-e",
         'tell application "System Events" to get name of first application process whose frontmost is true'],
        stderr=subprocess.DEVNULL).decode("utf-8", "ignore").strip()
    return _friendly(out)


# map common process/app names to friendly labels; otherwise clean the raw name
_APP_NAMES = {
    "excel": "Excel", "winword": "Word", "powerpnt": "PowerPoint", "outlook": "Outlook",
    "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "brave": "Brave",
    "safari": "Safari", "whatsapp": "WhatsApp", "telegram": "Telegram", "slack": "Slack",
    "teams": "Teams", "ms-teams": "Teams", "zoom": "Zoom", "code": "VS Code",
    "notepad": "Notepad", "explorer": "File Explorer", "finder": "Finder",
    "spotify": "Spotify", "vlc": "VLC", "acrobat": "Acrobat", "acrord32": "Acrobat",
    "googlechrome": "Chrome", "microsoft edge": "Edge", "youtube": "YouTube",
}

def _friendly(name):
    if not name:
        return ""
    key = name.lower().replace(".exe", "").replace(".app", "").strip()
    if key in _APP_NAMES:
        return _APP_NAMES[key]
    # Title-case a reasonable fallback, trimmed to keep the sheet tidy
    clean = name.replace(".exe", "").replace(".app", "").strip()
    return (clean[:24]) if clean else ""


# ----------------------------- time helpers -------------------------------
def date_str(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))

def time_str(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))

def dt_str(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

def hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))

def fmt(sec):
    sec = int(round(sec or 0)); h = sec // 3600; m = (sec % 3600) // 60
    return ("%dh %dm" % (h, m)) if h else ("%dm" % m if m else "0m")


# ----------------------------- HTTP ---------------------------------------
def call(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "text/plain;charset=UTF-8",
                                          "User-Agent": "TeamtrackAgent/2.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def do_login(url, username, password):
    try:
        r = call(url, {"action": "login", "username": username, "password": password})
    except Exception:
        return None, "Can't reach the server. Check your internet."
    if r.get("token"):
        return r["token"], None
    return None, (r.get("error") or "Login failed")


def fetch_break_threshold(url):
    """Read the admin-set break threshold (minutes) from the backend. Default 5."""
    try:
        r = call(url, {"action": "getSettings"})
        m = int(r.get("breakMinutes"))
        if 1 <= m <= 5:
            return m * 60
    except Exception:
        pass
    return 5 * 60


# ----------------------------- state / config -----------------------------
def load_saved_cfg():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cfg(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def fresh_state(now):
    return {"date": date_str(now), "login": now, "work_secs": 0.0,
            "break_secs": 0.0, "on_break": False, "break_start": None, "breaks": [],
            "apps": {}, "open": True, "last_save": now}


def apps_text(st, top=6):
    """Compact 'Excel 3h 10m, Chrome 1h, WhatsApp 20m' summary, biggest first."""
    apps = st.get("apps") or {}
    items = sorted(apps.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return ", ".join("%s %s" % (name, fmt(sec)) for name, sec in items if sec >= 30)


def load_saved_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _valid_state(st):
    """A resumed state must have every key the loop relies on, with sane types."""
    if not isinstance(st, dict):
        return False
    base = fresh_state(time.time())
    for k, v in base.items():
        if k not in st or not isinstance(st[k], type(v)):
            return False
    return True


def start_or_resume(now):
    """Resume a shift only if it was left OPEN recently (accidental close / crash).
    A cleanly logged-out shift, an old one, or a corrupt file starts fresh -- so a
    night shift ending after midnight never carries over, and bad data never crashes."""
    try:
        saved = load_saved_state()
        if (saved and _valid_state(saved) and saved.get("open")
                and (now - saved.get("last_save", 0) < RESUME_WINDOW)):
            saved["open"] = True
            return saved
    except Exception:
        pass
    return fresh_state(now)


def save_state(st):
    try:
        st["last_save"] = time.time()
        with open(STATE_PATH, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


def breaks_text(st):
    parts = ["%s\u2013%s" % (hm(s), hm(e)) for s, e in st["breaks"]]
    if st["on_break"] and st["break_start"]:
        parts.append("%s\u2013\u2026" % hm(st["break_start"]))
    return "; ".join(parts)


def send_sync(url, token, st, username, status_override=None, out=None):
    payload = {"action": "sync", "token": token,
               "date": st["date"], "login": time_str(st["login"]),
               "workingSeconds": int(round(st["work_secs"])),
               "breakSeconds": int(round(st["break_secs"])),
               "apps": apps_text(st)}
    if out:
        payload["out"] = out
    r = call(url, payload)
    if isinstance(r, dict) and r.get("error"):
        # token expired -> re-login handled by caller via return None
        return False
    return True


# ----------------------------- tracking loop ------------------------------
def run_tracking(url, token, st, username, on_update, stop_event,
                 threshold=BREAK_THRESHOLD, on_popup=None):
    from collections import deque
    last_loop = time.time(); last_sync = 0.0
    window = deque(maxlen=WINDOW_SAMPLES)      # recent active/idle samples
    last_real_active = time.time()             # last time of SUSTAINED (real) activity
    popup_until = 0.0
    last_held = ();  held_since = time.time()  # which keys are held + since when (keys only)
    # "no variety" history: (timestamp, keys_frozenset, moved?) per sample
    var_hist = deque()
    last_mouse = None

    while not stop_event.is_set():
        try:
            now = time.time()
            delta = min(DELTA_CLAMP, max(0.0, now - last_loop)); last_loop = now

            # NOTE: no midnight rollover -- a shift keeps its login date until logout,
            # so night shifts (e.g. 8pm-1am) stay on ONE row and never carry over.

            try:
                raw_idle = idle_seconds()
            except Exception:
                raw_idle = 0.0
            active_now = raw_idle < (LOOP_INTERVAL + 1)   # any input in the last sample?

            # stuck-key / frozen-mouse check: if the held keys + cursor position have
            # not changed at all for STUCK_SECONDS, the "input" is fake (weight on a
            # key, cursor frozen). A real person's keys/cursor change constantly.
            try:
                fp = input_fingerprint()
            except Exception:
                fp = None
            if fp is not None:
                keys, mouse = fp[0], fp[1]

                # --- stuck key: the SAME non-empty key set held, unchanged,
                #     for STUCK_SECONDS. Mouse is ignored here, so wiggling it
                #     does not reset the timer (that's the whole point). ---
                held_set = frozenset(keys)
                if held_set and held_set == last_held:
                    pass                          # still holding the same key(s)
                else:
                    last_held = held_set; held_since = now
                if held_set and (now - held_since) >= STUCK_SECONDS:
                    active_now = False            # key held too long -> fake
                    last_real_active = held_since # stop the work clock from the hold start

                # --- "no variety" rule: over the window, mostly key-holds with
                #     very few real mouse moves and few different keys = fake.
                #     Catches "hold a key + wiggle the mouse a little". ---
                moved = last_mouse is not None and (
                    abs(mouse[0] - last_mouse[0]) + abs(mouse[1] - last_mouse[1]) > 3)
                last_mouse = mouse
                var_hist.append((now, held_set, 1 if moved else 0))
                while var_hist and now - var_hist[0][0] > NOVAR_WINDOW:
                    var_hist.popleft()
                if active_now and var_hist and (now - var_hist[0][0]) >= NOVAR_WINDOW * 0.6:
                    distinct_keys = set()
                    for _, ks, _ in var_hist:
                        distinct_keys |= ks
                    moves = sum(m for _, _, m in var_hist)
                    had_any_input = any(ks or m for _, ks, m in var_hist)
                    if (had_any_input and moves < NOVAR_MIN_MOVES
                            and len(distinct_keys) < NOVAR_MIN_KEYS):
                        active_now = False
                        last_real_active = var_hist[0][0]

            window.append(1 if active_now else 0)
            sustained = sum(window) >= MIN_SUSTAINED      # real work, not a lone nudge
            if sustained:
                last_real_active = now

            eff_idle = now - last_real_active             # idle that ignores nudges

            if eff_idle < threshold:
                st["work_secs"] += delta
                try:                                       # log foreground app (name only)
                    app = active_app()
                    if app:
                        st["apps"][app] = st["apps"].get(app, 0.0) + delta
                except Exception:
                    pass
                if st["on_break"]:
                    st["breaks"].append([st["break_start"], now])
                    st["on_break"] = False; st["break_start"] = None
            else:
                if not st["on_break"]:
                    st["on_break"] = True; st["break_start"] = last_real_active
                    st["work_secs"] = max(0.0, st["work_secs"] - threshold)
                    st["break_secs"] += threshold
                else:
                    st["break_secs"] += delta
                if active_now and not sustained and on_popup and now >= popup_until:
                    on_popup()
                    popup_until = now + POPUP_COOLDOWN

            try:
                on_update("on_break" if st["on_break"] else "working")
            except Exception:
                pass

            if now - last_sync >= SYNC_INTERVAL:
                try: send_sync(url, token, st, username)
                except Exception: pass
                save_state(st); last_sync = now
        except Exception:
            # one bad iteration must never kill tracking -- skip and keep going
            pass

        time.sleep(LOOP_INTERVAL)


# ============================= GUI ========================================
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_saved_cfg()
        self.url = EMBEDDED_SCRIPT_URL or self.cfg.get("url", "https://script.google.com/macros/s/AKfycbxWVsZsix732bMqeR5Cs9bjVZIAPFxn8fjBU95avlHD7DmNulZRPR0igPT8FIrUo1tr6A/exec")
        self.token = None
        self.username = None
        self.session = None
        self.stop_event = None
        self.thread = None
        self.status = "working"

        root.title("Teamtrack")
        root.configure(bg=BG)
        root.geometry("380x460")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            self.h1 = tkfont.Font(family="Helvetica", size=22, weight="bold")
            self.big = tkfont.Font(family="Helvetica", size=17, weight="bold")
            self.base = tkfont.Font(family="Helvetica", size=12)
        except Exception:                      # some systems lack named fonts
            self.h1 = self.big = self.base = None

        self._build_login()
        self._build_status()
        self.show_login()

    # ---- widgets ----
    def _label(self, parent, text, fnt, fg=TEXT):
        return tk.Label(parent, text=text, font=fnt, fg=fg, bg=BG)

    def _entry(self, parent, show=None):
        e = tk.Entry(parent, show=show, font=self.base, bg=PANEL, fg=TEXT,
                     insertbackground=TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT)
        e.configure(width=28)
        return e

    def _button(self, parent, text, cmd, bg=ACCENT, fg="#ffffff"):
        return tk.Button(parent, text=text, command=cmd, font=self.big, bg=bg, fg=fg,
                         activebackground=bg, activeforeground=fg, relief="flat",
                         bd=0, padx=10, pady=10, cursor="hand2")

    def _build_login(self):
        f = tk.Frame(self.root, bg=BG); self.login_frame = f
        self._label(f, "Teamtrack", self.h1, ACCENT).pack(pady=(34, 4))
        self._label(f, "Log in to start work", self.base, MUTED).pack(pady=(0, 20))

        self.url_row = tk.Frame(f, bg=BG)
        self._label(self.url_row, "Server URL", self.base, MUTED).pack(anchor="w")
        self.url_entry = self._entry(self.url_row)
        self.url_entry.pack(pady=(2, 12))
        if not self.url:                       # only show if not embedded/saved
            self.url_row.pack()

        self._label(f, "Person ID", self.base, MUTED).pack(anchor="w", padx=36)
        self.user_entry = self._entry(f); self.user_entry.pack(pady=(2, 12))
        self.user_entry.insert(0, self.cfg.get("username", ""))

        self._label(f, "Password", self.base, MUTED).pack(anchor="w", padx=36)
        self.pass_entry = self._entry(f, show="*"); self.pass_entry.pack(pady=(2, 6))
        self.pass_entry.insert(0, self.cfg.get("password", ""))

        self.login_err = self._label(f, "", self.base, WARN); self.login_err.pack(pady=(4, 8))
        self._button(f, "Log in  &  start work", self.on_login).pack(pady=(6, 0))
        self.root.bind("<Return>", lambda e: self.on_login())

    def _build_status(self):
        f = tk.Frame(self.root, bg=BG); self.status_frame = f
        self.who = self._label(f, "", self.base, MUTED); self.who.pack(pady=(30, 8))
        self.dot = tk.Canvas(f, width=110, height=110, bg=BG, highlightthickness=0)
        self.dot.pack()
        self.badge = self._label(f, "Working", self.big, GOOD); self.badge.pack(pady=(6, 20))

        self.work_lbl = self._label(f, "Working today: 0m", self.base); self.work_lbl.pack(pady=2)
        self.break_lbl = self._label(f, "Break today: 0m", self.base); self.break_lbl.pack(pady=2)
        self.started_lbl = self._label(f, "Started: --", self.base, MUTED); self.started_lbl.pack(pady=2)

        self._button(f, "Log out  (end work)", self.on_logout, bg="#e5544b").pack(pady=(26, 6))
        self._label(f, "Keep this open while you work.\nYou can minimise it.",
                    self.base, MUTED).pack(pady=(6, 0))

    def show_login(self):
        self.status_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    def show_status(self):
        self.login_frame.pack_forget()
        self.status_frame.pack(fill="both", expand=True)

    def _draw_dot(self, color):
        self.dot.delete("all")
        self.dot.create_oval(15, 15, 95, 95, fill=color, outline="")

    # ---- actions ----
    def on_login(self):
        try:
            url = self.url or self.url_entry.get().strip()
            username = self.user_entry.get().strip()
            password = self.pass_entry.get()
            if not (url and username and password):
                self.login_err.config(text="Please fill in all fields."); return
            self.login_err.config(text="Logging in...")
            self.root.update_idletasks()
            token, err = do_login(url, username, password)
            if not token:
                self.login_err.config(text=err or "Login failed. Try again."); return
            self.login_err.config(text="")
            self.url, self.token, self.username = url, token, username
            try:
                self.threshold = fetch_break_threshold(url)
            except Exception:
                self.threshold = BREAK_THRESHOLD          # fall back to default
            save_cfg({"url": url, "username": username, "password": password})
            self._start_session()
            self.show_status()
        except Exception as e:
            # never leave the user stuck on a frozen screen
            try:
                self.login_err.config(text="Something went wrong. Please try again.")
            except Exception:
                pass

    def _start_session(self):
        now = time.time()
        self.session = start_or_resume(now)     # resume only a recently-interrupted shift
        self.status = "on_break" if self.session.get("on_break") else "working"
        self.who.config(text="Logged in as " + self.username)
        self.started_lbl.config(text="Started: " + hm(self.session["login"]))
        self._popup_pending = False
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=run_tracking,
            args=(self.url, self.token, self.session, self.username, self._on_update, self.stop_event),
            kwargs={"threshold": getattr(self, "threshold", BREAK_THRESHOLD),
                    "on_popup": self._flag_popup},
            daemon=True)
        self.thread.start()
        self._tick_ui()

    def _flag_popup(self):
        self._popup_pending = True               # set by worker thread; UI shows it

    def _on_update(self, status):
        self.status = status                    # set by worker thread; UI polls it

    def _tick_ui(self):
        if self.session is None:
            return
        try:
            if getattr(self, "_popup_pending", False):
                self._popup_pending = False
                messagebox.showwarning(
                    "Teamtrack",
                    "You appear to be away from your work.\n\n"
                    "Nudging the mouse or tapping a key to keep the timer active does not "
                    "count as working \u2014 this time is being recorded as a break.\n\n"
                    "Please resume actual work.")
            st = self.session
            self.work_lbl.config(text="Working today: " + fmt(st["work_secs"]))
            self.break_lbl.config(text="Break today: " + fmt(st["break_secs"]))
            if self.status == "on_break":
                self._draw_dot(WARN); self.badge.config(text="On break", fg=WARN)
            else:
                self._draw_dot(GOOD); self.badge.config(text="Working", fg=GOOD)
        except Exception:
            pass                               # a draw hiccup must not stop the clock
        self.root.after(1000, self._tick_ui)   # always reschedule

    def _stop_thread(self):
        if self.stop_event:
            self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def on_logout(self):
        if self.session:
            self._stop_thread()
            self.session["open"] = False        # shift is finished -> won't resume next login
            try:
                send_sync(self.url, self.token, self.session, self.username,
                          out=time_str(time.time()))
            except Exception:
                pass
            save_state(self.session)
        self.session = None; self.stop_event = None; self.thread = None
        self.pass_entry.delete(0, "end")
        self.pass_entry.insert(0, self.cfg.get("password", ""))
        self.login_err.config(text="Logged out. Work time saved.")
        self.show_login()

    def on_close(self):
        # closing the window is NOT a logout -- keep the shift open so a re-login
        # within a few hours resumes it; just checkpoint the totals first.
        if self.session:
            self._stop_thread()
            try:
                send_sync(self.url, self.token, self.session, self.username)
            except Exception:
                pass
            save_state(self.session)
        self.root.destroy()


def main():
    try:
        root = tk.Tk()
    except Exception as e:
        # no display / tkinter broken -- nothing we can show a window with
        sys.stderr.write("Teamtrack could not start a window: %s\n" % e)
        return
    try:
        App(root)
    except Exception as e:
        # building the UI failed -- show a plain error box instead of vanishing
        try:
            from tkinter import messagebox as _mb
            _mb.showerror("Teamtrack",
                          "Teamtrack could not start.\n\n%s\n\n"
                          "Please close and open it again, or contact your admin." % e)
        except Exception:
            sys.stderr.write("Teamtrack failed to start: %s\n" % e)
        return
    try:
        root.mainloop()
    except Exception as e:
        sys.stderr.write("Teamtrack stopped: %s\n" % e)


if __name__ == "__main__":
    main()
