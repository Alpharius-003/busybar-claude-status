#!/usr/bin/env python3
"""Wire the busybar pipeline into Codex's `notify` hook (auto-start).

    python3 adapters/install_codex_autostart.py install
    python3 adapters/install_codex_autostart.py uninstall

install:
  - backs up ~/.codex/config.toml
  - if a notify program is already configured, preserves it as
    adapters/codex_notify_chain.sh (called first by our wrapper)
  - sets notify = ["bash", "<repo>/adapters/codex_notify.sh"]

From then on, every Codex turn keeps the daemon + codex adapter alive —
nothing to start manually. uninstall restores the previous notify.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = pathlib.Path.home() / ".codex" / "config.toml"
CHAIN = HERE / "codex_notify_chain.json"
CHAIN_SH = HERE / "codex_notify_chain.sh"  # legacy (pre-Windows installs)
WRAPPER = HERE / "codex_notify.py"


def read_notify(text: str):
    m = re.search(r'^notify\s*=\s*(\[.*?\])\s*$', text, re.M)
    if not m:
        return None, None
    try:
        return json.loads(m.group(1)), m.group(0)
    except json.JSONDecodeError:
        return None, m.group(0)


def install():
    if not CONFIG.exists():
        sys.exit(f"{CONFIG} not found - is Codex installed?")
    text = CONFIG.read_text()
    argv, line = read_notify(text)

    new_argv = [sys.executable, str(WRAPPER)]
    if argv and any("codex_notify" in str(a) for a in argv):
        if argv == new_argv:
            print("notify already points at the wrapper - nothing to do")
            return
        # An older busybar wrapper (e.g. the bash one): just repoint;
        # the preserved chain files keep working as-is.
        backup = CONFIG.with_name(
            f"config.toml.backup-busybar-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(CONFIG, backup)
        print(f"backup: {backup}")
        CONFIG.write_text(text.replace(line, "notify = " + json.dumps(new_argv), 1))
        print(f"notify -> {WRAPPER.name} (upgraded from the shell wrapper)")
        return

    backup = CONFIG.with_name(f"config.toml.backup-busybar-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(CONFIG, backup)
    print(f"backup: {backup}")

    if argv:
        CHAIN.write_text(json.dumps(argv))
        print(f"previous notifier preserved -> {CHAIN.name}")

    new_line = "notify = " + json.dumps(new_argv)
    if line:
        text = text.replace(line, new_line, 1)
    else:
        text = new_line + "\n" + text
    CONFIG.write_text(text)
    print(f"notify -> {WRAPPER.name}. Codex will auto-start the pipeline on every turn.")


def uninstall():
    text = CONFIG.read_text()
    argv, line = read_notify(text)
    if not line or str(WRAPPER) not in json.dumps(argv or []):
        sys.exit("notify is not pointing at the wrapper - nothing to undo")
    argv_restored = None
    if CHAIN.exists():
        argv_restored = json.loads(CHAIN.read_text())
        CHAIN.unlink()
    elif CHAIN_SH.exists():
        original = re.search(r'^exec (.+) "\$@"$', CHAIN_SH.read_text(), re.M)
        argv_restored = re.findall(r'"([^"]*)"', original.group(1)) if original else []
        CHAIN_SH.unlink()
    if argv_restored is not None:
        CONFIG.write_text(text.replace(line, "notify = " + json.dumps(argv_restored), 1))
        print("original notifier restored")
    else:
        CONFIG.write_text(text.replace(line + "\n", "", 1).replace(line, "", 1))
        print("notify removed")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        sys.exit(__doc__)
