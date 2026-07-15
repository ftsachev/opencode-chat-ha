from __future__ import annotations

import asyncio
import json
import logging
import re
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
- Inspect before you edit.
- propose_* actions STAGE changes for user review, they do NOT apply immediately.
- Prefer minimal changes.
- Be concise. Use markdown for tables and code blocks.

The following external actions are available. When you need to use one, put
this EXACT format on its own line (no code block, no backticks):

!ACTION {{"action": "name", "arguments": {{}}}}

The system parses this line and executes the action. Do NOT wrap it in code
fences — just put it inline on its own line. Wait for the result before
continuing. You can request multiple actions in sequence.
IMPORTANT: Once a tool has been called and returned a result, DO NOT call
it again. Use the result you already received. If `truncated: true`,
note there are more results but answer with what you have. For
list_entities use the default limit (20) — the total count is always
accurate regardless of limit. Avoid large limits.

Available external actions:
{TOOL_DESCRIPTIONS}
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
        required = t.get("input_schema", {}).get("required", [])
        params = []
        for pname, pinfo in props.items():
            req_marker = "*" if pname in required else ""
            ptype = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            default = pinfo.get("default")
            parts = f"  {req_marker}{pname} ({ptype})"
            if pdesc:
                parts += f" — {pdesc}"
            if default is not None:
                parts += f" [default: {default}]"
            params.append(parts)
        lines.append(f"- {name}: {desc}")
        if params:
            lines.extend(params)
        lines.append("")
    return "\n".join(lines)


def _parse_tool_calls(text: str) -> list[dict[str, Any]]:
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


def _extract_text_from_assistant(messages: list) -> str | None:
    for m in messages:
        if m.get("type") == "assistant":
            content = m.get("content", [])
            texts = []
            for part in content:
                if part.get("type") == "text":
                    t = part.get("text", "")
                    if t.strip():
                        texts.append(t)
            if texts:
                return "\n".join(texts)
    return None


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
        self, method: str, path: str, body: dict | None = None, timeout: int = 120
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
            raw = resp.read()
            return json.loads(raw) if raw else {}
        except HTTPError as e:
            body_text = e.read().decode()
            _LOGGER.error("OpenCode API error %s %s: %s", method, path, body_text)
            raise
        except json.JSONDecodeError:
            _LOGGER.warning("Non-JSON response from %s %s", method, path)
            return {}

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def create_session(self) -> dict:
        resp = self._request("POST", "/api/session", {"title": "HA Chat"})
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    def list_sessions(self) -> list:
        resp = self._request("GET", "/api/session")
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp if isinstance(resp, list) else []

    def delete_session(self, session_id: str) -> bool:
        try:
            self._request("DELETE", f"/api/session/{session_id}")
            return True
        except Exception:
            return False

    def send_prompt(self, session_id: str, text: str) -> dict:
        return self._request(
            "POST",
            f"/api/session/{session_id}/prompt",
            {"prompt": {"text": text}},
            timeout=30,
        )

    def get_messages(self, session_id: str, limit: int = 50) -> list:
        resp = self._request(
            "GET", f"/api/session/{session_id}/message?limit={limit}"
        )
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp if isinstance(resp, list) else []

    def _poll_final_response(
        self, session_id: str, poll_interval: float = 1.0, max_wait: float = 120.0
    ) -> dict | None:
        """Poll until messages stabilize, then return the latest assistant message.
        Waits for the AI to finish all tool executions before returning.
        Messages are newest-first; latest assistant message is msgs[0] if type matches."""
        deadline = time.monotonic() + max_wait
        stable_for = 0.0
        last_count = len(self.get_messages(session_id))

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            msgs = self.get_messages(session_id)
            cur = len(msgs)
            if cur == last_count:
                stable_for += poll_interval
                # 3s stability: message count unchanged = AI done generating/executing
                if stable_for >= 3.0:
                    for m in msgs:
                        if m.get("type") == "assistant":
                            return m
                    return None
            else:
                last_count = cur
                stable_for = 0.0
        return None

    async def summarize_title(self, user_text: str) -> str:
        """Quick call to title a chat session via a temp session."""
        try:
            sess = await asyncio.get_event_loop().run_in_executor(
                None, self.create_session
            )
            sess_id = sess.get("id") or sess.get("sessionID", "")
            if not sess_id:
                return ""

            prompt_text = f"{TITLE_PROMPT}\n\n{user_text[:500]}"
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.send_prompt(sess_id, prompt_text)
            )

            resp_msg = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._poll_final_response(sess_id, max_wait=30.0)
            )

            title = ""
            if resp_msg:
                text = _extract_text_from_assistant([resp_msg])
                if text:
                    title = text.strip().strip("\"'.,!?")[:60]

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

        Uses the polling-based /prompt API. Returns the new messages
        appended (assistant turns + tool_result turns).
        """
        active_agent = agent or self._default_agent
        new_messages: list[Message] = []

        if not opencode_sid:
            sess = await asyncio.get_event_loop().run_in_executor(
                None, self.create_session
            )
            opencode_sid = sess.get("id") or sess.get("sessionID", "")

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

            _LOGGER.debug("Sending prompt turn %d to session %s", turn, opencode_sid)
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.send_prompt(opencode_sid, full_prompt)
            )

            assistant_msg = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._poll_final_response(opencode_sid)
            )

            if not assistant_msg:
                await emit({"type": "error", "error": "No response from AI"})
                break

            text_content = _extract_text_from_assistant([assistant_msg]) or ""
            if text_content:
                await emit({"type": "text_delta", "text": text_content})

            tool_calls = _parse_tool_calls(text_content)
            clean_text = _remove_tool_call_blocks(text_content)
            assistant_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": clean_text}
            ]

            if not tool_calls:
                await emit({"type": "turn_complete"})
                assistant_msg_obj = Message(
                    role="assistant", content=assistant_blocks
                )
                new_messages.append(assistant_msg_obj)
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
                result_text = json.dumps(result, default=str)
                clean_text += f"\n  [Called tool: {tc.get('name', '')} with args {json.dumps(tc.get('arguments', {}))}]"
                api_messages.append(
                    {
                        "role": "assistant",
                        "content": f"  [Tool result for {tc.get('name', '')}]: {result_text}",
                    }
                )

            api_messages.insert(
                -len(tool_calls),
                {"role": "assistant", "content": clean_text},
            )

            assistant_msg_obj = Message(role="assistant", content=assistant_blocks)
            new_messages.append(assistant_msg_obj)
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
    text = re.sub(
        r"```tool_call\n.*?\n```", "", text, flags=re.DOTALL
    )
    text = re.sub(
        r"```json\n.*?\n```", "", text, flags=re.DOTALL
    )
    lines = [l for l in text.split("\n") if not l.strip().startswith("!ACTION")]
    return "\n".join(lines).strip()
