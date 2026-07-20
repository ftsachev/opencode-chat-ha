# OpenCode Chat for Home Assistant

A Home Assistant sidebar integration that connects to an [OpenCode](https://opencode.ai) server, giving you an AI chat panel with 20+ HA-aware tools for managing your smart home.

**The approve-gate is the product.** Nothing reaches Home Assistant until a human reads a diff and clicks Apply. This is what distinguishes it from autonomous agent approaches.

## Features

- **Human-in-the-Loop** — All changes stage for review before applying
- **Sidebar Chat Panel** — Chat with OpenCode directly from the HA sidebar
- **20+ HA Tools** — List entities, query dashboards, create automations, call services
- **Diff-and-Approve** — Destructive changes show a diff before applying
- **Safe-Apply Pipeline** — Backup, validate, and rollback on failure
- **Streaming Responses** — Real-time text streaming via WebSocket
- **Session Management** — Create, rename, pin, and delete chat sessions
- **Dark Mode** — Automatic HA theme integration

## Requirements

- Home Assistant 2024.1.0 or newer
- An [OpenCode](https://opencode.ai) server running and accessible from your HA instance

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **Custom Repositories**
3. Add this repository URL: `https://github.com/ftsachev/opencode-chat-ha`
4. Select **Integration** as the category
5. Click **Install**
6. Restart Home Assistant

### Manual

1. Download the latest release from [GitHub](https://github.com/ftsachev/opencode-chat-ha/releases)
2. Extract `custom_components/opencode_chat/` to your HA config directory
3. Restart Home Assistant

## Setting Up the OpenCode Server

### Option A: magnusoverli/opencode Add-on (HA OS/Supervised only)

1. Add the add-on repository to HA
2. Install the OpenCode add-on
3. **Enable the LAN server** in the add-on configuration (defaults to off)
4. Note the URL (typically `http://<host-ip>:4096`)

### Option B: External Host

Run `opencode serve` on a machine accessible from your HA instance:

```bash
opencode serve --host 0.0.0.0 --port 4096
```

**Security warning:** Binding to `0.0.0.0` exposes an agent with filesystem and shell access to your LAN. A strong password and firewall rules are mandatory.

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **OpenCode Chat**
3. Enter your OpenCode server details:
   - **URL**: Your OpenCode server URL (e.g., `http://192.168.1.100:4096`)
   - **Password**: Your OpenCode server password
4. Optionally configure:
   - **Model ID**: Override the default model
   - **Agent Name**: Override the default agent
5. Click **Submit**

## Usage

After setup, a new **OpenCode Chat** panel appears in your sidebar. Click it to open the chat interface.

### Available Tools

The integration provides 20+ tools. All changes are staged for human review:

| Tool | Type | Description |
|------|------|-------------|
| `list_entities` | Read-only | List and filter HA entities |
| `get_entity` | Read-only | Get entity details and state |
| `list_areas` | Read-only | List HA areas |
| `list_dashboards` | Read-only | List Lovelace dashboards |
| `get_dashboard` | Read-only | Fetch dashboard config |
| `get_dashboard_view` | Read-only | Fetch a single view |
| `list_automations` | Read-only | List automations |
| `list_services` | Read-only | List HA services |
| `propose_dashboard_update` | Staged | Stage dashboard changes for review |
| `propose_dashboard_view_update` | Staged | Stage view changes for review |
| `propose_automation_create` | Staged | Stage new automation for review |
| `propose_automation_update` | Staged | Stage automation edit for review |
| `propose_automation_delete` | Staged | Stage automation deletion for review |
| `propose_service_call` | Staged | Stage service call for review |

### Session Management

- **New Chat**: Click the **+ New** button in the sidebar
- **Pin Session**: Click the pin icon to keep important sessions at the top
- **Delete Session**: Click the **✕** button to remove a session
- **Clear All**: Click **Clear All** to remove all sessions

## Security

- **Admin-only access**: All WebSocket API handlers require admin privileges
- **Human approve-gate**: All changes stage for review before applying
- **Safe-apply pipeline**: Backup, validate, and rollback on failure
- Passwords stored in HA's encrypted config entry storage
- Session IDs validated to prevent path traversal
- Input validation on all tool parameters
- Action blocks sanitized in tool results to prevent injection

**Note:** The `/opencode_chat_media` endpoint is served unauthenticated, protected only by random UUID filenames.

## Development

```bash
# Clone the repository
git clone https://github.com/ftsachev/opencode-chat-ha.git

# Run tests
cd opencode-chat-ha
python -m pytest tests/
```

## License

MIT License. See [LICENSE](LICENSE) for details.
