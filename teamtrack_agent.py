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

# Admin: paste your Apps Script /exec URL here BEFORE building the app, so the
# team only enters ID + password. Leave "" to be asked once.
EMBEDDED_SCRIPT_URL = ""

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
            "open": True, "last_save": now}


def load_saved_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def start_or_resume(now):
    """Resume a shift only if it was left OPEN recently (accidental close / crash).
    A cleanly logged-out shift, or an old one, starts fresh -- so a night shift that
    ended after midnight never carries over into the next day's login."""
    saved = load_saved_state()
    if saved and saved.get("open") and (now - saved.get("last_save", 0) < RESUME_WINDOW):
        saved["open"] = True
        return saved
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
               "breakSeconds": int(round(st["break_secs"]))}
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

    while not stop_event.is_set():
        now = time.time()
        delta = min(DELTA_CLAMP, max(0.0, now - last_loop)); last_loop = now

        # NOTE: no midnight rollover -- a shift keeps its login date until logout,
        # so night shifts (e.g. 8pm-1am) stay on ONE row and never carry over.

        raw_idle = idle_seconds()
        active_now = raw_idle < (LOOP_INTERVAL + 1)     # any input in the last sample?
        window.append(1 if active_now else 0)
        sustained = sum(window) >= MIN_SUSTAINED        # real work, not a lone nudge
        if sustained:
            last_real_active = now

        eff_idle = now - last_real_active               # idle that ignores nudges

        if eff_idle < threshold:
            st["work_secs"] += delta
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
            # trick detection: they're on break but just nudged to fake activity
            if active_now and not sustained and on_popup and now >= popup_until:
                on_popup()
                popup_until = now + POPUP_COOLDOWN

        on_update("on_break" if st["on_break"] else "working")

        if now - last_sync >= SYNC_INTERVAL:
            try: send_sync(url, token, st, username)
            except Exception: pass
            save_state(st); last_sync = now

        time.sleep(LOOP_INTERVAL)


# ============================= GUI ========================================
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_saved_cfg()
        self.url = EMBEDDED_SCRIPT_URL or self.cfg.get("url", "")
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

        self.h1 = tkfont.Font(family="Helvetica", size=22, weight="bold")
        self.big = tkfont.Font(family="Helvetica", size=17, weight="bold")
        self.base = tkfont.Font(family="Helvetica", size=12)

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
        url = self.url or self.url_entry.get().strip()
        username = self.user_entry.get().strip()
        password = self.pass_entry.get()
        if not (url and username and password):
            self.login_err.config(text="Please fill in all fields."); return
        self.login_err.config(text="Logging in...")
        self.root.update_idletasks()
        token, err = do_login(url, username, password)
        if not token:
            self.login_err.config(text=err); return
        self.login_err.config(text="")
        self.url, self.token, self.username = url, token, username
        self.threshold = fetch_break_threshold(url)   # admin-set break minutes
        save_cfg({"url": url, "username": username, "password": password})
        self._start_session()
        self.show_status()

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
        self.root.after(1000, self._tick_ui)

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
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
