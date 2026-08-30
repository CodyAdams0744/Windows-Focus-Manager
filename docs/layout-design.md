# Layout design notes

Status: band engine + schedule presets IMPLEMENTED (June 2026): strip, quadrant,
triptych, fgrid, auto mode with debounced count tiers, flips, focus-bias cap, tile gap,
and the full-width Layouts settings tile. `grid`/`master` removed (configs migrate:
grid → fgrid, master → strip). Remaining presets (accordion, sidebar, wings, cockpit)
and the v2 parking lot are still design-phase.

## Why grid and master failed

User testing of the first two 2D modes surfaced three problems:

1. **Too much window movement / unpredictable placement.** Cell and slot assignment
   re-sorted on focus change, so windows migrated to different screen regions.
2. **Windows got too small.** Unclamped weight math collapsed non-focused windows.
3. **Animation jank.** The gap-free pre-snap is undefined when layout structure changes
   between focus states, so these modes shipped without it.

## The hover-stability invariant

The core finding of the brainstorm. It is a hard filter for every layout idea:

> When the window under the cursor receives focus, that same window must still be
> under the cursor after retiling.

Reason: in hover mode, if focusing a window *relocates* it (to a master pane, a stage,
a center column), the cursor ends up over whatever window took its place. That window
then receives hover focus, swaps too, and the layout ping-pongs forever.

Consequences:

- Focus may only **stretch** the focused window in place. Windows never relocate on focus.
- Every window needs a fixed **anchor** (band + position). Focus moves shared boundaries only.
- Layouts that relocate on focus are rejected outright: swap-in-place, stage-with-shelf,
  center master, wide top, and the shipped `master` mode (which is why it felt wrong in
  hover mode).
- Anchored layouts are also exactly the ones where the gap-free pre-snap works, because
  the boundary structure is measurable from current rects. This is not a coincidence:
  stationary anchors + moving boundaries is what makes both properties hold.

Click/alt-tab mode does not strictly need the invariant, but adopting it globally avoids
shipping layouts that break the moment the user toggles hover mode.

## The band engine

All shortlisted layouts are presets of one engine. Build the engine once; each layout is
a configuration, not new code.

Concepts:

- **Band**: a vertical column or horizontal row of the monitor work area. A layout is an
  ordered list of bands along one axis; each band stacks its windows along the other axis.
- **Assignment**: windows are assigned to bands positionally (by original-rect center,
  nearest band) and the assignment is **sticky**. Dragging a window to another region and
  refocusing reassigns it, which doubles as the user's arrangement gesture.
- **Capacity**: a band may cap its window count (sidebar main pane = 1). Overflow goes to
  the next band.
- **Weights**: each band and each cell has a resting weight (default 1). The focused
  window's band and cell get a focus bias derived from `expand_ratio`, clamped.
  Boundaries are computed from weights, so everything stays gap-free.
- **Floors**: optional minimum share per band/cell (e.g. editor pane never below 34%,
  agent band never below 26%). Structural fix for "windows got too small".
- **Slivers**: a cell class that collapses to a fixed thin strip (title-bar height) when
  not focused, and expands in place when hovered/focused. Accordion and drawer behavior.
- **Fill order (large-cell side)**: when a window count doesn't divide evenly across
  bands, some band holds fewer windows and therefore the bigger cell(s). Which side that
  band sits on is a per-preset (eventually per-user) setting. User preference recorded:
  large window on the LEFT for the 3-window (quadrants) and 5-window (triptych) shapes.
- **Pre-snap**: because anchors never reassign on focus, the current boundary positions
  are measurable from current rects, and the strip-style gap-free animation guarantee
  carries over to every preset.

Shadow-inset compensation (the `_compensate` helper) applies per emitted rect, both axes.

## Shortlisted presets

| Preset | Bands | Notes |
|---|---|---|
| Strip (default) | 1 row of full-height columns (portrait: rows) | Existing behavior, unchanged |
| Quadrants | 2 columns, ≤2 windows each, extras stack in-corner | Column variant: unfocused column keeps its split; no window shrinks in both axes |
| Sidebar | Col 1 (capacity 1) + stack col | Positional master; best 3-5 windows |
| Fixed-cell grid | 2 rows of columns, sticky cells, clamped bias | The grid mode redone correctly; best 6-8 |
| Triptych | 3 columns with stacks | Quadrants scaled for ultrawide / 6+ |
| Wings | Side stack + center col (resting weight ~2.1, capacity 1) + side stack | Hover-stable center master; center is premium real estate, not premium occupant |
| Accordion | 1 row; non-focused cells are slivers | Max focused size, all windows visible/clickable |
| Cockpit | Main col (floor 34%, capacity 1) + stack col where cells 1-2 are full, rest slivers, soft bias | Coding preset: editor + preview/agent + utility drawer |

