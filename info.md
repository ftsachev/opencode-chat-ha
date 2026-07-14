# OpenCode Chat for Home Assistant

A sidebar chat panel for Home Assistant powered by [OpenCode](https://opencode.ai). Inspect entities, edit dashboards, and create automations in plain English with diff-and-approve gating.

## What it does

- Adds an **OpenCode Chat** entry to the HA sidebar.
- Multiple persistent chat sessions, auto-titled after the first message.
- Streaming responses — markdown, code blocks, and tables render live.
- OpenCode has a small, scoped set of HA tools:
  - `list_entities`, `get_entity`, `list_areas`
  - `list_dashboards`, `get_dashboard`, `list_lovelace_resources`
  - `list_automations`, `get_automation`, `list_services`
  - `list_automation_traces`, `get_automation_trace`, `get_state_history`
  - `propose_dashboard_view_update`, `propose_service_call`,
    `propose_automation_create/update/delete`
- Dashboard edits show a diff. Service calls show the payload. Automations show the YAML. Nothing is applied without you clicking **Apply**.

## Prerequisites

1. **OpenCode** must be installed and running with `opencode serve` on a machine accessible from your Home Assistant instance.
2. Home Assistant 2024.10 or later.

## Installation (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/ftsachev/opencode-chat-ha`, category "Integration".
2. Install **OpenCode Chat**.
3. Restart Home Assistant.
4. Settings → Devices & Services → **Add Integration** → search "OpenCode Chat".
5. Enter your OpenCode server URL (default: `http://localhost:4096`) and password if set.

## Setting up OpenCode

On your OpenCode host:

```bash
# Start the OpenCode server
opencode serve --port 4096 --hostname 0.0.0.0

# Optional: add a password
OPENCODE_SERVER_PASSWORD=your-password opencode serve --port 4096 --hostname 0.0.0.0
```

If OpenCode is on a different machine, make sure HA can reach it on the network.

## Limitations

- YAML-mode dashboards are read-only.
- Automations: create/update/delete requires `automation: !include automations.yaml` in `configuration.yaml`.
- Single integration instance only.
