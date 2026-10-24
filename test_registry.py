import copy
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import registry
import shelf


def window(wid, app="email", floating=True, focused=False, workspace=1):
    return {"id": wid, "pid": wid + 100, "app_id": app, "title": app + str(wid),
            "is_floating": floating, "is_focused": focused, "workspace_id": workspace,
            "layout": {"tile_pos_in_workspace_view": [300, 400] if floating else None,
                       "window_size": [700, 500]},
            "focus_timestamp": {"secs": wid, "nanos": 0}}


def identify(w):
    return {"id": w["id"], "pid": w["pid"], "session": "A"} if w else None


def sync(saved, windows, session="A"):
    return registry.reconcile(saved, windows, session, identify)


class Slots(unittest.TestCase):
    def test_holes_not_app_pins(self):
        r, _ = sync({}, [window(1), window(2, "files")])
        r, removed = sync(r, [window(2, "files")])
        self.assertEqual([(e["id"], e["slot"]) for e in r["entries"]], [(2, 2)])
        self.assertEqual(removed[0]["slot"], 1)
        r, _ = sync(r, [window(2, "files"), window(3, "any-new-app")])
        self.assertEqual([(e["id"], e["slot"]) for e in r["entries"]], [(3, 1), (2, 2)])

    def test_same_app_independent(self):
        r, _ = sync({}, [window(5), window(6), window(7)])
        r, _ = sync(r, [window(7), window(6)])
        self.assertEqual([(e["id"], e["slot"]) for e in r["entries"]], [(6, 2), (7, 3)])

    def test_unfloat_releases_and_refloat_gets_lowest_hole(self):
        r, _ = sync({}, [window(1), window(2), window(3)])
        token = r["entries"][1]["token"]
        r, _ = sync(r, [window(1), window(2, floating=False), window(3)])
        r, _ = sync(r, [window(1), window(2), window(3)])
        self.assertEqual([e["slot"] for e in r["entries"]], [1, 2, 3])
        self.assertNotEqual(r["entries"][1]["token"], token)

    def test_restart_preserves_live_assignments_and_tokens(self):
        r, _ = sync({}, [window(10), window(20)])
        r, _ = sync(r, [window(20)])
        restarted, _ = sync(copy.deepcopy(r), [window(20)])
        self.assertEqual(restarted, r)

    def test_new_compositor_rejects_old_ids(self):
        r, _ = sync({}, [window(1), window(2)])
        tokens = [e["token"] for e in r["entries"]]
        r, removed = sync(r, [window(1), window(2)], "B")
        self.assertEqual(len(removed), 2)
        self.assertNotEqual(tokens, [e["token"] for e in r["entries"]])

    def test_pid_change_does_not_reuse_private_identity(self):
        r, _ = sync({}, [window(1)])
        changed = window(1)
        changed["pid"] += 1
        result, removed = sync(r, [changed])
        self.assertEqual(len(removed), 1)
        self.assertNotEqual(result["entries"][0]["token"], r["entries"][0]["token"])

    def test_stale_capture_cannot_publish(self):
        r, _ = sync({}, [window(1)])
        e = r["entries"][0]
        e.update(stashed=True, generation=7)
        self.assertTrue(registry.capture_current(e, e["token"], 7))
        self.assertFalse(registry.capture_current(e, e["token"], 6))
        self.assertFalse(registry.capture_current(e, "old-lifetime", 7))
        e["stashed"] = False
        self.assertFalse(registry.capture_current(e, e["token"], 7))


