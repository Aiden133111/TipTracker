"""
Shows a proactive operator refill segment using refill_deck (see TIPTRACKER_MANUAL §11).

After refill_deck, re-run assign_tipracks if your pipette layout (e.g. mode=ALL) must
match the start of the run. Adjust slots and tip type for your deck.
"""

from __future__ import annotations

import sys
from pathlib import Path

from opentrons import protocol_api

_TIPTRACKER_ROOT = Path(__file__).resolve().parents[3]
if str(_TIPTRACKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_TIPTRACKER_ROOT))

from TipTracker import TipTracker  # noqa: E402

metadata = {
    "protocolName": "TipTracker refill segment (skill example)",
    "author": "TipTracker skill examples",
    "description": "refill_deck pause pattern between run phases",
    "source": "Custom",
}

requirements = {"robotType": "Flex", "apiLevel": "2.27"}

TIPS_200 = "opentrons_flex_96_filtertiprack_200ul"


def run(ctx: protocol_api.ProtocolContext):
    pip = ctx.load_instrument("flex_8channel_50", "right")
    waste = ctx.load_waste_chute()
    tracker = TipTracker(ctx, pip, waste, use_gripper=True)

    tracker.add_starting_tipracks(TIPS_200, ["B1", "D1"])
    tracker.active_pipette = pip
    tracker.assign_tipracks(TIPS_200, pipette=pip)

    tracker.pick_up()
    tracker.drop_tip()

    ctx.comment("Phase 2: operator refill on deck before long segment")
    tracker.refill_deck(TIPS_200, pipette=pip)
    tracker.assign_tipracks(TIPS_200, pipette=pip)

    tracker.pick_up()
    tracker.drop_tip()
