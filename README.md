# TipTracker V2

**Author:** Aiden McFadden  
**Version:** 4.1.1  
**Not affiliated with Opentrons.** Use at your own risk.

TipTracker V2 is the recommended TipTracker library for **Opentrons Flex** protocols (`robotType: Flex`, API level **2.27**). It keeps the same public API as TipTracker 3.0 (`TipTracker.py`) while restructuring refill internals around `RefillSnapshot`, `DeckRegion`, and `StackerSupply`.

Full API reference and cookbook: [`../TIPTRACKER_MANUAL.md`](../TIPTRACKER_MANUAL.md).  
Release notes: [`../CHANGELOG.md`](../CHANGELOG.md).

## Why V2

| | V1 (`TipTracker.py`) | V2 (`tiptrackerV2`) |
|--|----------------------|---------------------|
| Library version | 3.0 | **4.1.1** |
| Public methods | Same surface | Same surface (+ parity helpers) |
| Refill model | Inline refill branches | Snapshot-driven refill pipeline |
| Stacker rows | Opaque `[module, count, lid]` lists | Typed `StackerSupply` (legacy rows still stored) |
| Recommended for | Existing pasted copies | **New protocols** |

V2 matches V1 behavior for global adapters, partial nozzle layouts, forced pickup (`refill_forced_pickup_rack`), and adapter reload. Prefer V2 for new work.

## Install / import

This package is not on PyPI. Use it from a clone of this repository.

```bash
# From the TipTracker repo root (parent of tiptrackerV2/)
export PYTHONPATH="/path/to/TipTracker:${PYTHONPATH}"
```

```python
from tiptrackerV2 import TipTracker
```

On the robot, either:

1. Keep `tiptrackerV2/` next to your protocol and ensure the parent directory is on `sys.path`, or
2. Paste `TipTracker` from `tiptrackerV2/tiptrackerV2.py` into your protocol file (common for App uploads).

## Quick start

```python
from opentrons import protocol_api
from tiptrackerV2 import TipTracker

metadata = {"protocolName": "TipTracker V2 example"}
requirements = {"robotType": "Flex", "apiLevel": "2.27"}

TIPS_50 = "opentrons_flex_96_filtertiprack_50ul"

def run(ctx: protocol_api.ProtocolContext):
    pip = ctx.load_instrument("flex_8channel_50", "right")
    waste = ctx.load_waste_chute()

    tracker = TipTracker(ctx, pip, waste, use_gripper=True)
    tracker.add_starting_tipracks(TIPS_50, ["C1", "D1"])
    tracker.active_pipette = pip
    tracker.assign_tipracks(TIPS_50, pipette=pip)

    tracker.pick_up()
    # aspirate / dispense ...
    tracker.drop_tip()
```

**Setup order:** construct → `add_expansion_slots` (if using A4–D4) → optional `add_stacker` → `add_starting_tipracks` (or `load_tipracks` + `assign_slots`) → `assign_tipracks` → `pick_up` / `drop_tip`.

Do not assign `pip.tip_racks` yourself for tracked tip types unless you understand the side effects.

## Package layout

```
tiptrackerV2/
  __init__.py          # exports TipTracker, __version__
  tiptrackerV2.py      # implementation (version 4.1.1)
  README.md            # this file
```

Internal types (not required for normal protocol authors):

- `DeckRegion` — main / expansion / adapter slot classification
- `StackerSupply` — typed stacker inventory row
- `RefillSnapshot` — shared state for one OutOfTips refill attempt

## Testing

Regression protocols live in [`../v2_regression_tests/`](../v2_regression_tests/). They default to V2 (`TT_VERSION=v2`).

```bash
cd /path/to/TipTracker
export PYTHONPATH="$(pwd):${PYTHONPATH}"
opentrons_simulate v2_regression_tests/01_minimal_8ch.py
# Compare V1:
TT_VERSION=v1 opentrons_simulate v2_regression_tests/01_minimal_8ch.py
```

See [`../v2_regression_tests/README.md`](../v2_regression_tests/README.md).

## Migrating from V1

1. Change the import to `from tiptrackerV2 import TipTracker`.
2. Keep the same constructor arguments and public method calls.
3. Re-simulate with `opentrons_simulate` (and hardware-test before production).
4. If you previously pasted V1 into a protocol, replace that class body with the V2 class from `tiptrackerV2.py`.

No intentional public API breaks between 3.0 and 4.1.1.

## License

MIT — see [`../LICENSE.txt`](../LICENSE.txt).
