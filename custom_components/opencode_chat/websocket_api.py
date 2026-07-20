from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import voluptuous as vol
import yaml as yaml_lib
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import CONF_AGENT, CONF_MODEL, DOMAIN
from .media import delete_session_media, save_image
from .opencode_client import OpenCodeClient
from .storage import Message, PendingChange, SessionStore
from .tools import ToolRegistry

_LOGGER = logging.getLogger(__name__)


def _get_domain_data(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {})


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list_sessions)
    websocket_api.async_register_command(hass, ws_create_session)
    websocket_api.async_register_command(hass, ws_delete_session)
    websocket_api.async_register_command(hass, ws_rename_session)
    websocket_api.async_register_command(hass, ws_toggle_pin_session)
    websocket_api.async_register_command(hass, ws_get_session)
    websocket_api.async_register_command(hass, ws_chat)
    websocket_api.async_register_command(hass, ws_upload_image)
    websocket_api.async_register_command(hass, ws_list_pending)
    websocket_api.async_register_command(hass, ws_apply_change)
    websocket_api.async_register_command(hass, ws_reject_change)
    websocket_api.async_register_command(hass, ws_get_models)


@websocket_api.websocket_command({vol.Required("type"): "opencode_chat/list_sessions"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_list_sessions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    connection.send_result(msg["id"], store.list_sessions())


@websocket_api.websocket_command(
    {vol.Required("type"): "opencode_chat/create_session"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_create_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    session = await store.create()
    connection.send_result(
        msg["id"],
        {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": [],
            "pending_changes": [],
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/delete_session",
        vol.Required("session_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    session_id = msg["session_id"]
    await store.delete(session_id)
    delete_session_media(hass, session_id)
    connection.send_result(msg["id"], True)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/rename_session",
        vol.Required("session_id"): str,
        vol.Required("title"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_rename_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    session_id = msg["session_id"]
    await store.rename(session_id, msg["title"])
    connection.send_result(msg["id"], True)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/toggle_pin_session",
        vol.Required("session_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_toggle_pin_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    pinned = await store.toggle_pin(msg["session_id"])
    connection.send_result(msg["id"], {"pinned": pinned})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/get_session",
        vol.Required("session_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    session = store.get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    connection.send_result(
        msg["id"],
        {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at}
                for m in session.messages
            ],
            "pending_changes": [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "summary": c.summary,
                    "diff": c.diff,
                    "status": c.status,
                    "payload": c.payload,
                }
                for c in session.pending_changes
            ],
        },
    )


async def _stream_to_connection(hass, connection, msg_id, events):
    """Forward streaming events from the client to the WebSocket connection."""
    async for event in events:
        connection.send_message(
            websocket_api.messages.event_message(msg_id, event)
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/chat",
        vol.Required("session_id"): str,
        vol.Required("message"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_chat(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    data = _get_domain_data(hass)
    store: SessionStore = data.get("store")
    tools: ToolRegistry = data.get("tools")
    client: OpenCodeClient = data.get("client")
    entry = data.get("entry")

    session_id = msg["session_id"]
    user_text = msg["message"]

    session = store.get(session_id)
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return

    model = entry.options.get(CONF_MODEL, client._default_model) or None
    agent = entry.options.get(CONF_AGENT, client._default_agent) or None

    user_msg = Message(role="user", content=[{"type": "text", "text": user_text}])
    await store.append_message(session_id, user_msg)

    connection.send_message(
        websocket_api.messages.event_message(msg["id"], {
            "type": "user_message_appended",
        })
    )

    # Auto-title on first user message
    if len(session.messages) <= 2:
        title = await client.summarize_title(user_text)
        if title:
            await store.rename(session_id, title)
            connection.send_message(
                websocket_api.messages.event_message(msg["id"], {
                    "type": "session_renamed",
                    "title": title,
                })
            )

    async def emit(event: dict):
        connection.send_message(
            websocket_api.messages.event_message(msg["id"], event)
        )

    try:
        new_messages, opencode_sid = await client.stream_chat(
            history=session.messages,
            session_id=session_id,
            emit=emit,
            opencode_sid=session.opencode_session_id,
            model=model,
            agent=agent,
        )

        if opencode_sid and not session.opencode_session_id:
            await store.set_opencode_session(session_id, opencode_sid)

        for new_msg in new_messages:
            await store.append_message(session_id, new_msg)

        connection.send_message(
            websocket_api.messages.event_message(msg["id"], {
                "type": "chat_complete",
            })
        )
        connection.send_result(msg["id"], {"done": True})
    except Exception as e:
        _LOGGER.exception("Chat error")
        connection.send_message(
            websocket_api.messages.event_message(msg["id"], {
                "type": "error",
                "error": str(e),
            })
        )
        connection.send_error(msg["id"], "chat_error", str(e))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/upload_image",
        vol.Required("session_id"): str,
        vol.Required("media_type"): str,
        vol.Required("data"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_upload_image(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    try:
        ref = await hass.async_add_executor_job(
            save_image, hass, msg["session_id"], msg["media_type"], msg["data"]
        )
        connection.send_result(msg["id"], ref)
    except ValueError as e:
        connection.send_error(msg["id"], "invalid_image", str(e))


@websocket_api.websocket_command(
    {vol.Required("type"): "opencode_chat/list_pending"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_list_pending(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    connection.send_result(msg["id"], store.list_pending())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/apply_change",
        vol.Required("session_id"): str,
        vol.Required("change_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_apply_change(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    session_id = msg["session_id"]
    change_id = msg["change_id"]

    change = store.get(session_id)
    if change is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return

    pending = None
    for c in change.pending_changes:
        if c.id == change_id:
            pending = c
            break

    if pending is None:
        connection.send_error(msg["id"], "not_found", "Change not found")
        return

    try:
        result = await _execute_change(hass, store, session_id, pending)
        await store.set_change_status(session_id, change_id, "accepted")
        connection.send_result(
            msg["id"],
            {"status": "accepted", "result": result},
        )
    except Exception as e:
        _LOGGER.exception("Apply failed")
        connection.send_error(msg["id"], "apply_failed", str(e))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "opencode_chat/reject_change",
        vol.Required("session_id"): str,
        vol.Required("change_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_reject_change(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store: SessionStore = _get_domain_data(hass).get("store")
    session_id = msg["session_id"]
    change_id = msg["change_id"]
    await store.set_change_status(session_id, change_id, "rejected")
    connection.send_result(msg["id"], {"status": "rejected"})


@websocket_api.websocket_command(
    {vol.Required("type"): "opencode_chat/get_models"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_models(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    client: OpenCodeClient = _get_domain_data(hass).get("client")
    try:
        providers = await hass.async_add_executor_job(
            lambda: client._request("GET", "/config/providers")
        )
        models = []
        for p in providers.get("providers", []):
            for m in p.get("models", []):
                models.append({"id": m.get("id"), "name": m.get("name", m.get("id"))})
        connection.send_result(msg["id"], models)
    except Exception:
        connection.send_result(msg["id"], [])


async def _execute_change(
    hass: HomeAssistant, store: SessionStore, session_id: str, change: PendingChange
) -> dict:
    kind = change.kind
    payload = change.payload

    if kind in ("dashboard_update", "dashboard_view_update"):
        url_path = payload.get("url_path", "lovelace")
        dash = hass.data.get("lovelace", {}).get("dashboards", {}).get(url_path)
        if dash is None:
            raise ValueError(f"Dashboard {url_path} not found")

        if kind == "dashboard_view_update":
            view_path = payload.get("view_path", "")
            new_view = payload.get("view", {})
            config = await dash.async_load() or {"views": []}
            views = config.get("views", [])
            idx = int(view_path) if view_path.isdigit() else next(
                (i for i, v in enumerate(views) if v.get("path") == view_path),
                len(views),
            )
            if idx < len(views):
                views[idx] = new_view
            else:
                views.append(new_view)
            config["views"] = views
            await dash.async_save(config)
        elif kind == "dashboard_update":
            await dash.async_save(payload.get("config", {}))

        return {"applied": True, "url_path": url_path}

    if kind == "automation_create":
        config = payload.get("config", {})
        automations_path = hass.config.path("automations.yaml")
        existing = []
        try:
            with open(automations_path) as f:
                existing = yaml_lib.safe_load(f) or []
        except (FileNotFoundError, yaml_lib.YAMLError):
            existing = []
        if not isinstance(existing, list):
            existing = [existing] if existing else []

        # Ensure we have a valid automation ID
        auto_id = config.get("id")
        if not auto_id:
            auto_id = f"auto_{uuid.uuid4().hex[:8]}"
            config["id"] = auto_id

        existing.append(config)
        with open(automations_path, "w") as f:
            yaml_lib.dump(existing, f, default_flow_style=False)

        await hass.services.async_call("automation", "reload", {}, blocking=True)
        return {"applied": True, "automation_id": auto_id}

    if kind == "automation_update":
        automation_id = payload.get("automation_id", "")
        config = payload.get("config", {})
        automations_path = hass.config.path("automations.yaml")
        try:
            with open(automations_path) as f:
                existing = yaml_lib.safe_load(f) or []
        except (FileNotFoundError, yaml_lib.YAMLError):
            existing = []
        if not isinstance(existing, list):
            existing = [existing] if existing else []

        entity_id = f"automation.{automation_id}" if not automation_id.startswith("automation.") else automation_id
        for i, auto in enumerate(existing):
            if isinstance(auto, dict) and (
                auto.get("id") == automation_id
                or auto.get("alias") == automation_id
            ):
                existing[i] = {**auto, **config}
                break
        else:
            existing.append(config)

        with open(automations_path, "w") as f:
            yaml_lib.dump(existing, f, default_flow_style=False)

        await hass.services.async_call("automation", "reload", {}, blocking=True)
        return {"applied": True, "automation_id": automation_id}

    if kind == "automation_delete":
        automation_id = payload.get("automation_id", "")
        automations_path = hass.config.path("automations.yaml")
        try:
            with open(automations_path) as f:
                existing = yaml_lib.safe_load(f) or []
        except (FileNotFoundError, yaml_lib.YAMLError):
            existing = []
        if not isinstance(existing, list):
            existing = [existing] if existing else []

        remaining = [
            a
            for a in existing
            if not (
                a.get("id") == automation_id
                or a.get("alias") == automation_id
            )
        ]

        with open(automations_path, "w") as f:
            yaml_lib.dump(remaining, f, default_flow_style=False)

        await hass.services.async_call("automation", "reload", {}, blocking=True)
        return {"applied": True, "automation_id": automation_id}

    if kind == "service_call":
        domain = payload.get("domain")
        service = payload.get("service")
        service_data = payload.get("service_data", {})
        target = payload.get("target", {})
        service_call_data = {**service_data}
        if target:
            for key, val in target.items():
                service_call_data[key] = val
        await hass.services.async_call(
            domain, service, service_call_data, blocking=True
        )
        return {"applied": True, "service": f"{domain}.{service}"}

    raise ValueError(f"Unknown change kind: {kind}")
