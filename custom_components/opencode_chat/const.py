DOMAIN = "opencode_chat"

CONF_URL = "url"
CONF_PASSWORD = "password"
CONF_MODEL = "model"
CONF_AGENT = "agent"

DEFAULT_URL = "http://localhost:4096"
DEFAULT_MODEL = ""
DEFAULT_AGENT = ""

PANEL_URL_PATH = "opencode-chat"
PANEL_TITLE = "OpenCode Chat"
PANEL_ICON = "mdi:chat-processing"

FRONTEND_URL = "/opencode_chat_static"
FRONTEND_SCRIPT = "opencode-chat-panel.js"

STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 1

MAX_TURNS_PER_REQUEST = 16
DEFAULT_MAX_TOKENS = 8192
SSE_RECONNECT_DELAY = 2.0
POLL_INTERVAL = 0.5
