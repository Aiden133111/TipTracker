# TipTracker Library Manual

This document describes the public behavior of **`TipTracker.py`**: the `TipTracker` class for Opentrons Flex protocols (**library version 3.0**, see `TipTracker.metadata['Version']`). It is written to match the implementation as closely as possible; when in doubt, the source docstrings in `TipTracker.py` are authoritative.

---

## Table of contents

1. [Overview](#1-overview)
2. [Requirements](#2-requirements)
3. [Integrating the library](#3-integrating-the-library)
4. [Concepts and data model](#4-concepts-and-data-model)
5. [Typical setup order](#5-typical-setup-order)
6. [Constructor](#6-constructor)
7. [Configuration properties](#7-configuration-properties-you-may-set)
8. [Deck and slot management](#8-deck-and-slot-management)
9. [Loading and assigning tip racks](#9-loading-and-assigning-tip-racks)
10. [Primary runtime API: pick up and drop](#10-primary-runtime-api-pick-up-and-drop)
11. [Refill, reload, and operator workflows](#11-refill-reload-and-operator-workflows)
12. [Clearing, wasting, and carouseling](#12-clearing-wasting-and-carouseling)
13. [Stackers](#13-stackers)
14. [Expansion deck (A4, B4, C4, D4)](#14-expansion-deck-a4-b4-c4-d4)
15. [Flex 96-channel and tip rack adapters](#15-flex-96-channel-and-tip-rack-adapters)
16. [Counters and diagnostics](#16-counters-and-diagnostics)
17. [Errors and process exit](#17-errors-and-process-exit)
18. [Related files](#18-related-files)
19. [Usage examples (cookbook 19.1–19.50)](#19-usage-examples)

---

## 1. Overview

TipTracker exists because high-throughput Flex layouts often store tips **off the main pipetting grid** (expansion row, stackers) and need the **gripper** to shuttle racks. The Opentrons API does not automatically chain “out of tips → move a full rack here → discard empty rack” for you. TipTracker centralizes that logic.

**Refill priority (conceptual summary from the class docstring):**

1. Tips in normal assigned deck slots.
2. If `pick_up_slots` is set for that rack type, shuffle so pickup can occur from the forced slot.
3. Shuttle from **expansion** slots onto the deck (or carousel) when configured.
4. Pull from **stackers** when inventory remains.
5. **Manual** refills: pause and ask the operator to place racks, up to optional per-type **max rack** limits.

The class also documents **proactive** entry points (`refill_deck`, `reload_*_tipracks`, stacker refills, etc.) for pausing before a run segment instead of waiting for `pick_up` to exhaust tips.

---

## 2. Requirements

- **Robot:** Opentrons Flex (`requirements["robotType"]` in example protocols: `"Flex"`).
- **API level:** The library file is maintained against a current Flex API (e.g. **2.27** in the module header); align your protocol `requirements` with your robot software.
- **Imports used internally:** `protocol_api`, `OutOfTipsError`, nozzle layout constants (`ALL`, `COLUMN`, `SINGLE`, `ROW`, `PARTIAL_COLUMN`), `NozzleConfigurationType`.

TipTracker is **not** validated or endorsed by Opentrons. Test with `opentrons_simulate` and on hardware before production use.

---

## 3. Integrating the library

**Options:**

1. **Import** `TipTracker` from `TipTracker.py` if your upload path supports it (e.g. local simulate with `PYTHONPATH` or a single combined file on the robot).
2. **Copy-paste** the class into your protocol file (common on-instrument). In that case, fatal errors can print a **call stack** to stderr when `verbose_tracebacks=True` (default), to help locate your protocol line.

The test harness `tiptrack_testing.py` embeds a full copy of the class for self-contained simulation.

---

## 4. Concepts and data model

### 4.1 API load names

Almost every method identifies a tip type by the **labware API load name** string (e.g. `opentrons_flex_96_filtertiprack_50ul`). Use the exact names your labware definitions use.

In the **source code**, that string is consistently named **`tip_load_name`** in locals and in most public method parameters (replacing older names like `rackname` or generic `name` in helpers). Batch loading still uses **`tiprack1` … `tiprack4`** / **`slots1` … `slots4`**. **`assign_tipracks`** keeps the first parameter **`rack_name`** for compatibility with existing keyword calls (`rack_name=TIPS_50`). After resolving a pipette selector to an `InstrumentContext`, the code uses **`resolved_pipette`** in `assign_tipracks` and operator-refill paths.

### 4.2 Main dictionaries (mostly read-only for callers)

| Attribute | Role |
|-----------|------|
| `tipracks` | Map: load name → list of **Labware** on the **main deck** (not adapters, not expansion tracking lists in the same way). |
| `ex_racks` | Map: load name → list of racks on **registered expansion** slots that the tracker is following. |
| `rack_assignments` | Map: load name → list of **slot IDs** where that type should be **refilled** and checked. Updated via `assign_slots` / `add_starting_tipracks`, not by arbitrary direct edits. |
| `adapter_pickup_tipracks` | Map: load name → list of **Labware** used for **96-channel adapter** pickup paths. May contain internal placeholders during adapter reassignment flows. |
| `tiprack_adapters` | Map: slot string → `[current_rack_load_name, adapter_labware]` for Flex **96 tip rack adapter** on that slot. |
| `stackers` | Map: rack load name → list of `[FlexStackerContext, count, lid_flag, ...]` internal rows (see stacker section). |
| `empty_ex_slots` | Tracks expansion slots that became empty for prioritization on refill. |
| `storing_stackers` | Optional stackers used to **store empty** racks during carousel flows. |

### 4.3 Counters

- `tip_counts`: cumulative tips consumed **by type** (uses active nozzle count when incrementing).
- `tip_rack_counts`: how many racks have been **loaded** per type (including stacker loads where applicable).
- `pick_up_count` / `drop_count`: per-`InstrumentContext` call counts.

### 4.4 Pipette tip type binding

- `pipette_1_tip_type` / `pipette_2_tip_type`: strings set by `assign_tipracks` for whichever pipette was configured.
- `active_pipette`: optional default for `pick_up` / `drop_tip` when you omit the pipette argument.

---

## 5. Typical setup order

1. Create `ProtocolContext`, load **pipette(s)** and **waste** (`WasteChute` or `TrashBin`).
2. `TipTracker(ctx, pipette1, waste_bin, pipette2=..., use_gripper=..., debugging=..., suppress_comments=..., verbose_tracebacks=...)`.
3. If using expansion column: **`add_expansion_slots`** before loading or assigning tips on A4/B4/C4/D4.
4. Optional: **`add_stacker`** for Flex stackers (supply or empty-rack storage).
5. **`add_starting_tipracks`** (recommended) **or** `load_tipracks` + **`assign_slots`**.
6. Optional: set `global_adapter`, `pick_up_slots`, `open_slot`, `ignore_slots`, `max_racks_count` entries, `carousel_tips` / `use_chute` overrides.
7. **`assign_tipracks`** (with `mode=ALL` etc. for 96-channel adapter workflows).
8. During the run: **`pick_up`** / **`drop_tip`**, occasional **`reset_rack_list`** after you move labware outside TipTracker, proactive **`refill_*`** as needed.

Skipping `assign_slots` after only `load_tipracks` can leave **`rack_assignments`** empty and cause failures inside `pick_up` (e.g. `KeyError` on assigned slots).

---

## 6. Constructor

```python
TipTracker(
    ctx: protocol_api.ProtocolContext,
    pipette1: protocol_api.InstrumentContext,
    waste_bin: protocol_api.WasteChute | protocol_api.TrashBin,
    pipette2: protocol_api.InstrumentContext | None = None,
    use_gripper: bool = False,
    debugging: bool = False,
    suppress_comments: bool = False,
    verbose_tracebacks: bool = True,
)
```

- **`ctx`:** protocol context for loads, moves, pauses.
- **`pipette1` / `pipette2`:** fixed for the run; do not swap mid-run.
- **`use_gripper`:** strongly recommended for automatic rack moves.
- **`debugging`:** when True, many branches also `print()` to the terminal/sim log.
- **`suppress_comments`:** when True, `print_comments` is false so fewer `ctx.comment` lines.
- **`verbose_tracebacks`:** on fatal configuration errors, prints an extra interpreter stack to **stderr** (useful when the class is pasted into a large protocol).

**Waste behavior defaults:** If `waste_bin` is a `WasteChute`, `use_chute` is True and `carousel_tips` is False. If it is a `TrashBin`, `use_chute` is False and `carousel_tips` is True (empty racks tend to stay on deck until you carousel or pause).

---

## 7. Configuration properties (you may set)

| Property | Purpose |
|----------|---------|
| `open_slot` | Slot (string) or staging object used as **carousel** buffer when moving racks without the chute, or when stacker shuttle holds non-tip labware. |
| `global_adapter` | When True, a single adapter can be **reused** across tip types; tracker may shuffle racks onto it (see §15). |
| `ignore_slots` | List of slot IDs **not** to waste/refill automatically (e.g. reusable tip racks). |
| `pick_up_slots` | Dict: rack load name → slot where **all** pickups for that type should occur (partial-tip spatial constraints). |
| `max_racks_count` | Dict: rack load name → cap on how many racks to load for that type (refill behavior when nearing cap). |
| `debug` | Mirrors constructor debugging; may be toggled mid-run. |
| `print_comments` | Run-log comments; toggling mirrors `suppress_comments` intent. |
| `use_chute` | Allow chute disposal when True. |
| `carousel_tips` | When True, prefer shuffling empty racks instead of only using the chute. |
| `verbose_tracebacks` | Fatal error stack printing. |

---

## 8. Deck and slot management

### `add_expansion_slots(slots: str | list[str])`

Registers which of **`A4`, `B4`, `C4`, `D4`** are in play. **`load_tipracks`** refuses expansion slots unless this was called first. Dedupes and validates against `EXPANSION_DECK_SLOTS`.

### `assign_slots(tiprack1, slots1, tiprack2=None, slots2=None, tiprack3=None, slots3=None, tiprack4=None, slots4=None, clear_other_slots=False)`

- Declares **reload / tracking** slots for up to **four** tip types at once.
- Validates: no duplicate tip types in one call, no duplicate slot strings across pairs.
- Resolves **slot conflicts** with other rack types by removing conflicting slots from other assignments (may `print` / `comment` a warning).
- If a slot holds a **Flex tip rack adapter**, updates `tiprack_adapters[slot][0]` and may seed `adapter_pickup_tipracks` with a **`REPLACE_ME`** sentinel row until real racks are bound (96-channel replacement flows).

### `reset_rack_list(rack_names: str | list[str] | None)`

Walks `ctx.deck` and rebuilds `tipracks`, `ex_racks`, and `adapter_pickup_tipracks` for the given name(s) (or **all** known types if `None`). Call after **manual** deck edits so lists match reality.

---

## 9. Loading and assigning tip racks

### `add_starting_tipracks(tiprack1, slots1, tiprack2=None, slots2=None, tiprack3=None, slots3=None, tiprack4=None, slots4=None, max_racks_1=None, max_racks_2=None, max_racks_3=None, max_racks_4=None, adapters: list[str] = [])`

- Validates each non-null tiprack/slot pair is complete and that **tip types** and **slots** are unique across the four pairs.
- Sets `max_racks_count` from `max_racks_*` when provided.
- Calls **`load_tipracks`** then **`assign_slots`** with the same arguments.

### `load_tipracks(tiprack1, slots1, tiprack2=None, slots2=None, tiprack3=None, slots3=None, tiprack4=None, slots4=None, adapters: list[str] = [])`

Loads labware for each slot in the four groups:

- **String slot in `adapters` or already in `tiprack_adapters`:** loads or reuses **Flex 96 tip rack adapter**, loads tip rack on adapter, updates `tiprack_adapters`, `adapter_pickup_tipracks`, `tip_rack_counts`, and expansion tracking when applicable. If the adapter **already holds the same load name**, it **reuses** the child labware and still performs **bookkeeping** so internal state matches the deck (no duplicate `load_labware` for the same rack).
- **String slot, normal deck:** `load_labware` or reuse if deck already matches; updates counts and `tipracks` / `ex_racks` as appropriate.
- **Labware adapter object:** loads child rack on that adapter.
- **Labware tip rack object:** treats as existing rack reference path.

Respects **`max_racks_count`** against **`tip_rack_counts`** to skip loading when capped.

### `assign_tipracks(rack_name, pipette=None, mode=None, start=None, end=None)`

- Selects pipette from argument or **`active_pipette`** (must be set if pipette omitted).
- Updates `pipette_1_tip_type` / `pipette_2_tip_type`.
- **`mode`** handling:
  - Partial layouts (`COLUMN`, `SINGLE`, `ROW`, `PARTIAL_COLUMN`): `configure_nozzle_layout` with `tip_racks=self.tipracks[rack_name]`; may trigger manual refill if lists empty.
  - **`ALL`** on **Flex 96-channel:** uses `adapter_pickup_tipracks[rack_name]` when present; if `global_adapter`, may call `assign_slots` to the first adapter slot; otherwise falls back to `tipracks[rack_name]` for `configure_nozzle_layout`.
  - **`mode is None`:** on 96-channel with adapter pickup defined, sets `pip.tip_racks` to adapter list; else deck `tipracks`.

Misconfiguration commonly ends in **`_fatal_tracker_error`** with `KeyError` if `tipracks` / adapter lists are missing for the chosen mode.

---

## 10. Primary runtime API: pick up and drop

### `pick_up(pipette=None, locus=None, refill_all=False, set_active_pipette=False) -> int`

Resolves **active pipette** and **`tip_load_name`** (the current tip type’s API load name) from `pipette_*_tip_type`. Builds lists of **vacant** assigned slots and **empty** racks (including adapter-mounted racks when tracked). Handles the **`REPLACE_ME`** adapter replacement branch when pipette tip rack list uses the internal sentinel format.

Then attempts **`active_pipette.pick_up_tip(locus)`**. On failure, executes a large refill branch: waste empty racks (subject to `_waste_empty_rack_now`), expansion shuttle, stacker `grab_from_stacker`, manual `refill_deck` / `reload_deck_tipracks`, etc., depending on configuration.

**Return codes (int):**

| Code | Meaning (from docstring) |
|------|---------------------------|
| 0 | Normal pickup from existing deck supply. |
| 1 | Had to **carousel** to pick up. |
| 2 | Wasted tip path involving **expansion** shuttle. |
| 3 | Wasted tip path involving **stacker**. |
| 4 | **Manual refill** path started. |

### `drop_tip(pipette=None, locus=None, return_tip=False)`

Increments `drop_count` then calls `return_tip` or `drop_tip` on the instrument. Does not encode refill logic.

### `shuffle_for_forced_pickup(tip_load_name, pick_up_slot, pipette)`

Used when **`pick_up_slots`** forces a location; shuffles racks so the next pickup can occur from that slot.

---

## 11. Refill, reload, and operator workflows

### `refill_tips(tip_load_name, slots, waste_all_old=True)`

Clears **exhausted** racks on relevant slots via **`clear_old`**, then **`load_tipracks`** onto empty slots in the union of cleared + empty positions. Honors **`ignore_slots`**. If `waste_all_old` is True, candidate slots are all `rack_assignments[name]` (minus ignores); if False, only the `slots` list drives clearing candidates.

### `refill_deck` / `refill_main_deck_slots` / `refill_expansion_slots`

**Operator-centric:** calls `refill_tips`, homes, **`ctx.pause`** for the user to place racks, then optionally **`assign_tipracks`** when `pipette` and `reassign_pipette` are set.

- **`refill_deck`:** default slot list is all assigned slots for that type minus `ignore_slots` (can include expansion if assigned).
- **`refill_main_deck_slots`:** excludes registered expansion slots.
- **`refill_expansion_slots`:** only expansion-assigned slots; raises if **`add_expansion_slots`** was never called.

### `reload_deck_tipracks` / `reload_main_deck_tipracks` / `reload_expansion_tipracks`

**Pause + `load_tipracks` only** (no `reset_rack_list` / `assign_tipracks`); intended to be composed by callers such as `pick_up` after internal supply is exhausted.

---

## 12. Clearing, wasting, and carouseling

### `clear_old(tip_load_name, slots_to_clear=None, save_tips=True)`

Removes racks of **`name`** from the deck and updates **`tipracks` / `ex_racks` / `adapter_pickup_tipracks`**.

- `slots_to_clear is None`: clear every **assigned** slot that still has matching deck labware, then reset all internal lists for that type.
- `slots_to_clear` list: partial clear.

**`save_tips` (legacy name):** When **False** and **chute + gripper**, racks go to waste automatically; otherwise a **pause** then **off-deck** moves without gripper for the toss leg.

### `waste_tips(slots: str | list[str] | protocol_api.Labware)`

Moves racks to chute or off-deck. Docstring notes **adapters are not supported in all code paths**; prefer tracker-managed refills for adapter-heavy flows.

### `carousel(tiprack_to_move_away, tiprack_to_move_in)`

Three-step shuffle using **`open_slot`**: move away rack to open slot, move in rack into vacated slot, update `open_slot` to where the incoming rack came from. Accepts labware objects or slot strings; resolves adapter parents. May interact with **`storing_stackers`** for empty-rack stacker storage.

### `replace_tips(old_rack_name, new_rack_name, number_to_replace=None, manually_remove=True)`

Pauses / clears old type and loads new type on the freed slots (see implementation for slot list derivation).

---

## 13. Stackers

### `add_stacker(slot, rackname, initial_count, lid, load_on_shuttle=True, use_for_storing_empty=False)`

Loads **`flexStackerModuleV1`**, registers it under `stackers[rackname]`, and either:

- **`use_for_storing_empty=False`:** calls **`load_tips_in_stacker`** to populate inventory, or
- **`use_for_storing_empty=True`:** marks stacker for **empty rack storage**, forces **`carousel_tips`** and sets **`open_slot`** to the stacker when needed.

Returns the **`FlexStackerContext`**.

### `load_tips_in_stacker(stacker, rackname, quantity, lid=None, load_on_shuttle=True)`

Uses `set_stored_labware` and optional `load_labware` on shuttle; updates internal stacker counts and **`tip_rack_counts`**.

### `move_from_stacker(rackname) -> protocol_api.Labware`

Retrieves next rack from the first stacker row with count > 0; handles lids, wrong labware on shuttle (needs **`open_slot`**), and count decrements.

### `grab_from_stacker(tip_load_name, empty_slots=[])`

Places retrieved racks onto **`empty_slots`**; uses **`carousel`** when `carousel_tips` is True.

### `store_in_stacker(labware, store_stacker, force_store=False)`

Stores labware in a stacker configured for **empty** racks; optional `force_store` bypasses load-name match checks.

### `refill_stacker_supply(tip_load_name, deposit_targets=None)`

Runs **`fill`** on each stacker for that type (operator physically refills module). When **`max_racks_count`** matches consumed racks, may shuttle **last** racks onto `deposit_targets` and clear `call_refill`.

### `reload_stacker_inventory(tip_load_name, quantity, lid=None, load_on_shuttle=True)`

Per stacker: pause, then **`load_tips_in_stacker`** for proactive inventory refresh.

---

## 14. Expansion deck (A4, B4, C4, D4)

- Constant: `TipTracker.EXPANSION_DECK_SLOTS` is a `frozenset` of the four IDs.
- **`add_expansion_slots`** must precede loading tips on those slots.
- **`ex_racks`** and **`empty_ex_slots`** participate in **`pick_up`** refill ordering and in **`reset_rack_list`**.

---

## 15. Flex 96-channel and tip rack adapters

- Use **`adapters=['A1', ...]`** in **`add_starting_tipracks`** / **`load_tipracks`** to load **`opentrons_flex_96_tiprack_adapter`** and child racks.
- **`assign_tipracks(..., mode=ALL)`** on a Flex 96-channel pipette uses **`adapter_pickup_tipracks`** for `configure_nozzle_layout` when that map contains the type.
- **`global_adapter`:** one adapter slot can serve multiple tip types; tracker may move the correct rack onto the adapter before pickup.
- **`assign_slots`** on an adapter slot updates which **logical** tip type owns that adapter for replacement flows.

---

## 16. Counters and diagnostics

- Inspect **`tip_counts`**, **`tip_rack_counts`**, **`pick_up_count`**, **`drop_count`** after simulation to tune **`max_racks_count`**.
- Use **`debugging=True`** for verbose **`print`** output alongside optional **`ctx.comment`** lines when `suppress_comments` is False.

---

## 17. Errors and process exit

- **`_fatal_tracker_error`** logs a banner (and optional stack), prints traceback, then **`exit(1)`**. This terminates the protocol process abruptly by design for unrecoverable configuration errors.
- Validate **`rack_assignments`**, adapter maps, and **`pick_up_slots`** early in development to avoid mid-run exits.

---

## 18. Related files

| File | Purpose |
|------|---------|
| `TipTracker.py` | Canonical library implementation. |
| `tiptrack_testing.py` | Embedded class copy + `run()` harness protocol for simulation. |
| `stacker_example.py` / `stacker_testing880.py` | Additional examples (if present in your checkout). |

---

## 19. Usage examples

Subsections **19.1** through **19.50** form a **cookbook of fifty** small patterns. Combine them for full protocols. Every snippet is **illustrative**: adjust deck slots, load names, and instrument mounts to match your labware definitions and robot layout. The API load names below match the Flex filter tip racks used in `tiptrack_testing.py`; if your protocol uses non-filter definitions, swap the strings but keep them **identical** to what `load_labware` expects.

### 19.1 Shared constants (typical Flex filter racks)

```python
from opentrons import protocol_api
from opentrons.protocol_api import (
    ALL,
    COLUMN,
    ROW,
    SINGLE,
    PARTIAL_COLUMN,
)

TIPS_50 = 'opentrons_flex_96_filtertiprack_50ul'
TIPS_200 = 'opentrons_flex_96_filtertiprack_200ul'
TIPS_1000 = 'opentrons_flex_96_filtertiprack_1000ul'
```

### 19.2 Minimal single-pipette workflow (waste chute + gripper)

Use **`add_starting_tipracks`** so both **`load_tipracks`** and **`assign_slots`** run; then **`assign_tipracks`** before the first **`pick_up`**. Prefer **`use_gripper=True`** when the robot has a gripper so refills and rack moves stay automated.

```python
def run(ctx: protocol_api.ProtocolContext):
    pip = ctx.load_instrument('flex_8channel_50', 'right')
    waste = ctx.load_waste_chute()

    tracker = TipTracker(ctx, pip, waste, use_gripper=True)

    tracker.add_starting_tipracks(TIPS_50, ['C1', 'D1'])
    tracker.active_pipette = pip
    tracker.assign_tipracks(TIPS_50, pipette=pip)

    tracker.pick_up()
    # ... aspirate / dispense ...
    tracker.drop_tip()
```

### 19.3 Switching tip types on the same pipette

Call **`assign_tipracks`** with the **API load name** of the rack you want to use next, then **`pick_up`** / **`drop_tip`** as usual. TipTracker updates `pipette_*_tip_type` so **`pick_up`** resolves the correct supply and refill rules.

```python
tracker.add_starting_tipracks(TIPS_200, ['B1'], TIPS_50, ['C1'])
tracker.active_pipette = pip

tracker.assign_tipracks(TIPS_200, pipette=pip)
tracker.pick_up()
tracker.drop_tip()

tracker.assign_tipracks(TIPS_50, pipette=pip)
tracker.pick_up()
tracker.drop_tip()
```

### 19.4 Two pipettes in one tracker

Pass both instruments to the constructor. Select which head is active with **`active_pipette`** or pass **`pipette=`** explicitly on each call.

```python
p_left = ctx.load_instrument('flex_96channel_1000', 'left')
p_right = ctx.load_instrument('flex_8channel_50', 'right')
waste = ctx.load_waste_chute()

tracker = TipTracker(ctx, p_left, waste, pipette2=p_right, use_gripper=True)

tracker.add_starting_tipracks(
    TIPS_1000, ['A1'],
    TIPS_50, ['C1'],
    adapters=['A1'],  # 96-channel: rack on Flex tip rack adapter
)

tracker.assign_tipracks(TIPS_1000, pipette=p_left, mode=ALL)
tracker.pick_up(pipette=p_left)
tracker.drop_tip(pipette=p_left)

tracker.assign_tipracks(TIPS_50, pipette=p_right)
tracker.pick_up(pipette=p_right)
tracker.drop_tip(pipette=p_right)
```

For Flex **96-channel**, a second consecutive **`pick_up`** in **`mode=ALL`** typically needs **another** full rack on an adapter-mounted position; the harness comment in `tiptrack_testing.py` calls this out. Plan deck or stacker inventory accordingly.

### 19.5 Expansion row (A4–D4) as staged inventory

Call **`add_expansion_slots`** **before** loading or assigning tips on column 4. Then include those slots in **`add_starting_tipracks`** / **`assign_slots`** so refills can shuttle racks from expansion onto the main grid when deck slots empty out.

```python
tracker.add_expansion_slots(['A4', 'B4', 'C4'])
tracker.add_starting_tipracks(
    TIPS_50, ['C1', 'D1', 'A4', 'B4'],  # A4/B4 hold backup racks
)
tracker.assign_tipracks(TIPS_50, pipette=pip)
```

### 19.6 Forcing all pickups from one slot (`pick_up_slots`)

Useful when partial nozzle layouts or geometry require tips to be taken from a **specific** deck position. After assignments and **`assign_tipracks`**, set the map entry to the slot string.

```python
tracker.add_starting_tipracks(TIPS_200, ['B1', 'D1'])
tracker.assign_slots(TIPS_200, ['B1', 'D1'])
tracker.assign_tipracks(TIPS_200, pipette=pip, mode=COLUMN)  # example partial layout

tracker.pick_up_slots[TIPS_200] = 'B1'
tracker.pick_up(pipette=pip)
```

### 19.7 Limiting how many racks load (`max_racks_count`)

Pass **`max_racks_1` … `max_racks_4`** to **`add_starting_tipracks`** (aligned with each tiprack pair), or set **`tracker.max_racks_count[load_name]`** later. This caps how aggressively the tracker loads new racks during refills—useful when you want to avoid over-filling the deck.

```python
tracker.add_starting_tipracks(
    TIPS_50, ['C1', 'D1'],
    max_racks_1=6,  # cap for tiprack1 (TIPS_50)
)
```

### 19.8 Excluding a slot from auto waste / refill (`ignore_slots`)

For a reusable rack or a position you manage yourself, append the slot id so **`clear_old`** / refill paths skip it.

```python
tracker.ignore_slots.append('C1')
try:
    tracker.pick_up(pipette=pip)
    tracker.drop_tip(pipette=pip)
finally:
    tracker.ignore_slots.remove('C1')
```

### 19.9 Proactive operator refill (`refill_deck`)

Pause **before** a long segment so the operator can place fresh racks, without waiting for **`OutOfTipsError`**. Pass the **API load name** and the pipette you want **`assign_tipracks`** to run on after the pause (when **`reassign_pipette`** is True, the default). For 96-channel ALL layouts you may still need **`assign_tipracks(..., mode=ALL)`** after refill depending on your workflow—mirror what you use at protocol start.

```python
tracker.refill_deck(TIPS_50, pipette=pip)
```

To limit clearing to main grid or expansion only, use **`refill_main_deck_slots`** or **`refill_expansion_slots`** (see §11).

### 19.10 After manual deck changes (`reset_rack_list`)

If your protocol (or operator) moves tip racks outside TipTracker’s helpers, rebuild internal lists before the next **`pick_up`**.

```python
tracker.reset_rack_list(TIPS_50)      # one type
# tracker.reset_rack_list(None)       # all known types
```

### 19.11 If you only call `load_tipracks` (advanced)

**`load_tipracks`** alone does not populate **`rack_assignments`**. Always follow with **`assign_slots`** (or use **`add_starting_tipracks`**, which does both). Otherwise **`pick_up`** can fail with missing assignment data (e.g. **`KeyError`** on configured slots).

```python
tracker.assign_slots(TIPS_50, ['D2'])
tracker.load_tipracks(TIPS_50, ['D2'])
tracker.assign_tipracks(TIPS_50, pipette=pip)
```

### 19.12 Validate with simulation

Run the bundled harness under the Opentrons simulator to exercise adapters, expansion registration, **`pick_up_slots`**, **`ignore_slots`**, **`reset_rack_list`**, and counter updates:

```bash
opentrons_simulate path/to/tiptrack_testing.py
```

Point **`path/to`** at your checkout’s `TipTracker/tiptrack_testing.py`. Enable optional **`add_stacker`** lines in `run()` when you are ready to test stacker-heavy paths (see §19.13).

### 19.13 Flex stacker as automated supply

Register a **`flexStackerModuleV1`** with **`add_stacker`** so the tracker knows inventory per **`tip_load_name`**. The **`lid`** argument is the **labware load name** for stacker lids (often **`opentrons_flex_tiprack_lid`**); use **`None`** if your racks do not use lids. **`load_on_shuttle=True`** keeps one rack ready on the shuttle when counts allow (see **`load_tips_in_stacker`** in §13).

Place the stacker on a **deck slot that does not conflict** with other modules—if you called **`add_expansion_slots`**, do not put the stacker on a slot you registered as expansion-only staging unless your layout intentionally shares it.

```python
LID = 'opentrons_flex_tiprack_lid'  # or None

tracker.add_starting_tipracks(TIPS_1000, ['A1'], adapters=['A1'])
tracker.add_stacker('C4', TIPS_1000, initial_count=7, lid=LID, load_on_shuttle=True)
tracker.assign_tipracks(TIPS_1000, pipette=p96, mode=ALL)

# During the run, pick_up() will pull from assigned deck slots first, then use stacker
# paths when supply is exhausted (see class docstring priority list).
```

**Proactive stacker maintenance (operator-facing):**

- **`reload_stacker_inventory(tip_load_name, quantity, lid=None, load_on_shuttle=True)`** — pauses so someone can load hardware, then calls **`load_tips_in_stacker`** for **each** module registered under that tip type. Use **`lid`** when stackers were configured with lids (`add_stacker` stored a lid flag per row).
- **`refill_stacker_supply(tip_load_name, deposit_targets=None)`** — runs **`FlexStackerContext.fill`** on each stacker for that type (physical refill in the module). When **`max_racks_count`** matches consumed racks for the type, the implementation may shuttle **last** racks onto **`deposit_targets`** instead of pausing for a deck refill; pass the same kinds of targets **`pick_up`** would use when calling outside an automatic refill (often an empty list is enough if you only need the **`fill`** behavior).

**Direct pull onto specific deck slots:** **`grab_from_stacker(tip_load_name, empty_slots=[...])`** retrieves racks from stackers and places them on the listed slots. When **`carousel_tips`** is True, the implementation may **`carousel`** onto empty positions instead of a straight shuttle; set **`open_slot`** appropriately (§19.15). For a single returned **`Labware`** handle, **`move_from_stacker`** exists but is lower-level (lid removal, wrong labware on shuttle, etc.—see §13).

### 19.14 Stacker reserved for empty racks (`use_for_storing_empty`)

When **`add_stacker(..., use_for_storing_empty=True)`**, the tracker treats that module as **empty-rack storage** during carousel-heavy flows: it forces **`carousel_tips`** on if it was off, and may set **`open_slot`** to the stacker context so shuffles can park spent racks. Use this when you are **not** using the waste chute for every empty rack, or when you need a defined home for empties during **`carousel`** (see §12, §13).

```python
tracker.add_stacker('D4', TIPS_50, initial_count=0, lid=None, use_for_storing_empty=True)
# Pair with trash-bin-style defaults or explicit carousel_tips / open_slot tuning; test on hardware.
```

Supply stackers (**`use_for_storing_empty=False`**, the default) and storage stackers can coexist for different **`tip_load_name`** keys; keep load names consistent with **`add_starting_tipracks`** / **`assign_slots`**.

### 19.15 On-deck carousel (staging slot + `carousel`)

**`carousel(tiprack_to_move_away, tiprack_to_move_in)`** swaps two racks via **`open_slot`**: it moves **`tiprack_to_move_away`** to the open staging location, moves **`tiprack_to_move_in`** into the slot that was vacated, then updates **`open_slot`** to where the incoming rack came from. Arguments may be **labware objects** or **deck slot strings**; adapter slots resolve to the child rack on the Flex tip rack adapter (see implementation docstring in §12).

**Requirements:**

1. Set **`tracker.open_slot`** to an **empty deck slot** (string) or to a staging object your layout keeps clear—**before** calling **`carousel`**, or you will get **`ValueError: No open slot defined`**.
2. Use a **gripper-capable** setup (**`use_gripper=True`**) unless you accept manual **`move_labware`** behavior from the API.

**When this matters:** With a **`TrashBin`**, **`carousel_tips`** defaults to **True** so empty racks tend to stay on deck (§6–§7). With a **`WasteChute`**, the tracker defaults to chute disposal; if you set **`use_chute=False`** and **`carousel_tips=True`**, you are opting into shuffling empties instead of sending every rack down the chute—**`open_slot`** must always point at usable staging.

```python
# Example: two full racks on B1 and C1; D2 is a deliberately empty buffer slot.
tracker.open_slot = 'D2'
tracker.carousel('B1', 'C1')  # rack from B1 → D2; rack from C1 → B1; open_slot becomes C1 (conceptually the “from” side of the incoming rack)
```

After **`waste_tips`**, chute moves, or operator deck edits, **labware references can go stale**—prefer **`reset_rack_list`** (§19.10) before relying on **`carousel`** again. The harness in **`tiptrack_testing.py`** keeps carousel exercises commented for this reason; simulate isolated segments when learning the behavior.

### 19.16 `TrashBin` defaults (more manual, more carousel)

Passing a **`TrashBin`** sets **`use_chute=False`** and **`carousel_tips=True`** by default so empty racks tend to stay on deck until you **`carousel`**, pause, or run a refill helper. Pair with an explicit **`open_slot`** staging slot if you rely on shuffles.

```python
trash = ctx.load_trash_bin('A3')  # example slot; match your deck map
tracker = TipTracker(ctx, pip, trash, use_gripper=True)
tracker.open_slot = 'D2'
```

### 19.17 Waste chute on, but keep some racks on deck

You can override constructor defaults when you want the chute for tips but still **`carousel`** some racks:

```python
waste = ctx.load_waste_chute()
tracker = TipTracker(ctx, pip, waste, use_gripper=True)
tracker.use_chute = True
tracker.carousel_tips = True
tracker.open_slot = 'D2'
```

### 19.18 `global_adapter` for one adapter serving multiple tip types

After **`add_starting_tipracks(..., adapters=['A1'])`** (or equivalent), set **`tracker.global_adapter = True`** so the tracker can reuse the first adapter slot across types (see §15). You still need racks loaded and **`assign_tipracks`** aligned with each segment.

```python
tracker.add_starting_tipracks(TIPS_1000, ['A1'], TIPS_200, ['B1'], adapters=['A1'])
tracker.global_adapter = True
```

### 19.19 Three or four tip types in one `add_starting_tipracks` call

Use **`tiprack1`/`slots1` … `tiprack4`/`slots4`** when you need several distinct API load names at start (**each** pair must use a **different** tip type and **non-overlapping** slots). Below loads three types; add **`tiprack4=`** / **`slots4=`** when you have a fourth distinct load name (for example another volume your labware definitions expose).

```python
tracker.add_starting_tipracks(
    TIPS_50, ['C1'],
    TIPS_200, ['D1'],
    TIPS_1000, ['B1'],
)
```

### 19.20 `assign_slots(..., clear_other_slots=True)`

For a given **`tiprack1`**, **`clear_other_slots=True`** **replaces** **`rack_assignments[tiprack1]`** with **`slots1`**. When **`False`** and that type was already tracked, new slots are **appended** instead. (Cross-type slot conflicts are still resolved by stripping the conflicting slot from **other** types—see §8.)

```python
tracker.assign_slots(TIPS_50, ['C1', 'D1'], clear_other_slots=True)
```

### 19.21 `refill_main_deck_slots` (expansion row excluded)

Same operator flow as **`refill_deck`**, but slot selection ignores registered expansion slots—useful when only the main grid should be cleared for a pause.

```python
tracker.refill_main_deck_slots(TIPS_50, pipette=pip)
```

### 19.22 `refill_expansion_slots` (column 4 only)

Requires **`add_expansion_slots`** first. Clears and pauses for refills **only** on expansion assignments for that **`tip_load_name`**.

```python
tracker.add_expansion_slots(['A4', 'B4'])
tracker.refill_expansion_slots(TIPS_50, pipette=pip)
```

### 19.23 `reload_deck_tipracks` / `reload_main_deck_tipracks` / `reload_expansion_tipracks`

Lighter than **`refill_deck`**: home, pause, then **`load_tipracks`** on the resolved slot list—**no** automatic **`reset_rack_list`** / **`assign_tipracks`**. Often composed after stacker or internal exhaustion (see §11).

```python
tracker.reload_main_deck_tipracks(TIPS_50)
tracker.assign_tipracks(TIPS_50, pipette=pip)  # caller-owned follow-up
```

### 19.24 `refill_deck` without pipette reassignment

When refilling a **secondary** tip type during a multi-type pause, skip **`assign_tipracks`** so you do not clobber the active head’s layout:

```python
tracker.refill_deck(TIPS_200, pipette=pip, reassign_pipette=False)
```

### 19.25 `refill_tips` with `waste_all_old=False`

Restrict clearing exhausted racks to the **slots you pass** instead of scanning every assignment for that type.

```python
tracker.refill_tips(TIPS_50, slots=['D1', 'D2'], waste_all_old=False)
```

### 19.26 `clear_old` for every assigned slot of a type

Pass **`slots_to_clear=None`** to clear all assigned slots that still hold that load name, then reset internal lists for the type (see §12).

```python
tracker.clear_old(TIPS_50, slots_to_clear=None, save_tips=True)
```

### 19.27 `clear_old` for a subset of slots

Pass a **list of slot strings** when only part of the map should be cleared (e.g. before loading a plate into one bay).

```python
tracker.clear_old(TIPS_50, slots_to_clear=['C1'], save_tips=True)
```

### 19.28 `clear_old` with automatic chute disposal

When **`save_tips=False`**, **`use_chute`**, and **`use_gripper`** are all on, exhausted racks can be discarded without a manual removal pause (see **`_clear_old_use_gripper_to_waste`** in the source).

```python
tracker.clear_old(TIPS_50, slots_to_clear=['C1'], save_tips=False)
```

### 19.29 `waste_tips` for ad-hoc rack removal

Moves racks to chute or off-deck per configuration; **does not** fully refresh internal maps by itself—follow with **`reset_rack_list`** / **`load_tipracks`** / **`assign_slots`** as your workflow requires (§12).

```python
tracker.waste_tips(['C1', 'D1'])
tracker.reset_rack_list(TIPS_50)
```

### 19.30 `replace_tips` (swap API load name on existing slots)

Clears **`old_rack_name`** from a slice of **`rack_assignments`**, reassigns slots to **`new_rack_name`**, and **`load_tipracks`** on the freed slots. **`number_to_replace`** limits how many slots participate; **`manually_remove=False`** only when chute+gripper paths match your safety review.

```python
tracker.replace_tips(TIPS_200, TIPS_50, number_to_replace=1, manually_remove=True)
tracker.assign_tipracks(TIPS_50, pipette=pip)
```

### 19.31 `pick_up` return codes for logging

**`pick_up`** returns **0–4** (normal, carousel, expansion waste path, stacker waste path, manual refill started). Log them during development to see which refill branch fired.

```python
rc = tracker.pick_up(pipette=pip)
ctx.comment(f'TipTracker pick_up return code: {rc}')
```

### 19.32 `pick_up(..., locus=well)` for a specific column / well

Forces the next pickup to a particular well when you need deterministic tip consumption (e.g. column-aligned partial layouts).

```python
rack = tracker.tipracks[TIPS_50][0]
tracker.pick_up(pipette=pip, locus=rack.wells_by_name()['A1'])
```

### 19.33 `pick_up(..., refill_all=True)`

When **`True`**, running out can trigger broader refill behavior (all racks of that type emptying together)—use when you want coordinated segment refills rather than a single-rack minimum fix.

```python
tracker.pick_up(pipette=pip, refill_all=True)
```

### 19.34 `pick_up(..., set_active_pipette=True)` when addressing pipette `2`

Useful in dual-pipette protocols so the tracker’s **`active_pipette`** tracks the head you just touched.

```python
tracker.pick_up(pipette=2, set_active_pipette=True)
tracker.drop_tip()
```

### 19.35 `drop_tip(..., return_tip=True)` then reuse

Returns the tip to the rack; a subsequent **`pick_up`** on the same assignment can pick that tip again when layout allows (see harness in **`tiptrack_testing.py`**).

```python
tracker.drop_tip(pipette=pip, return_tip=True)
tracker.pick_up(pipette=pip)
```

### 19.36 Pipette selectors: `1`, `'2'`, or instrument object

**`assign_tipracks`**, **`pick_up`**, and **`drop_tip`** accept **`1` / `2`**, string aliases like **`'one'`**, or the **`InstrumentContext`**.

```python
tracker.assign_tipracks(TIPS_50, pipette=1)
tracker.pick_up(1)
tracker.drop_tip('two')  # requires pipette2=... in TipTracker(...)
```

### 19.37 `assign_tipracks(..., mode=PARTIAL_COLUMN, start=..., end=...)`

**`start`** and **`end`** are only consulted for **`PARTIAL_COLUMN`** (Opentrons nozzle API). Use nozzle addresses your version documents.

```python
tracker.assign_tipracks(TIPS_50, pipette=pip, mode=PARTIAL_COLUMN, start='A1', end='A12')
```

### 19.38 `assign_tipracks(..., mode=ROW)` or `mode=SINGLE`

Same pattern as **`COLUMN`**: partial layouts use **`tipracks[rack_name]`** for **`configure_nozzle_layout`**. Ensure racks exist before the call.

```python
tracker.assign_tipracks(TIPS_200, pipette=pip, mode=ROW)
tracker.pick_up(pipette=pip)
```

### 19.39 `assign_tipracks(..., mode=None)` on Flex 96-channel with adapters

When **`mode is None`**, a Flex 96-channel pipette with **`adapter_pickup_tipracks[rack_name]`** populated gets **`tip_racks`** set to the adapter list **without** re-running **`configure_nozzle_layout`**—handy after a partial layout when you are not ready for **`ALL`** again.

```python
tracker.assign_tipracks(TIPS_1000, pipette=p96, mode=None)
```

### 19.40 Two stacker modules for the same `tip_load_name`

Calling **`add_stacker`** twice with the same **`tip_load_name`** **appends** a second row in **`stackers[tip_load_name]`**; **`move_from_stacker`** drains the first row with positive count before the next.

```python
tracker.add_stacker('B4', TIPS_1000, 5, 'opentrons_flex_tiprack_lid', True)
tracker.add_stacker('C4', TIPS_1000, 5, 'opentrons_flex_tiprack_lid', True)
```

### 19.41 `load_tips_in_stacker` using the returned module handle

**`add_stacker`** returns **`FlexStackerContext`**; you can call **`load_tips_in_stacker`** later to bump inventory after an operator restocks without rebuilding the module.

```python
stk = tracker.add_stacker('B4', TIPS_50, 3, None, load_on_shuttle=True)
# ... later ...
tracker.load_tips_in_stacker(stk, TIPS_50, quantity=5, lid=None, load_on_shuttle=True)
```

### 19.42 `store_in_stacker` for empty racks (advanced)

Use with stackers registered under **`use_for_storing_empty`** (§19.14). **`force_store=True`** bypasses load-name mismatch guards when you accept the risk.

```python
tracker.store_in_stacker(empty_rack_labware, store_stacker=stk, force_store=False)
```

### 19.43 `move_from_stacker` when you place racks yourself

Lower-level: returns **`Labware`**, handles lids and wrong shuttle labware. You are responsible for **`move_labware`** onto the deck unless you let **`grab_from_stacker`** do it.

```python
rack = tracker.move_from_stacker(TIPS_50)
ctx.move_labware(rack, 'D1', use_gripper=tracker.use_gripper)
tracker.reset_rack_list(TIPS_50)
```

### 19.44 `shuffle_for_forced_pickup` (explicit)

Normally **`pick_up_slots`** triggers this internally; you can call it when building custom flows so the forced slot has a rack before **`pick_up_tip`**.

```python
tracker.shuffle_for_forced_pickup(TIPS_200, pick_up_slot='B1', pipette=pip)
```

### 19.45 Import `TipTracker` from a sibling file (simulate / dev)

```bash
export PYTHONPATH="/path/to/folder_containing_TipTracker.py:${PYTHONPATH}"
opentrons_simulate /path/to/my_protocol.py
```

```python
from TipTracker import TipTracker  # module name follows your filename
```

### 19.46 Embed the class in one file (`verbose_tracebacks=True`)

When you paste **`TipTracker`** into a large protocol, keep **`verbose_tracebacks=True`** (default) so **`_fatal_tracker_error`** prints a stderr call stack pointing at your protocol line (§17).

```python
tracker = TipTracker(ctx, pip, waste, verbose_tracebacks=True)
```

### 19.47 Toggle `debug` and `print_comments` mid-run

**`tracker.debug`** controls **`print`** tracing; **`tracker.print_comments`** mirrors run-log **`ctx.comment`** density. Flip them per phase.

```python
tracker.debug = True
tracker.print_comments = False
```

### 19.48 Set `max_racks_count` after construction

Besides **`max_racks_*`** kwargs on **`add_starting_tipracks`**, assign the dict directly when caps depend on runtime math.

```python
tracker.max_racks_count[TIPS_50] = 10
```

### 19.49 Read counters after a batch for throughput estimates

**`tip_counts`**, **`tip_rack_counts`**, **`pick_up_count`**, and **`drop_count`** are useful in simulation to size **`max_racks_count`** and stacker inventory.

```python
ctx.comment(f'{tracker.tip_counts=} {tracker.tip_rack_counts=}')
```

### 19.50 Pre–`pick_up` checklist (avoid `KeyError` / fatal exits)

1. **`add_starting_tipracks`** or **`assign_slots` + `load_tipracks`**.  
2. **`add_expansion_slots`** before any expansion slot loads.  
3. **`assign_tipracks`** for the pipette you will use (with **`mode`** for partial / **`ALL`** for 96 adapter).  
4. Adapter workflows: **`adapters=[...]`** in load calls so **`adapter_pickup_tipracks`** is populated when the 96-channel needs it.  
5. After any manual deck edit: **`reset_rack_list`**.

---

*End of manual.*
