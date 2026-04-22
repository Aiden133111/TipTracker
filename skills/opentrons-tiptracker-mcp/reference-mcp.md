# OpentronsAI MCP — agent reference

## Server identifier

In Cursor, the Opentrons Gradio MCP server is commonly exposed as **`user-OpentronsAI`** (see `SERVER_METADATA.json` in the MCP tools folder). The user’s **`.vscode/mcp.json`** may use a different key; always use the **server id** that Cursor lists for that MCP connection when calling **`call_mcp_tool`**.

## Tools (typical shapes)

### `opentronsai_mcp_server_get_relevant_api_docs`

- **Argument:** `query` (string) — e.g. `WasteChute Flex`, `flexStackerModuleV1`, `configure_nozzle_layout PARTIAL_COLUMN`.
- **Use:** Ground truth for API details before editing protocols.

### `opentronsai_mcp_server_generate_protocol`

- **Argument:** `message` (string) — full user intent, deck constraints, labware load names.
- **Use:** Draft only. In the same `message` (or a follow-up simulation pass), require **Flex**, **`apiLevel`** matching TipTracker, and explicit **TipTracker** steps if you want the model to attempt them; still verify line-by-line.

### `opentronsai_mcp_server_simulate_protocol`

- **Argument:** `protocol` (string) — entire Python file contents.
- **Use:** Regression check after changes. If simulation omits local imports, flatten TipTracker into one file or ensure the simulator environment includes the repo on `PYTHONPATH` (see `examples/README.md`).

## TipTracker-specific `message` / `query` hints

Include in MCP prompts where relevant:

- `TipTracker pick_up drop_tip assign_tipracks add_starting_tipracks add_expansion_slots`
- `opentrons_flex_96_tiprack_adapter` and `adapter_pickup_tipracks` for 96-channel
- `robotType Flex`, waste chute vs trash bin behavior

This steers both **get_relevant_api_docs** and **generate_protocol** toward compatible answers.
