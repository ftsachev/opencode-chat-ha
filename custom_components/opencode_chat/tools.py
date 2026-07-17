from __future__ import annotations

import difflib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import timedelta
from typing import Any

import yaml as yaml_lib
from homeassistant.components.lovelace.const import ConfigNotFound
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util

from .storage import PendingChange, SessionStore

_LOGGER = logging.getLogger(__name__)


def _validate_propose_config(config: Any, kind: str) -> None:
    """Validate propose payload to prevent malformed data from LLM hallucinations."""
    if not isinstance(config, dict):
        raise ValueError(f"{kind}: config must be a dict, got {type(config).__name__}")
    if kind == "automation_create":
        if "alias" not in config and "trigger" not in config:
            raise ValueError(
                f"{kind}: config must contain at least 'alias' or 'trigger' key"
            )
    elif kind == "automation_update":
        if "id" not in config and "alias" not in config:
            raise ValueError(
                f"{kind}: config should contain 'id' or 'alias' to identify the automation"
            )


def _target_key(change: PendingChange) -> str:
    p = change.payload or {}
    kind = change.kind
    if kind == "dashboard_update":
        return f"dashboard:{p.get('url_path', '')}"
    if kind in ("automation_update", "automation_delete"):
        return f"automation:{p.get('automation_id', '')}"
    if kind == "automation_create":
        config = p.get("config") or {}
        return f"automation_create:{config.get('alias', change.id)}"
    if kind == "service_call":
        target_json = json.dumps(p.get("target") or {}, sort_keys=True)
        return f"service:{p.get('domain')}.{p.get('service')}:{target_json}"
    return change.id


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_entities",
        "description": (
            "List Home Assistant entities. Filter by domain, area, label, and/or "
            "a keyword search across entity_id and friendly name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "HA domain filter (e.g. 'sensor', 'light')"},
                "area": {"type": "string", "description": "Area name filter"},
                "label": {"type": "string", "description": "Filter by label (case-insensitive)"},
                "search": {"type": "string", "description": "Keyword search"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "get_entity",
        "description": "Get the full state and attributes for a single entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_areas",
        "description": "List Home Assistant areas with their IDs and names.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_dashboards",
        "description": (
            "List Lovelace dashboards. Each entry has a url_path, title, "
            "mode, and whether it has a stored config."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_dashboard",
        "description": (
            "Fetch a Lovelace dashboard config by url_path. "
            "Use 'lovelace' for the default dashboard. "
            "Set summary=true for lightweight view listing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "summary": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "get_dashboard_view",
        "description": (
            "Fetch a single view from a Lovelace dashboard by its path. "
            "Much cheaper than get_dashboard for large dashboards. "
            "Use get_dashboard(summary=true) first to discover view paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "view_path": {"type": "string", "description": "Path of the view"},
            },
            "required": ["view_path"],
        },
    },
    {
        "name": "list_lovelace_resources",
        "description": (
            "List custom Lovelace cards/modules installed on this HA."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_automations",
        "description": (
            "List automations defined in HA. Returns entity_id, automation_id, "
            "name, state (on/off), area, and labels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Keyword to filter by name"},
                "area": {"type": "string", "description": "Filter by area name"},
                "label": {"type": "string", "description": "Filter by label"},
            },
        },
    },
    {
        "name": "list_automation_traces",
        "description": (
            "List recent execution traces of an automation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["automation_id"],
        },
    },
    {
        "name": "get_automation_trace",
        "description": "Get the full step-by-step trace of one automation execution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["automation_id", "run_id"],
        },
    },
    {
        "name": "get_state_history",
        "description": "Get recent state changes for an entity (last N hours).",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_services",
        "description": (
            "List HA services. Use domain and/or search to narrow results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Filter by service domain"},
                "search": {"type": "string", "description": "Keyword search"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "get_service",
        "description": "Get full parameter details for a single HA service.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
            },
            "required": ["domain", "service"],
        },
    },
    {
        "name": "propose_dashboard_update",
        "description": (
            "Propose a new full Lovelace dashboard config. Staged for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "new_config": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["new_config", "summary"],
        },
    },
    {
        "name": "propose_dashboard_view_update",
        "description": (
            "Propose updating a single view in a Lovelace dashboard. "
            "Staged for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "view_path": {"type": "string"},
                "new_view": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["view_path", "new_view", "summary"],
        },
    },
    {
        "name": "propose_automation_create",
        "description": (
            "Propose creating a new automation. Staged for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "config": {"type": "object", "description": "Automation config (alias, trigger, action, ...)"},
                "summary": {"type": "string"},
            },
            "required": ["config", "summary"],
        },
    },
    {
        "name": "propose_automation_update",
        "description": (
            "Propose updating an existing automation by automation_id. "
            "Staged for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "config": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["automation_id", "config", "summary"],
        },
    },
    {
        "name": "propose_automation_delete",
        "description": (
            "Propose deleting an automation. Staged for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["automation_id", "summary"],
        },
    },
    {
        "name": "get_automation",
        "description": "Fetch the full config of an automation by automation_id.",
        "input_schema": {
            "type": "object",
            "properties": {"automation_id": {"type": "string"}},
            "required": ["automation_id"],
        },
    },
    {
        "name": "propose_service_call",
        "description": (
            "Propose calling a Home Assistant service. Staged for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "service_data": {"type": "object", "default": {}},
                "target": {"type": "object", "default": {}},
                "summary": {"type": "string"},
            },
            "required": ["domain", "service", "summary"],
        },
    },
]


