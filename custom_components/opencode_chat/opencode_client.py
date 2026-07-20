from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Awaitable
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .const import DEFAULT_MAX_TOKENS, MAX_TURNS_PER_REQUEST
from .storage import Message, SessionStore
from .tools import TOOL_DEFINITIONS, ToolRegistry, _validate_propose_config

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
this EXACT format on its own line inside a fenced code block:

```action
{"action": "name", "arguments": {}}
```

The system parses this block and executes the action. Wait for the result
before continuing. You can request multiple actions in sequence.
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


TOOL_DESCRIPTIONS = _build_tool_descriptions()


def _parse_tool_calls(text: str) -> list[dict[str, Any]]:
    """Parse ```action fenced blocks from LLM response text as tool calls."""
    import re
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
        self._auth_header: dict[str, str] = {}
        if password:
            import base64
            creds = base64.b64encode(f"opencode:{password}".encode()).decode()
            self._auth_header = {"Authorization": f"Basic {creds}"}

    def _request(
        self, method: str, path: str, body: dict | None = None, timeout: int = 120,
        retries: int = 2, retry_delay: float = 1.0,
    ) -> Any:
        url = f"{self._url}{path}"
        data = json.dumps(body).encode() if body else None
        last_error = None

        for attempt in range(retries + 1):
            req = Request(
                url, data=data, method=method, headers=self._auth_header
            )
            if body:
                req.add_header("Content-Type", "application/json")
            try:
                resp = urlopen(req, timeout=timeout)
                raw = resp.read()
                return json.loads(raw) if raw else {}
            except HTTPError as e:
                last_error = e
                # Retry on 5xx (server errors) and 429 (rate limit), not on 4xx (client errors)
                if e.code < 500 and e.code != 429:
                    body_text = e.read().decode()
                    _LOGGER.error("OpenCode API error %s %s: %s", method, path, body_text)
                    raise
                if attempt < retries:
                    _LOGGER.warning(
                        "OpenCode API %s %s returned %d, retrying in %.1fs (%d/%d)",
                        method, path, e.code, retry_delay, attempt + 1, retries,
                    )
                    time.sleep(retry_delay)
                    continue
                body_text = e.read().decode()
                _LOGGER.error("OpenCode API error %s %s: %s", method, path, body_text)
                raise
            except (URLError, OSError) as e:
                last_error = e
                if attempt < retries:
                    _LOGGER.warning(
                        "OpenCode API %s %s network error: %s, retrying in %.1fs (%d/%d)",
                        method, path, e, retry_delay, attempt + 1, retries,
                    )
                    time.sleep(retry_delay)
                    continue
                raise
            except json.JSONDecodeError:
                _LOGGER.warning("Non-JSON response from %s %s", method, path)
                return {}
        raise last_error  # should not reach, but satisfies type checker

    def health(self) -> dict:
        return self._request("GET", "/global/health")

    def create_session(self) -> dict:
        resp = self._request("POST", "/session", {"title": "HA Chat"})
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    def list_sessions(self) -> list:
        resp = self._request("GET", "/session")
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp if isinstance(resp, list) else []

    def delete_session(self, session_id: str) -> bool:
        try:
            self._request("DELETE", f"/session/{session_id}")
            return True
        except Exception:
            return False

    def send_prompt(self, session_id: str, text: str) -> dict:
        """Send a prompt and wait for the response (synchronous)."""
        return self._request(
            "POST",
            f"/session/{session_id}/message",
            {"message": text},
            timeout=120,
        )

    def get_messages(self, session_id: str, limit: int = 50) -> list:
        resp = self._request(
            "GET", f"/session/{session_id}/message?limit={limit}"
        )
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp if isinstance(resp, list) else []

    async def summarize_title(self, user_text: str) -> str:
        """Quick call to title a chat session via a temp session."""
        try:
            loop = asyncio.get_running_loop()
            sess = await loop.run_in_executor(None, self.create_session)
            sess_id = sess.get("id") or sess.get("sessionID", "")
            if not sess_id:
                return ""

            prompt_text = f"{TITLE_PROMPT}\n\n{user_text[:500]}"
            resp = await loop.run_in_executor(
                None, lambda: self.send_prompt(sess_id, prompt_text)
            )

            title = ""
            if resp:
                text = _extract_text_from_assistant([resp]) if isinstance(resp, dict) else None
                if text:
                    title = text.strip().strip("\"'.,!?")[:60]

            await loop.run_in_executor(
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
    ) -> tuple[list[Message], str]:
        """Run a tool-use loop, using OpenCode as the LLM backend.

        Uses the send-and-wait /message API (POST /session/{id}/message).
        Returns a tuple of (new_messages, opencode_session_id).
        """
        active_agent = agent or self._default_agent
        new_messages: list[Message] = []

        loop = asyncio.get_running_loop()

        if not opencode_sid:
            sess = await loop.run_in_executor(None, self.create_session)
            opencode_sid = sess.get("id") or sess.get("sessionID", "")

        state_block = _session_state_block(self._tools.store, session_id)
        system_text = SYSTEM_PROMPT.format(
            TOOL_DESCRIPTIONS=TOOL_DESCRIPTIONS
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
            # POST /session/{id}/message is a send-and-wait endpoint: OpenCode
            # blocks server-side until the assistant has replied and returns
            # the result directly in the response body, so there is no
            # separate response to poll for afterwards.
            send_resp = await loop.run_in_executor(
                None, lambda: self.send_prompt(opencode_sid, full_prompt)
            )

            # The response may be the assistant message directly, or wrapped
            # in a {"data": ...} envelope like other endpoints (see
            # create_session / list_sessions above).
            payload = (
                send_resp.get("data", send_resp)
                if isinstance(send_resp, dict)
                else send_resp
            )
            if isinstance(payload, list):
                assistant_messages = payload
            elif isinstance(payload, dict):
                assistant_messages = [payload]
            else:
                assistant_messages = []

            text_content = _extract_text_from_assistant(assistant_messages) or ""

            if not text_content:
                await emit({"type": "error", "error": "No response from AI"})
                break

            await emit({"type": "text_delta", "text": text_content})

            tool_calls = _parse_tool_calls(text_content)
            clean_text = _remove_tool_call_blocks(text_content)
            assistant_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": clean_text}
            ]
            seen_calls: set[str] = set()

            if not tool_calls:
                await emit({"type": "turn_complete"})
                assistant_msg_obj = Message(
                    role="assistant", content=assistant_blocks
                )
                new_messages.append(assistant_msg_obj)
                break

            # Track how many entries this turn actually appends to
            # api_messages: the dedupe branch below `continue`s without
            # appending, so that count is not always len(tool_calls).
            turn_start_len = len(api_messages)

            for tc in tool_calls:
                tc_name = tc.get("name", "")
                tc_args = tc.get("arguments", {})
                tc_id = uuid.uuid4().hex

                # Validate tool call
                validation_error = _validate_tool_call(tc_name, tc_args)
                if validation_error:
                    await emit({"type": "tool_result", "id": tc_id, "name": tc_name, "result": {"error": validation_error}})
                    api_messages.append({"role": "assistant", "content": f"  [Tool error for {tc_name}]: {validation_error}"})
                    continue

                # Dedupe: skip if same tool+args already called this turn
                call_key = f"{tc_name}:{json.dumps(tc_args, sort_keys=True)}"
                if call_key in seen_calls:
                    await emit({"type": "tool_result", "id": tc_id, "name": tc_name, "result": {"skipped": "duplicate call"}})
                    continue
                seen_calls.add(call_key)

                await emit({"type": "tool_use_start", "id": tc_id, "name": tc_name})
                result = await self._tools.call(tc_name, tc_args, session_id, tc_id)
                # Sanitize tool result to prevent action block injection
                if isinstance(result, dict) and "text" in result:
                    result["text"] = _sanitize_tool_result(result["text"])
                elif isinstance(result, str):
                    result = _sanitize_tool_result(result)
                await emit({"type": "tool_result", "id": tc_id, "name": tc_name, "result": result})
                assistant_blocks.append({"type": "tool_use", "id": tc_id, "name": tc_name, "input": tc_args})
                result_text = json.dumps(result, default=str)[:2000]
                clean_text += f"\n  [Called tool: {tc_name} with args {json.dumps(tc_args)}]"
                api_messages.append({"role": "assistant", "content": f"  [Tool result for {tc_name}]: {result_text}"})

            # Insert the assistant's text immediately before the entries this
            # turn appended (tool errors/results), preserving all pre-existing
            # history. Using the actual appended count (not len(tool_calls))
            # keeps ordering correct even when calls were deduped/skipped.
            appended_count = len(api_messages) - turn_start_len
            _insert_turn_assistant_text(api_messages, appended_count, clean_text)

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
    """Remove ```action fenced blocks from text."""
    import re
    BLOCK_RE = re.compile(r"```action\s*\n.*?\n```", re.DOTALL)
    return BLOCK_RE.sub("", text).strip()


