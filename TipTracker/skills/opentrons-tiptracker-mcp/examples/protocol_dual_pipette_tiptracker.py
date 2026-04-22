"""
Flex protocol: 96-channel (adapter) + 8-channel, expansion row registered.

Demonstrates add_expansion_slots, adapters= in add_starting_tipracks, dual pipette
TipTracker construction, and assign_tipracks with mode=ALL for 96-channel.

Simulate with PYTHONPATH pointing at the TipTracker repo root (parent of TipTracker.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

from opentrons import protocol_api
from opentrons.protocol_api import ALL

_TIPTRACKER_ROOT = Path(__file__).resolve().parents[3]
if str(_TIPTRACKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_TIPTRACKER_ROOT))

from TipTracker import TipTracker  # noqa: E402

metadata = {
    "protocolName": "TipTracker dual pipette (skill example)",
    "author": "TipTracker skill examples",
    "description": "96ch ALL on adapter + 8ch filter tips; expansion slots registered",
    "source": "Custom",
}

requirements = {
    "robotType": "Flex",
    "apiLevel": "2.27",
}

TIPS_50 = "opentrons_flex_96_filtertiprack_50ul"
TIPS_1000 = "opentrons_flex_96_filtertiprack_1000ul"


def run(ctx: protocol_api.ProtocolContext):
    p96 = ctx.load_instrument("flex_96channel_1000", "left")
    p8 = ctx.load_instrument("flex_8channel_50", "right")
    waste = ctx.load_waste_chute()

    tracker = TipTracker(ctx, p96, waste, pipette2=p8, use_gripper=True)

    tracker.add_expansion_slots(["A4", "B4"])
    tracker.add_starting_tipracks(
        TIPS_1000,
        ["A1"],
        TIPS_50,
        ["C1", "A4"],
        adapters=["A1"],
    )

    tracker.active_pipette = p96
    tracker.assign_tipracks(TIPS_1000, pipette=p96, mode=ALL)
    tracker.pick_up(pipette=p96)
    tracker.drop_tip(pipette=p96)

    tracker.active_pipette = p8
    tracker.assign_tipracks(TIPS_50, pipette=p8)
    tracker.pick_up(pipette=p8)
    tracker.drop_tip(pipette=p8)
