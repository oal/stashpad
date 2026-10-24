#!/usr/bin/python3
"""Multi-window live trial: short serialized moves, independent static capture.

The accepted single-window source is frozen in single_window.py and
before-multi-window/. Its clipboard-safe capture and typed Niri IPC are reused.
"""
import copy
import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

import registry as model
import single_window as single

BASE = Path(__file__).resolve().parent
REGISTRY = BASE / "registry.json"
HEALTH = BASE / "panel-health.json"
BACKUP = BASE / "before-multi-window"
PLUGIN = single.PLUGIN
PANEL = single.PANEL
PICKER = PLUGIN + ":picker"
request = single.request
niri_action = single.niri_action
read_json = single.read_json
atomic_json = single.atomic_json
command_lock = single.command_lock
identity = single.identity
position = single.edge.position


def resize(window, target_w, target_h):
    cur_w, cur_h = window["layout"]["window_size"]
    if target_w is not None and target_w != cur_w:
        niri_action("SetWindowWidth", id=window["id"],
                    change={"AdjustFixed": target_w - cur_w})
    if target_h is not None and target_h != cur_h:
        niri_action("SetWindowHeight", id=window["id"],
                    change={"AdjustFixed": target_h - cur_h})


WS_NAME = "shelf"
# How long to let the edge-slide spring settle before teleporting to the
# shelf workspace. niri's critically-damped window-movement spring covers
# ~98% of the distance within ~300 ms; the teleport swallows the tiny
# remainder, so waiting longer just leaves the parked sliver visible.
EDGE_SETTLE = 0.3
NIRI_CONFIG = Path.home() / ".config/niri/config.kdl"
WS_MARK = ("// BEGIN floating-shelf workspace (owned by shelf.py)\n"
           'workspace "shelf"\n'
           "// END floating-shelf workspace\n")


def shelf_ws():
    return next((w for w in request("Workspaces") if (w.get("name") or "") == WS_NAME), None)


def write_niri_config(next_text):
    candidate = NIRI_CONFIG.parent / ".shelf-ws-check.kdl"
    try:
        candidate.write_text(next_text)
        subprocess.run(["niri", "validate", "-c", str(candidate)], check=True)
    finally:
        candidate.unlink(missing_ok=True)
    NIRI_CONFIG.write_text(next_text)


def ensure_shelf_ws():
    if shelf_ws():
        return
    s = NIRI_CONFIG.read_text()
    if WS_MARK not in s:
        write_niri_config(s.rstrip("\n") + "\n\n" + WS_MARK + "\n")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and shelf_ws() is None:
        time.sleep(.05)
    if shelf_ws() is None:
        raise RuntimeError("shelf workspace never appeared")


def drop_shelf_ws():
    if any(e["stashed"] for e in read_json(REGISTRY)["entries"]):
        return
    ws = shelf_ws()
    if ws and any(w["workspace_id"] == ws["id"] for w in request("Windows")):
        return  # something else moved there; leave the named workspace alone
    s = NIRI_CONFIG.read_text()
    if WS_MARK in s:
        write_niri_config(s.replace(WS_MARK, ""))
wake = single.wake
panel = single.panel
NOTIFY = single.NOTIFY

BASE_BIND = single.edge.BIND.replace(str(single.OLD), str(BASE / "shelf.py"))
MULTI_BIND = BASE_BIND.replace("Temporary Dolphin edge-shelf trial.", "Temporary floating-window shelf trial.").replace("Toggle Dolphin edge shelf", "Stash floating / summon recent shelf window")
EXTRA_BINDS = ('    // BEGIN multi-window shelf trial (owned shortcuts)\n'
    '    Mod+Shift+S repeat=false hotkey-overlay-title="Search floating shelf" { spawn "noctalia" "msg" "panel-toggle" "' + PICKER + '"; }\n' +
    ''.join('    Mod+Alt+' + str(i) + ' repeat=false hotkey-overlay-title="Floating shelf slot ' + str(i) + '" { spawn "python3" "' + str(BASE / 'shelf.py') + '" "slot" "' + str(i) + '"; }\n' for i in range(1, 10)) +
    '    // END multi-window shelf trial\n\n')


