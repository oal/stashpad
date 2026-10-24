"""Pure lifetime-slot model. No app pinning, IPC, filesystem or UI dependencies."""
import copy
import uuid


def eligible(window):
    # Niri exposes mapped toplevels, not wl_subsurface menus/tooltips. It does
    # not expose transient-parent/type: do not invent title-based exclusions.
    return bool(window.get("is_floating") and window.get("app_id")
                and window.get("pid") and window.get("layout", {}).get("tile_pos_in_workspace_view"))


def reconcile(saved, windows, session, identify, token_factory=lambda: uuid.uuid4().hex):
    """Release closed/tiled lifetimes first. Never renumber a survivor."""
    old = copy.deepcopy(saved)
    valid = {w["id"]: w for w in windows if eligible(w)}
    removed, kept, used = [], [], set()
    for entry in old.get("entries", []):
        w = valid.get(entry["id"])
        key = identify(w) if w else None
        if (old.get("session") == session and key and entry.get("identity") == key
                and entry.get("slot", 0) > 0 and entry["slot"] not in used):
            kept.append(entry)
            used.add(entry["slot"])
        else:
            removed.append(entry)
    known = {e["id"] for e in kept}
    for wid, w in sorted(valid.items()):
        if wid in known:
            continue
        key = identify(w)
        if not key:
            continue
        slot = 1
        while slot in used:
            slot += 1
        used.add(slot)
        kept.append({"id": wid, "identity": key, "token": token_factory(),
                     "slot": slot, "generation": 0, "stashed": False, "last_used": 0})
    return {"version": 1, "session": session, "revision": old.get("revision", 0),
            "entries": sorted(kept, key=lambda e: e["slot"])}, removed


def capture_current(entry, token, generation):
    return bool(entry and entry.get("token") == token and entry.get("stashed")
                and entry.get("generation") == generation)
