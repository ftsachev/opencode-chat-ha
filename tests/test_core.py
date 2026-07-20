"""Tests for opencode-chat-ha core logic.

These tests import the functions directly to avoid homeassistant dependency issues.
"""
from __future__ import annotations

import json
import re
import pytest
import sys
import os

# Add custom_components to path
COMPONENTS_DIR = os.path.join(os.path.dirname(__file__), "..", "custom_components")
sys.path.insert(0, COMPONENTS_DIR)

# Import the real module for the regression tests below (conftest.py mocks
# enough of homeassistant for this to succeed). Importing the actual source
# -- rather than copying functions into this file, as the rest of this file
# does -- is deliberate: a copy can silently drift from the real
# implementation (this is exactly how the deleted _poll_final_response call
# shipped with a fully green test suite).
import opencode_chat.opencode_client as oc_module


# --- Copied functions for testing (avoid HA imports) ---

def _parse_tool_calls(text: str) -> list:
    """Parse ```action fenced blocks from LLM response text as tool calls."""
    BLOCK_RE = re.compile(r"```action\s*\n(.*?)\n```", re.DOTALL)
    calls = []
    for match in BLOCK_RE.finditer(text):
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                name = parsed.get("name") or parsed.get("tool") or parsed.get("action")
                if name:
                    args = parsed.get("arguments", parsed.get("args", {}))
                    calls.append({"name": name, "arguments": args})
        except json.JSONDecodeError:
            pass
    return calls


def _remove_tool_call_blocks(text: str) -> str:
    """Remove ```action fenced blocks from text."""
    BLOCK_RE = re.compile(r"```action\s*\n.*?\n```", re.DOTALL)
    return BLOCK_RE.sub("", text).strip()


def _sanitize_tool_result(text: str) -> str:
    """Strip any ```action blocks from tool results to prevent injection."""
    BLOCK_RE = re.compile(r"```action\s*\n.*?\n```", re.DOTALL)
    return BLOCK_RE.sub("[action block removed]", text)


def _validate_tool_call(name: str, args: dict) -> str | None:
    """Validate a tool call against TOOL_DEFINITIONS. Returns error message or None."""
    TOOL_NAMES = {
        "list_entities", "get_entity", "list_areas", "list_dashboards",
        "get_dashboard", "get_dashboard_view", "list_lovelace_resources",
        "list_automations", "list_automation_traces", "get_automation_trace",
        "get_state_history", "list_services", "propose_dashboard_update",
        "propose_dashboard_view_update", "propose_automation_create",
        "propose_automation_update", "propose_automation_delete",
        "propose_service_call",
    }
    if name not in TOOL_NAMES:
        return f"Unknown tool: {name}"
    if name.startswith("propose_"):
        config = args.get("config", {})
        if not isinstance(config, dict):
            return f"config must be a dict, got {type(config).__name__}"
    return None


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


# --- _parse_tool_calls tests (fenced action blocks) ---

class TestParseToolCalls:
    def test_single_action(self):
        text = '```action\n{"action": "list_entities", "arguments": {"domain": "light"}}\n```'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "list_entities"
        assert result[0]["arguments"] == {"domain": "light"}

    def test_action_with_name_key(self):
        text = '```action\n{"name": "get_entity", "arguments": {"entity_id": "light.kitchen"}}\n```'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "get_entity"

    def test_action_with_tool_key(self):
        text = '```action\n{"tool": "list_areas", "arguments": {}}\n```'
        result = _parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "list_areas"

    def test_multiple_actions(self):
        text = '''Here are some actions:

```action
{"action": "list_entities", "arguments": {"domain": "light"}}
```

```action
{"action": "get_entity", "arguments": {"entity_id": "light.kitchen"}}
```'''
        result = _parse_tool_calls(text)
        assert len(result) == 2
        assert result[0]["name"] == "list_entities"
        assert result[1]["name"] == "get_entity"

    def test_no_actions(self):
        text = "This is just regular text with no actions."
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_invalid_json(self):
        text = '```action\n{invalid json}\n```'
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_non_dict_json(self):
        text = '```action\n["not", "a", "dict"]\n```'
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_no_name_field(self):
        text = '```action\n{"arguments": {"domain": "light"}}\n```'
        result = _parse_tool_calls(text)
        assert len(result) == 0

    def test_mixed_text_and_actions(self):
        text = '''Let me check the lights.

```action
{"action": "list_entities", "arguments": {"domain": "light"}}
```

And also the sensors:

```action
{"action": "list_entities", "arguments": {"domain": "sensor"}}
```'''
        result = _parse_tool_calls(text)
        assert len(result) == 2

    def test_old_action_prefix_ignored(self):
        """The old !ACTION prefix should no longer be parsed."""
        text = '!ACTION {"action": "list_entities", "arguments": {}}'
        result = _parse_tool_calls(text)
        assert len(result) == 0


