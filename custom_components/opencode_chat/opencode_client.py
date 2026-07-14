from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Awaitable
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .const import DEFAULT_MAX_TOKENS, MAX_TURNS_PER_REQUEST
from .storage import Message, SessionStore
from .tools import TOOL_DEFINITIONS, ToolRegistry

_LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an in-house assistant inside a Home Assistant installation.

You help the user inspect their smart home, modify their Lovelace dashboards,
and trigger services.

Rules:
- Inspect before you edit. Use list_dashboards / get_dashboard before \
  proposing an update. Use list_entities / get_entity to discover the right \
  entity_ids.
- propose_dashboard_update, propose_service_call, propose_automation_create, \
  propose_automation_update, and propose_automation_delete all STAGE changes \
  for the user to review. They do NOT apply immediately.
- If you propose a revised change for the SAME target, the older pending \
  change is AUTOMATICALLY replaced.
- For automations: use list_automations to find an automation_id, then \
  get_automation to read the full config before propose_automation_update.
- For debugging: list_automation_traces shows recent runs. \
  get_automation_trace shows the full step-by-step. get_state_history \
  shows entity state changes.
- When proposing a dashboard change, use propose_dashboard_view_update. \
  Call get_dashboard(summary=true) for view paths, then get_dashboard_view \
  to fetch the view to modify.
- Prefer minimal changes.
- Be concise. Use markdown for tables and code blocks.

AVAILABLE TOOLS:
{TOOL_DESCRIPTIONS}

When you need to use a tool, output it as a JSON code block with exactly \
this format:

```tool_call
{{"name": "tool_name", "arguments": {{"arg1": "val1"}}}}
```