def session():
    s = Path(os.environ["NIRI_SOCKET"]).stat()
    return [Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            s.st_dev, s.st_ino, s.st_ctime_ns]


def image_path(value):
    if not value:
        return None
    p = Path(value)
    return p if p.parent == BASE and p.name.startswith("multi-thumbnail-") else None


def delete_frames(entry):
    for name in ("image", "retired_image"):
        p = image_path(entry.pop(name, None))
        if p:
            p.unlink(missing_ok=True)
    entry.pop("capture_generation", None)


def save(value):
    value["revision"] = value.get("revision", 0) + 1
    atomic_json(REGISTRY, value)


def entry_for(value, wid, token=None):
    return next((e for e in value.get("entries", []) if e["id"] == wid
                 and (token is None or e["token"] == token)), None)


def synchronize():
    """Caller holds command lock. Actual compositor state wins."""
    windows = request("Windows")
    old = read_json(REGISTRY)
    value, removed = model.reconcile(old, windows, session(), identity)
    for e in removed:
        delete_frames(e)
    for e in value["entries"]:
        w = next(w for w in windows if w["id"] == e["id"])
        e.update(title=w.get("title") or w["app_id"], app_id=w["app_id"],
                 workspace_id=w["workspace_id"], focused=w["is_focused"])
        stamp = w.get("focus_timestamp") or {}
        if w["is_focused"] and stamp != e.get("focus_stamp"):
            e["last_used"] = time.monotonic()
        e["focus_stamp"] = stamp
        if e.get("stashed") and (e.get("stashed_ws") is None or w["workspace_id"] != e["stashed_ws"]):
            e["stashed"] = False
            e["stashed_ws"] = None
            e["generation"] += 1
        if e.get("image") and not (image_path(e["image"]) and Path(e["image"]).is_file()):
            delete_frames(e)
    if value != old:
        save(value)
    return value, windows


def reap_retired(value):
    health = read_json(HEALTH)
    acknowledged = set(health.get("images") or [])
    changed = False
    keep = set()
    for e in value["entries"]:
        if e.get("retired_image") and (not health.get("open") or e.get("image") in acknowledged):
            p = image_path(e.pop("retired_image"))
            if p:
                p.unlink(missing_ok=True)
            changed = True
        keep.update(e.get(n) for n in ("image", "retired_image"))
    for orphan in BASE.glob("multi-thumbnail-*.png"):
        if str(orphan) not in keep:
            orphan.unlink(missing_ok=True)
    if changed:
        save(value)


def snapshot(value):
    workspaces = request("Workspaces")
    current = next((w for w in workspaces if w["is_focused"]), {})
    names = {w["id"]: str(w.get("name") or w["idx"]) for w in workspaces}
    rows = []
    for e in value["entries"]:
        fresh = e.get("image") and e.get("capture_generation") == e["generation"]
        preview_state = ("ready" if fresh else "unavailable" if e.get("failed_generation") == e["generation"]
                         else "loading" if e["stashed"] else "uncaptured")
        rows.append({k: e.get(k) for k in ("id", "token", "slot", "title", "app_id", "stashed", "focused", "image", "generation", "workspace_id", "last_used")} | {
            "workspace": names.get(e["workspace_id"], "?"), "preview_state": preview_state,
            "cached": bool(e.get("image") and not fresh)})
    # Global list; opaque full-height rail covers every parked edge behind it.
    return {"generation": value["revision"], "visible": bool(rows), "entries": rows,
            "workspace_id": current.get("id"), "output": current.get("output")}