class ToolRegistry:
    def __init__(self, hass: HomeAssistant, store: SessionStore) -> None:
        self._hass = hass
        self._store = store
        self._handlers = {
            "list_entities": self._list_entities,
            "get_entity": self._get_entity,
            "list_areas": self._list_areas,
            "list_dashboards": self._list_dashboards,
            "get_dashboard": self._get_dashboard,
            "get_dashboard_view": self._get_dashboard_view,
            "list_lovelace_resources": self._list_lovelace_resources,
            "list_automations": self._list_automations,
            "list_automation_traces": self._list_automation_traces,
            "get_automation_trace": self._get_automation_trace,
            "get_state_history": self._get_state_history,
            "list_services": self._list_services,
            "get_service": self._get_service,
            "get_automation": self._get_automation,
            "propose_dashboard_update": self._propose_dashboard_update,
            "propose_dashboard_view_update": self._propose_dashboard_view_update,
            "propose_automation_create": self._propose_automation_create,
            "propose_automation_update": self._propose_automation_update,
            "propose_automation_delete": self._propose_automation_delete,
            "propose_service_call": self._propose_service_call,
        }

    @property
    def hass(self) -> HomeAssistant:
        return self._hass

    @property
    def store(self) -> SessionStore:
        return self._store

    async def call(
        self, name: str, args: dict, session_id: str, tool_use_id: str
    ) -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"Unknown tool: {name}"}
        _LOGGER.debug("Tool %s called (session=%s, id=%s)", name, session_id, tool_use_id)
        try:
            result = await handler(args, session_id, tool_use_id)
            _LOGGER.debug("Tool %s completed (session=%s)", name, session_id)
            return result
        except Exception as e:
            _LOGGER.exception("Tool %s failed", name)
            return {"error": str(e)}

    def _build_diff(self, old: str, new: str) -> str:
        return "\n".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="current",
                tofile="proposed",
                lineterm="",
            )
        )

    async def _list_entities(self, args: dict, sid: str, tid: str) -> dict:
        domain = args.get("domain")
        area_name = args.get("area")
        label = args.get("label")
        search = args.get("search")
        limit = min(args.get("limit", 20), 100)

        registry = er.async_get(self._hass)
        area_reg = ar.async_get(self._hass)
        area_map = {a.id: a.name for a in area_reg.async_list_areas()}

        area_id = None
        if area_name:
            area_id = next(
                (a.id for a in area_reg.async_list_areas() if a.name.lower() == area_name.lower()),
                None,
            )

        label_id = None
        if label:
            label_id = next(
                (lbl.label_id for lbl in (getattr(area_reg, "async_list_labels", lambda: [])()) if lbl.label_id.lower() == label.lower()),
                None,
            )

        matches = []
        total_count = 0
        for entity in registry.entities.values():
            if domain and entity.domain != domain:
                continue
            if area_id and entity.area_id != area_id:
                continue
            if label_id and label_id not in (entity.labels or []):
                continue
            name = (entity.name or entity.entity_id).lower()
            if search and search.lower() not in name and search.lower() not in entity.entity_id:
                continue
            total_count += 1
            if len(matches) >= limit:
                continue
            state_obj = self._hass.states.get(entity.entity_id)
            matches.append(
                {
                    "entity_id": entity.entity_id,
                    "name": entity.name or entity.entity_id,
                    "domain": entity.domain,
                    "area": area_map.get(entity.area_id, ""),
                    "state": state_obj.state if state_obj else None,
                    "labels": list(entity.labels or []),
                }
            )

        return {
            "summary": f"{total_count} matching entities, showing {len(matches)} (limit {limit})",
            "total": total_count,
            "entities": matches,
            "truncated": total_count > limit,
        }

    async def _get_entity(self, args: dict, sid: str, tid: str) -> dict:
        entity_id = args["entity_id"]
        state = self._hass.states.get(entity_id)
        if state is None:
            return {"error": f"Entity {entity_id} not found"}
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
        }

    async def _list_areas(self, args: dict, sid: str, tid: str) -> dict:
        area_reg = ar.async_get(self._hass)
        areas = [
            {"area_id": a.id, "name": a.name, "labels": list(a.labels or [])}
            for a in area_reg.async_list_areas()
        ]
        return {"areas": areas}

    async def _list_dashboards(self, args: dict, sid: str, tid: str) -> dict:
        try:
            from homeassistant.components.lovelace.dashboard import _async_dashboards

            dashboards = []
            for url_path, dash in self._hass.data.get("lovelace", {}).get("dashboards", {}).items():
                try:
                    config = await dash.async_load()
                    has_config = config is not None
                except ConfigNotFound:
                    has_config = False
                dashboards.append(
                    {
                        "url_path": url_path,
                        "title": getattr(dash, "title", url_path),
                        "mode": getattr(dash, "mode", "storage"),
                        "has_stored_config": has_config,
                    }
                )
            return {"dashboards": dashboards}
        except Exception as e:
            return {"error": str(e), "dashboards": []}

    async def _get_dashboard(self, args: dict, sid: str, tid: str) -> dict:
        url_path = args.get("url_path", "lovelace")
        summary = args.get("summary", False)
        try:
            from homeassistant.components.lovelace.dashboard import LovelaceStorage

            dash = self._hass.data.get("lovelace", {}).get("dashboards", {}).get(url_path)
            if dash is None:
                return {"error": f"Dashboard {url_path} not found"}

            config = await dash.async_load()
            if config is None:
                return {"url_path": url_path, "config": None, "views": []}

            if summary:
                views = []
                for i, view in enumerate(config.get("views", [])):
                    views.append(
                        {
                            "index": i,
                            "path": view.get("path", str(i)),
                            "title": view.get("title", f"View {i}"),
                            "card_count": len(view.get("cards", [])),
                        }
                    )
                return {"url_path": url_path, "views": views}

            return {"url_path": url_path, "config": config}
        except Exception as e:
            return {"error": str(e)}

    async def _get_dashboard_view(self, args: dict, sid: str, tid: str) -> dict:
        url_path = args.get("url_path", "lovelace")
        view_path = args["view_path"]
        try:
            dash = self._hass.data.get("lovelace", {}).get("dashboards", {}).get(url_path)
            if dash is None:
                return {"error": f"Dashboard {url_path} not found"}
            config = await dash.async_load()
            if config is None:
                return {"error": "Dashboard has no config"}
            views = config.get("views", [])
            idx = int(view_path) if view_path.isdigit() else next(
                (i for i, v in enumerate(views) if v.get("path") == view_path), -1
            )
            if idx < 0 or idx >= len(views):
                return {"error": f"View {view_path} not found"}
            return {"url_path": url_path, "view_path": view_path, "view": views[idx]}
        except Exception as e:
            return {"error": str(e)}

    async def _list_lovelace_resources(self, args: dict, sid: str, tid: str) -> dict:
        try:
            resources = self._hass.data.get("lovelace", {}).get("resources", [])
            return {
                "resources": [
                    {"url": r.url, "type": r.type, "id": r.id}
                    for r in (resources or [])
                ]
            }
        except Exception as e:
            return {"error": str(e), "resources": []}

    async def _list_automations(self, args: dict, sid: str, tid: str) -> dict:
        search = args.get("search", "").lower()
        area_name = args.get("area", "").lower()
        label = args.get("label", "").lower()

        area_reg = ar.async_get(self._hass)
        area_map = {a.id: a.name for a in area_reg.async_list_areas()}

        registry = er.async_get(self._hass)

        automations = []
        for entity_id in self._hass.states.async_entity_ids("automation"):
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            name = state.attributes.get("friendly_name", entity_id).lower()
            if search and search not in name:
                continue
            entry = registry.entities.get(entity_id)
            if not entry:
                continue
            entry_area = area_map.get(entry.area_id, "").lower()
            if area_name and area_name not in entry_area:
                continue
            if label and label not in [l.lower() for l in (entry.labels or [])]:
                continue
            automations.append(
                {
                    "entity_id": entity_id,
                    "automation_id": entity_id.replace("automation.", ""),
                    "name": state.attributes.get("friendly_name", entity_id),
                    "state": state.state,
                    "area": area_map.get(entry.area_id, ""),
                    "labels": list(entry.labels or []),
                }
            )

        return {"automations": automations}

    async def _list_automation_traces(self, args: dict, sid: str, tid: str) -> dict:
        automation_id = args["automation_id"]
        limit = args.get("limit", 10)
        try:
            from homeassistant.components.trace import _get_traces_for_automation

            entity_id = f"automation.{automation_id}" if not automation_id.startswith("automation.") else automation_id
            traces = await _get_traces_for_automation(self._hass, entity_id)
            return {
                "traces": [
                    {
                        "run_id": t.get("run_id"),
                        "timestamp": t.get("timestamp"),
                        "trigger": t.get("trigger"),
                        "conditions": t.get("conditions"),
                        "error": t.get("error"),
                    }
                    for t in (traces or [])[:limit]
                ]
            }
        except Exception as e:
            return {"error": str(e), "traces": []}

    async def _get_automation_trace(self, args: dict, sid: str, tid: str) -> dict:
        automation_id = args["automation_id"]
        run_id = args["run_id"]
        try:
            from homeassistant.components.trace import _get_trace

            entity_id = f"automation.{automation_id}" if not automation_id.startswith("automation.") else automation_id
            trace = await _get_trace(self._hass, entity_id, run_id)
            return {"trace": trace}
        except Exception as e:
            return {"error": str(e)}

    async def _get_state_history(self, args: dict, sid: str, tid: str) -> dict:
        entity_id = args["entity_id"]
        hours = args.get("hours", 24)
        try:
            from homeassistant.components.recorder.history import get_significant_states
            from datetime import datetime

            start = dt_util.utcnow() - timedelta(hours=hours)
            end = dt_util.utcnow()
            states = await self._hass.async_add_executor_job(
                get_significant_states,
                self._hass,
                start,
                end,
                [entity_id],
            )
            entries = [
                {
                    "state": s.state,
                    "last_changed": s.last_changed.isoformat(),
                    "attributes": dict(s.attributes),
                }
                for s in states.get(entity_id, [])
            ]
            return {"entity_id": entity_id, "states": entries}
        except Exception as e:
            return {"error": str(e)}

    async def _list_services(self, args: dict, sid: str, tid: str) -> dict:
        domain = args.get("domain")
        search = args.get("search", "").lower()
        limit = args.get("limit", 100)

        services = []
        for dom, svcs in self._hass.services.async_services().items():
            if domain and dom != domain:
                continue
            for svc_name, svc_info in svcs.items():
                desc = (svc_info.get("description") or "").lower()
                if search and search not in svc_name.lower() and search not in desc:
                    continue
                services.append(
                    {
                        "domain": dom,
                        "service": svc_name,
                        "description": svc_info.get("description", ""),
                    }
                )
                if len(services) >= limit:
                    break
            if len(services) >= limit:
                break

        return {"services": services, "truncated": len(services) >= limit}

    async def _get_service(self, args: dict, sid: str, tid: str) -> dict:
        domain = args["domain"]
        service = args["service"]
        services = self._hass.services.async_services()
        svcs = services.get(domain, {})
        svc_info = svcs.get(service)
        if svc_info is None:
            return {"error": f"Service {domain}.{service} not found"}
        return {
            "domain": domain,
            "service": service,
            "description": svc_info.get("description", ""),
            "fields": svc_info.get("fields", {}),
        }

    async def _get_automation(self, args: dict, sid: str, tid: str) -> dict:
        automation_id = args["automation_id"]
        try:
            from homeassistant.components.automation.config import _get_config

            entity_id = f"automation.{automation_id}" if not automation_id.startswith("automation.") else automation_id
            config = await _get_config(self._hass, entity_id)
            return {"automation_id": automation_id, "config": config}
        except Exception as e:
            return {"error": str(e)}

    async def _propose_dashboard_update(
        self, args: dict, sid: str, tid: str
    ) -> dict:
        url_path = args.get("url_path", "lovelace")
        new_config = args.get("new_config", {})
        summary = args.get("summary", "")

        if not isinstance(new_config, dict):
            raise ValueError("new_config must be a dict")
        if "views" not in new_config:
            raise ValueError("new_config must contain a 'views' key")

        diff = self._build_diff(
            json.dumps({}, indent=2),
            json.dumps(new_config, indent=2),
        )

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="dashboard_update",
            summary=summary,
            payload={"url_path": url_path, "config": new_config},
            diff=diff,
            source_tool_use_id=tid,
        )

        store = self._store
        existing = None
        session = store.get(sid)
        if session:
            tkey = _target_key(change)
            for pc in session.pending_changes:
                if pc.status == "pending" and _target_key(pc) == tkey:
                    existing = pc
                    break

        if existing:
            existing.payload = change.payload
            existing.diff = change.diff
            existing.summary = change.summary
            existing.source_tool_use_id = tid
            await store.async_save()
        else:
            await store.add_pending(sid, change)

        return {
            "staged": True,
            "change_id": change.id,
            "kind": "dashboard_update",
            "summary": summary,
            "diff": diff,
        }

    async def _propose_dashboard_view_update(
        self, args: dict, sid: str, tid: str
    ) -> dict:
        url_path = args.get("url_path", "lovelace")
        view_path = args["view_path"]
        new_view = args["new_view"]
        summary = args.get("summary", "")

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="dashboard_view_update",
            summary=summary,
            payload={
                "url_path": url_path,
                "view_path": view_path,
                "view": new_view,
            },
            diff=f"View: {view_path}\n{json.dumps(new_view, indent=2)}",
            source_tool_use_id=tid,
        )

        store = self._store
        existing = None
        session = store.get(sid)
        if session:
            tkey = _target_key(change)
            for pc in session.pending_changes:
                if pc.status == "pending" and _target_key(pc) == tkey:
                    existing = pc
                    break

        if existing:
            existing.payload = change.payload
            existing.diff = change.diff
            existing.summary = change.summary
            existing.source_tool_use_id = tid
            await store.async_save()
        else:
            await store.add_pending(sid, change)

        return {
            "staged": True,
            "change_id": change.id,
            "kind": "dashboard_view_update",
            "summary": summary,
            "diff": change.diff,
        }

    async def _propose_automation_create(
        self, args: dict, sid: str, tid: str
    ) -> dict:
        config = args.get("config", {})
        summary = args.get("summary", "")

        _validate_propose_config(config, "automation_create")

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="automation_create",
            summary=summary,
            payload={"config": config},
            diff=json.dumps(config, indent=2),
            source_tool_use_id=tid,
        )

        await self._store.add_pending(sid, change)
        return {
            "staged": True,
            "change_id": change.id,
            "kind": "automation_create",
            "summary": summary,
            "diff": change.diff,
        }

    async def _propose_automation_update(
        self, args: dict, sid: str, tid: str
    ) -> dict:
        automation_id = args.get("automation_id", "")
        config = args.get("config", {})
        summary = args.get("summary", "")

        if not automation_id:
            raise ValueError("automation_id is required")
        _validate_propose_config(config, "automation_update")

        diff = self._build_diff(
            json.dumps({}, indent=2),
            json.dumps(config, indent=2),
        )

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="automation_update",
            summary=summary,
            payload={"automation_id": automation_id, "config": config},
            diff=diff,
            source_tool_use_id=tid,
        )

        store = self._store
        existing = None
        session = store.get(sid)
        if session:
            tkey = _target_key(change)
            for pc in session.pending_changes:
                if pc.status == "pending" and _target_key(pc) == tkey:
                    existing = pc
                    break

        if existing:
            existing.payload = change.payload
            existing.diff = change.diff
            existing.summary = change.summary
            existing.source_tool_use_id = tid
            await store.async_save()
        else:
            await store.add_pending(sid, change)

        return {
            "staged": True,
            "change_id": change.id,
            "kind": "automation_update",
            "summary": summary,
            "diff": diff,
        }

    async def _propose_automation_delete(
        self, args: dict, sid: str, tid: str
    ) -> dict:
        automation_id = args["automation_id"]
        summary = args.get("summary", "")

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="automation_delete",
            summary=summary,
            payload={"automation_id": automation_id},
            diff=f"Delete automation: {automation_id}",
            source_tool_use_id=tid,
        )

        await self._store.add_pending(sid, change)
        return {
            "staged": True,
            "change_id": change.id,
            "kind": "automation_delete",
            "summary": summary,
        }

    async def _propose_service_call(
        self, args: dict, sid: str, tid: str
    ) -> dict:
        domain = args["domain"]
        service = args["service"]
        service_data = args.get("service_data", {})
        target = args.get("target", {})
        summary = args.get("summary", "")

        payload = {
            "domain": domain,
            "service": service,
            "service_data": service_data,
            "target": target,
        }

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="service_call",
            summary=summary,
            payload=payload,
            diff=json.dumps(payload, indent=2),
            source_tool_use_id=tid,
        )

        await self._store.add_pending(sid, change)
        return {
            "staged": True,
            "change_id": change.id,
            "kind": "service_call",
            "summary": summary,
            "diff": change.diff,
        }
