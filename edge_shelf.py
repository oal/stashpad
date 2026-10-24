#!/usr/bin/env python3
"""Temporary, Dolphin-only edge-shelf trial. Run on the host."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
STATE = BASE / "window.json"
STRUT = "    struts {\n        // Temporary Dolphin edge-shelf trial.\n        left 100\n    }"
BIND = ('    // Temporary Dolphin edge-shelf trial.\n'
        '    Mod+S repeat=false hotkey-overlay-title="Toggle Dolphin edge shelf" { spawn "python3" "'
        + str(Path(__file__).resolve()) + '" "toggle"; }\n\n')


def niri(*args):
    return subprocess.check_output(["niri", "msg", *args], text=True)


def position(window):
    return window["layout"]["tile_pos_in_workspace_view"]


def restore(window, state):
    current = position(window)
    original = state["original_position"]
    # Relative moves avoid guessing the bar's working-area offset.
    niri("action", "move-floating-window", "--id", str(window["id"]),
         "-x", f"{original[0] - current[0]:+g}",
         "-y", f"{original[1] - current[1]:+g}")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    if action not in ("toggle", "stash", "restore", "undo"):
        raise SystemExit("Usage: shelf.py [toggle|stash|restore|undo]")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    windows = json.loads(niri("--json", "windows"))
    matches = [w for w in windows if w["app_id"] == "org.kde.dolphin"]
    window = next((w for w in matches if w["id"] == state.get("window_id")
                   and w["pid"] == state.get("pid")), None)
    if window is None and len(matches) == 1:
        window = matches[0]
        state = {}
    if action != "undo" and window is None:
        raise SystemExit("Open exactly one Dolphin window for this trial.")
    if window is not None and not window["is_floating"]:
        if action != "undo":
            raise SystemExit("Dolphin must be floating (Super+V).")
        window = None

    stashed = bool(window and state.get("stashed") and position(window)[0] < 100)
    if action == "undo":
        config = Path.home() / ".config/niri/config.kdl"
        text = config.read_text()
        if STRUT not in text or BIND not in text:
            raise SystemExit("Trial config was changed; refusing automatic undo. Backup: "
                             + str(BASE / "config.kdl.before"))
        if stashed:
            restore(window, state)
        config.write_text(text.replace(STRUT, "    struts {\n    }", 1).replace(BIND, "", 1))
        state["stashed"] = False
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        print("Trial removed; original margin and shortcuts restored.")
        return

    if action == "restore" or (action == "toggle" and stashed):
        if stashed:
            restore(window, state)
            state["stashed"] = False
            STATE.write_text(json.dumps(state, indent=2) + "\n")
            if action == "toggle":
                niri("action", "focus-window", "--id", str(window["id"]))
            print("Dolphin restored.")
        return
    if stashed:
        return
    state = {"window_id": window["id"], "pid": window["pid"],
             "original_position": position(window), "stashed": False}
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    niri("action", "move-floating-window", "--id", str(window["id"]), "-x", "-10000")
    state["stashed"] = True
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    if window["is_focused"]:
        niri("action", "focus-tiling")
    print("Dolphin parked at the left edge.")


if __name__ == "__main__":
    with (BASE / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main()
