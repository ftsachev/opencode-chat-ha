from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION


@dataclass
class Message:
    role: str
    content: list[dict[str, Any]]
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class PendingChange:
    id: str
    kind: str
    summary: str
    payload: dict[str, Any]
    diff: str | None = None
    source_tool_use_id: str | None = None
    status: str = "pending"


@dataclass
class Session:
    id: str
    title: str
    created_at: float = 0.0
    updated_at: float = 0.0
    messages: list[Message] = None
    pending_changes: list[PendingChange] = None
    opencode_session_id: str | None = None

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if self.messages is None:
            self.messages = []
        if self.pending_changes is None:
            self.pending_changes = []


class SessionStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._sessions: dict[str, Session] = {}
        self._pending_index: dict[str, tuple[str, PendingChange]] = {}  # change_id -> (session_id, change)
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load() or {}
        for raw in data.get("sessions", []):
            session = Session(
                id=raw["id"],
                title=raw["title"],
                created_at=raw.get("created_at", time.time()),
                updated_at=raw.get("updated_at", time.time()),
                opencode_session_id=raw.get("opencode_session_id"),
                messages=[Message(**m) for m in raw.get("messages", [])],
                pending_changes=[
                    PendingChange(**p) for p in raw.get("pending_changes", [])
                ],
            )
            self._sessions[session.id] = session
            for change in session.pending_changes:
                self._pending_index[change.id] = (session.id, change)
        self._loaded = True

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "sessions": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                        "opencode_session_id": s.opencode_session_id,
                        "messages": [asdict(m) for m in s.messages],
                        "pending_changes": [asdict(p) for p in s.pending_changes],
                    }
                    for s in self._sessions.values()
                ]
            }
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = sorted(
            self._sessions.values(), key=lambda s: s.updated_at, reverse=True
        )
        return [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "message_count": len(s.messages),
                "has_pending": any(c.status == "pending" for c in s.pending_changes),
            }
            for s in sessions
        ]

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_or_raise(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    async def create(self, title: str = "New chat") -> Session:
        session = Session(id=uuid.uuid4().hex, title=title)
        self._sessions[session.id] = session
        await self.async_save()
        return session

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        await self.async_save()

    async def rename(self, session_id: str, title: str) -> None:
        session = self.get_or_raise(session_id)
        session.title = title
        session.updated_at = time.time()
        await self.async_save()

    async def append_message(self, session_id: str, message: Message) -> None:
        session = self.get_or_raise(session_id)
        session.messages.append(message)
        session.updated_at = time.time()
        await self.async_save()

    async def set_opencode_session(
        self, session_id: str, opencode_session_id: str
    ) -> None:
        session = self.get_or_raise(session_id)
        session.opencode_session_id = opencode_session_id
        await self.async_save()

    async def add_pending(self, session_id: str, change: PendingChange) -> None:
        session = self.get_or_raise(session_id)
        session.pending_changes.append(change)
        self._pending_index[change.id] = (session_id, change)
        await self.async_save()

    async def remove_pending(
        self, session_id: str, change_id: str
    ) -> PendingChange | None:
        indexed = self._pending_index.pop(change_id, None)
        if indexed:
            session = self.get_or_raise(session_id)
            for i, change in enumerate(session.pending_changes):
                if change.id == change_id:
                    removed = session.pending_changes.pop(i)
                    await self.async_save()
                    return removed
        return None

    async def set_change_status(
        self, session_id: str, change_id: str, status: str
    ) -> PendingChange | None:
        session = self.get_or_raise(session_id)
        for change in session.pending_changes:
            if change.id == change_id:
                change.status = status
                await self.async_save()
                return change
        return None

    def list_pending(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": session_id,
                "change": {
                    "id": change.id,
                    "kind": change.kind,
                    "summary": change.summary,
                    "diff": change.diff,
                    "payload": change.payload,
                    "status": change.status,
                },
            }
            for session_id, change in self._pending_index.values()
            if change.status == "pending"
        ]
