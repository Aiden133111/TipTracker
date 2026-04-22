# Example protocols (TipTracker + Flex)

## Layout

Examples live under `TipTracker/skills/opentrons-tiptracker-mcp/examples/`. Each file prepends the **TipTracker repository root** (three levels up) to `sys.path` so `from TipTracker import TipTracker` resolves to `TipTracker/TipTracker.py`.

If you move the skill folder, adjust `_TIPTRACKER_ROOT = Path(__file__).resolve().parents[3]` to point at the directory that **contains** `TipTracker.py`.

## Local simulation (CLI)

From anywhere, with the Opentrons CLI installed:

```bash
export PYTHONPATH="/Users/you/Desktop/ProtocolDev/TipTracker:${PYTHONPATH}"
opentrons_simulate /Users/you/Desktop/ProtocolDev/TipTracker/skills/opentrons-tiptracker-mcp/examples/protocol_minimal_tiptracker.py
```

Use your actual path to the **TipTracker** directory (parent of `TipTracker.py`).

## Cursor MCP simulation

Paste the full file contents into **`opentronsai_mcp_server_simulate_protocol`** (`protocol` argument). If the remote simulator cannot import TipTracker, either:

- paste the **`TipTracker` class** into the protocol file for that test, or  
- strip TipTracker and validate only the non-tracker portion, then run **`opentrons_simulate`** locally with `PYTHONPATH`.

## Files

| File | Intent |
|------|--------|
| `protocol_minimal_tiptracker.py` | Single 8-channel pipette, waste chute, two deck racks. |
| `protocol_dual_pipette_tiptracker.py` | Two pipettes, expansion slots, 96-channel adapter + 8-channel tips. |
| `protocol_operator_refill_segment.py` | `refill_deck` between phases; shows reassignment after pause. |
