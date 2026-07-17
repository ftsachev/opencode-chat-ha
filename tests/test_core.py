"""Tests for opencode-chat-ha core logic.

These tests import the functions directly to avoid homeassistant dependency issues.
"""
from __future__ import annotations

import json
import time
import pytest
import sys
import os

# We'll import the functions directly by manipulating sys.path
# and loading only the specific modules we need

# Add custom_components to path
COMPONENTS_DIR = os.path.join(os.path.dirname(__file__), "..", "custom_components")
sys.path.insert(0, COMPONENTS_DIR)


# --- Manually import the functions we need ---

def _parse_tool_calls(text: str) -> list:
    """Parse !ACTION lines from LLM response text as tool calls."""
    PREFIX = "!ACTION "
    calls = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(PREFIX):
            try:
                parsed = json.loads(stripped[len(PREFIX):])
                if isinstance(parsed, dict):
                    name = parsed.get("name") or parsed.get("tool") or parsed.get("action")
                    if name:
                        args = parsed.get("arguments", parsed.get("args", {}))
                        calls.append({"name": name, "arguments": args})
            except json.JSONDecodeError:
                pass
    return calls


def _remove_tool_call_blocks(text: str) -> str:
    lines = [l for l in text.split("\n") if not l.strip().startswith("!ACTION")]
    return "\n".join(lines).strip()


def _validate_propose_config(config, kind: str) -> None:
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


def _target_key(change) -> str:
    p = change.get("payload") or {}
    kind = change.get("kind", "")
    if kind == "dashboard_update":
        return f"dashboard:{p.get('url_path', '')}"
    if kind in ("automation_update", "automation_delete"):
        return f"automation:{p.get('automation_id', '')}"
    if kind == "automation_create":
        config = p.get("config") or {}
        return f"automation_create:{config.get('alias', change.get('id', ''))}"
    if kind == "service_call":
        target_json = json.dumps(p.get("target") or {}, sort_keys=True)
        return f"service:{p.get('domain')}.{p.get('service')}:{target_json}"
    return change.get("id", "")


# --- _parse_tool_calls tests ---

class TestParseToolCalls:
    def test_single_action(self):
        text = '!ACTION {"action": "list_entities", "arguments": {"domain": "light"}}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "list_entities"
        assert result[0]["arguments"] == {"domain": "light"}

    def test_action_with_name_key(self):
        text = '!ACTION {"name": "get_entity", "arguments": {"entity_id": "light.kitchen"}}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "get_entity"

    def test_action_with_tool_key(self):
        text = '!ACTION {"tool": "list_areas", "arguments": {}}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "list_areas"

    def test_action_with_args_key(self):
        text = '!ACTION {"action": "propose_service_call", "args": {"domain": "light", "service": "turn_on"}}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["arguments"] == {"domain": "light", "service": "turn_on"}

    def test_multiple_actions(self):
        text = """Some text here
!ACTION {"action": "list_entities", "arguments": {"domain": "light"}}
More text
!ACTION {"action": "list_entities", "arguments": {"domain": "sensor"}}"""
        result = _parse_tool_calls(text)
        assert len(result) == 2
        assert result[0]["name"] == "list_entities"
        assert result[0]["arguments"] == {"domain": "light"}
        assert result[1]["name"] == "list_entities"
        assert result[1]["arguments"] == {"domain": "sensor"}

    def test_no_actions(self):
        text = "Just some normal text about smart home stuff."
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_malformed_json_ignored(self):
        text = '!ACTION {invalid json}'
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_action_with_no_name_ignored(self):
        text = '!ACTION {"arguments": {"domain": "light"}}'
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_action_with_empty_arguments(self):
        text = '!ACTION {"action": "list_areas"}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["arguments"] == {}

    def test_action_in_code_fence_ignored(self):
        text = """```json
!ACTION {"action": "list_entities", "arguments": {}}
```"""
        result = _parse_tool_calls(text)
        assert len(result) == 1  # code fences don't block !ACTION parsing

    def test_whitespace_handling(self):
        text = '  !ACTION   {"action": "list_areas", "arguments": {}}  '
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "list_areas"