Rejected (violate hover-stability): swap-in-place, stage with shelf, center master,
wide top, dwindle was acceptable but uninteresting to the user, magnify strip and
overlay grid evaluated but not shortlisted.

## Layout schedule (count-dependent layouts)

Different presets depending on how many windows are on the monitor. Per monitor, the
engine picks the preset from a count table.

Recommended default schedule (general use):

| Window count | Preset | Rationale |
|---|---|---|
| 1 | strip (full screen) | Trivial case |
| 2 | strip | Two clean columns, nothing beats it |
| 3-4 | quadrants | Corner anchors at their best; only two boundary lines move. At 3: full-height window on the left, stacked pair on the right (user preference) |
| 5-6 | triptych | Third column absorbs the crowd; no window below ~1/6 screen. At 5: full-height window leads on the left (user preference) |
| 7-8 | fixed-cell grid | Two anchored rows; the only shape keeping everyone usable. At 7: roomier 3-cell row on the bottom (default, flippable) |

Coding profile alternative: same table but 5+ → cockpit, so utilities collapse to
slivers instead of consuming cells.

Design notes:

- **Safe under the invariant.** Hover-stability constrains focus-driven changes only.
  Schedule switches are triggered by windows opening/closing, which are discrete events
  where movement is expected, and the cursor is not in a feedback loop.
- **Continuity is free.** All presets assign bands positionally, so a window in the left
  region stays in the left region across a preset switch. Boundaries reshuffle, homes don't.
- **Debounce required.** Short-lived windows (dialogs, splash screens) must not thrash the
  schedule. Only switch presets after the manageable-window count has been stable for
  ~1 second. The existing manageability filters catch most of this already.
- **UI**: three dropdowns ("1-2 windows", "3-4", "5+"), default "same for all counts".
- **Config sketch**:

```json
"layout_schedule": [
  { "max_windows": 2, "mode": "strip" },
  { "max_windows": 4, "mode": "quadrant" },
  { "mode": "cockpit" }
]
```

`layout_mode` (single value) remains supported as the simple case; a schedule overrides it.

## Implementation plan (when building)

1. Band engine in `window_manager.py`: band assignment (sticky map hwnd → band/slot),
   weight resolution (resting x focus bias, floors, slivers), rect emission with
   `_compensate`, generalized pre-snap from measured boundaries.
2. Preset table: each shortlisted layout as a declarative band spec.
3. Dispatcher: `_layout_focus_targets` picks preset via `layout_mode` / `layout_schedule`
   + per-monitor window count (with debounce).
4. Settings UI: preset picker (replacing the current Columns/Grid/Master buttons) and the
   three schedule dropdowns.
5. Remove `grid` and `master` modes after the engine ships (config values migrate:
   `grid` → fixed-cell grid preset, `master` → sidebar).
6. Test priority: hover mode with 3-8 windows per preset, schedule switching while
   hovering, portrait monitor transposition, pre-snap gap-freeness.

## v2 parking lot

- **Role pinning by exe**: apps remember their band/slot by executable name
  (`code.exe` → main pane, `spotify.exe` → sliver). Reuses the existing exe-matching
  infrastructure from exclusions. Deferred by user decision ("maybe later").
- **Resting weights as a user setting**: per-band asymmetry (staircase strip, bigger
  center) exposed in the UI.
- ~~**Per-monitor layout mode**~~ IMPLEMENTED (June 2026): `monitor_overrides` config
  (layout_mode + expand_ratio per monitor, identified by monitor-rect left/top),
  edited from the Monitors tile. Remaining per-monitor ideas (tiers, flips, gap per
  monitor) stay parked.
- **Draggable boundaries**: drag a shared tile edge to retune that monitor's ratio,
  persisted.
- **Hover boundary guard**: ignore hover focus while the cursor is within a few pixels
  of a moving boundary (mainly relevant if magnify-style falloff is ever revisited).

## Decision log

- Grid + master implemented, tested, disliked (movement, size collapse, jank).
- Quadrant concept introduced by user ("4 apps in 4 corners"); column variant chosen
  over pure cross (avoids double-axis shrink of the diagonal window); in-corner
  stacking chosen for overflow.
- Hover feedback loop identified by user; formalized as the hover-stability invariant.
- Shortlist assembled across three brainstorm rounds (see preset table).
- Coding preset: Cockpit chosen over Flow.
- Role pinning deferred to v2.
- Count-dependent layout schedule endorsed; design above.
