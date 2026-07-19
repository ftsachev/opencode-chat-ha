# OpenCode Chat for Home Assistant

A Home Assistant sidebar integration that connects to an [OpenCode](https://opencode.ai) server, giving you an AI chat panel with 20+ HA-aware tools for managing your smart home.

## Features

- **Sidebar Chat Panel** — Chat with OpenCode directly from the HA sidebar
- **20+ HA Tools** — List entities, control devices, create automations, manage dashboards
- **Diff-and-Approve** — Destructive changes show a diff before applying
- **Streaming Responses** — Real-time text streaming via WebSocket
- **Session Management** — Create, rename, pin, and delete chat sessions
- **Image Support** — Upload images to conversations
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

The integration provides 20+ tools for managing your home:

| Tool | Description |
|------|-------------|
| `list_entities` | List and filter HA entities |
| `get_entity` | Get entity details and state |
| `call_service` | Call any HA service |
| `create_automation` | Create new automations |
| `update_automation` | Update existing automations |
| `delete_automation` | Delete automations |
| `update_dashboard` | Update dashboard configurations |
| `update_dashboard_view` | Update specific dashboard views |
| `upload_image` | Upload images to conversations |

### Session Management

- **New Chat**: Click the **+ New** button in the sidebar
- **Pin Session**: Click the pin icon to keep important sessions at the top
- **Delete Session**: Click the **✕** button to remove a session
- **Clear All**: Click **Clear All** to remove all sessions

## Security

- Admin access required for panel access
- Passwords stored in HA's encrypted config entry storage
- Session IDs validated to prevent path traversal
- Input validation on all tool parameters

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