Then wait for the result. You can call multiple tools in sequence. \
Each tool_call must be on its own line in its own code block.
"""

TITLE_PROMPT = (
    "Summarize this user request as a 4-6 word chat title. No quotes, no "
    "punctuation at the end, sentence case. Just the title."
)

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]


def _build_tool_descriptions() -> str:
    lines = []
    for t in TOOL_DEFINITIONS:
        name = t["name"]
        desc = t["description"]
        props = t.get("input_schema", {}).get("properties", {})
        params = []
        for pname, pinfo in props.items():
            required = pname in t.get("input_schema", {}).get("required", [])
            marker = "*" if required else ""
            ptype = pinfo.get("type", "string")
            params.append(f"  {marker}{pname} ({ptype})")
        lines.append(f"- {name}: {desc}")
        if params:
            lines.extend(params)
        lines.append("")
    return "\n".join(lines)


def _parse_tool_calls(text: str) -> list[dict[str, Any]]:
    """Parse tool_call JSON code blocks from LLM response text."""
    calls = []
    lines = text.split("\n")
    in_block = False
    buffer = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```tool_call"):
            in_block = True
            buffer = ""
        elif in_block and stripped.startswith("```"):
            in_block = False
            try:
                parsed = json.loads(buffer)
                if isinstance(parsed, dict) and "name" in parsed:
                    calls.append(parsed)
            except json.JSONDecodeError:
                pass
            buffer = ""
        elif in_block:
            buffer += line + "\n"
    return calls


class OpenCodeClient:
    def __init__(
        self,
        url: str,
        password: str,
        model: str,
        agent: str,
        tools: ToolRegistry,
    ) -> None:
        self._url = url.rstrip("/")
        self._password = password
        self._default_model = model
        self._default_agent = agent
        self._tools = tools
        self._tool_descriptions = _build_tool_descriptions()
        self._auth_header: dict[str, str] = {}
        if password:
            import base64
            creds = base64.b64encode(f"opencode:{password}".encode()).decode()
            self._auth_header = {"Authorization": f"Basic {creds}"}

    def _request(
        self, method: str, path: str, body: dict | None = None, timeout: int = 60
    ) -> Any:
        import urllib.request

        url = f"{self._url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method, headers=self._auth_header
        )
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except HTTPError as e:
            body_text = e.read().decode()
            _LOGGER.error("OpenCode API error %s %s: %s", method, path, body_text)
            raise

    def health(self) -> dict:
        return self._request("GET", "/global/health")

    def create_session(self) -> dict:
        return self._request("POST", "/session", {"title": "HA Chat"})

    def list_sessions(self) -> list:
        return self._request("GET", "/session")

    def delete_session(self, session_id: str) -> bool:
        return self._request("DELETE", f"/session/{session_id}")

    def send_message(
        self,
        session_id: str,
        content: str,
        system: str | None = None,
        tools: list | None = None,
        agent: str | None = None,
        no_reply: bool = False,
    ) -> dict:
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": content}],
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if agent:
            body["agent"] = agent
        if no_reply:
            return self._request("POST", f"/session/{session_id}/prompt_async", body)
        return self._request(
            "POST", f"/session/{session_id}/message", body, timeout=120
        )

    def get_messages(self, session_id: str, limit: int = 50) -> list:
        return self._request("GET", f"/session/{session_id}/message?limit={limit}")

    async def summarize_title(self, user_text: str) -> str:
        """Quick call to title a chat session via a temp session."""
        try:
            sess = await asyncio.get_event_loop().run_in_executor(
                None, self.create_session
            )
            sess_id = sess["id"]
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.send_message(
                    sess_id,
                    f"{TITLE_PROMPT}\n\n{user_text[:500]}",
                    system="You generate short chat titles. Respond with only the title, no quotes.",
                    agent=self._default_agent or None,
                ),
            )
            title = ""
            for part in resp.get("parts", []):
                if part.get("type") == "text":
                    title = part.get("text", "").strip().strip("\"'.,!?")[:60]
                    break
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.delete_session(sess_id)
            )
            return title
        except Exception:
            _LOGGER.exception("Title summarization failed")
            return ""

    async def stream_chat(
        self,
        history: list[Message],
        session_id: str,
        emit: EventEmitter,
        opencode_sid: str | None = None,
        model: str | None = None,
        agent: str | None = None,
    ) -> list[Message]:
        """Run a tool-use loop, using OpenCode as the LLM backend.

        Returns the new messages appended (assistant turns + tool_result turns).
        """
        active_model = model or self._default_model
        active_agent = agent or self._default_agent
        new_messages: list[Message] = []

        if not opencode_sid:
            sess = await asyncio.get_event_loop().run_in_executor(
                None, self.create_session
            )
            opencode_sid = sess["id"]

        state_block = _session_state_block(self._tools.store, session_id)
        system_text = SYSTEM_PROMPT.format(
            TOOL_DESCRIPTIONS=self._tool_descriptions
        )
        if state_block:
            system_text += f"\n\n[Session state]\n{state_block}\n"

        api_messages = _history_to_text(history)

        for turn in range(MAX_TURNS_PER_REQUEST):
            full_prompt = system_text + "\n\n"
            for msg in api_messages:
                full_prompt += f"{msg['role']}: {msg['content']}\n\n"
            full_prompt += "assistant:"

            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.send_message(
                    opencode_sid,
                    full_prompt,
                    agent=active_agent or None,
                ),
            )

            text_content = ""
            tool_calls: list[dict] = []
            for part in resp.get("parts", []):
                if part.get("type") == "text":
                    chunk = part.get("text", "")
                    text_content += chunk
                    await emit({"type": "text_delta", "text": chunk})

            tool_calls = _parse_tool_calls(text_content)

            clean_text = _remove_tool_call_blocks(text_content)
            assistant_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": clean_text}
            ]

            if not tool_calls:
                await emit({"type": "turn_complete"})
                assistant_msg = Message(
                    role="assistant", content=assistant_blocks
                )
                new_messages.append(assistant_msg)
                break

            for tc in tool_calls:
                await emit(
                    {
                        "type": "tool_use_start",
                        "id": uuid.uuid4().hex,
                        "name": tc.get("name", ""),
                    }
                )
                tc_id = uuid.uuid4().hex
                result = await self._tools.call(
                    tc.get("name", ""),
                    tc.get("arguments", {}),
                    session_id,
                    tc_id,
                )
                await emit(
                    {
                        "type": "tool_result",
                        "id": tc_id,
                        "name": tc.get("name", ""),
                        "result": result,
                    }
                )
                assistant_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc_id,
                        "name": tc.get("name", ""),
                        "input": tc.get("arguments", {}),
                    }
                )
                api_messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(tc.get("arguments", {})),
                    }
                )
                api_messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(result, default=str),
                    }
                )

            assistant_msg = Message(role="assistant", content=assistant_blocks)
            new_messages.append(assistant_msg)
        else:
            await emit(
                {
                    "type": "error",
                    "error": f"Hit max turn limit ({MAX_TURNS_PER_REQUEST})",
                }
            )

        return new_messages, opencode_sid


def _session_state_block(store, session_id: str) -> str:
    session = store.get(session_id) if store else None
    if not session or not session.pending_changes:
        return ""
    icon = {"accepted": "\u2713", "rejected": "\u2717", "pending": "\u23f3"}
    lines = ["[Session state - status of past proposals]"]
    for c in session.pending_changes:
        prefix = icon.get(c.status, "?")
        lines.append(f"{prefix} {c.status}: {c.summary} ({c.kind})")
    return "\n".join(lines)


def _history_to_text(history: list[Message]) -> list[dict[str, str]]:
    out = []
    for m in history:
        text_parts = []
        for block in m.content:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                text_parts.append(
                    f"[Called tool: {block.get('name')} with args {json.dumps(block.get('input', {}))}]"
                )
            elif block.get("type") == "tool_result":
                text_parts.append(
                    f"[Tool result: {block.get('content', '')[:500]}]"
                )
        content = "\n".join(text_parts)
        out.append({"role": m.role, "content": content})
    return out


def _remove_tool_call_blocks(text: str) -> str:
    import re
    return re.sub(
        r"```tool_call\n.*?\n```", "", text, flags=re.DOTALL
    ).strip()
