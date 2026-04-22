# TipTracker

**Author:** Aiden McFadden  
**Not affiliated with Opentrons.** Use at your own risk.

TipTracker is a Python library for **Opentrons Flex** protocols (`robotType: Flex`, API level **2.27** in the bundled file). It wraps tip handling so you call `pick_up` / `drop_tip` instead of manually juggling `move_labware`, expansion staging slots (A4–D4), Flex stackers, empty racks, and refills.

**What it does:** Keeps internal lists of where each tip type lives (main deck, expansion row, adapters for 96-channel ALL pickup, stackers). When tips run out, it follows a defined priority (assigned deck slots → forced pickup slots → expansion → stackers → operator pauses). It can discard empty racks through the **waste chute**, **carousel** them with an `open_slot`, or pause for manual removal when using a **trash bin**.

**Full documentation:** See [`TIPTRACKER_MANUAL.md`](TIPTRACKER_MANUAL.md) for setup order, every public method, state dictionaries, 96-channel adapters, stackers, refill helpers, and troubleshooting.

**Version:** Library metadata in `TipTracker.py` reports **3.0** (see `TipTracker.metadata`).

**Quick start:** Load instruments and waste as usual, construct `TipTracker(ctx, pipette1, waste_bin, ...)`, call `add_expansion_slots` if you use column 4, `add_stacker` / `add_starting_tipracks` (or `load_tipracks` + `assign_slots`), then `assign_tipracks`, `pick_up`, and `drop_tip`. Do not assign `pip.tip_racks` directly for tracked types unless you know the implications.

**Test harness:** `tiptrack_testing.py` is an executable protocol that exercises many APIs under `opentrons_simulate`.
