"""
Minimal Flex protocol using TipTracker (single 8-channel pipette, waste chute).

Simulate (from repo machine):
  export PYTHONPATH="/path/to/TipTracker:${PYTHONPATH}"
  opentrons_simulate protocol_minimal_tiptracker.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from opentrons import protocol_api

# Repository root: .../TipTracker/ (directory containing TipTracker.py)
_TIPTRACKER_ROOT = Path(__file__).resolve().parents[3]
if str(_TIPTRACKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_TIPTRACKER_ROOT))

from TipTracker import TipTracker  # noqa: E402

metadata = {
    "protocolName": "TipTracker minimal (skill example)",
    "author": "TipTracker skill examples",
    "description": "Single pipette; add_starting_tipracks + assign_tipracks + pick_up/drop_tip",
    "source": "Custom",
}

requirements = {
    "robotType": "Flex",
    "apiLevel": "2.27",
}

TIPS_50 = "opentrons_flex_96_filtertiprack_50ul"


def run(ctx: protocol_api.ProtocolContext):
    pip = ctx.load_instrument("flex_8channel_50", "right")
    waste = ctx.load_waste_chute()

    tracker = TipTracker(ctx, pip, waste, use_gripper=True, debugging=False, suppress_comments=False)
    tracker.add_starting_tipracks(TIPS_50, ["C1", "D1"])
    tracker.active_pipette = pip
    tracker.assign_tipracks(TIPS_50, pipette=pip)

    tracker.pick_up()
    ctx.comment("Tip picked up via TipTracker")
    tracker.drop_tip()
