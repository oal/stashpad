#!/usr/bin/python3
"""Bounded live measurements; numbers are command/IPC readiness, not animation.

Briefly restores/re-stashes Dolphin. Never changes clipboard or app contents.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

import shelf

BASE = Path(__file__).resolve().parent
COMMAND = ["python3", str(BASE / "shelf.py")]
results = {}
initial_focus = next((w["id"] for w in shelf.request("Windows") if w["is_focused"]), None)


def command(action):
    began = time.perf_counter()
    done = subprocess.run(COMMAND + [action], capture_output=True, text=True, timeout=5,
                          env={**os.environ, "SHELF_PROFILE": "1"})
    value = {"action": action, "command_ms": round((time.perf_counter() - began) * 1000, 2),
             "returncode": done.returncode}
    if done.returncode:
        raise RuntimeError(done.stderr)
    for line in done.stderr.splitlines():
        if line.startswith('{"toggle_profile"'):
            value.update(json.loads(line)["toggle_profile"])
    return value


def native_shelf():
    return any(item["namespace"] == "noctalia-panel" and item["keyboard_interactivity"] == "None"
               for item in shelf.request("Layers"))


def settled(desired, timeout=3):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        value = shelf.status()
        quiet = not list(BASE.glob("pending-preview-*.png")) and not shelf.request("Casts")
        if (value["desired_stashed"] == desired and value["visible"] == desired and value["valid"] == desired
                and native_shelf() == desired and quiet):
            return value
        time.sleep(.005)
    raise RuntimeError("Shelf did not settle: " + json.dumps(shelf.status()))


results["sequential"] = []
command("restore")
settled(False)
with ThreadPoolExecutor(max_workers=10) as pool:
    for _ in range(3):
        began = time.perf_counter()
        pending = pool.submit(command, "stash")
        move_ms = None
        shelf_ms = None
        while True:
            value = shelf.status()
            if value["desired_stashed"] and move_ms is None:
                move_ms = round((time.perf_counter() - began) * 1000, 2)
            if value["visible"] and native_shelf() and shelf_ms is None:
                shelf_ms = round((time.perf_counter() - began) * 1000, 2)
            if value["valid"] and native_shelf() and shelf.read_json(shelf.HEALTH).get("content") == "ready":
                preview_ms = round((time.perf_counter() - began) * 1000, 2)
                break
            if time.perf_counter() - began > 3:
                raise RuntimeError("Preview readiness timed out")
            time.sleep(.005)
        stash = pending.result()
        stash["observed_parked_ms"] = move_ms
        stash["observed_native_shelf_ready_ms"] = shelf_ms
        stash["observed_native_preview_ready_ms"] = preview_ms
        settled(True)
        began = time.perf_counter()
        pending = pool.submit(command, "restore-focus")
        while shelf.status()["desired_stashed"] or native_shelf():
            if time.perf_counter() - began > 2:
                raise RuntimeError("Restore/hide timed out")
            time.sleep(.002)
        hidden_ms = round((time.perf_counter() - began) * 1000, 2)
        restore = pending.result()
        restore["observed_restored_and_panel_hidden_ms"] = hidden_ms
        settled(False)
        results["sequential"].append({"stash": stash, "restore": restore})
    command("stash")
    settled(True)
    results["bursts"] = []
    for count, interval in [(2, .040), (3, .040), (6, .040), (7, .015)]:
        initial = shelf.status()
        generation = initial["generation"]
        expected = initial["desired_stashed"] != bool(count % 2)
        began = time.perf_counter()
        futures = []
        for index in range(count):
            target = began + index * interval
            time.sleep(max(0, target - time.perf_counter()))
            futures.append(pool.submit(command, "toggle"))
        commands = [future.result() for future in futures]
        commands_done_ms = round((time.perf_counter() - began) * 1000, 2)
        final = settled(expected)
        item = {"count": count, "interval_ms": interval * 1000,
                "all_commands_done_ms": commands_done_ms,
                "preview_and_cleanup_settled_ms": round((time.perf_counter() - began) * 1000, 2),
                "all_presses_counted": final["generation"] - generation == count,
                "expected_stashed": expected, "actual_stashed": final["desired_stashed"],
                "commands": commands}
        assert item["all_presses_counted"]
        results["bursts"].append(item)
    time.sleep(.6)
    final = shelf.status()
    results["no_late_state_change"] = final["generation"] == results["bursts"][-1]["commands"][-1]["generation"]
    results["final_status"] = final

results["capture_phases"] = []
for _ in range(2):
    window_id = shelf.read_json(shelf.edge.STATE)["window_id"]
    began = time.perf_counter()
    captured = subprocess.run(["/usr/bin/python3", str(BASE / "capture.py"), str(window_id),
                               str(BASE / "profile-capture.png")], capture_output=True, text=True,
                              timeout=15, env={**os.environ, "SHELF_PROFILE": "1"})
    assert captured.returncode == 0, captured.stderr
    results["capture_phases"].append({"process_ms": round((time.perf_counter() - began) * 1000, 2),
                                     **json.loads(captured.stderr)["capture_profile"]})
(BASE / "profile-capture.png").unlink(missing_ok=True)
results["casts_after_tests"] = shelf.request("Casts")
results["pending_images_after_tests"] = [p.name for p in BASE.glob("pending-preview-*.png")]
results["saved_original_position"] = shelf.read_json(shelf.edge.STATE)["original_position"]
if initial_focus:
    shelf.niri_action("FocusWindow", id=initial_focus)
(BASE / "performance-after.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2))