def _sanitize_tool_result(text: str) -> str:
    """Strip any ```action blocks from tool results to prevent injection."""
    import re
    BLOCK_RE = re.compile(r"```action\s*\n.*?\n```", re.DOTALL)
    return BLOCK_RE.sub("[action block removed]", text)


def _insert_turn_assistant_text(
    api_messages: list[dict[str, str]], appended_count: int, clean_text: str
) -> None:
    """Insert this turn's assistant text right before its own tool results.

    `appended_count` is how many entries this turn's tool-call loop actually
    appended to `api_messages` (the dedupe branch skips a call without
    appending, so it can be less than len(tool_calls)). Inserting at
    `-appended_count` keeps all pre-existing history in place and puts
    `clean_text` immediately before the first tool-result entry from this
    turn. When nothing was appended (e.g. every call was deduped), just
    append.
    """
    if appended_count > 0:
        api_messages.insert(-appended_count, {"role": "assistant", "content": clean_text})
    else:
        api_messages.append({"role": "assistant", "content": clean_text})


def _validate_tool_call(name: str, args: dict[str, Any]) -> str | None:
    """Validate a tool call against TOOL_DEFINITIONS. Returns error message or None."""
    tool_def = next((t for t in TOOL_DEFINITIONS if t["name"] == name), None)
    if tool_def is None:
        return f"Unknown tool: {name}"
    schema = tool_def.get("input_schema", {})
    required = schema.get("required", [])
    for field in required:
        if field not in args:
            return f"Missing required argument: {field}"
    # Validate propose_* payloads
    if name.startswith("propose_"):
        try:
            _validate_propose_config(args.get("config", {}), name.replace("propose_", ""))
        except ValueError as e:
            return str(e)
    return None
