---
name: opentrons-tiptracker-mcp
description: >-
  Builds Opentrons Flex Python protocols using TipTracker for tip logistics and the
  OpentronsAI MCP server for API docs, generation, and simulation. Use when the user
  asks for Flex protocols, TipTracker integration, gripper/stacker/expansion tip refills,
  or wants Opentrons MCP (OpentronsAI) combined with this repository’s TipTracker library.
---

# Opentrons Flex + TipTracker (MCP-assisted)

## When this skill applies

Use this workflow for **Opentrons Flex** protocols where tips are managed through **`TipTracker`** (not raw `pipette.pick_up_tip()` for tracked types) and when the **OpentronsAI MCP** server is available in Cursor. The repository library is **version 3.0** (`TipTracker.metadata['Version']`).

## MCP server (OpentronsAI)

1. **Discover the server id** in the active Cursor MCP config (often **`user-OpentronsAI`**, display name **OpentronsAI**). If tools fail, open MCP settings and match the identifier shown for the Opentrons / Gradio MCP endpoint.
2. **Before each tool call**, read that server’s tool descriptor under the Cursor MCP tools path (schema JSON) so arguments match exactly.
3. **Default tools** (names may match exactly on your install):

| Tool | Purpose |
|------|--------|
| `opentronsai_mcp_server_get_relevant_api_docs` | Pass a **`query`** string; returns API excerpts (XML). Call before writing or reviewing unfamiliar API surface. |
| `opentronsai_mcp_server_generate_protocol` | Pass a **`message`** describing the protocol; use for first drafts, then **rewrite** to add TipTracker per this skill. |
| `opentronsai_mcp_server_simulate_protocol` | Pass full protocol source as **`protocol`** string; validates / simulates. Run after TipTracker integration. |

Use **`call_mcp_tool`** with `server` set to the Opentrons server identifier from the user’s environment, `toolName` exactly as registered, and `arguments` per the descriptor.

## Recommended agent workflow

1. **Clarify** robot (`Flex`), API level (align with `TipTracker.py` **`requirements["apiLevel"]`**), pipette models, waste (chute vs trash), gripper, expansion row, stackers, adapters (96-channel).
2. **`opentronsai_mcp_server_get_relevant_api_docs`** — query focused strings (e.g. `Flex load_waste_chute`, `move_labware use_gripper`, `configure_nozzle_layout ALL`).
3. **Draft** protocol structure (`metadata`, `requirements`, `run(ctx)`).
4. **Integrate TipTracker** using the principles below; wire **`tracker.pick_up` / `tracker.drop_tip`** instead of direct tip picks for tracked types.
5. **`opentronsai_mcp_server_simulate_protocol`** — paste final protocol; fix errors until clean or document hardware-only gaps.
6. Optionally **`opentronsai_mcp_server_generate_protocol`** for inspiration only — **always** reconcile output with TipTracker and with docs from step 2 (generated code often uses `pick_up_tip()` without TipTracker).

## TipTracker principles (non-negotiable)

- **Setup order:** construct `TipTracker(ctx, pipette1, waste, pipette2=..., use_gripper=...)` → if using **A4/B4/C4/D4** tips, **`add_expansion_slots` first** → **`add_starting_tipracks`** (or `load_tipracks` + **`assign_slots`**) → optional stackers / properties → **`assign_tipracks`** → runtime **`pick_up` / `drop_tip`**.
- **Never** leave `rack_assignments` empty: do not call **`load_tipracks`** without **`assign_slots`** (or use **`add_starting_tipracks`** which does both).
- **Tip type key** is always the **labware API load name** string (e.g. `opentrons_flex_96_filtertiprack_50ul`).
- **96-channel + adapter:** use **`adapters=['A1', ...]`** in `add_starting_tipracks` / `load_tipracks`; for full head use **`assign_tipracks(..., mode=ALL)`**; see manual §15.
- **After manual deck edits** outside TipTracker, call **`reset_rack_list`** before the next `pick_up`.

Full behavior, return codes, refill helpers, and 50 cookbook snippets: **[`TIPTRACKER_MANUAL.md`](../../TIPTRACKER_MANUAL.md)** (repo root).

## Protocol skeleton (TipTracker + Flex)

```python
from opentrons import protocol_api
from opentrons.protocol_api import ALL  # only if using 96-ch ALL layout

# TipTracker import: see examples/ for sys.path pattern from this repo layout

requirements = {
    "robotType": "Flex",
    "apiLevel": "2.27",  # keep in sync with TipTracker.py
}

def run(ctx: protocol_api.ProtocolContext):
    pip = ctx.load_instrument("flex_8channel_50", "right")
    waste = ctx.load_waste_chute()
    tracker = TipTracker(ctx, pip, waste, use_gripper=True)
    tracker.add_starting_tipracks("opentrons_flex_96_filtertiprack_50ul", ["C1", "D1"])
    tracker.active_pipette = pip
    tracker.assign_tipracks("opentrons_flex_96_filtertiprack_50ul", pipette=pip)
    tracker.pick_up()
    # aspirate / dispense ...
    tracker.drop_tip()
```

## Supporting files

- **[`reference-mcp.md`](reference-mcp.md)** — MCP usage notes and message/query crafting.
- **[`examples/README.md`](examples/README.md)** — how to run simulations and imports.
- Example protocols in **`examples/`** — copy-paste starting points.