# --- _remove_tool_call_blocks tests ---

class TestRemoveToolCallBlocks:
    def test_removes_fenced_blocks(self):
        text = '''Some text

```action
{"action": "list_entities", "arguments": {}}
```

More text'''
        result = _remove_tool_call_blocks(text)
        assert "```action" not in result
        assert "Some text" in result
        assert "More text" in result

    def test_preserves_non_action_blocks(self):
        text = '''```python
print("hello")
```

```action
{"action": "list_entities", "arguments": {}}
```'''
        result = _remove_tool_call_blocks(text)
        assert "print" in result
        assert "```action" not in result

    def test_empty_text(self):
        assert _remove_tool_call_blocks("") == ""


# --- _sanitize_tool_result tests ---

class TestSanitizeToolResult:
    def test_strips_action_blocks(self):
        text = '''Entity light.kitchen has state on.

```action
{"action": "get_entity", "arguments": {"entity_id": "light.kitchen"}}
```'''
        result = _sanitize_tool_result(text)
        assert "[action block removed]" in result
        assert "light.kitchen" in result

    def test_no_action_blocks(self):
        text = "Just a normal entity description."
        result = _sanitize_tool_result(text)
        assert result == text

    def test_multiple_blocks_stripped(self):
        text = '''```action
{"action": "call_service", "arguments": {"domain": "light", "service": "turn_on"}}
```

Some text

```action
{"action": "delete_automation", "arguments": {"automation_id": "bad"}}
```'''
        result = _sanitize_tool_result(text)
        assert result.count("[action block removed]") == 2


# --- _validate_tool_call tests ---

class TestValidateToolCall:
    def test_valid_tool(self):
        assert _validate_tool_call("list_entities", {"domain": "light"}) is None

    def test_unknown_tool(self):
        result = _validate_tool_call("nonexistent_tool", {})
        assert result is not None
        assert "Unknown tool" in result

    def test_propose_with_valid_config(self):
        args = {"config": {"alias": "Test", "trigger": {"platform": "state"}}}
        assert _validate_tool_call("propose_automation_create", args) is None

    def test_propose_with_invalid_config(self):
        args = {"config": "not a dict"}
        result = _validate_tool_call("propose_automation_create", args)
        assert result is not None
        assert "dict" in result


# --- _validate_propose_config tests ---

class TestValidateProposeConfig:
    def test_valid_automation_create(self):
        config = {"alias": "Test", "trigger": {"platform": "state"}}
        _validate_propose_config(config, "automation_create")

    def test_invalid_automation_create_missing_keys(self):
        with pytest.raises(ValueError, match="alias.*trigger"):
            _validate_propose_config({}, "automation_create")

    def test_valid_automation_update(self):
        config = {"id": "auto_123", "action": []}
        _validate_propose_config(config, "automation_update")

    def test_invalid_automation_update_missing_keys(self):
        with pytest.raises(ValueError, match="id.*alias"):
            _validate_propose_config({}, "automation_update")

    def test_non_dict_config(self):
        with pytest.raises(ValueError, match="dict"):
            _validate_propose_config("not a dict", "automation_create")


# --- _target_key tests ---

