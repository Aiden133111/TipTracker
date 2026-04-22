# Installing this skill in Cursor

Cursor loads agent skills from:

| Location | Scope |
|----------|--------|
| `~/.cursor/skills/<skill-folder>/` | All projects |
| `<workspace>/.cursor/skills/<skill-folder>/` | That workspace only |

This repository ships the skill under **`TipTracker/skills/opentrons-tiptracker-mcp/`**.

**Option A — symlink (macOS / Linux):**

```bash
mkdir -p ~/.cursor/skills
ln -s "/absolute/path/to/TipTracker/skills/opentrons-tiptracker-mcp" ~/.cursor/skills/opentrons-tiptracker-mcp
```

**Option B — copy:** duplicate the folder into `~/.cursor/skills/` (you must re-copy after edits).

**Option C — workspace:** if your Cursor workspace root is only the `TipTracker` repo, you can use:

```text
TipTracker/.cursor/skills/opentrons-tiptracker-mcp -> ../skills/opentrons-tiptracker-mcp
```

(Or copy the skill folder into `TipTracker/.cursor/skills/`.)

## Opentrons MCP

Add the Opentrons MCP server in **Cursor Settings → MCP** (HTTP URL pattern matches Opentrons’ hosted MCP). The agent discovers tools under the server id shown in Cursor (often **`user-OpentronsAI`**).
