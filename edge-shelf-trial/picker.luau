--!nonstrict
local helper = "/var/home/olav/Projects/stashpad/shelf.py"
local snapshot = noctalia.state.get("shelf") or {}
local opened = false
local query = ""
local selected = 1
local results = {}
local openRevision = 0
local focusContext = nil
local pendingSelection = nil
local busy = false
local icons = {}

local function appIcon(entry)
    if icons[entry.app_id] == nil then
        icons[entry.app_id] = noctalia.appIconPath(entry.app_id, 32) or false
    end
    if icons[entry.app_id] then
        return ui.image({ path = icons[entry.app_id], width = 20, height = 20, fit = "contain" })
    end
    return ui.glyph({ name = "app-window", size = 16, color = "on_surface" })
end

local function thumb(entry)
    if entry.image then
        return ui.image({ path = entry.image, width = 72, height = 40, fit = "cover", radius = 6 })
    end
    if entry.stashed then
        return ui.column({ width = 72, height = 40, align = "center", justify = "center",
            fill = "surface_variant", radius = 6 }, {
            ui.glyph({ name = entry.preview_state == "unavailable" and "photo-off" or "photo",
                size = 14, color = "on_surface_variant" }),
        })
    end
    return nil
end

local function dispatch(entry)
    local revision = openRevision
    -- Resolve while the picker STILL owns keyboard focus. Closing it first
    -- would refocus an app and could turn a summon into an unintended stash.
    noctalia.runAsync({ "/usr/bin/python3", helper, "picker-focus", noctalia.json.encode(focusContext) }, function(result)
        if not opened or revision ~= openRevision then return end
        local focus = result.exitCode == 0 and noctalia.json.decode(result.stdout) or nil
        panel.close()
        if not focus then
            noctalia.notifyError("Floating shelf", "Could not check window focus. Try the picker again.")
            return
        end
        local action = focus.token == entry.token and "picker-toggle" or "summon"
        noctalia.runAsync({ "/usr/bin/python3", helper, action, tostring(entry.id), entry.token }, function(actionResult)
            if actionResult.exitCode ~= 0 then noctalia.notifyError("Floating shelf", "Could not activate window. Try Super+S.") end
        end)
    end)
end

local function activate(entry)
    if not opened or not entry or busy then return end
    busy = true
    if focusContext then dispatch(entry) else pendingSelection = entry end
end

local function render()
    if not opened then return end
    results = {}
    for _, entry in ipairs(snapshot.entries or {}) do
        local state = entry.stashed and "stashed" or "visible"
        local haystack = string.lower(tostring(entry.slot) .. " " .. entry.app_id .. " " .. entry.title .. " " .. state)
        local needle = string.lower(query):match("^%s*(.-)%s*$")
        local matches = string.find(haystack, needle, 1, true) ~= nil
        if matches then table.insert(results, entry) end
    end
    -- Most recently viewed first; slot order breaks ties (stable list).
    table.sort(results, function(a, b)
        local ua, ub = a.last_used or 0, b.last_used or 0
        if ua ~= ub then return ua > ub end
        return (a.slot or 0) < (b.slot or 0)
    end)
    selected = math.clamp(selected, 1, math.max(1, #results))
    local rows = {}
    for index, entry in ipairs(results) do
        local children = {
            appIcon(entry),
            ui.column({ gap = 4, flexGrow = 1 }, {
                ui.label({ text = entry.title, fontWeight = "semibold", maxLines = 1 }),
                ui.label({ text = entry.app_id, color = "on_surface_variant", fontSize = 12, maxLines = 1 }),
            }),
        }
        if not entry.stashed then
            table.insert(children, ui.glyph({ name = "eye", size = 16, color = "on_surface_variant" }))
        end
        local picture = thumb(entry)
        if picture then table.insert(children, picture) end
        table.insert(rows, ui.row({ key = entry.token, paddingH = 12, paddingV = 8, gap = 12,
            align = "center", radius = 8, fill = index == selected and "surface_variant" or "surface",
            onClick = function() activate(entry) end,
            tooltip = entry.stashed and "Stashed — click to summon" or "Visible — click to stash",
        }, children))
    end
    if #rows == 0 then
        table.insert(rows, ui.label({ text = #((snapshot or {}).entries or {}) == 0
            and "No floating windows. Float a window with Super+V to add it."
            or "No matching windows. Try an app name, title, number or state.", maxLines = 3, color = "on_surface_variant" }))
    end
    panel.render(ui.column({ gap = 12, flexGrow = 1 }, {
        ui.row({ align = "center" }, {
            ui.label({ text = "Floating windows", fontSize = 18, fontWeight = "bold", flexGrow = 1 }),
            ui.button({ glyph = "x", variant = "ghost", tooltip = "Close · Esc", onClick = function() panel.close() end }),
        }),
        ui.input({ key = "search-" .. tostring(openRevision), value = query, focus = true,
            placeholder = "Search app, title or state…",
            onChange = function(text) query = text; selected = 1; render() end,
            onSubmit = function() activate(results[selected]) end }),
        ui.scroll({ key = "results", height = 0, flexGrow = 1, gap = 4,
            revealKey = results[selected] and results[selected].token or "" }, rows),
        ui.label({ text = "↑ ↓ select · Enter toggle · Esc close",
            fontSize = 12, color = "on_surface_variant", maxLines = 2 }),
    }))
end

noctalia.state.watch("shelf", function(value)
    if value and snapshot.generation and value.generation and value.generation < snapshot.generation then return end
    snapshot = value or {}
    if opened then render() end
end)

function onOpen(_context)
    opened = true
    query = ""
    selected = 1
    openRevision += 1
    focusContext = nil
    pendingSelection = nil
    busy = false
    snapshot = noctalia.state.get("shelf") or {}
    render()
    local revision = openRevision
    -- Niri retains each app's focus timestamp when layer-shell takes focus.
    noctalia.runAsync({ "/usr/bin/python3", helper, "picker-focus" }, function(result)
        if not opened or revision ~= openRevision then return end
        focusContext = result.exitCode == 0 and noctalia.json.decode(result.stdout) or nil
        if not focusContext then
            panel.close()
            noctalia.notifyError("Floating shelf", "Could not remember window focus. Try the picker again.")
        elseif pendingSelection then
            local entry = pendingSelection
            pendingSelection = nil
            dispatch(entry)
        end
    end)
end

function onClose() opened = false; pendingSelection = nil end

function onKey(chord, pressed)
    if not pressed then return end
    if chord == "Down" then selected = math.min(#results, selected + 1)
    elseif chord == "Up" then selected = math.max(1, selected - 1) end
    render()
end

function onIpc(event, payload)
    if event == "query" then query = payload or ""; selected = 1; render()
    elseif event == "key" then onKey(payload, true)
    elseif event == "submit" then activate(results[selected])
    elseif event == "inspect" then
        noctalia.writeFile("../picker-debug.json", noctalia.json.encode({ open = opened, query = query,
            selected = selected, results = results, focus = focusContext, busy = busy }, true))
    end
end