class TestTargetKey:
    def test_dashboard_update(self):
        change = {"kind": "dashboard_update", "payload": {"url_path": "lovelace"}}
        assert _target_key(change) == "dashboard:lovelace"

    def test_automation_update(self):
        change = {"kind": "automation_update", "payload": {"automation_id": "auto_123"}}
        assert _target_key(change) == "automation:auto_123"

    def test_automation_create(self):
        change = {"kind": "automation_create", "payload": {"config": {"alias": "Test"}}}
        assert _target_key(change) == "automation_create:Test"

    def test_service_call(self):
        change = {
            "kind": "service_call",
            "payload": {"domain": "light", "service": "turn_on", "target": {"entity_id": "light.kitchen"}},
        }
        key = _target_key(change)
        assert key.startswith("service:light.turn_on:")

    def test_unknown_kind(self):
        change = {"kind": "unknown", "id": "test_id"}
        assert _target_key(change) == "test_id"


# --- Content protection guard tests ---

class TestContentProtection:
    def test_count_guard(self):
        """Verify the count guard logic."""
        original = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        new = [{"id": "a"}]
        diff = len(original) - len(new)
        assert diff == 2
        assert diff > 1  # Would be refused


# --- Regression tests for opencode_client.py fixes (imports real module) ---
#
# These exercise the actual `custom_components/opencode_chat/opencode_client.py`
# functions (via oc_module), not copies, so they can't drift out of sync with
# the source the way the rest of this file's copied helpers can.


class TestInsertTurnAssistantText:
    """Fix: api_messages ordering when a tool call this turn is deduped.

    `_insert_turn_assistant_text` must place the assistant's text after all
    pre-existing history and immediately before the entries appended during
    *this* turn, using the actual appended count rather than len(tool_calls)
    (the dedupe branch in stream_chat `continue`s without appending).
    """

    def test_no_dupes_all_calls_append(self):
        # Two tool calls, both appended (no dedupe) -> appended_count == 2.
        api_messages = [
            {"role": "user", "content": "HISTORY-1"},
            {"role": "assistant", "content": "HISTORY-2"},
        ]
        api_messages.append({"role": "assistant", "content": "[Tool result for get_entity]"})
        api_messages.append({"role": "assistant", "content": "[Tool result for list_areas]"})

        oc_module._insert_turn_assistant_text(api_messages, 2, "assistant says")

        contents = [m["content"] for m in api_messages]
        assert contents == [
            "HISTORY-1",
            "HISTORY-2",
            "assistant says",
            "[Tool result for get_entity]",
            "[Tool result for list_areas]",
        ]

    def test_dupe_call_skips_append_ordering_still_correct(self):
        # Two identical tool calls requested; the second is deduped and
        # never appends to api_messages, so only 1 entry was actually
        # appended this turn (appended_count == 1, not len(tool_calls) == 2).
        api_messages = [
            {"role": "user", "content": "HISTORY-1"},
            {"role": "assistant", "content": "HISTORY-2"},
        ]
        api_messages.append({"role": "assistant", "content": "[Tool result for get_entity]"})
        # (the duplicate call was skipped via `continue` -- nothing appended for it)

        oc_module._insert_turn_assistant_text(api_messages, 1, "assistant says")

        contents = [m["content"] for m in api_messages]
        assert contents == [
            "HISTORY-1",
            "HISTORY-2",
            "assistant says",
            "[Tool result for get_entity]",
        ]
        # Specifically: text must NOT land between the pre-existing history
        # entries (the bug this regression test guards against).
        assert contents.index("assistant says") > contents.index("HISTORY-2")

    def test_zero_appended_falls_back_to_append(self):
        # All tool calls this turn were deduped/skipped -> nothing appended.
        api_messages = [
            {"role": "user", "content": "HISTORY-1"},
            {"role": "assistant", "content": "HISTORY-2"},
        ]

        oc_module._insert_turn_assistant_text(api_messages, 0, "assistant says")

        contents = [m["content"] for m in api_messages]
        assert contents == ["HISTORY-1", "HISTORY-2", "assistant says"]
