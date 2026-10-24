#!/usr/bin/python3
"""Capture one complete Niri window through Mutter ScreenCast, never clipboard.

Uses already-installed Fedora PyGObject/GStreamer. Always stops the short-lived
cast, including timeout/failure. Nothing is installed on the immutable host.
"""
import os
import json
from pathlib import Path
import signal
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst

DEST = "org.gnome.Mutter.ScreenCast"
ROOT = "/org/gnome/Mutter/ScreenCast"


def capture(window_id, path, cancelled=None):
    os.umask(0o077)
    path = Path(path).resolve()
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    session = None
    pipeline = None
    subscription = None
    began = time.perf_counter()
    timings = {}

    def check():
        if cancelled and cancelled():
            raise InterruptedError("Preview superseded")

    def mark(name):
        timings[name] = round((time.perf_counter() - began) * 1000, 2)

    def call(object_path, interface, method, args=None):
        return connection.call_sync(DEST, object_path, interface, method,
                                    args, None, Gio.DBusCallFlags.NONE, 3000, None)

    def interrupted(_signal, _frame):
        raise InterruptedError("Capture interrupted")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        check()
        session = call(ROOT, DEST, "CreateSession", GLib.Variant("(a{sv})", ({},))).unpack()[0]
        if str(window_id).startswith("monitor:"):
            stream = call(session, DEST + ".Session", "RecordMonitor", GLib.Variant("(sa{sv})", (
                str(window_id).split(":", 1)[1], {"cursor-mode": GLib.Variant("u", 0)},
            ))).unpack()[0]
        else:
            stream = call(session, DEST + ".Session", "RecordWindow", GLib.Variant("(a{sv})", ({
                "window-id": GLib.Variant("t", int(window_id)),
                "cursor-mode": GLib.Variant("u", 0),
            },))).unpack()[0]
        node = []
        subscription = connection.signal_subscribe(
            DEST, DEST + ".Stream", "PipeWireStreamAdded", stream, None,
            Gio.DBusSignalFlags.NONE,
            lambda _c, _s, _p, _i, _m, params: node.append(params.unpack()[0]))
        call(session, DEST + ".Session", "Start")
        mark("session_started_ms")
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not node and time.monotonic() < deadline:
            check()
            context.iteration(False)
            time.sleep(0.005)
        if not node:
            raise RuntimeError("Niri did not provide a window stream within 5 seconds")
        mark("node_ready_ms")
        check()
        Gst.init(None)
        mark("gstreamer_initialized_ms")
        pipeline = Gst.parse_launch(
            f"pipewiresrc path={node[0]} do-timestamp=true ! glupload ! "
            "glcolorconvert ! gldownload ! videoconvert ! "
            "pngenc snapshot=true ! filesink name=target")
        pipeline.get_by_name("target").set_property("location", str(path))
        pipeline.set_state(Gst.State.PLAYING)
        mark("pipeline_playing_ms")
        deadline = time.monotonic() + 6
        message = None
        while message is None and time.monotonic() < deadline:
            check()
            message = pipeline.get_bus().timed_pop_filtered(
                20 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if message is None:
            raise RuntimeError("Window capture timed out")
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            raise RuntimeError(f"{error}: {debug}")
        if not path.exists() or path.stat().st_size < 100:
            raise RuntimeError("No image was produced")
        mark("frame_written_ms")
        check()
        print(path)
    finally:
        mark("teardown_started_ms")
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if subscription is not None:
            connection.signal_unsubscribe(subscription)
        if session is not None:
            call(session, DEST + ".Session", "Stop")
        mark("session_stopped_ms")
        if os.environ.get("SHELF_PROFILE"):
            print(json.dumps({"capture_profile": timings}), file=sys.stderr, flush=True)


if __name__ == "__main__":
    capture(sys.argv[1], sys.argv[2])
