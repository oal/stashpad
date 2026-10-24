#!/usr/bin/python3
"""Bounded native shelf regressions. Includes the deterministic old failure."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import types

import shelf

BASE = Path(__file__).resolve().parent
results = {}
original_focus = next((w["id"] for w in shelf.request("Windows") if w["is_focused"]), None)


def cmd(*args):
    return subprocess.check_output(args, text=True, timeout=15)


def change(action):
    began = time.perf_counter()
    cmd("python3", str(BASE / "shelf.py"), action)
    return round((time.perf_counter() - began) * 1000, 2)


def native():
    return any(v["namespace"] == "noctalia-panel" and v["keyboard_interactivity"] == "None"
               for v in shelf.request("Layers"))


def wait(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.003)
    raise RuntimeError("Condition timed out: " + json.dumps({"helper": shelf.status(), "ui": shelf.read_json(shelf.HEALTH), "native": native()}))


def rendered(desired, ready=False):
    state = shelf.status()
    health = shelf.read_json(shelf.HEALTH)
    if desired:
        good = (state["visible"] and native() and health.get("open")
                and health.get("generation") == state["generation"]
                and health.get("image") == state["image"]
                and health.get("content") in (("ready",) if ready else ("ready", "loading", "unavailable"))
                and (not ready or state["valid"]))
    else:
        good = not state["desired_stashed"] and not native() and not health.get("open")
    return {"state": state, "render": health} if good else None


def quiet():
    return not shelf.request("Casts") and not list(BASE.glob("pending-preview-*.png"))


def inspect():
    cmd("noctalia", "msg", "plugin", "oal/edge-shelf-trial:monitor", "all", "inspect")
    return shelf.read_json(BASE / "debug-state.json")


def click_restore():
    cmd("noctalia", "msg", "plugin", "oal/edge-shelf-trial:shelf", "all", "restore")
    wait(lambda: rendered(False))
    wait(quiet)
    window = next(w for w in shelf.request("Windows") if w["id"] == shelf.read_json(shelf.edge.STATE)["window_id"])
    assert window["is_focused"]
    assert window["layout"]["tile_pos_in_workspace_view"] == shelf.read_json(shelf.edge.STATE)["original_position"]


change("stash")
wait(lambda: rendered(True, ready=True))
pid = None
for proc in Path("/proc").iterdir():
    if proc.name.isdigit():
        try:
            if (proc / "cmdline").read_bytes() == b"/usr/bin/python3\0" + str(BASE / "shelf.py").encode() + b"\0watch\0":
                pid = int(proc.name)
        except OSError:
            pass
assert pid, "No watcher"
wait(lambda: "poll" in Path(f"/proc/{pid}/wchan").read_text() or "select" in Path(f"/proc/{pid}/wchan").read_text())
before = shelf.status()
os.kill(pid, signal.SIGSTOP)
try:
    change("restore")
    change("stash")
    wait(lambda: shelf.status()["valid"])
    assert not native(), "Expected panel to remain closed while watcher is deliberately paused"
finally:
    resumed = time.perf_counter()
    os.kill(pid, signal.SIGCONT)
after = wait(lambda: rendered(True, ready=True))
results["coalesced_restore_stash"] = {"before_generation": before["generation"],
    "after": after, "native_reopened_ms_after_resume": round((time.perf_counter() - resumed) * 1000, 2)}

results["bursts"] = []
with ThreadPoolExecutor(max_workers=8) as pool:
    for count, interval in [(2, .040), (3, .040), (6, .040), (7, .015)]:
        before = shelf.status()
        expected = before["desired_stashed"] != bool(count % 2)
        began = time.perf_counter()
        futures = []
        for index in range(count):
            time.sleep(max(0, began + interval * index - time.perf_counter()))
            futures.append(pool.submit(change, "toggle"))
        timings = [future.result() for future in futures]
        actual = wait(lambda: rendered(expected, ready=expected))
        wait(quiet)
        time.sleep(.12)
        assert rendered(expected, ready=expected), "Late stale panel/window update"
        assert shelf.status()["generation"] - before["generation"] == count
        results["bursts"].append({"count": count, "interval_ms": interval * 1000,
            "commands_ms": timings, "expected_stashed": expected,
            "actual": actual, "no_late_regression": True, "all_presses_counted": True})

# Reproduce a failed preview without waiting six seconds or altering the
# installed capture code. The real native panel must remain actionable.
change("restore")
wait(lambda: rendered(False))
wait(quiet)
with shelf.command_lock():
    shelf.cleanup_image()  # Deliberately exercise first-stash/no-cache fallback.
original_start = shelf.start_preview
old_capture = sys.modules.get("capture")
try:
    shelf.start_preview = lambda: None
    shelf.change("stash")

    def fail_capture(*_args):
        raise RuntimeError("Injected capture failure for regression test")

    sys.modules["capture"] = types.SimpleNamespace(capture=fail_capture)
    shelf.preview()
finally:
    shelf.start_preview = original_start
    if old_capture is None:
        sys.modules.pop("capture", None)
    else:
        sys.modules["capture"] = old_capture
failed = wait(lambda: rendered(True))
wait(lambda: shelf.read_json(shelf.HEALTH).get("content") == "unavailable")
results["failed_capture_still_actionable"] = {"native": native(), "helper": shelf.status(), "shared": inspect()}
cmd("/usr/bin/python3", str(BASE / "capture.py"), "monitor:HDMI-A-2", str(BASE / "review-fallback-desktop.png"))
click_restore()
results["fallback_restore_callback"] = "restored exact coordinates and focused Dolphin; no stale panel/cast"

change("stash")
wait(lambda: bool(shelf.request("Casts")))
wait(lambda: rendered(True))
began = time.perf_counter()
click_restore()
results["click_during_active_capture"] = {"restored_and_capture_clean_ms": round((time.perf_counter() - began) * 1000, 2)}
time.sleep(.3)
assert rendered(False) and quiet()
results["click_during_active_capture"]["no_late_reopen"] = True

change("stash")
wait(lambda: rendered(True, ready=True))
click_restore()
results["thumbnail_restore_callback"] = "restored exact coordinates and focused Dolphin"
change("stash")
wait(lambda: rendered(True, ready=True))
# Unexpected native close is also repaired, even without a new toggle.
cmd("noctalia", "msg", "panel-close", shelf.PANEL)
began = time.perf_counter()
wait(lambda: not native())
results["unexpected_close_repaired"] = wait(lambda: rendered(True, ready=True))
results["unexpected_close_repair_ms"] = round((time.perf_counter() - began) * 1000, 2)
wait(quiet)
results["final_shared_state"] = inspect()
results["casts"] = shelf.request("Casts")
results["pending_images"] = [p.name for p in BASE.glob("pending-preview-*.png")]
if original_focus:
    shelf.niri_action("FocusWindow", id=original_focus)
cmd("/usr/bin/python3", str(BASE / "capture.py"), "monitor:HDMI-A-2", str(BASE / "review-ready-desktop.png"))
(BASE / "sync-regression-results.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2))
