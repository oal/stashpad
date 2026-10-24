#!/usr/bin/python3
"""Bounded live stale-while-refresh tests; only closes its own test window."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import types

import shelf

os.umask(0o077)
BASE = Path(__file__).resolve().parent
results = {}
original_focus = next((w["id"] for w in shelf.request("Windows") if w["is_focused"]), None)


def cmd(*args):
    return subprocess.check_output(args, text=True, timeout=15)


def change(action, launch=True):
    if launch:
        began = time.perf_counter()
        cmd("/usr/bin/python3", str(BASE / "shelf.py"), action)
        return round((time.perf_counter() - began) * 1000, 2)
    start = shelf.start_preview
    try:
        shelf.start_preview = lambda: None
        shelf.change(action)
    finally:
        shelf.start_preview = start


def native():
    return any(v["namespace"] == "noctalia-panel" and v["keyboard_interactivity"] == "None"
               for v in shelf.request("Layers"))


def wait(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.004)
    raise RuntimeError("Timed out: " + json.dumps({"state": shelf.status(), "ui": shelf.read_json(shelf.HEALTH)}))


def rendered(image=None, fresh=False):
    state = shelf.status()
    ui = shelf.read_json(shelf.HEALTH)
    return (state["visible"] and native() and ui.get("open")
            and ui.get("generation") == state["generation"]
            and ui.get("image") == state["image"]
            and (image is None or state["image"] == image)
            and (not fresh or state["valid"]))


def quiet():
    return not shelf.request("Casts") and not list(BASE.glob("pending-preview-*.png"))


def cached_file():
    return shelf.read_json(shelf.META)["image"]


def screenshot(name):
    cmd("/usr/bin/python3", str(BASE / "capture.py"), "monitor:HDMI-A-2", str(BASE / name))


change("restore")
wait(quiet)
with shelf.command_lock():
    shelf.cleanup_image()  # Explicitly reset cache to exercise first-stash fallback.
change("stash", launch=False)
wait(lambda: rendered() and shelf.read_json(shelf.HEALTH).get("content") == "loading")
assert shelf.status()["image"] is None
results["first_stash_placeholder"] = True
cmd("/usr/bin/python3", str(BASE / "shelf.py"), "preview")
wait(lambda: rendered(fresh=True))
first = cached_file()
change("restore")
wait(lambda: not native())
assert cached_file() == first and Path(first).exists()
change("stash", launch=False)
wait(lambda: rendered(first))
assert shelf.status()["cached"] and not shelf.status()["valid"]
results["restash_immediate_cached_native_image"] = {"state": shelf.status(), "ui": shelf.read_json(shelf.HEALTH)}
screenshot("review-cached-desktop.png")

# Pause only the trial watcher: the UI is intentionally still displaying the
# old path while a real complete capture publishes its replacement.
watchers = []
for proc in Path("/proc").iterdir():
    if proc.name.isdigit():
        try:
            if (proc / "cmdline").read_bytes() == b"/usr/bin/python3\0" + str(BASE / "shelf.py").encode() + b"\0watch\0":
                watchers.append(int(proc.name))
        except OSError:
            pass
assert len(watchers) == 1
pid = watchers[0]
wait(lambda: any(word in Path(f"/proc/{pid}/wchan").read_text() for word in ("poll", "select")))
os.kill(pid, signal.SIGSTOP)
try:
    cmd("/usr/bin/python3", str(BASE / "shelf.py"), "preview")
    updated = cached_file()
    assert updated != first and Path(updated).exists() and Path(first).exists()
    assert shelf.read_json(shelf.META)["retired_image"] == first
    assert shelf.read_json(shelf.HEALTH)["image"] == first
    assert len(list(BASE.glob("thumbnail-*.png"))) == 2
    results["atomic_handoff_keeps_displayed_path"] = True
finally:
    os.kill(pid, signal.SIGCONT)
wait(lambda: rendered(updated, fresh=True))
wait(lambda: not Path(first).exists())
assert len(list(BASE.glob("thumbnail-*.png"))) == 1
results["old_path_reaped_after_native_ack"] = True
screenshot("review-refreshed-desktop.png")

# Reuse is never allowed for a different window, PID/starttime or session.
window = shelf.matching(shelf.request("Windows"), shelf.read_json(shelf.edge.STATE))
meta = shelf.read_json(shelf.META)
assert shelf.cached_for(meta, window)
rejected = []
for field in ("window_id", "pid", "process_start", "session"):
    foreign = deepcopy(meta)
    foreign["identity"][field] = "not-this-lifetime"
    assert not shelf.cached_for(foreign, window)
    rejected.append(field)
legacy = deepcopy(meta)
legacy.pop("identity")
assert not shelf.cached_for(legacy, window)
results["foreign_and_legacy_cache_rejected"] = rejected + ["legacy-no-identity"]

def injected_capture(*_args):
    raise RuntimeError("Injected cache-refresh failure")


def corrupt_capture(_window, path, _cancelled):
    Path(path).write_bytes(b"not a decodable PNG" * 20)


for name, fake in [("failed_capture", injected_capture), ("undecodable_capture", corrupt_capture)]:
    change("restore")
    change("stash", launch=False)
    module = sys.modules.get("capture")
    try:
        sys.modules["capture"] = types.SimpleNamespace(capture=fake)
        shelf.preview()
    finally:
        if module is None:
            sys.modules.pop("capture", None)
        else:
            sys.modules["capture"] = module
    wait(lambda: rendered(updated) and shelf.status()["preview_state"] == "unavailable")
    assert cached_file() == updated and Path(updated).exists() and quiet()
    results[name + "_retains_cache"] = True

change("restore")
change("stash")
wait(lambda: bool(shelf.request("Casts")))
assert shelf.status()["image"] == updated
restore_ms = change("restore")
wait(quiet)
time.sleep(.15)
assert not native() and not shelf.status()["desired_stashed"] and cached_file() == updated
results["actual_inflight_cancellation"] = {"restore_command_ms": restore_ms, "cache_unchanged": True,
                                         "no_late_panel": True, "no_cast_or_pending": quiet()}
change("stash", launch=False)
wait(lambda: rendered(updated))
cmd("noctalia", "msg", "plugin", shelf.PANEL, "all", "restore")
wait(lambda: not native() and not shelf.status()["desired_stashed"])
restored = shelf.matching(shelf.request("Windows"), shelf.read_json(shelf.edge.STATE))
assert restored["is_focused"] and shelf.edge.position(restored) == shelf.read_json(shelf.edge.STATE)["original_position"]
assert cached_file() == updated
results["cached_image_restore_callback"] = "exact saved position and current window focus"

results["bursts"] = []
with ThreadPoolExecutor(max_workers=8) as pool:
    for count, interval in [(3, .015), (6, .015), (7, .015), (2, .040)]:
        before = shelf.status()
        expected = before["desired_stashed"] != bool(count % 2)
        began = time.perf_counter()
        jobs = []
        for i in range(count):
            time.sleep(max(0, began + i * interval - time.perf_counter()))
            jobs.append(pool.submit(change, "toggle"))
        timings = [job.result() for job in jobs]
        wait(lambda: rendered(fresh=True) if expected else not native() and not shelf.status()["desired_stashed"])
        wait(quiet)
        time.sleep(.15)
        assert (rendered(fresh=True) if expected else not native() and not shelf.status()["desired_stashed"])
        assert shelf.status()["generation"] - before["generation"] == count
        wait(lambda: len(list(BASE.glob("thumbnail-*.png"))) == 1)
        results["bursts"].append({"count": count, "interval_ms": interval * 1000,
                                  "commands_ms": timings, "expected_stashed": expected, "passed": True})

# Test close cleanup using a new disposable Dolphin window, never the user's.
change("restore")
wait(quiet)
saved = shelf.read_json(shelf.edge.STATE)
old_image = cached_file()
known = {w["id"] for w in shelf.request("Windows")}
created = None
try:
    subprocess.Popen(["dolphin", "--new-window", str(BASE)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created = wait(lambda: next((w for w in shelf.request("Windows") if w["app_id"] == "org.kde.dolphin" and w["id"] not in known), None))
    shelf.niri_action("MoveWindowToFloating", id=created["id"])
    created = wait(lambda: next((w for w in shelf.request("Windows") if w["id"] == created["id"] and w["is_floating"]), None))
    with shelf.command_lock():
        shelf.atomic_json(shelf.edge.STATE, {"window_id": created["id"], "pid": created["pid"],
                                           "original_position": shelf.edge.position(created), "stashed": False})
    change("stash", launch=False)
    wait(lambda: rendered() and shelf.status()["image"] is None)
    assert not Path(old_image).exists(), "Old window's cache crossed identities"
    cmd("/usr/bin/python3", str(BASE / "shelf.py"), "preview")
    wait(lambda: rendered(fresh=True))
    disposable_image = cached_file()
    change("restore")
    shelf.niri_action("CloseWindow", id=created["id"])
    wait(lambda: not any(w["id"] == created["id"] for w in shelf.request("Windows")))
    wait(lambda: not shelf.META.exists() and not list(BASE.glob("thumbnail-*.png")))
    assert not Path(disposable_image).exists()
    results["different_window_and_close_cleanup"] = {"old_cache_never_displayed": True,
                                                     "restored_window_close_evicted_cache": True}
finally:
    if created and any(w["id"] == created["id"] for w in shelf.request("Windows")):
        change("restore")
        shelf.niri_action("CloseWindow", id=created["id"])
    with shelf.command_lock():
        shelf.atomic_json(shelf.edge.STATE, saved)

change("stash")
wait(lambda: rendered(fresh=True))
wait(quiet)
wait(lambda: len(list(BASE.glob("thumbnail-*.png"))) == 1)
assert BASE.stat().st_mode & 0o777 == 0o700
assert Path(cached_file()).stat().st_mode & 0o077 == 0
results["private_and_bounded"] = {"directory": "0700", "image": "0600", "cached_files": 1,
                                  "pending_files": 0, "casts": []}
results["final_state"] = shelf.status()
if original_focus:
    shelf.niri_action("FocusWindow", id=original_focus)
(BASE / "cache-regression-results.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2))
