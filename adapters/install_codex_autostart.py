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
CHAIN = HERE / "codex_notify_chain.sh"
WRAPPER = HERE / "codex_notify.sh"


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

    new_argv = ["bash", str(WRAPPER)]
    if argv == new_argv:
        print("notify already points at the wrapper - nothing to do")
        return

    backup = CONFIG.with_name(f"config.toml.backup-busybar-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(CONFIG, backup)
    print(f"backup: {backup}")

    if argv:
        quoted = " ".join(f'"{a}"' for a in argv)
        CHAIN.write_text(
            "#!/bin/bash\n"
            "# Your pre-busybar Codex notifier, preserved by install_codex_autostart.py.\n"
            f'exec {quoted} "$@"\n'
        )
        CHAIN.chmod(0o755)
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
    if CHAIN.exists():
        original = re.search(r'^exec (.+) "\$@"$', CHAIN.read_text(), re.M)
        argv_restored = re.findall(r'"([^"]*)"', original.group(1)) if original else []
        CONFIG.write_text(text.replace(line, "notify = " + json.dumps(argv_restored), 1))
        CHAIN.unlink()
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