def start_preview():
    with (BASE / "multi-preview-errors.log").open("a") as errors:
        subprocess.Popen(["/usr/bin/python3", str(BASE / "shelf.py"), "preview"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=errors, start_new_session=True)


def stash(value, entry, window):
    if entry["stashed"]:
        return
    size = list(window["layout"]["window_size"])
    entry.update(original_position=position(window), original_workspace=window["workspace_id"],
                 restore_size=size, stashed=True,
                 generation=entry["generation"] + 1, last_used=time.monotonic())
    entry.pop("preview_error", None)
    save(value)
    try:
        # Slide toward the TOP edge first: niri animates on-screen floating
        # moves (window-movement spring), and the parked coordinates then sit
        # at the top edge, so summoning back re-animates a slide-down —
        # matching the stash pills in the top bar. The teleport to the shelf
        # happens after the spring has settled.
        x, y = position(window)
        dy = -(y + size[1] + 600)
        if dy < 0:
            single.move(window, 0, dy)
            time.sleep(EDGE_SETTLE)
            window = next((w for w in request("Windows") if w["id"] == window["id"]), window)
        # Park on a dedicated dynamic workspace owned by this backend: fully
        # invisible on every other workspace; niri deletes it when the last
        # stash leaves. Corner-parking was rejected: niri always keeps ~39 px
        # of a mapped floating window on-screen and above tiling windows.
        ensure_shelf_ws()
        niri_action("MoveWindowToWorkspace", window_id=window["id"],
                    reference={"Name": WS_NAME}, focus=False)
        moved = next((w for w in request("Windows") if w["id"] == window["id"]), None)
        if moved is None or not moved.get("workspace_id") or moved["workspace_id"] == window["workspace_id"]:
            raise RuntimeError("window did not move to the shelf workspace")
        entry["stashed_ws"] = moved["workspace_id"]
        save(value)
    except Exception:
        entry["stashed"] = False
        entry["stashed_ws"] = None
        entry["generation"] += 1
        save(value)
        raise
    if window["is_focused"]:
        niri_action("FocusTiling")
        if (next((w for w in request("Workspaces") if w["is_focused"]), {}).get("name") or "") == WS_NAME:
            # No tiled window existed (e.g. stash issued while the shelf ws was
            # focused): hand focus to a real workspace so the shelf stays hidden.
            dest = next((w["id"] for w in request("Workspaces") if (w.get("name") or "") != WS_NAME), None)
            if dest is not None:
                try:
                    niri_action("FocusWorkspace", reference={"Id": dest})
                except Exception:
                    pass
    start_preview()


def restore_entry(value, entry, window, summon=False):
    target = next((w for w in request("Workspaces") if w["is_focused"]), {}) if summon else {}
    parked = entry["stashed"]
    original = entry.get("original_position") if parked else position(window)
    entry["generation"] += 1
    save(value)  # Cancel THIS row's capture before any movement.
    if summon and target.get("id") != window["workspace_id"]:
        niri_action("MoveWindowToWorkspace", window_id=window["id"],
                    reference={"Id": target["id"]}, focus=False)
        window = next(w for w in request("Windows") if w["id"] == entry["id"])
    sws = entry.get("stashed_ws")
    if parked and sws is not None and window.get("workspace_id") == sws:
        ids = {w["id"] for w in request("Workspaces")}
        dest = entry.get("original_workspace")
        if dest not in ids:
            dest = next((w["id"] for w in request("Workspaces") if w["is_focused"]), None)
        if dest is not None:
            niri_action("MoveWindowToWorkspace", window_id=window["id"], reference={"Id": dest}, focus=False)
            time.sleep(0.12)
            window = next((w for w in request("Windows") if w["id"] == entry["id"]), window)
    entry["stashed_ws"] = None
    restore = entry.get("restore_size")
    if restore and parked:
        window = next((w for w in request("Windows") if w["id"] == entry["id"]), window)
        resize(window, restore[0], restore[1])
        time.sleep(0.12)
        window = next((w for w in request("Windows") if w["id"] == entry["id"]), window)
    if original and (parked or (summon and target.get("id") != entry["workspace_id"])):
        now = position(window)
        single.move(window, original[0] - now[0], original[1] - now[1])
    if summon:
        niri_action("FocusWindow", id=window["id"])
        entry["last_used"] = time.monotonic()
    entry["stashed"] = False
    save(value)
    drop_shelf_ws()


def picker_focus(prior=None):
    """Last actual app focus survives layer-shell keyboard focus loss.

    Query again BEFORE closing the picker, so focus changes while open win but
    automatic app refocus caused by panel.close cannot change the decision.
    Preserve a newer closed-window context rather than falsely treating the
    previous surviving window as focused. Tokens never transfer to slot reuse.
    """
    with command_lock():
        value, windows = synchronize()
        current_session = value["session"]
        latest = max((w for w in windows if w.get("focus_timestamp")),
                     key=lambda w: (w["focus_timestamp"]["secs"], w["focus_timestamp"]["nanos"]), default=None)
        stamp = [latest["focus_timestamp"]["secs"], latest["focus_timestamp"]["nanos"]] if latest else [-1, 0]
        if prior and prior.get("session") == current_session and prior.get("stamp", [-1, 0]) > stamp:
            return prior
        entry = entry_for(value, latest["id"]) if latest else None
        return {"session": current_session, "stamp": stamp,
                "token": entry["token"] if entry else None}


def cycle(value, windows, direction):
    """Swap the sole visible shelf window with the next/previous stashed one.

    Super+Left/Right takeover: when the focused window is the only visible
    shelf entry, stashing it and summoning the neighbor in SLOT order (pill
    left-to-right) keeps exactly one float on screen. Any other focus state
    falls through to the column-focus action the key originally performed.
    """
    step = 1 if str(direction) in ("right", "next", "1", "+1") else -1
    ordered = sorted(value["entries"], key=lambda e: e["slot"])
    entry = None
    focused = next((w for w in windows if w["is_focused"]), None)
    if focused:
        entry = entry_for(value, focused["id"])
    target = None
    if entry and not entry["stashed"] and sum(1 for e in ordered if not e["stashed"]) == 1:
        idx = ordered.index(entry)
        for offset in range(1, len(ordered)):
            cand = ordered[(idx + step * offset) % len(ordered)]
            if cand["stashed"]:
                target = cand
                break
    if target is None:
        niri_action("FocusColumnRight" if step > 0 else "FocusColumnLeft")
        return
    stash(value, entry, next(w for w in windows if w["id"] == entry["id"]))
    summoned = next((w for w in request("Windows") if w["id"] == target["id"]), None)
    if summoned is not None:
        restore_entry(value, target, summoned, summon=True)


def change(action, argument=None, token=None):
    with command_lock():
        value, windows = synchronize()
        focused = next((w for w in windows if w["is_focused"]), None)
        if action in ("restore", "restore-all"):
            for e in value["entries"]:
                if e["stashed"]:
                    restore_entry(value, e, next(w for w in windows if w["id"] == e["id"]))
        elif action == "cycle":
            cycle(value, windows, argument)
        else:
            entry = None
            if action == "slot":
                entry = next((e for e in value["entries"] if e["slot"] == int(argument)), None)
            elif action in ("activate", "summon", "picker-toggle"):
                entry = entry_for(value, int(argument), token)
            elif action in ("toggle", "stash"):
                entry = entry_for(value, focused["id"]) if focused else None
                if not entry and action == "toggle":
                    entry = max((e for e in value["entries"] if e["stashed"]),
                                key=lambda e: e["last_used"], default=None)
            elif action == "restore-focus":
                entry = max((e for e in value["entries"] if e["stashed"]),
                            key=lambda e: e["last_used"], default=None)
            if entry:
                window = next(w for w in windows if w["id"] == entry["id"])
                should_stash = (not entry["stashed"] if action == "picker-toggle" else
                                action == "stash" or (action not in ("summon", "restore-focus") and window["is_focused"]
                                                       and not (action == "toggle" and entry["stashed"])))
                if should_stash:
                    stash(value, entry, window)
                else:
                    restore_entry(value, entry, window, summon=True)
        reap_retired(value)
    wake()


def preview():
    """One capture queue, independent generations and caches for every row."""
    with (BASE / "multi-preview.lock").open("a") as owned:
        try:
            fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            with command_lock():
                value, windows = synchronize()
                candidates = [e for e in value["entries"] if e["stashed"]
                              and e.get("capture_generation") != e["generation"]
                              and e.get("failed_generation") != e["generation"]]
                if not candidates:
                    # Release under command lock: final stash cannot lose worker.
                    fcntl.flock(owned, fcntl.LOCK_UN)
                    return
                entry = copy.deepcopy(max(candidates, key=lambda e: e["last_used"]))
                wid, key, token, generation = entry["id"], entry["identity"], entry["token"], entry["generation"]
            candidate = BASE / ("multi-pending-preview-" + uuid.uuid4().hex + ".png")

            def cancelled():
                current = entry_for(read_json(REGISTRY), wid, token)
                return not model.capture_current(current, token, generation)

            captured, error = False, None
            try:
                from capture import capture
                capture(wid, candidate, cancelled)
                import gi
                gi.require_version("GdkPixbuf", "2.0")
                from gi.repository import GdkPixbuf
                decoded = GdkPixbuf.Pixbuf.new_from_file(str(candidate))
                if decoded.get_width() <= 0 or decoded.get_height() <= 0:
                    raise RuntimeError("Captured PNG has no pixels")
                del decoded
                captured = True
            except InterruptedError:
                pass
            except Exception as failure:
                error = str(failure)
            try:
                deadline = time.monotonic() + 2
                while captured and not cancelled():
                    with command_lock():
                        value, _ = synchronize()
                        reap_retired(value)
                        live = entry_for(value, wid, token)
                        if not live or not live.get("retired_image"):
                            break
                    if time.monotonic() > deadline:
                        captured = False
                        error = "Native image handoff pending; keeping last good frame."
                        break
                    time.sleep(.01)
                with command_lock():
                    value, windows = synchronize()
                    live = entry_for(value, wid, token)
                    window = next((w for w in windows if w["id"] == wid), None)
                    if model.capture_current(live, token, generation) and identity(window) == key:
                        if captured and candidate.exists():
                            final = BASE / ("multi-thumbnail-" + uuid.uuid4().hex + ".png")
                            candidate.replace(final)
                            if live.get("image"):
                                live["retired_image"] = live["image"]
                            live.update(image=str(final), capture_generation=generation)
                            live.pop("preview_error", None)
                        else:
                            live.update(failed_generation=generation, preview_error=error or "Capture canceled; stash again to refresh.")
                        save(value)
            finally:
                candidate.unlink(missing_ok=True)
            wake()


def status():
    with command_lock():
        value, _ = synchronize()
        return snapshot(value)


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
    previous = displayed = None
    with (BASE / "watch.lock").open("a") as owned:
        # A hot-reloaded service starts its replacement before its previous
        # child has finished finally/restore. Briefly wait for ownership; never
        # run competing watchers and never exit prematurely during that handoff.
        deadline = time.monotonic() + 4
        while running:
            try:
                fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return
                time.sleep(.01)
        if not running:
            return
        with socket.socket(socket.AF_UNIX) as events, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifications:
            NOTIFY.unlink(missing_ok=True)
            notifications.bind(str(NOTIFY))
            events.connect(os.environ["NIRI_SOCKET"])
            events.sendall(b'"EventStream"\n')
            try:
                while running:
                    with command_lock():
                        value, _ = synchronize()
                        reap_retired(value)
                        view = snapshot(value)
                        # The persistent shelf panel is retired; the bar widget is
                        # purely state-driven, so no surface probing, panel
                        # open/close or repair republish passes are needed.
                        if view != previous:
                            print(json.dumps(view), flush=True)
                            previous = view
                    ready, _, _ = select.select([events, notifications], [], [], .25)
                    if notifications in ready:
                        notifications.recv(4096)
                    if events in ready and not events.recv(262144):
                        break
            finally:
                try:
                    try:
                        change("restore-all")
                    finally:
                        panel("close")
                        with command_lock():
                            value = read_json(REGISTRY)
                            for e in value.get("entries", []):
                                delete_frames(e)
                            save(value)
                finally:
                    NOTIFY.unlink(missing_ok=True)


def wait_workers():
    for name in ("watch.lock", "multi-preview.lock", "preview.lock"):
        with (BASE / name).open("a") as owned:
            deadline = time.monotonic() + 8
            while True:
                try:
                    fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Trial worker still stopping; retry rollback in a moment.")
                    time.sleep(.02)


def config_without_multi(text):
    if EXTRA_BINDS not in text:
        raise RuntimeError("Owned shortcuts edited; refusing rollback. See before-multi-window/config.kdl.")
    if MULTI_BIND not in text:
        raise RuntimeError("Super+S edited; refusing automatic rollback.")
    return text.replace(EXTRA_BINDS, "", 1).replace(MULTI_BIND, BASE_BIND, 1)


def rollback(single_only=False):
    import visual
    config = Path.home() / ".config/niri/config.kdl"
    visual.restore_owned_config(config_without_multi(config.read_text()))
    change("restore-all")
    subprocess.run(["noctalia", "msg", "plugins", "disable", PLUGIN], check=True)
    wait_workers()
    clean = visual.restore_owned_config(config_without_multi(config.read_text()))
    config.write_text(clean if single_only else clean.replace(BASE_BIND, single.edge.BIND, 1))
    visual.deactivate()
    state = read_json(single.edge.STATE)
    state["stashed"] = False
    atomic_json(single.edge.STATE, state)
    with command_lock():
        for e in read_json(REGISTRY).get("entries", []):
            delete_frames(e)
        REGISTRY.unlink(missing_ok=True)
        single.cleanup_image()
    if single_only:
        for name in ("shelf.py", "README.md"):
            shutil.copy2(BACKUP / name, BASE / name)
        for name in ("plugin.toml", "monitor.luau", "panel.luau"):
            shutil.copy2(BACKUP / "plugin" / name, BASE / "plugin" / name)
        (BASE / "plugin/picker.luau").unlink(missing_ok=True)
        subprocess.run(["noctalia", "msg", "config-reload"], check=True)
        subprocess.run(["noctalia", "msg", "plugins", "enable", PLUGIN], check=True)
        print("Known-good single-Dolphin thumbnail trial restored. Dolphin visible; Super+S stashes it.")
    else:
        if single.DROPIN.is_symlink() and single.DROPIN.resolve() == BASE / "plugin":
            single.DROPIN.unlink()
        subprocess.run(["noctalia", "msg", "config-reload"], check=True)
        print("Thumbnail shelf removed. Original edge-only Super+S and its undo work again.")


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
        elif action == "picker-focus":
            print(json.dumps(picker_focus(json.loads(sys.argv[2]) if len(sys.argv) > 2 else None)))
        elif action in ("undo", "rollback-single"):
            rollback(action == "rollback-single")
        elif action in ("toggle", "stash", "restore", "restore-all", "restore-focus", "slot", "activate", "summon", "picker-toggle", "cycle"):
            change(action, sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
        else:
            raise RuntimeError("Usage: shelf.py [toggle|cycle left|right|slot N|activate ID TOKEN|summon ID TOKEN|restore-all|status|rollback-single|undo]")
    except Exception as error:
        print("[shelf] " + str(error), file=sys.stderr)
        if action not in ("watch", "preview", "status", "picker-focus"):
            subprocess.run(["noctalia", "msg", "notification-show", "Floating shelf", str(error)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(1)