class Commands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.windows = [window(1, focused=True), window(2, workspace=2)]
        self.actions = []
        self.patches = [patch.object(shelf, "BASE", self.base),
                        patch.object(shelf, "REGISTRY", self.base / "registry.json"),
                        patch.object(shelf, "HEALTH", self.base / "health.json"),
                        patch.object(shelf, "EDGE_SETTLE", 0),
                        patch.object(shelf, "ensure_shelf_ws"),
                        patch.object(shelf, "drop_shelf_ws"),
                        patch.object(shelf, "session", return_value="A"),
                        patch.object(shelf, "identity", side_effect=identify),
                        patch.object(shelf, "command_lock", side_effect=nullcontext),
                        patch.object(shelf, "request", side_effect=self.request),
                        patch.object(shelf, "niri_action", side_effect=self.action),
                        patch.object(shelf.single, "move", side_effect=self.move),
                        patch.object(shelf, "wake"), patch.object(shelf, "start_preview")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def request(self, name):
        if name == "Windows":
            return copy.deepcopy(self.windows)
        if name == "Workspaces":
            return [{"id": 1, "idx": 1, "is_focused": True, "output": "A"},
                    {"id": 2, "idx": 2, "is_focused": False, "output": "A"},
                    {"id": 9, "idx": 0, "name": "shelf", "is_focused": False, "output": "A"}]
        raise AssertionError(name)

    def action(self, name, **kw):
        self.actions.append((name, kw))
        if name == "FocusTiling":
            for w in self.windows:
                w["is_focused"] = False
        elif name == "FocusWindow":
            for w in self.windows:
                w["is_focused"] = w["id"] == kw["id"]
        elif name == "MoveWindowToWorkspace":
            reference = kw["reference"]
            dest = 9 if "Name" in reference else reference["Id"]
            next(w for w in self.windows if w["id"] == kw["window_id"])["workspace_id"] = dest

    def move(self, window, x, y=0):
        pos = next(w for w in self.windows if w["id"] == window["id"])["layout"]["tile_pos_in_workspace_view"]
        pos[0] = max(-900, pos[0] + x)
        pos[1] += y

    def test_toggle_counts_each_press(self):
        for _ in range(7):
            shelf.change("toggle")
        self.assertTrue(shelf.status()["entries"][0]["stashed"])
        shelf.change("toggle")
        self.assertFalse(shelf.status()["entries"][0]["stashed"])
        self.assertEqual(self.windows[0]["layout"]["tile_pos_in_workspace_view"], [300, 400])

    def test_slot_summons_remote_visible_does_not_hide(self):
        shelf.change("slot", "2")
        self.assertEqual(self.windows[1]["workspace_id"], 1)
        self.assertTrue(self.windows[1]["is_focused"])
        self.assertFalse(shelf.status()["entries"][1]["stashed"])
        shelf.change("slot", "2")
        self.assertTrue(shelf.status()["entries"][1]["stashed"])

    def test_stale_click_after_slot_reuse_is_noop(self):
        old = shelf.status()["entries"][0]
        self.windows = [window(3, focused=True), window(2)]
        shelf.change("activate", "1", old["token"])
        self.assertFalse(self.actions)

    def test_last_good_frame_retained_across_toggle(self):
        shelf.change("toggle")
        r = shelf.read_json(shelf.REGISTRY)
        p = self.base / "multi-thumbnail-test.png"
        p.write_bytes(b"fake validated image")
        r["entries"][0].update(image=str(p), capture_generation=r["entries"][0]["generation"])
        shelf.save(r)
        shelf.change("toggle")
        shelf.change("toggle")
        row = shelf.status()["entries"][0]
        self.assertTrue(row["cached"])
        self.assertEqual(row["image"], str(p))
        self.assertTrue(p.exists())

    def test_close_cleans_cache_without_renumber(self):
        r = shelf.status()
        p = self.base / "multi-thumbnail-test.png"
        p.write_bytes(b"private")
        state = shelf.read_json(shelf.REGISTRY)
        state["entries"][0]["image"] = str(p)
        shelf.save(state)
        self.windows.pop(0)
        self.assertEqual(shelf.status()["entries"][0]["slot"], 2)
        self.assertFalse(p.exists())

    def test_empty_slot_is_benign(self):
        shelf.change("slot", "9")
        self.assertFalse(self.actions)

    def test_failed_restore_remains_recoverable(self):
        shelf.change("toggle")
        with patch.object(shelf.single, "move", side_effect=RuntimeError("move rejected")):
            with self.assertRaises(RuntimeError):
                shelf.change("toggle")
        entry = shelf.read_json(shelf.REGISTRY)["entries"][0]
        self.assertTrue(entry["stashed"])
        self.assertEqual(entry["original_position"], [300, 400])
        shelf.change("toggle")
        self.assertFalse(shelf.status()["entries"][0]["stashed"])

    def test_only_floating_window_can_toggle_back_without_tiling_focus(self):
        real_action = self.action
        with patch.object(shelf, "niri_action", side_effect=lambda name, **kw: None if name == "FocusTiling" else real_action(name, **kw)):
            shelf.change("toggle")
            self.assertTrue(self.windows[0]["is_focused"])
            self.assertTrue(shelf.status()["entries"][0]["stashed"])
            shelf.change("toggle")
            self.assertFalse(shelf.status()["entries"][0]["stashed"])

    def test_picker_remembers_app_when_panel_owns_keyboard(self):
        self.windows[0]["focus_timestamp"]["secs"] = 30
        for w in self.windows:
            w["is_focused"] = False
        focus = shelf.picker_focus()
        self.assertEqual(focus["token"], shelf.status()["entries"][0]["token"])
        shelf.change("picker-toggle", "1", focus["token"])
        self.assertTrue(shelf.status()["entries"][0]["stashed"])
        self.assertFalse(shelf.status()["entries"][1]["stashed"])

    def test_picker_new_app_focus_while_open_wins(self):
        self.windows[0]["focus_timestamp"]["secs"] = 30
        prior = shelf.picker_focus()
        self.windows[0]["is_focused"] = False
        self.windows[1]["focus_timestamp"]["secs"] = 40
        current = shelf.picker_focus(prior)
        self.assertNotEqual(current["token"], prior["token"])
        self.assertEqual(current["token"], shelf.status()["entries"][1]["token"])

    def test_picker_focus_change_to_tiling_invalidates_old_float(self):
        self.windows[0]["focus_timestamp"]["secs"] = 30
        prior = shelf.picker_focus()
        self.windows.append(window(3, floating=False))
        self.windows[-1]["focus_timestamp"]["secs"] = 40
        self.assertIsNone(shelf.picker_focus(prior)["token"])

    def test_picker_closed_focus_does_not_fall_back_to_old_survivor(self):
        self.windows[0]["focus_timestamp"]["secs"] = 30
        prior = shelf.picker_focus()
        self.windows.pop(0)
        self.assertEqual(shelf.picker_focus(prior), prior)
        shelf.change("picker-toggle", "1", prior["token"])
        self.assertFalse(self.actions)

    def test_picker_prior_from_another_session_is_rejected(self):
        self.windows[0]["focus_timestamp"]["secs"] = 30
        prior = shelf.picker_focus()
        prior.update(session="old-session", stamp=[999999, 0], token="stale")
        self.assertNotEqual(shelf.picker_focus(prior)["token"], "stale")

    def test_picker_toggle_stashed_window_restores_not_noop(self):
        original = shelf.status()["entries"][0]
        shelf.change("picker-toggle", "1", original["token"])
        self.assertTrue(shelf.status()["entries"][0]["stashed"])
        shelf.change("picker-toggle", "1", original["token"])
        self.assertFalse(shelf.status()["entries"][0]["stashed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
