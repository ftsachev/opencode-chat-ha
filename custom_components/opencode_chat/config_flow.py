from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    CONF_AGENT,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_URL,
    DEFAULT_AGENT,
    DEFAULT_MODEL,
    DEFAULT_URL,
    DOMAIN,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): str,
        vol.Optional(CONF_PASSWORD, default=""): str,
        vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): str,
        vol.Optional(CONF_AGENT, default=DEFAULT_AGENT): str,
    }
)


class OpenCodeChatConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                import urllib.request
                import json

                url = user_input[CONF_URL].rstrip("/")
                password = user_input.get(CONF_PASSWORD, "")
                req = urllib.request.Request(f"{url}/global/health")
                if password:
                    import base64
                    creds = base64.b64encode(
                        f"opencode:{password}".encode()
                    ).decode()
                    req.add_header("Authorization", f"Basic {creds}")
                resp = await self.hass.async_add_executor_job(
                    lambda: urllib.request.urlopen(req, timeout=5)
                )
                data = json.loads(resp.read())
                if not data.get("healthy"):
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title="OpenCode Chat",
                        data={
                            CONF_URL: url,
                            CONF_PASSWORD: password,
                        },
                        options={
                            CONF_MODEL: user_input.get(CONF_MODEL, DEFAULT_MODEL),
                            CONF_AGENT: user_input.get(CONF_AGENT, DEFAULT_AGENT),
                        },
                    )
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return OpenCodeChatOptionsFlow(config_entry)


class OpenCodeChatOptionsFlow(OptionsFlow):
    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_model = self.config_entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        current_agent = self.config_entry.options.get(CONF_AGENT, DEFAULT_AGENT)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_MODEL, default=current_model): str,
                    vol.Optional(CONF_AGENT, default=current_agent): str,
                }
            ),
        )
