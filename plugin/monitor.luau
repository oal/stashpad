--!nonstrict
local helper = "/var/home/olav/Projects/stashpad/shelf.py"

noctalia.runStream("exec /usr/bin/python3 " .. helper .. " watch", function(line)
    local value = noctalia.json.decode(line)
    if value then
        noctalia.state.set("shelf", value)
    end
end)

function onIpc(event, _payload)
    if event == "inspect" then
        noctalia.writeFile("../debug-state.json", noctalia.json.encode({
            snapshot = noctalia.state.get("shelf"),
            panel = noctalia.state.get("shelf.ui"),
            capturedAtMs = noctalia.nowMs(),
        }, true))
    end
end

-- runStream teardown / parent-death SIGTERM makes the owning watcher restore
-- all parked windows and clear capture files in its finally block. Do not also
-- launch an untracked late restore that could race a replacement watcher.
-- The replacement waits briefly for watch.lock before binding its wake socket.
