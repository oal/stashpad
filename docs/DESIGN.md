# Design notes

The decisions behind stashpad, in the order they were discovered. All statements
verified against niri 26.04 + Noctalia 5.3.0.

## Dead ends, and why

- **Sidebar rail panel with a niri strut** (v1): a Noctalia panel showing cards,
  plus `layout { struts { left 152 } }` in niri config to keep windows clear of
  it. Works, costs a 152 px full-height band of screen. Rejected for space.
- **Corner-parking stashes in place**: niri's `MoveFloatingWindow` clamps so
  ~39 px of any mapped window stays on-screen, and floating windows render
  *above* tiling — parked windows leave a floating dot over your work.
  Rejected visually; this clamp is also why "hide by shrinking" fails:
  `SetWindowWidth/Height` clamps to the app's minimum size hint.
- **Noctalia desktop widget**: renders on the bottom layer (under windows) with
  `exclusiveZone = -1` (no strut) — wrong z-order for a shelf you click.
- **Panel chrome**: Noctalia 5.3 panels are one host-decorated card
  (`Panel::hasDecoration()` is never overridden for plugin panels); the bar is
  chromeless because bar widgets self-draw pills on a transparent surface.
  The 5.4 `[[layer]]` API (plugin API 36) is the way to a chromeless floating
  widget — future option, not available at build time.
- **Opacity fade while stashing**: niri has no opacity animation at all.
  `window-rule { opacity … }` + `toggle-window-rule-opacity --id` exist but the
  flip is instant (verified frame-identical). Approximating a fade needs a
  dynamically rewritten rule block; not worth it — the slide does the work.
- **Minimize / hidden workspace**: not in niri 26.04 (hidden workspaces are
  draft PR niri-wm/niri#2997); `set-window-variable` is rolling-main only.

## The workspace-shelf trick

`stash()` writes a small marked block into niri `config.kdl`

```kdl
// BEGIN floating-shelf workspace (owned by the backend)
workspace "shelf"
// END
```

(validated with a throwaway config before writing; niri hot-reloads), moves the
window there, and removes the block when the last stash leaves — dynamic
workspaces vanish when empty, so the shelf is *born with the first stash and
dies with the last*. Costs: while stashes exist the named workspace sorts
before dynamic ones and is traversable (overview, Mod+Page_Up/Down). The block
write + `MoveWindowToWorkspace` is the only config mutation the tool performs.

`MoveWindowToWorkspace` with a reference that doesn't exist silently no-ops —
the workspace must be live before the move.

## Slide choreography

Workspace teleports are instant, but niri *animates* `MoveFloatingWindow`
moves (the `window-movement` spring). So:

1. stash: slide to the top edge (spring), wait `EDGE_SETTLE` (~0.3 s — the
   critically-damped spring is ~98 % done; the teleport swallows the rest),
   then teleport to the shelf workspace.
2. summon: teleport back (floating coordinates are carried), window lands at
   the parked edge position, then the final delta-move animates a slide-down.

Direction is vertical so stashes emerge from the bar where the pills live, and
niri's clamp sliver (~39 px) hides behind the 44 px bar — windows visually
appear from / disappear into the bar.

## Thumbnails

Captures use niri's `org.gnome.Mutter.ScreenCast` D-Bus API with
`RecordWindow` — window-targeted, no full-screen recording, survives occlusion.
Each stash bumps a generation; a preview worker (spawned on demand, flock'd to
one instance) renders the PNG. Quick start/stop casts trip Noctalia's privacy
toast; suppress for the caster process only:

```toml
[shell.privacy]
screen_filter_regex = "python3"
```

(the toast is an internal shell surface — it never appears on the
notifications bus or in notification history; debug the cast side with
`journalctl --user | grep -i StartCast` and `niri msg -j casts`.)

## Plugin ↔ backend contract

The Luau side never talks to niri. The service (`monitor.luau`) streams
`shelf.py watch`; the backend pushes a JSON snapshot to
`noctalia.state.set("shelf")`; bar + picker render that state and call back
via `shelf.py picker-toggle <id> <token>` with per-entry tokens that reject
stale clicks. Watcher teardown restores stashes — restarting the plugin
un-parks everything on purpose (no lost windows on crash/logout).
