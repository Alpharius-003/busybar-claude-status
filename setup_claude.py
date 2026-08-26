#!/usr/bin/env python3
"""Wire this repo into Claude Code (statusline + hooks), or unwire it.

    python3 setup_claude.py install
    python3 setup_claude.py uninstall

install:
  - backs up ~/.claude/settings.json (and your statusline script, if any)
  - appends state-reporting hook commands for this repo alongside whatever
    hooks you already have (nothing is replaced)
  - forwards the statusline payload to the daemon:
      * if you already have a statusLine command, it is wrapped — your
        original command still renders your status bar
      * if you have none, a minimal one is installed (model · ctx · plan)

uninstall reverses both. Re-running install is a no-op if already wired.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
CLAUDE_DIR = pathlib.Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
WRAPPER = CLAUDE_DIR / "busybar-statusline.sh"

REPORT = f"bash {HERE}/report.sh state "
HOOK_STATES = {
    "SessionStart": "IDLE",
    "SessionEnd": "ENDED",
    "UserPromptSubmit": "THINKING",
    "PreToolUse": "WORKING",
    "PostToolUse": "WORKING",
    "PostToolUseFailure": "ERROR",
    "PermissionRequest": "WAIT",
    "PermissionDenied": "ERROR",
    "Elicitation": "WAIT",
    "StopFailure": "FAILED",
    "Stop": "COMPLETE",
}

MARKER = "# busybar-claude-status forwarder"
FALLBACK_STATUSLINE = """\
input=$(cat)
{marker}
( printf '%s' "$input" | bash {here}/report.sh statusline >/dev/null 2>&1 & )
model=$(printf '%s' "$input" | /usr/bin/python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('model',{{}}).get('display_name') or '')" 2>/dev/null)
printf '[%s]' "${{model:-Claude}}"
"""
WRAP_STATUSLINE = """\
input=$(cat)
{marker}
( printf '%s' "$input" | bash {here}/report.sh statusline >/dev/null 2>&1 & )
printf '%s' "$input" | {original}
"""


def backup(path: pathlib.Path):
    if path.exists():
        dest = path.with_name(path.name + ".backup-busybar-" + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(path, dest)
        print(f"backup: {dest}")


def load_settings() -> dict:
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}


def save_settings(cfg: dict):
    SETTINGS.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def install():
    cfg = load_settings()
    backup(SETTINGS)

    # 1. hooks (append alongside existing ones)
    hooks = cfg.setdefault("hooks", {})
    added = 0
    for event, state in HOOK_STATES.items():
        groups = hooks.setdefault(event, [{"hooks": []}])
        cmd = REPORT + state
        if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
            continue
        groups[0].setdefault("hooks", []).append({"type": "command", "command": cmd})
        added += 1
    print(f"hooks: {added} command(s) added")

    # 2. statusline forwarding
    sl = cfg.get("statusLine") or {}
    original = sl.get("command", "")
    if str(HERE) in original or (WRAPPER.exists() and str(HERE) in WRAPPER.read_text()):
        print("statusline: already forwarding")
    else:
        # If the existing command is a plain script file, append the
        # forward line in place; otherwise wrap the command.
        target = pathlib.Path(original.split()[-1]).expanduser() if original else None
        if target and target.is_file() and "input=$(cat)" in target.read_text():
            backup(target)
            text = target.read_text().replace(
                "input=$(cat)",
                "input=$(cat)\n"
                f"{MARKER}\n"
                f"( printf '%s' \"$input\" | bash {HERE}/report.sh statusline >/dev/null 2>&1 & )",
                1,
            )
            target.write_text(text)
            print(f"statusline: forward line added to {target}")
        else:
            template = WRAP_STATUSLINE if original else FALLBACK_STATUSLINE
            WRAPPER.write_text("#!/bin/bash\n" + template.format(
                marker=MARKER, here=HERE, original=original))
            WRAPPER.chmod(0o755)
            cfg["statusLine"] = {"type": "command", "command": f"bash {WRAPPER}"}
            if original:
                cfg["statusLine"]["_busybar_original"] = original
            print(f"statusline: wrapper installed at {WRAPPER}")

    save_settings(cfg)
    print("done — restart your Claude Code sessions to pick up the hooks.")


def uninstall():
    cfg = load_settings()
    backup(SETTINGS)

    removed = 0
    for groups in (cfg.get("hooks") or {}).values():
        for g in groups:
            before = len(g.get("hooks", []))
            g["hooks"] = [h for h in g.get("hooks", []) if str(HERE) not in h.get("command", "")]
            removed += before - len(g["hooks"])
    print(f"hooks: {removed} command(s) removed")

    sl = cfg.get("statusLine") or {}
    if WRAPPER.exists() and str(WRAPPER) in sl.get("command", ""):
        original = sl.get("_busybar_original")
        cfg["statusLine"] = ({"type": "command", "command": original}
                             if original else None)
        if cfg["statusLine"] is None:
            cfg.pop("statusLine", None)
        WRAPPER.unlink()
        print("statusline: wrapper removed")
    else:
        target = pathlib.Path((sl.get("command", "").split() or [""])[-1]).expanduser()
        if target.is_file() and MARKER in target.read_text():
            lines = target.read_text().splitlines(keepends=True)
            out, skip = [], 0
            for line in lines:
                if MARKER in line:
                    skip = 2  # marker + forward line
                if skip:
                    skip -= 1
                    continue
                out.append(line)
            backup(target)
            target.write_text("".join(out))
            print(f"statusline: forward line removed from {target}")

    save_settings(cfg)
    print("done.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        sys.exit(__doc__)
