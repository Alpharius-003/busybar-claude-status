#!/usr/bin/env python3
"""BusyBar Claude Code status daemon.

Collects status reports from Claude Code (statusline JSON + hook state
events) and serves them to renderers:

    statusline-command.sh --.
                            +--> POST 127.0.0.1:8765 --> daemon (session store)
    settings.json hooks ----'                              |
                                                           +--> GET /status
                                                           |    (device JS app polls
                                                           |     via USB network)
                                                           +--> direct render
                                                                (RENDER_MODE)

Renders directly to the device with pre-rendered .anim ring animations
(played natively by the firmware). RENDER_MODE picks the trigger:
always-on ("auto") or gated by the on-device theme selection ("theme").
GET /status also feeds the future >=1.2.0 on-device JS app
(install_app.py), which polls it over the USB network (10.0.4.21).

Layout (72x16 front display, same in both renderers):

    ############################   1px ring: native .anim per state
    #  Fable 5 max      [####-] #   model+effort (/effort color) | ctx bar
    #  5h95% 7d99%        WORK  #   paired plan usage | state word
    ############################
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

LISTEN_PORT = 8765
LISTEN_ADDRS = ["127.0.0.1", "10.0.4.21"]  # loopback + USB network (device side)

# How the daemon renders to the device (env BUSYBAR_RENDER_MODE overrides):
#   "auto"  - whenever Claude Code is active (the always-on behavior).
#   "theme" - only while "claude" is the device's currently selected
#             BUSY/CUSTOM theme. That makes the on-device theme picker a
#             manual on/off switch for the status display (see claude_card.py
#             to bind the CUSTOM key). NOTE: while any focus session is
#             actually RUNNING, firmware 1.1.1 blocks all canvas drawing,
#             so the display pauses and resumes when the session ends.
#   "off"   - data bridge only (for the future >=1.2.0 JS app).
RENDER_MODE = os.environ.get("BUSYBAR_RENDER_MODE", "auto")
APP_NAME = "claude_status"   # canvas app name; .anim assets live under it
DRAW_PRIORITY = 50
THEME_NAME = "claude"        # installed in /ext/apps_assets/busy/themes/
SNAPSHOT_POLL_S = 2.0

TEXT_TIMEOUT_S = 15
ANIM_TIMEOUT_S = 120
ANIM_REFRESH_S = 60.0
KEEPALIVE_S = 8.0
COMPLETE_HOLD_S = 30.0
IDLE_CLEAR_AFTER_S = 600.0
SESSION_EXPIRE_S = 6 * 3600

STATE_ANIMS = {
    "THINKING": "think.anim", "WORKING": "work.anim", "WAIT": "wait.anim",
    "ERROR": "error.anim", "FAILED": "error.anim", "COMPLETE": "done.anim",
    "IDLE": "idle.anim",
}
STATE_WORDS = {
    "THINKING": "THINK", "WORKING": "WORK", "WAIT": "WAIT",
    "ERROR": "ERR", "FAILED": "FAIL", "COMPLETE": "DONE", "IDLE": "IDLE",
}
STATE_COLORS = {
    "THINKING": "#AF87FFFF", "WORKING": "#FFB000FF", "WAIT": "#FF6A00FF",
    "ERROR": "#FF2020FF", "FAILED": "#FF2020FF", "COMPLETE": "#20C040FF",
    "IDLE": "#808080FF",
}
# Straight from the Claude Code theme palette:
# inactive / permission / warning / fastMode / effortUltra
EFFORT_COLORS = {
    "low": "#999999FF", "medium": "#99CCFFFF", "high": "#FFC107FF",
    "xhigh": "#FF7814FF", "max": "#AF87FFFF",
}
MODEL_FALLBACK_COLOR = "#FFFFFFFF"
USAGE_COLOR = "#A0A0A0FF"
FONT = "small"

BAR_X, BAR_Y, BAR_W, BAR_H = 50, 3, 20, 4
BAR_TRACK_COLOR = "#262626FF"
MODEL_MAX_PX = BAR_X - 2 - 3


class Transport:
    """Pluggable link to the Busy Bar device."""

    def draw(self, payload: dict) -> bool:
        raise NotImplementedError

    def clear(self, app_name: str) -> bool:
        raise NotImplementedError


class UsbHttpTransport(Transport):
    """Device plugged in over USB: fixed address, no auth."""

    BASE = "http://10.0.4.20/api"
    TIMEOUT_S = 2.0

    def _request(self, method: str, path: str, body: bytes | None = None) -> bool:
        req = urllib.request.Request(
            self.BASE + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_S):
                return True
        except urllib.error.HTTPError as e:
            if e.code != 409:  # 409: an active BUSY session owns the screen
                log(f"device HTTP {e.code} on {method} {path}")
            return False
        except OSError:
            return False

    def draw(self, payload: dict) -> bool:
        return self._request("POST", "/display/draw", json.dumps(payload).encode())

    def clear(self, app_name: str) -> bool:
        return self._request(
            "DELETE", "/display/draw?application_name=" + urllib.parse.quote(app_name)
        )

    def get_json(self, path: str) -> dict | None:
        try:
            with urllib.request.urlopen(self.BASE + path, timeout=self.TIMEOUT_S) as r:
                return json.loads(r.read())
        except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
            return None


# --------------------------------------------------------------------------
# Session store
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.dirty = threading.Event()

    def _session(self, sid: str) -> dict:
        return self.sessions.setdefault(
            sid, {"state": "IDLE", "state_ts": 0.0, "last_active": 0.0, "data": {}}
        )

    def report_state(self, sid: str, state: str):
        now = time.time()
        with self.lock:
            if state == "ENDED":
                self.sessions.pop(sid, None)
            else:
                s = self._session(sid)
                s["state"] = state
                s["state_ts"] = now
                s["last_active"] = now
        self.dirty.set()

    def report_statusline(self, sid: str, data: dict):
        now = time.time()
        with self.lock:
            s = self._session(sid)
            s["data"] = data
            s["last_active"] = now
        self.dirty.set()

    def active_session(self) -> dict | None:
        now = time.time()
        with self.lock:
            for sid in [
                sid for sid, s in self.sessions.items()
                if now - s["last_active"] > SESSION_EXPIRE_S
            ]:
                del self.sessions[sid]
            if not self.sessions:
                return None
            return max(self.sessions.values(), key=lambda s: s["last_active"])


STORE = Store()


# --------------------------------------------------------------------------
# Data extraction
# --------------------------------------------------------------------------

def effective_state(sess: dict) -> str:
    state = sess["state"]
    if state == "COMPLETE" and time.time() - sess["state_ts"] > COMPLETE_HOLD_S:
        return "IDLE"
    return state


def ctx_used_pct(sess: dict) -> float | None:
    ctx = sess["data"].get("context_window") or {}
    if ctx.get("used_percentage") is not None:
        return ctx["used_percentage"]
    if ctx.get("remaining_percentage") is not None:
        return 100 - ctx["remaining_percentage"]
    return None


def plan_left_pct(sess: dict, window: str) -> int | None:
    w = (sess["data"].get("rate_limits") or {}).get(window) or {}
    if w.get("used_percentage") is None:
        return None
    resets = w.get("resets_at")
    if resets and resets <= time.time():
        return 100
    return max(0, round(100 - w["used_percentage"]))


def status_snapshot() -> dict:
    """The merged view served on GET /status (what renderers consume)."""
    sess = STORE.active_session()
    if sess is None:
        return {"state": "IDLE", "model": None, "effort": None,
                "ctx_used": None, "five_left": None, "week_left": None, "age_s": None}
    d = sess["data"]
    return {
        "state": effective_state(sess),
        "model": (d.get("model") or {}).get("display_name")
                 or (d.get("model") or {}).get("id"),
        "effort": (d.get("effort") or {}).get("level"),
        "ctx_used": ctx_used_pct(sess),
        "five_left": plan_left_pct(sess, "five_hour"),
        "week_left": plan_left_pct(sess, "seven_day"),
        "age_s": round(time.time() - sess["last_active"], 1),
    }


def ctx_color(used: float) -> str:
    if used >= 90:
        return "#FF2020FF"
    if used >= 80:
        return "#FF6A00FF"
    if used >= 50:
        return "#FFB000FF"
    return "#20C040FF"


def plan_color(left: int) -> str:
    if left <= 10:
        return "#FF2020FF"
    if left <= 25:
        return "#FF6A00FF"
    return USAGE_COLOR


# The small font is proportional (measured on-device); deliberately errs wide.
_NARROW = set("iljI.,;:' ")


def est_width(text: str) -> int:
    w = 0
    for ch in text:
        if ch in _NARROW:
            w += 3
        elif ch.isdigit():
            w += 4
        elif ch.isupper() or ch in "MWmw%":
            w += 5
        else:
            w += 4
    return w


# --------------------------------------------------------------------------
# Direct-push renderer (optional; mirrors the device JS app's visuals)
# --------------------------------------------------------------------------

def _rect(eid, x, y, w, h, color):
    return {"id": eid, "type": "rectangle", "display": "front",
            "x": x, "y": y, "width": w, "height": h,
            "border_width": 0,  # undocumented; defaults to 1px white border
            "fill": "solid", "fill_colors": [color], "timeout": TEXT_TIMEOUT_S}


def _text(eid, x, y, align, text, color):
    return {"id": eid, "type": "text", "display": "front",
            "x": x, "y": y, "align": align,
            "text": text, "font": FONT, "color": color, "timeout": TEXT_TIMEOUT_S}


def anim_element(state: str) -> dict:
    return {"id": "ring", "type": "animation", "display": "front",
            "x": 0, "y": 0, "path": STATE_ANIMS.get(state, "idle.anim"),
            "loop": True, "timeout": ANIM_TIMEOUT_S}


def info_elements(status: dict) -> list[dict]:
    """Text rows + ctx bar for a /status snapshot (ring is separate)."""
    elements = []
    state = status["state"]

    name, effort = status.get("model") or "", status.get("effort") or ""
    label = f"{name} {effort}".strip()
    while len(label) > 3 and est_width(label) > MODEL_MAX_PX:
        name = name[:-1]
        label = f"{name} {effort}".strip()
    if label:
        color = EFFORT_COLORS.get(effort, MODEL_FALLBACK_COLOR)
        elements.append(_text("model", 3, 0, "top_left", label, color))

    used = status.get("ctx_used")
    elements.append(_rect("ctrack", BAR_X, BAR_Y, BAR_W, BAR_H, BAR_TRACK_COLOR))
    if used is not None and used > 0:
        fill = max(1, min(BAR_W, round(BAR_W * used / 100)))
        elements.append(_rect("cfill", BAR_X, BAR_Y, fill, BAR_H, ctx_color(used)))

    five, week = status.get("five_left"), status.get("week_left")
    if five is not None and week is not None:
        usage, worst = f"5h{five}% 7d{week}%", min(five, week)
    elif five is not None:
        usage, worst = f"5h{five}%", five
    elif week is not None:
        usage, worst = f"7d{week}%", week
    else:
        usage = None
    if usage:
        elements.append(_text("usage", 3, 15, "bottom_left", usage, plan_color(worst)))

    elements.append(_text("state", 69, 15, "bottom_right",
                          STATE_WORDS.get(state, state[:5]),
                          STATE_COLORS.get(state, STATE_COLORS["IDLE"])))
    return elements


THEME_ACTIVE = threading.Event()


def snapshot_watch_loop(transport: Transport, stop: threading.Event):
    """Poll the device's BUSY snapshot: the currently selected theme acts
    as the on-device manual switch for the status display."""
    while not stop.is_set():
        active = False
        snap = transport.get_json("/busy/snapshot")
        if snap:
            s = snap.get("snapshot") or {}
            active = (s.get("busy_bar_settings") or {}).get("theme") == THEME_NAME
        if active != THEME_ACTIVE.is_set():
            log(f"claude theme {'selected' if active else 'deselected'} on device")
            (THEME_ACTIVE.set if active else THEME_ACTIVE.clear)()
            STORE.dirty.set()
        stop.wait(SNAPSHOT_POLL_S)


def render_loop(transport: Transport, stop: threading.Event):
    """State-driven anim swap + text updates; the theme gate (if enabled)
    decides whether we render at all."""
    last_texts = None
    last_texts_ts = 0.0
    last_anim = None
    last_anim_ts = 0.0
    drawn = False
    while not stop.is_set():
        STORE.dirty.clear()
        now = time.time()
        sess = STORE.active_session()

        want = False
        if sess is not None:
            idle_expired = (
                effective_state(sess) == "IDLE"
                and now - max(sess["state_ts"], sess["last_active"]) > IDLE_CLEAR_AFTER_S
            )
            gate_ok = THEME_ACTIVE.is_set() if RENDER_MODE == "theme" else True
            want = gate_ok and not idle_expired

        if not want:
            if drawn:
                transport.clear(APP_NAME)
                drawn, last_anim, last_texts = False, None, None
        else:
            priority = DRAW_PRIORITY
            status = status_snapshot()
            anim = anim_element(status["state"])
            if anim["path"] != last_anim or now - last_anim_ts > ANIM_REFRESH_S:
                if transport.draw({"application_name": APP_NAME,
                                   "priority": priority, "elements": [anim]}):
                    last_anim, last_anim_ts, drawn = anim["path"], now, True
            texts = info_elements(status)
            encoded = json.dumps(texts, sort_keys=True)
            if encoded != last_texts or now - last_texts_ts > KEEPALIVE_S:
                if transport.draw({"application_name": APP_NAME,
                                   "priority": priority, "elements": texts}):
                    last_texts, last_texts_ts, drawn = encoded, now, True
        STORE.dirty.wait(timeout=0.5)


# --------------------------------------------------------------------------
# Report/status server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code: int, body: bytes = b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            self._reply(200, json.dumps(status_snapshot()).encode())
        elif self.path == "/health":
            with STORE.lock:
                snapshot = {
                    sid: {k: s[k] for k in ("state", "state_ts", "last_active")}
                    for sid, s in STORE.sessions.items()
                }
            self._reply(200, json.dumps(snapshot).encode())
        else:
            self._reply(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        sid = data.get("session_id") or "unknown"

        if parsed.path == "/state":
            state = urllib.parse.parse_qs(parsed.query).get("state", ["WORKING"])[0]
            STORE.report_state(sid, state)
            self._reply(200)
        elif parsed.path == "/statusline":
            STORE.report_statusline(sid, data)
            self._reply(200)
        else:
            self._reply(404)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def serve_on(addr: str, stop: threading.Event) -> ThreadingHTTPServer | None:
    try:
        server = ThreadingHTTPServer((addr, LISTEN_PORT), Handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def usb_bind_loop(stop: threading.Event, servers: list):
    """The USB interface (10.0.4.21) appears only while the device is
    plugged in — keep retrying so the device app can always reach us."""
    while not stop.is_set():
        server = serve_on("10.0.4.21", stop)
        if server:
            servers.append(server)
            log("USB-interface listener up on 10.0.4.21")
            return
        stop.wait(timeout=30)


def main():
    stop = threading.Event()
    servers = []

    primary = serve_on("127.0.0.1", stop)
    if primary is None:
        return 0  # another instance already owns the port
    servers.append(primary)
    threading.Thread(target=usb_bind_loop, args=(stop, servers), daemon=True).start()

    transport = UsbHttpTransport()

    def shutdown(*_):
        stop.set()
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if RENDER_MODE != "off":
        threading.Thread(target=render_loop, args=(transport, stop), daemon=True).start()
    if RENDER_MODE == "theme":
        threading.Thread(target=snapshot_watch_loop, args=(transport, stop), daemon=True).start()
    log(f"listening on :{LISTEN_PORT}, render_mode={RENDER_MODE}")

    while not stop.is_set():
        stop.wait(timeout=3600)
    if RENDER_MODE != "off":
        transport.clear(APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
