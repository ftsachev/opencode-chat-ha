# OpenCode Chat for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
![HA](https://img.shields.io/badge/Home%20Assistant-2024.10+-blue)

A sidebar chat panel for Home Assistant powered by [OpenCode](https://opencode.ai).
Inspect entities, edit dashboards, and create automations in plain English — with
**diff-and-approve gating** on every destructive action.

---

## Features

- **Sidebar chat panel** — multi-session, persistent conversations, auto-titled
- **Streaming responses** — markdown, code blocks, tables render live
- **18 HA-integrated tools** — list entities, get dashboards, propose changes
- **Diff-and-approve** — dashboard edits show a diff, service calls show the payload,
  automations show the YAML. Nothing is applied without clicking **Apply**
- **Config flow** — set server URL, password, optional model/agent override
- **Single-instance** integration, HA 2024.10+

## Architecture

Designed after [`claude_chat_for_homeassistant`](https://github.com/h0jeZvgoxFepBQ2C/claude_chat_for_homeassistant)
but substitutes Anthropic's streaming SDK for OpenCode's `POST /session/:id/message`
REST endpoint.

```
HA frontend (Lit)  ←WebSocket→  HA backend (Python)  ←HTTP→  OpenCode server
                                    │
                                    ├─ ToolRegistry (18 tools)
                                    ├─ SessionStore (persistent)
                                    └─ Diff/Approve (pending changes)
```

## Prerequisites

1. **OpenCode CLI** — `npm install -g @opencode-ai/cli` or download from
   [opencode.ai](https://opencode.ai)
2. **OpenCode server** running on a machine reachable from HA: `opencode serve`
3. **Home Assistant** 2024.10 or later

## Installation

### HACS (recommended)

1. Add this repo as a custom repository:
   HACS → Integrations → ⋮ → Custom repositories →
   `https://github.com/ftsachev/opencode-chat-ha` → category **Integration**
2. Click **Install**
3. Restart Home Assistant
4. **Settings → Devices & Services → Add Integration → OpenCode Chat**
5. Enter your OpenCode server URL and password

### Manual

Copy `custom_components/opencode_chat/` to your HA `config/custom_components/`
directory, restart HA, then add the integration via the UI.

## Setting up OpenCode

```bash
# Start the server (no auth — use only on localhost)
opencode serve --port 4096

# Or with a password
OPENCODE_SERVER_PASSWORD=your-password opencode serve --port 4096 --hostname 0.0.0.0
```

If OpenCode is on a different machine, make sure HA can reach it on the network
and set the URL accordingly in the config flow.

## Tools available to OpenCode

| Tool | Description |
|------|-------------|
| `list_entities` | List all entities with state and attributes |
| `get_entity` | Get detailed state of a specific entity |
| `list_areas` | List all areas |
| `list_dashboards` | List all dashboards |
| `get_dashboard` | Get full dashboard JSON |
| `list_lovelace_resources` | List Lovelace dashboard resources |
| `list_automations` | List all automations |
| `get_automation` | Get full automation config |
| `list_services` | List all available services |
| `list_automation_traces` | List recent automation traces |
| `get_automation_trace` | Get trace details |
| `get_state_history` | Get state history for an entity |
| `propose_dashboard_view_update` | Propose a dashboard view change (requires approval) |
| `propose_service_call` | Propose a service call (requires approval) |
| `propose_automation_create` | Propose creating an automation (requires approval) |
| `propose_automation_update` | Propose updating an automation (requires approval) |
| `propose_automation_delete` | Propose deleting an automation (requires approval) |
| `get_server_info` | Get HA server info and config |

## Limitations

- YAML-mode dashboards are read-only (only storage-mode dashboards can be edited)
- Automations require `automation: !include automations.yaml` in `configuration.yaml`
- Single integration instance only
- Image attachment support (upload via chat UI)

## Development

The integration lives at `custom_components/opencode_chat/`:

```
custom_components/opencode_chat/
├── __init__.py          # Panel registration, asset serving
├── config_flow.py       # Config flow + options flow
├── const.py             # Constants
├── opencode_client.py   # OpenCode API client + tool-loop
├── tools.py             # 18 HA tool definitions + ToolRegistry
├── websocket_api.py     # 10 WebSocket commands
├── storage.py           # Session store (persistent)
├── media.py             # Image attachment handling
├── services.yaml        # send_message service
├── translations/        # i18n (en)
└── frontend/
    └── opencode-chat-panel.js  # Lit-element sidebar panel
```

## License

MIT