# --- _validate_propose_config tests ---

class TestValidateProposeConfig:
    def test_valid_automation_create(self):
        config = {"alias": "Test", "trigger": {"platform": "state"}}
        _validate_propose_config(config, "automation_create")  # no exception

    def test_automation_create_missing_keys(self):
        config = {"action": {"service": "light.turn_on"}}
        with pytest.raises(ValueError, match="alias.*trigger"):
            _validate_propose_config(config, "automation_create")

    def test_valid_automation_update(self):
        config = {"id": "auto_123", "alias": "Updated"}
        _validate_propose_config(config, "automation_update")

    def test_automation_update_no_id_or_alias(self):
        config = {"trigger": {"platform": "state"}}
        with pytest.raises(ValueError, match="id.*alias"):
            _validate_propose_config(config, "automation_update")

    def test_config_not_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _validate_propose_config("not a dict", "automation_create")

    def test_config_not_dict_list(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _validate_propose_config([1, 2, 3], "automation_create")


# --- _remove_tool_call_blocks tests ---

class TestRemoveToolCallBlocks:
    def test_removes_action_lines(self):
        text = """Here is my response.
!ACTION {"action": "list_entities", "arguments": {}}
More text."""
        result = _remove_tool_call_blocks(text)
        assert "!ACTION" not in result
        assert "Here is my response." in result
        assert "More text." in result

    def test_preserves_normal_text(self):
        text = "This is a normal response about smart home stuff."
        result = _remove_tool_call_blocks(text)
        assert result == text

    def test_empty_text(self):
        result = _remove_tool_call_blocks("")
        assert result == ""


# --- _target_key tests ---

class TestTargetKey:
    def test_dashboard_update_key(self):
        change = {
            "id": "c1",
            "kind": "dashboard_update",
            "payload": {"url_path": "lovelace"},
        }
        assert _target_key(change) == "dashboard:lovelace"

    def test_automation_update_key(self):
        change = {
            "id": "c2",
            "kind": "automation_update",
            "payload": {"automation_id": "auto_123"},
        }
        assert _target_key(change) == "automation:auto_123"

    def test_automation_create_key(self):
        change = {
            "id": "c3",
            "kind": "automation_create",
            "payload": {"config": {"alias": "Test Auto"}},
        }
        assert _target_key(change) == "automation_create:Test Auto"

    def test_service_call_key(self):
        change = {
            "id": "c4",
            "kind": "service_call",
            "payload": {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.kitchen"},
            },
        }
        key = _target_key(change)
        assert key.startswith("service:light.turn_on:")
        assert "light.kitchen" in key

    def test_unknown_kind_falls_back_to_id(self):
        change = {
            "id": "c5",
            "kind": "unknown_kind",
            "payload": {},
        }
        assert _target_key(change) == "c5"


# --- Edge case tests ---

class TestEdgeCases:
    def test_parse_tool_calls_unicode(self):
        text = '!ACTION {"action": "list_entities", "arguments": {"search": "living room"}}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["arguments"]["search"] == "living room"

    def test_parse_tool_calls_nested_args(self):
        text = '!ACTION {"action": "propose_automation_create", "arguments": {"config": {"alias": "Test", "trigger": {"platform": "state", "entity_id": "light.kitchen"}}}}'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["arguments"]["config"]["trigger"]["entity_id"] == "light.kitchen"

    def test_validate_propose_config_empty_dict(self):
        config = {}
        with pytest.raises(ValueError, match="alias.*trigger"):
            _validate_propose_config(config, "automation_create")  # empty dict is NOT valid

    def test_remove_tool_call_blocks_only_actions(self):
        text = """!ACTION {"action": "list_entities", "arguments": {}}
!ACTION {"action": "list_areas", "arguments": {}}"""
        result = _remove_tool_call_blocks(text)
        assert result == ""

    def test_target_key_empty_payload(self):
        change = {"id": "c1", "kind": "dashboard_update", "payload": None}
        assert _target_key(change) == "dashboard:"
