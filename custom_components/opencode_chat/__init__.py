from __future__ import annotations

import hashlib
import logging
import os

from homeassistant.components.frontend import (
    async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AGENT,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_URL,
    DEFAULT_AGENT,
    DEFAULT_MODEL,
    DOMAIN,
    FRONTEND_SCRIPT,
    FRONTEND_URL,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
)
from .media import ensure_media_root, media_root
from .opencode_client import OpenCodeClient
from .storage import SessionStore
from .tools import ToolRegistry
from .websocket_api import async_register_commands

MEDIA_URL = "/opencode_chat_media"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    url = entry.data[CONF_URL]
    password = entry.data.get(CONF_PASSWORD, "")
    model = entry.options.get(CONF_MODEL, DEFAULT_MODEL)
    agent = entry.options.get(CONF_AGENT, DEFAULT_AGENT)

    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)

    client = OpenCodeClient(
        url=url,
        password=password,
        model=model,
        agent=agent,
        tools=tools,
    )

    hass.data[DOMAIN] = {
        "store": store,
        "tools": tools,
        "client": client,
        "entry": entry,
    }

    if not hass.data.get(f"{DOMAIN}_ws_registered"):
        async_register_commands(hass)
        hass.data[f"{DOMAIN}_ws_registered"] = True

    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    panel_js_path = os.path.join(frontend_dir, FRONTEND_SCRIPT)
    asset_hash = await hass.async_add_executor_job(_asset_hash, panel_js_path)
    await hass.async_add_executor_job(ensure_media_root, hass)
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(FRONTEND_URL, frontend_dir, cache_headers=False),
            StaticPathConfig(MEDIA_URL, media_root(hass), cache_headers=True),
        ]
    )

    async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": "opencode-chat-panel",
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{FRONTEND_URL}/{FRONTEND_SCRIPT}?v={asset_hash}",
            }
        },
        require_admin=True,
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _asset_hash(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return "0"
    return h.hexdigest()[:10]


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(DOMAIN, None)
    return True
