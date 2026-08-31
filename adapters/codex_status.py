#!/usr/bin/env python3
"""Codex CLI adapter for the busybar daemon.

Zero-config and rename-proof: everything is derived from what Codex
itself writes (config.toml defaults + the newest session rollout under
~/.codex/sessions). No model-name tables anywhere:

  - label:   the raw model id, prettified by GENERIC rules only
             ("gpt-5.6-sol" -> "5.6 Sol"; a future "gpt-7-luna" ->
             "7 Luna" with zero changes here), plus the reasoning effort
  - badges:  service_tier other than default becomes a badge
             ("fast" renders as a lightning bolt on the display)
  - context: last_token_usage.total_tokens / model_context_window
  - quotas:  Codex's own rate_limits windows, named from window_minutes
             (600 -> "10h", 10080 -> "7d") - names survive plan changes
  - state:   rollout file activity (recent writes = WORKING)

Usage:
    python3 adapters/codex_status.py            # loop, report every 2s
    python3 adapters/codex_status.py --once -v  # single probe, print it
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import report  # noqa: E402  (daemon/hub address + host headers from env.sh)

DAEMON = report.BASE + "/v1/report"
CODEX_HOME = pathlib.Path.home() / ".codex"
SESSIONS = CODEX_HOME / "sessions"

ACTIVE_S = 8        # rollout written this recently -> WORKING
COMPLETE_S = 120    # ... this recently -> COMPLETE
POLL_S = 2.0
TAIL_BYTES = 128 * 1024

VENDOR_PREFIXES = ("gpt-", "chatgpt-", "openai-")


def prettify_model(model: str) -> str:
    """Generic prettifier - strips vendor prefixes and title-cases word
    tokens. Never matches specific model names."""
    m = model.lower()
    for p in VENDOR_PREFIXES:
        if m.startswith(p):
            model = model[len(p):]
            break
    parts = []
    for tok in re.split(r"[-_]", model):
        parts.append(tok if any(c.isdigit() for c in tok) else tok.capitalize())
    return " ".join(p for p in parts if p)


def window_name(minutes: float) -> str:
    hours = minutes / 60
    return f"{round(hours)}h" if hours < 48 else f"{round(hours / 24)}d"


def config_defaults() -> dict:
    """Minimal TOML pluck of the keys we need (no toml dependency)."""
    out = {}
    try:
        for line in (CODEX_HOME / "config.toml").read_text().splitlines():
            m = re.match(r'\s*(model|model_reasoning_effort|service_tier)\s*=\s*"([^"]*)"', line)
            if m:
                out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def newest_rollout() -> pathlib.Path | None:
    files = list(SESSIONS.glob("*/*/*/rollout-*.jsonl"))
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def _last(pattern: str, text: str) -> str | None:
    hits = re.findall(pattern, text)
    return hits[-1] if hits else None


def probe() -> dict | None:
    rollout = newest_rollout()
    defaults = config_defaults()
    if rollout is None and not defaults:
        return None

    model = defaults.get("model")
    effort = defaults.get("model_reasoning_effort")
    tier = defaults.get("service_tier")
    context_pct = None
    quotas = None
    state = "IDLE"
    session_id = "config"

    if rollout is not None:
        session_id = rollout.stem.split("rollout-")[-1][:64]
        age = time.time() - rollout.stat().st_mtime
        state = "WORKING" if age < ACTIVE_S else ("COMPLETE" if age < COMPLETE_S else "IDLE")

        with rollout.open("rb") as f:
            f.seek(max(0, rollout.stat().st_size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")

        model = _last(r'"model"\s*:\s*"([^"]+)"', tail) or model
        effort = _last(r'"reasoning_effort"\s*:\s*"([^"]+)"', tail) or effort
        tier = _last(r'"service_tier"\s*:\s*"([^"]+)"', tail) or tier

        tc = _last(r'"type":"token_count","info":(\{.*?"model_context_window":\d+\})', tail)
        if tc:
            try:
                info = json.loads(tc)
                window = info.get("model_context_window")
                last = (info.get("last_token_usage") or {}).get("total_tokens")
                if window and last:
                    context_pct = round(min(100, last * 100 / window), 1)
            except json.JSONDecodeError:
                pass

        rl = _last(r'"rate_limits":(\{.*?\}\})', tail)
        if rl:
            try:
                limits = json.loads(rl)
                quotas = []
                for k in ("primary", "secondary"):
                    w = limits.get(k) or {}
                    if w.get("used_percent") is not None and w.get("window_minutes"):
                        quotas.append({
                            "name": window_name(w["window_minutes"]),
                            "left_pct": max(0, round(100 - w["used_percent"])),
                            "resets_at": w.get("resets_at"),
                        })
                quotas = quotas or None
            except json.JSONDecodeError:
                pass

    if not model:
        return None

    label = prettify_model(model)
    badges = None
    if tier and tier not in ("default", "standard"):
        badges = [tier]
        if tier != "fast":  # unknown tiers also get spelled out
            label += f" {tier}"
    if effort:
        label += f" {effort}"

    return {
        "source": "codex", "session_id": session_id, "state": state,
        "label": label, "context_pct": context_pct, "quotas": quotas,
        "badges": badges, "ttl_s": 600,
    }


def report_headers() -> dict:
    return dict(report.HEADERS)


def post(report: dict) -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(
            DAEMON, data=json.dumps(report).encode(), method="POST",
            headers=report_headers()), timeout=2).read()
        return True
    except OSError:
        return False


def _emit(verbose: bool):
    report = probe()
    if report:
        if verbose:
            print(json.dumps(report, ensure_ascii=False), flush=True)
        post(report)
    elif verbose:
        print("no codex data found", flush=True)


def main():
    once = "--once" in sys.argv
    verbose = "-v" in sys.argv
    if once:
        _emit(verbose)
        return
    # Report only around real activity: every report bumps the session's
    # last-active timestamp, and an idle Codex must not keep stealing the
    # display from other agents. While the rollout advances we report
    # (WORKING); once it stops we send ONE closing report (COMPLETE/IDLE
    # per age) and then go silent - the daemon's own decay and ttl take
    # it from there.
    last_mtime = None
    closed = False
    while True:
        rollout = newest_rollout()
        mtime = rollout.stat().st_mtime if rollout else None
        if mtime != last_mtime:
            last_mtime = mtime
            closed = False
            _emit(verbose)
        elif not closed and mtime is not None and time.time() - mtime > ACTIVE_S:
            _emit(verbose)
            closed = True
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
