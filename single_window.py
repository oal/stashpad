#!/usr/bin/python3
"""Fast reversible shelf: short serialized moves, supersedable preview work.

No new daemon: the existing Noctalia runStream watcher still owns the panel.
Every toggle changes desired state; only preview refreshes may be coalesced.
"""
import ctypes
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
import uuid

BASE = Path(__file__).resolve().parent
OLD = BASE / "edge_shelf.py"
META = BASE / "thumbnail.json"
CONTROL = BASE / "control.json"
HEALTH = BASE / "panel-health.json"
PANEL = "oal/edge-shelf-trial:shelf"
PLUGIN = "oal/edge-shelf-trial"
DROPIN = Path.home() / ".local/share/noctalia/plugins/edge-shelf-trial"
NOTIFY = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "noctalia-dolphin-thumbnail-trial.sock"
spec = importlib.util.spec_from_file_location("edge_shelf", OLD)
edge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def atomic_json(path, data):
    # All writers of each destination hold the same short command lock.
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(data, indent=2) + "\n")
    pending.replace(path)


def command_lock():
    lock = (edge.BASE / "lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def request(name):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(os.environ["NIRI_SOCKET"])
        sock.sendall((json.dumps(name) + "\n").encode())
        with sock.makefile("r") as stream:
            reply = json.loads(stream.readline())
    if "Err" in reply:
        raise RuntimeError(str(reply["Err"]))
    return reply["Ok"][name] if isinstance(name, str) else reply["Ok"]


def niri_action(name, **values):
    return request({"Action": {name: values}})


def move(window, x, y=0):
    niri_action("MoveFloatingWindow", id=window["id"],
                x={"AdjustFixed": x}, y={"AdjustFixed": y})


def panel(action):
    try:
        subprocess.run(["noctalia", "msg", "panel-" + action, PANEL],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=.5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # Window moves/recovery must work without the shell.


def wake():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(b"update", str(NOTIFY))
    except OSError:
        pass  # The 250 ms fallback still handles a missed notification.


def cleanup_image():
    # Never unlink an encoder's in-flight pending-preview-* file.
    for path in BASE.glob("thumbnail-*.png"):
        path.unlink(missing_ok=True)
    META.unlink(missing_ok=True)


def matching(windows, state):
    return next((w for w in windows if w["id"] == state.get("window_id")
                 and w["pid"] == state.get("pid") and w["app_id"] == "org.kde.dolphin"), None)


def identity(window):
    """Never reuse a private frame across compositor/process/window lifetimes."""
    if not window:
        return None
    try:
        sock = Path(os.environ["NIRI_SOCKET"]).stat()
        start = Path(f'/proc/{window["pid"]}/stat').read_text().rsplit(")", 1)[1].split()[19]
        return {"window_id": window["id"], "pid": window["pid"], "process_start": start,
                "session": [Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                            sock.st_dev, sock.st_ino, sock.st_ctime_ns]}
    except (OSError, KeyError, IndexError):
        return None  # Fail closed: an unverifiable identity never exposes a cache.


def cached_for(meta, window):
    key = identity(window)
    image = Path(meta.get("image", "/nonexistent"))
    return bool(key and meta.get("identity") == key and image.parent == BASE
                and image.name.startswith("thumbnail-") and image.is_file())


def prune_cache(window):
    meta = read_json(META)
    if meta and not cached_for(meta, window):
        cleanup_image()


def reap_retired():
    """Keep the displaced path until native UI has consumed its replacement.

    One current cache, at most one temporary retiring frame. A second refresh
    cannot publish until the first handoff is acknowledged (or panel closed).
    """
    meta = read_json(META)
    keep = {meta.get("image"), meta.get("retired_image")}
    # Recover an interrupted publish without ever touching encoder-owned files.
    for orphan in BASE.glob("thumbnail-*.png"):
        if str(orphan) not in keep:
            orphan.unlink(missing_ok=True)
    retired = meta.get("retired_image")
    if not retired:
        return True
    health = read_json(HEALTH)
    if health.get("open") and health.get("image") != meta.get("image"):
        return False
    path = Path(retired)
    if path.parent == BASE and path.name.startswith("thumbnail-"):
        path.unlink(missing_ok=True)
    meta.pop("retired_image", None)
    atomic_json(META, meta)
    return True


def stashed(window, state):
    pos = window and edge.position(window)
    return bool(pos and window["is_floating"] and state.get("stashed") and pos[0] < 100)


def next_control(state, desired):
    value = {"generation": read_json(CONTROL).get("generation", 0) + 1,
             "window_id": state.get("window_id"), "pid": state.get("pid"),
             "desired_stashed": desired}
    atomic_json(CONTROL, value)
    return value


def start_preview():
    # The worker lock coalesces previews only, never key presses. An existing
    # worker rechecks latest desired generation before exiting under our lock.
    with (BASE / "preview-errors.log").open("a") as errors:
        subprocess.Popen(["/usr/bin/python3", str(BASE / "shelf.py"), "preview"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=errors, start_new_session=True)


def change(action):
    began = time.perf_counter()
    with command_lock():
        locked = time.perf_counter()
        state = read_json(edge.STATE)
        window = matching(request("Windows"), state)
        prune_cache(window)
        parked = stashed(window, state)
        restore = action in ("restore", "restore-focus") or (action == "toggle" and parked)
        if restore:
            control = next_control(state, False)  # Cancels capture without waiting for it.
            if parked:
                current = edge.position(window)
                original = state["original_position"]
                move(window, original[0] - current[0], original[1] - current[1])
            moved = time.perf_counter()
            state["stashed"] = False
            atomic_json(edge.STATE, state)
            # Panel operations share the short lock with the watcher: it cannot
            # open from a stale pre-restore snapshot after this close.
            panel("close")
            reap_retired()  # Ordinary restore deliberately retains last good frame.
            if parked and action in ("restore-focus", "toggle"):
                niri_action("FocusWindow", id=window["id"])
        else:
            if not window:
                dolphins = [w for w in request("Windows") if w["app_id"] == "org.kde.dolphin"]
                window = dolphins[0] if len(dolphins) == 1 else None
            if not window or not window["is_floating"]:
                raise RuntimeError("Open exactly one floating Dolphin window (Super+V).")
            prune_cache(window)
            if not parked:
                state = {"window_id": window["id"], "pid": window["pid"],
                         "original_position": edge.position(window), "stashed": True}
                control = next_control(state, True)
                # Persist intent before moving so an interrupted command remains
                # recoverable by the original helper as well as this one.
                atomic_json(edge.STATE, state)
                try:
                    move(window, -10000)
                except Exception:
                    state["stashed"] = False
                    atomic_json(edge.STATE, state)
                    next_control(state, False)
                    raise
                if window["is_focused"]:
                    niri_action("FocusTiling")
            else:
                control = read_json(CONTROL)
                if not control.get("desired_stashed") or control.get("window_id") != window["id"]:
                    control = next_control(state, True)
            moved = time.perf_counter()
            start_preview()
    wake()
    if os.environ.get("SHELF_PROFILE"):
        print(json.dumps({"toggle_profile": {"action": action, "generation": control["generation"],
              "desired_stashed": control["desired_stashed"],
              "lock_wait_ms": round((locked - began) * 1000, 2),
              "move_ack_ms": round((moved - began) * 1000, 2),
              "total_ms": round((time.perf_counter() - began) * 1000, 2)}}), file=sys.stderr)


def preview():
    """One cancellable capture at a time; publishing cannot affect window/panel."""
    capture_lock = (BASE / "preview.lock").open("a")
    try:
        fcntl.flock(capture_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        capture_lock.close()
        return
    try:
        while True:
            with command_lock():
                control = read_json(CONTROL)
                meta = read_json(META)
                generation = control.get("generation")
                needed = (control.get("desired_stashed")
                          and meta.get("generation") != generation
                          and control.get("failed_generation") != generation)
                if not needed:
                    # Release preview ownership WHILE the command lock is held.
                    # A final stash cannot race our exit and lose its worker.
                    fcntl.flock(capture_lock, fcntl.LOCK_UN)
                    return
                window = matching(request("Windows"), control)
                if not window or not stashed(window, read_json(edge.STATE)):
                    fcntl.flock(capture_lock, fcntl.LOCK_UN)
                    return
                key = identity(window)
                if key is None:
                    raise RuntimeError("Cannot verify Dolphin identity; refusing a private preview.")
            image = BASE / ("pending-preview-" + uuid.uuid4().hex + ".png")

            def cancelled():
                latest = read_json(CONTROL)
                return latest.get("generation") != generation or not latest.get("desired_stashed")

            error = None
            captured = False
            try:
                # GI/GStreamer imports and all capture/teardown happen outside
                # the command lock. Restore is independent of their latency.
                from capture import capture
                capture(window["id"], image, cancelled)
                # Validate complete PNG decoding before exposing a new path.
                # Noctalia 5.3's ui.image uses synchronous setSourceFile, not
                # its async image loader. Decode work stays off toggle's lock.
                import gi
                gi.require_version("GdkPixbuf", "2.0")
                from gi.repository import GdkPixbuf
                decoded = GdkPixbuf.Pixbuf.new_from_file(str(image))
                if decoded.get_width() <= 0 or decoded.get_height() <= 0:
                    raise RuntimeError("Captured PNG has no pixels")
                del decoded
                captured = True
            except InterruptedError:
                pass
            except Exception as failure:
                error = str(failure)
            # A slow/paused UI must not accumulate retired private frames or
            # lose a path it has yet to render. Waiting only delays refresh.
            deadline = time.monotonic() + 2
            while captured and not cancelled():
                with command_lock():
                    if reap_retired():
                        break
                if time.monotonic() >= deadline:
                    captured = False
                    error = "Native image handoff pending; keeping last good preview."
                    break
                time.sleep(.01)
            with command_lock():
                latest = read_json(CONTROL)
                live = matching(request("Windows"), control)
                current = bool(live and identity(live) == key
                               and stashed(live, read_json(edge.STATE)))
                if not cancelled() and current and captured and image.exists():
                    final = BASE / ("thumbnail-" + uuid.uuid4().hex + ".png")
                    old = read_json(META)
                    image.replace(final)
                    value = {"generation": generation, "window_id": window["id"],
                             "pid": window["pid"], "identity": key, "image": str(final)}
                    if cached_for(old, live):
                        value["retired_image"] = old["image"]
                    atomic_json(META, value)
                elif not cancelled() and current and not captured:
                    error = error or "Preview interrupted; toggle to refresh."
                    latest["failed_generation"] = generation
                    latest["preview_error"] = error
                    atomic_json(CONTROL, latest)
                    print("[preview] " + error, file=sys.stderr, flush=True)
            image.unlink(missing_ok=True)
            wake()
    finally:
        capture_lock.close()


def _status():
    state = read_json(edge.STATE)
    control = read_json(CONTROL)
    meta = read_json(META)
    window = matching(request("Windows"), state)
    parked = stashed(window, state)
    available = bool(parked and cached_for(meta, window))
    valid = bool(available and meta.get("generation") == control.get("generation"))
    workspace = next((w for w in request("Workspaces") if window and w["id"] == window["workspace_id"]), {})
    preview_state = ("ready" if valid else "unavailable" if control.get("failed_generation") == control.get("generation") else "loading")
    return {"visible": bool(parked and workspace.get("is_active")), "valid": valid,
            "desired_stashed": bool(parked), "generation": control.get("generation"),
            "preview_state": preview_state, "image": meta.get("image") if available else None,
            "cached": bool(available and not valid),
            "title": window["title"] if window else "Dolphin",
            "workspace_id": window["workspace_id"] if window else None}


def status():
    with command_lock():
        return _status()


def watch():
    owner = os.getppid()
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    if os.getppid() != owner:
        return
    running = True

    def stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    previous = None
    displayed = None
    with (BASE / "watch.lock").open("a") as owner_lock:
        fcntl.flock(owner_lock, fcntl.LOCK_EX)
        with socket.socket(socket.AF_UNIX) as events, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifications:
            NOTIFY.unlink(missing_ok=True)
            notifications.bind(str(NOTIFY))
            events.connect(os.environ["NIRI_SOCKET"])
            events.sendall(b'"EventStream"\n')
            try:
                while running:
                    with command_lock():
                        prune_cache(matching(request("Windows"), read_json(META)))
                        reap_retired()
                        value = _status()
                        if not value["desired_stashed"] and read_json(CONTROL).get("desired_stashed"):
                            next_control(read_json(edge.STATE), False)
                            value = _status()
                        health = read_json(HEALTH)
                        surface = any(item["namespace"] == "noctalia-panel"
                                      and item["keyboard_interactivity"] == "None"
                                      for item in request("Layers"))
                        acknowledged = (health.get("open") and health.get("generation") == value["generation"]
                                        and health.get("image") == value["image"]
                                        and health.get("content") in ("ready", "loading", "unavailable"))
                        repair = value["visible"] and (not surface or not acknowledged)
                        if value != previous or repair:
                            print(json.dumps(value), flush=True)
                            previous = value
                        # Restore closes directly. A quick re-stash can coalesce
                        # away the intermediate false value, so boolean-only
                        # tracking is wrong. Reconcile each intent generation,
                        # plus actual native-surface and rendered-content ack.
                        wanted = (value["visible"], value["generation"])
                        if wanted != displayed or repair or (not value["visible"] and health.get("open")):
                            panel("open" if value["visible"] else "close")
                            displayed = wanted
                    ready, _, _ = select.select([events, notifications], [], [], .25)
                    if notifications in ready:
                        notifications.recv(4096)
                    if events in ready and not events.recv(65536):
                        break
            finally:
                change("restore")
                with command_lock():
                    cleanup_image()  # Disable/uninstall/shutdown, unlike ordinary restore.
                NOTIFY.unlink(missing_ok=True)


def undo():
    config = Path.home() / ".config/niri/config.kdl"
    old_bind = edge.BIND
    new_bind = old_bind.replace(str(OLD), str(BASE / "shelf.py"))
    text = config.read_text()
    if new_bind not in text and old_bind not in text:
        raise RuntimeError("Super+S was edited; refusing to overwrite. See config.kdl.before-thumbnail.")
    change("restore")
    if new_bind in text:
        config.write_text(text.replace(new_bind, old_bind, 1))
    subprocess.run(["noctalia", "msg", "plugins", "disable", PLUGIN], check=True)
    # Disable acknowledges before runStream teardown finishes. Wait for our
    # watcher and short preview worker to release ownership, so no late exit
    # callback can restore a newly used edge-only shelf after undo returns.
    for name in ("watch.lock", "preview.lock"):
        with (BASE / name).open("a") as owned:
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Trial helper is still stopping; retry undo in a moment.")
                    time.sleep(.01)
    with command_lock():
        cleanup_image()  # Also works if no watcher was running when undo started.
    if DROPIN.is_symlink() and DROPIN.resolve() == BASE / "plugin":
        DROPIN.unlink()
    subprocess.run(["noctalia", "msg", "config-reload"], check=True)
    print("Thumbnail trial removed. Original edge-only Super+S and its undo work again.")


if __name__ == "__main__":
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    try:
        if action == "watch":
            watch()
        elif action == "preview":
            preview()
        elif action == "status":
            print(json.dumps(status()))
        elif action == "undo":
            undo()
        elif action in ("toggle", "stash", "restore", "restore-focus"):
            change(action)
        else:
            raise RuntimeError("Usage: shelf.py [toggle|stash|restore|restore-focus|status|undo]")
    except Exception as error:
        print(f"[shelf] {error}", file=sys.stderr)
        if action not in ("watch", "preview", "status"):
            subprocess.run(["noctalia", "msg", "notification-show", "Dolphin shelf", str(error)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(1)
