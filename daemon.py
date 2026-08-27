#!/usr/bin/env python3
"""BusyBar agent-status daemon.

A provider-agnostic status display core with pluggable adapters and
transports:

    claude-code (built-in adapter: /statusline + /state) --.
    codex / cursor / anything (POST /v1/report) ----------+--> SessionStore
                                                          |       |
                              GET /status  <--------------+   Renderer
                              (device JS app, debugging)          |
                                                              Transport
                                                        (usb / wifi / cloud)

The CORE understands only the normalized report schema (see
docs/EXTENDING.md): state, label, label_color, context_pct, quotas.
Everything Claude-specific — statusline JSON parsing, /effort palette
colors, the 5h/7d rate-limit windows — lives in the claude adapter
functions and never leaks into the renderer.

Layout (72x16 front display):

    ############################    1px per-pixel animated ring (.anim)
    #  Fable 5 max      [##----] #  label (label_color)     | context bar
    #  5h85% 7d97%        WORK   #  quotas                  | state word
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
#   "auto"  - whenever any reporting agent is active (the always-on behavior)
#   "theme" - only while "claude" is the device's currently selected
#             BUSY/CUSTOM theme (on-device manual switch; see claude_card.py)
#   "off"   - data bridge only
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
DEFAULT_TTL_S = 6 * 3600

STATES = ("THINKING", "WORKING", "WAIT", "ERROR", "FAILED", "COMPLETE", "IDLE")
STATE_ANIMS = {
    "THINKING": "think.anim", "WORKING": "work.anim", "WAIT": "wait.anim",
    "ERROR": "error.anim", "FAILED": "error.anim", "COMPLETE": "done.anim",
    "IDLE": "idle.anim",
}
STATE_WORDS = {
    "THINKING": "THINK", "WORKING": "WORK", "WAIT": "WAIT",
    "ERROR": "ERR", "FAIL": "FAIL", "FAILED": "FAIL", "COMPLETE": "DONE",
    "IDLE": "IDLE",
}
STATE_COLORS = {
    "THINKING": "#AF87FFFF", "WORKING": "#FFB000FF", "WAIT": "#FF6A00FF",
    "ERROR": "#FF2020FF", "FAILED": "#FF2020FF", "COMPLETE": "#20C040FF",
    "IDLE": "#808080FF",
}

LABEL_FALLBACK_COLOR = "#FFFFFFFF"
QUOTA_COLOR = "#A0A0A0FF"
FONT = "small"

BAR_X, BAR_Y, BAR_W, BAR_H = 50, 3, 20, 4
BAR_TRACK_COLOR = "#262626FF"
LABEL_MAX_PX = BAR_X - 2 - 3


# --------------------------------------------------------------------------
# Transports (env BUSYBAR_TRANSPORT: usb | wifi | cloud; see docs/EXTENDING.md)
# --------------------------------------------------------------------------

class HttpTransport:
    """Busy Bar HTTP API over any of its three routes."""

    TIMEOUT_S = 2.0

    def __init__(self, base: str, headers: dict | None = None):
        self.base = base
        self.headers = headers or {}

    def _request(self, method: str, path: str, body: bytes | None = None) -> bool:
        headers = dict(self.headers)
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_S):
                return True
        except urllib.error.HTTPError as e:
            if e.code != 409:  # 409: an active focus session owns the screen
                log(f"device HTTP {e.code} on {method} {path}")
            return False
        except OSError:
            return False  # unplugged / offline; retried on the normal cadence

    def draw(self, payload: dict) -> bool:
        return self._request("POST", "/display/draw", json.dumps(payload).encode())

    def clear(self, app_name: str) -> bool:
        return self._request(
            "DELETE", "/display/draw?application_name=" + urllib.parse.quote(app_name)
        )

    def get_json(self, path: str) -> dict | None:
        req = urllib.request.Request(self.base + path, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_S) as r:
                return json.loads(r.read())
        except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
            return None


def make_transport() -> HttpTransport:
    kind = os.environ.get("BUSYBAR_TRANSPORT", "usb")
    if kind == "usb":
        return HttpTransport("http://10.0.4.20/api")
    if kind == "wifi":
        host = os.environ.get("BUSYBAR_HOST")
        if not host:
            sys.exit("wifi transport needs BUSYBAR_HOST (device LAN IP)")
        headers = {}
        if os.environ.get("BUSYBAR_TOKEN"):
            headers["x-api-token"] = os.environ["BUSYBAR_TOKEN"]
        t = HttpTransport(f"http://{host}/api", headers)
        t.TIMEOUT_S = 4.0
        return t
    if kind == "cloud":
        token = os.environ.get("BUSYBAR_TOKEN")
        if not token:
            sys.exit("cloud transport needs BUSYBAR_TOKEN (API token from the BUSY app)")
        t = HttpTransport("https://api.busy.app/busybar",
                          {"authorization": f"Bearer {token}"})
        t.TIMEOUT_S = 8.0
        return t
    if kind == "ble":
        sys.exit("BLE transport is designed but not implemented yet - see docs/EXTENDING.md")
    sys.exit(f"unknown BUSYBAR_TRANSPORT {kind!r} (usb|wifi|cloud)")


# --------------------------------------------------------------------------
# Session store (normalized records only)
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.dirty = threading.Event()

    def report(self, source: str, session_id: str, fields: dict):
        """Merge a normalized report. `fields` may contain: state, label,
        label_color, context_pct, quotas, ttl_s, ended."""
        key = f"{source}:{session_id}"
        now = time.time()
        with self.lock:
            if fields.get("ended"):
                self.sessions.pop(key, None)
            else:
                s = self.sessions.setdefault(key, {
                    "source": source, "state": "IDLE", "state_ts": 0.0,
                    "last_active": 0.0, "label": None, "label_color": None,
                    "context_pct": None, "quotas": None, "ttl_s": DEFAULT_TTL_S,
                })
                if "state" in fields:
                    s["state"] = fields["state"]
                    s["state_ts"] = now
                for k in ("label", "label_color", "context_pct", "quotas", "ttl_s"):
                    if k in fields:
                        s[k] = fields[k]
                s["last_active"] = now
        self.dirty.set()

    def active_session(self) -> dict | None:
        now = time.time()
        with self.lock:
            for key in [k for k, s in self.sessions.items()
                        if now - s["last_active"] > s.get("ttl_s", DEFAULT_TTL_S)]:
                del self.sessions[key]
            if not self.sessions:
                return None
            return dict(max(self.sessions.values(), key=lambda s: s["last_active"]))


STORE = Store()


def effective_state(sess: dict) -> str:
    state = sess["state"]
    if state == "COMPLETE" and time.time() - sess["state_ts"] > COMPLETE_HOLD_S:
        return "IDLE"
    return state


def status_snapshot() -> dict:
    """Normalized active view: what renderers (and GET /status) consume."""
    sess = STORE.active_session()
    if sess is None:
        return {"source": None, "state": "IDLE", "label": None,
                "label_color": None, "context_pct": None, "quotas": None,
                "age_s": None}
    now = time.time()
    quotas = []
    for q in sess.get("quotas") or []:
        left = q.get("left_pct")
        # A quota window whose reset time has passed is back to full.
        if q.get("resets_at") and q["resets_at"] <= now:
            left = 100
        quotas.append({"name": q.get("name", ""), "left_pct": left})
    return {
        "source": sess["source"],
        "state": effective_state(sess),
        "label": sess.get("label"),
        "label_color": sess.get("label_color"),
        "context_pct": sess.get("context_pct"),
        "quotas": quotas or None,
        "age_s": round(now - sess["last_active"], 1),
    }


# --------------------------------------------------------------------------
# Claude Code adapter: statusline JSON + hook states -> normalized reports.
# All Claude-specific semantics live here.
# --------------------------------------------------------------------------

# Straight from the Claude Code theme palette:
# inactive / permission / warning / fastMode / effortUltra
CLAUDE_EFFORT_COLORS = {
    "low": "#999999FF", "medium": "#99CCFFFF", "high": "#FFC107FF",
    "xhigh": "#FF7814FF", "max": "#AF87FFFF",
}


def claude_statusline_report(data: dict) -> dict:
    model = (data.get("model") or {})
    name = model.get("display_name") or model.get("id") or ""
    effort = (data.get("effort") or {}).get("level") or ""

    label = f"{name} {effort}".strip()
    while len(label) > 3 and est_width(label) > LABEL_MAX_PX:
        name = name[:-1]  # shorten the model name, keep the effort word
        label = f"{name} {effort}".strip()

    ctx = data.get("context_window") or {}
    context_pct = ctx.get("used_percentage")
    if context_pct is None and ctx.get("remaining_percentage") is not None:
        context_pct = 100 - ctx["remaining_percentage"]

    quotas = []
    rl = data.get("rate_limits") or {}
    for qname, key in (("5h", "five_hour"), ("7d", "seven_day")):
        w = rl.get(key) or {}
        if w.get("used_percentage") is not None:
            quotas.append({
                "name": qname,
                "left_pct": max(0, round(100 - w["used_percentage"])),
                "resets_at": w.get("resets_at"),
            })

    fields = {"context_pct": context_pct, "quotas": quotas or None}
    if label:
        fields["label"] = label
        fields["label_color"] = CLAUDE_EFFORT_COLORS.get(effort, LABEL_FALLBACK_COLOR)
    return fields


# --------------------------------------------------------------------------
# Renderer (normalized fields only)
# --------------------------------------------------------------------------

# The device's small font is proportional (measured on-device); errs wide.
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


def bar_color(used: float) -> str:
    if used >= 90:
        return "#FF2020FF"
    if used >= 80:
        return "#FF6A00FF"
    if used >= 50:
        return "#FFB000FF"
    return "#20C040FF"


def quota_color(left: int) -> str:
    if left <= 10:
        return "#FF2020FF"
    if left <= 25:
        return "#FF6A00FF"
    return QUOTA_COLOR


def _norm_color(c, fallback: str) -> str:
    if not isinstance(c, str) or not c.startswith("#") or len(c) not in (7, 9):
        return fallback
    return (c + "FF") if len(c) == 7 else c


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
    """Text rows + context bar for a normalized snapshot (ring is separate)."""
    elements = []
    state = status["state"]

    label = status.get("label") or ""
    label = "".join(ch for ch in label if 0x20 <= ord(ch) <= 0x7E)  # ASCII-only font
    while label and est_width(label) > LABEL_MAX_PX:
        label = label[:-1]
    if label:
        elements.append(_text("model", 3, 0, "top_left", label,
                              _norm_color(status.get("label_color"), LABEL_FALLBACK_COLOR)))

    used = status.get("context_pct")
    elements.append(_rect("ctrack", BAR_X, BAR_Y, BAR_W, BAR_H, BAR_TRACK_COLOR))
    if isinstance(used, (int, float)) and used > 0:
        fill = max(1, min(BAR_W, round(BAR_W * min(used, 100) / 100)))
        elements.append(_rect("cfill", BAR_X, BAR_Y, fill, BAR_H, bar_color(used)))

    quotas = [q for q in (status.get("quotas") or []) if q.get("left_pct") is not None][:2]
    if quotas:
        text = " ".join(f"{q['name']}{q['left_pct']}%" for q in quotas)
        worst = min(q["left_pct"] for q in quotas)
        elements.append(_text("usage", 3, 15, "bottom_left", text, quota_color(worst)))

    elements.append(_text("state", 69, 15, "bottom_right",
                          STATE_WORDS.get(state, state[:5]),
                          STATE_COLORS.get(state, STATE_COLORS["IDLE"])))
    return elements


THEME_ACTIVE = threading.Event()


def snapshot_watch_loop(transport: HttpTransport, stop: threading.Event):
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


def render_loop(transport: HttpTransport, stop: threading.Event):
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
            status = status_snapshot()
            anim = anim_element(status["state"])
            if anim["path"] != last_anim or now - last_anim_ts > ANIM_REFRESH_S:
                if transport.draw({"application_name": APP_NAME,
                                   "priority": DRAW_PRIORITY, "elements": [anim]}):
                    last_anim, last_anim_ts, drawn = anim["path"], now, True
            texts = info_elements(status)
            encoded = json.dumps(texts, sort_keys=True)
            if encoded != last_texts or now - last_texts_ts > KEEPALIVE_S:
                if transport.draw({"application_name": APP_NAME,
                                   "priority": DRAW_PRIORITY, "elements": texts}):
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
        if self.path in ("/status", "/v1/status"):
            self._reply(200, json.dumps(status_snapshot()).encode())
        elif self.path == "/health":
            with STORE.lock:
                snapshot = {
                    key: {k: s[k] for k in ("source", "state", "state_ts", "last_active")}
                    for key, s in STORE.sessions.items()
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

        if parsed.path == "/v1/report":
            # The provider-agnostic reporting endpoint. See docs/EXTENDING.md.
            source = str(data.get("source") or "")[:32]
            session_id = str(data.get("session_id") or "")[:128]
            if not source or not session_id:
                self._reply(400, b'{"error":"source and session_id are required"}')
                return
            fields: dict = {}
            if data.get("ended"):
                fields["ended"] = True
            state = data.get("state")
            if state is not None:
                if state not in STATES:
                    self._reply(400, json.dumps(
                        {"error": f"state must be one of {list(STATES)}"}).encode())
                    return
                fields["state"] = state
            if "label" in data:
                fields["label"] = str(data["label"] or "")[:64] or None
            if "label_color" in data:
                fields["label_color"] = str(data["label_color"] or "") or None
            if "context_pct" in data:
                v = data["context_pct"]
                fields["context_pct"] = max(0.0, min(100.0, float(v))) if v is not None else None
            if "quotas" in data:
                qs = data["quotas"] or []
                fields["quotas"] = [
                    {"name": str(q.get("name", ""))[:6],
                     "left_pct": max(0, min(100, round(q["left_pct"]))),
                     "resets_at": q.get("resets_at")}
                    for q in qs[:4] if isinstance(q, dict) and q.get("left_pct") is not None
                ] or None
            if "ttl_s" in data and data["ttl_s"]:
                fields["ttl_s"] = max(10.0, float(data["ttl_s"]))
            STORE.report(source, session_id, fields)
            self._reply(200, b'{"ok":true}')

        elif parsed.path == "/state":
            # claude adapter: hook events
            sid = data.get("session_id") or "unknown"
            state = urllib.parse.parse_qs(parsed.query).get("state", ["WORKING"])[0]
            if state == "ENDED":
                STORE.report("claude-code", sid, {"ended": True})
            elif state in STATES:
                STORE.report("claude-code", sid, {"state": state})
            self._reply(200)

        elif parsed.path == "/statusline":
            # claude adapter: statusline payload
            sid = data.get("session_id") or "unknown"
            STORE.report("claude-code", sid, claude_statusline_report(data))
            self._reply(200)

        else:
            self._reply(404)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def serve_on(addr: str) -> ThreadingHTTPServer | None:
    try:
        server = ThreadingHTTPServer((addr, LISTEN_PORT), Handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def usb_bind_loop(stop: threading.Event, servers: list):
    """The USB interface (10.0.4.21) appears only while the device is
    plugged in - keep retrying so the device app can always reach us."""
    while not stop.is_set():
        server = serve_on(LISTEN_ADDRS[1])
        if server:
            servers.append(server)
            log(f"USB-interface listener up on {LISTEN_ADDRS[1]}")
            return
        stop.wait(timeout=30)


def main():
    stop = threading.Event()
    servers = []

    primary = serve_on(LISTEN_ADDRS[0])
    if primary is None:
        return 0  # another instance already owns the port
    servers.append(primary)
    threading.Thread(target=usb_bind_loop, args=(stop, servers), daemon=True).start()

    transport = make_transport()

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
    log(f"listening on :{LISTEN_PORT}, render_mode={RENDER_MODE}, "
        f"transport={os.environ.get('BUSYBAR_TRANSPORT', 'usb')}")

    while not stop.is_set():
        stop.wait(timeout=3600)
    if RENDER_MODE != "off":
        transport.clear(APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
