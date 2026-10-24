--!nonstrict
-- Top-bar shelf: renders the live "shelf" state (pills with app icon +
-- cached window thumbnail) and toggles a window on click:
-- stashed -> summon (slide-down), visible -> stash (slide-up).
local helper = "/var/home/olav/Projects/stashpad/shelf.py"
local snapshot = noctalia.state.get("shelf") or {}
local busy = {}
local icons = {}

local function activate(entry)
    if busy[entry.token] then return end
    busy[entry.token] = true
    noctalia.runAsync({ "/usr/bin/python3", helper, "picker-toggle", tostring(entry.id), entry.token }, function(result)
        busy[entry.token] = nil
        if result.exitCode ~= 0 then
            noctalia.notifyError("Floating shelf", "Window action failed. Super+S can retry.")
        end
    end)
end

local function appIcon(entry)
    if icons[entry.app_id] == nil then
        icons[entry.app_id] = noctalia.appIconPath(entry.app_id, 32) or false
    end
    if icons[entry.app_id] then
        return ui.image({ path = icons[entry.app_id], width = 18, height = 18, fit = "contain" })
    end
    return ui.glyph({ name = "app-window", size = 14, color = "on_surface" })
end

local function render()
    local entries = snapshot.entries or {}
    if #entries == 0 then
        barWidget.render(ui.glyph({ name = "archive", size = 16, color = "on_surface_variant" }))
        barWidget.setTooltip("Floating shelf: no windows parked. Super+V floats, Super+S stashes the focused window.")
        return
    end
    local children = {}
    for _, entry in ipairs(entries) do
        local thumb
        if entry.image then
            thumb = ui.image({ path = entry.image, width = 48, height = 24, fit = "cover", radius = 6 })
        else
            thumb = ui.column({ width = 48, height = 24, align = "center", justify = "center",
                fill = "surface", radius = 6 }, {
                ui.glyph({ name = entry.preview_state == "unavailable" and "photo-off" or "photo",
                    size = 13, color = "on_surface_variant" }),
            })
        end
        table.insert(children, ui.row({ key = "bar-" .. entry.token, gap = 6, align = "center",
            paddingH = 6, paddingV = 3, radius = 10, fill = "surface_variant",
            border = entry.focused and "primary" or "outline/0.35", borderWidth = 1,
            onClick = function() activate(entry) end,
            tooltip = entry.app_id .. "\n" .. entry.title
                .. "\n" .. (entry.stashed and "Stashed" or "Visible") .. " · click to toggle",
        }, {
            appIcon(entry),
            thumb,
        }))
    end
    barWidget.render(ui.row({ gap = 6, align = "center" }, children))
    barWidget.setTooltip("Floating shelf: click a card to stash/summon · Super+Shift+S picker")
end

noctalia.state.watch("shelf", function(value)
    if value and snapshot.generation and value.generation and value.generation < snapshot.generation then return end
    snapshot = value or {}
    render()
end)

function update()
    snapshot = noctalia.state.get("shelf") or snapshot
    render()
end
