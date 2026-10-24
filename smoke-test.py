#!/usr/bin/python3
"""Bounded live smoke test; restores original app focus and leaves shelf parked."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

import shelf as trial

BASE = Path(__file__).resolve().parent
results = {}


def cmd(*args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def wait(test):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.1)
    raise RuntimeError("Condition timed out")


def layers():
    return json.loads(cmd("niri", "msg", "--json", "layers"))


def shelf_layers():
    return [item for item in layers() if item["namespace"] == "noctalia-panel"
            and item["keyboard_interactivity"] == "None"]


def clipboard():
    types = cmd("wl-paste", "--list-types").splitlines()
    # Do not request X11 pseudo-targets such as SAVE_TARGETS (not actual data).
    fingerprints = {}
    for mime in types:
        if "/" in mime:
            try:
                data = subprocess.check_output(["wl-paste", "--no-newline", "--type", mime], timeout=2)
                fingerprints[mime] = hashlib.sha256(data).hexdigest()
            except subprocess.TimeoutExpired:
                fingerprints[mime] = "unavailable"
    return {"types": types, "data_hashes": fingerprints}


original_focus = next((w["id"] for w in trial.request("Windows") if w["is_focused"]), None)
wait(lambda: trial.status()["valid"] and bool(shelf_layers()))
results["persistent_panel_keyboard_focus"] = shelf_layers()
clipboard_before = clipboard()
cmd("/usr/bin/python3", str(BASE / "capture.py"), "14", str(BASE / "capture-test.png"))
results["clipboard_unchanged_across_capture"] = clipboard_before == clipboard()
results["casts_after_single_capture"] = json.loads(cmd("niri", "msg", "--json", "casts"))
if json.loads(cmd("noctalia", "msg", "status"))["panelOpen"]:
    raise RuntimeError("A user panel is already open; refusing the coexistence test")
for name in ["launcher", "control-center"]:
    cmd("noctalia", "msg", "panel-open", name)
    wait(lambda: json.loads(cmd("noctalia", "msg", "status"))["activePanelId"] == name)
    results["coexists_with_" + name] = bool(shelf_layers()) and trial.status()["visible"]
    cmd("noctalia", "msg", "panel-close", name)
    wait(lambda: not json.loads(cmd("noctalia", "msg", "status"))["panelOpen"])
# Dispatch the exact image-click callback, without injecting pointer events.
cmd("noctalia", "msg", "plugin", "oal/edge-shelf-trial:shelf", "all", "restore")
wait(lambda: not trial.read_json(trial.edge.STATE).get("stashed") and not shelf_layers())
window = next(w for w in trial.request("Windows") if w["id"] == 14)
results["image_restore_callback_position"] = trial.edge.position(window)
results["image_restore_callback_focused_dolphin"] = window["is_focused"]
results["restore_retained_same_window_cache"] = trial.cached_for(
    trial.read_json(trial.META), window) and len(list(BASE.glob("thumbnail-*.png"))) == 1
if original_focus:
    cmd("niri", "msg", "action", "focus-window", "--id", str(original_focus))
# Exactly the command installed as Super+S.
cmd("python3", str(BASE / "shelf.py"), "toggle")
wait(lambda: trial.status()["valid"] and bool(shelf_layers()))
results["opening_shelf_preserved_focus"] = next(
    (w["id"] for w in trial.request("Windows") if w["is_focused"]), None) == original_focus
image = trial.read_json(trial.META)["image"]
cmd("python3", str(BASE / "shelf.py"), "stash")
results["repeated_stash_idempotent"] = image == trial.read_json(trial.META)["image"]
results["final_status"] = trial.status()
(BASE / "test-results.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2), flush=True)
