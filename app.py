"""A small, single-process terminal chat service.

The module contains the HTTP API, in-memory runtime state, and local JSON/file
persistence used by the MVP. Deployment values come from config.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import UploadFile as StarletteUploadFile

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Only these MIME types are accepted so the attachment surface stays limited
# to configuration/text files and common small images.
ALLOWED_ATTACHMENT_TYPES = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}


def utc_now() -> str:
    """Return the current UTC time in the ISO 8601 format stored in messages."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ChannelCreate(BaseModel):
    """Request body for creating one public channel."""

    name: str = Field(min_length=1, max_length=32)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        """Normalize a channel name and reject characters outside the URL-safe format."""
        value = value.strip().lower()
        if not CHANNEL_RE.fullmatch(value):
            raise ValueError("name must contain only lowercase letters, numbers, '-' or '_'")
        return value


class MessageCreate(BaseModel):
    """JSON request body for a text-only message."""

    nickname: str = Field(min_length=1, max_length=24)
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("nickname", "content")
    @classmethod
    def non_blank(cls, value: str) -> str:
        """Trim user text and reject empty message fields."""
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class MultipartMessageCreate(BaseModel):
    """Validated fields shared by text and attachment multipart messages."""

    nickname: str = Field(min_length=1, max_length=24)
    content: str = Field(default="", max_length=5000)

    @field_validator("nickname")
    @classmethod
    def valid_nickname(cls, value: str) -> str:
        """Trim and validate the nickname used by a multipart message."""
        value = value.strip()
        if not value:
            raise ValueError("nickname must not be blank")
        return value

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """Trim optional multipart message text while preserving internal newlines."""
        return value.strip()


def safe_filename(filename: str | None) -> str:
    """Return a display-only filename, never used as a filesystem path."""
    name = Path(filename or "attachment").name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:255] or "attachment"


@dataclass(frozen=True)
class AppConfig:
    """Immutable settings loaded once when the deployment starts."""

    host: str = "127.0.0.1"
    port: int = 8000
    warning_text: str = "This is an insecured messaging server, be careful what you post"
    max_history: int = 1000
    max_channels: int = 10
    max_messages_per_client: int = 50
    rate_window_seconds: int = 10
    max_attachment_size: int = 5 * 1024 * 1024
    page_title: str = "local chat"

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """Load and validate immutable deployment settings from a JSON file."""
        try:
            with path.open(encoding="utf-8") as handle:
                values = json.load(handle)
            if not isinstance(values, dict):
                raise ValueError("config must be a JSON object")
            config = cls(**values)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"cannot load config: {exc}") from exc
        if (
            not config.warning_text.strip()
            or not config.page_title.strip()
            or not config.host.strip()
            or not (1 <= config.port <= 65535)
            or any(
                value <= 0
                for value in (
                    config.max_history,
                    config.max_channels,
                    config.max_messages_per_client,
                    config.rate_window_seconds,
                    config.max_attachment_size,
                )
            )
        ):
            raise RuntimeError("config values must be positive and text values must not be blank")
        return config


class JsonStore:
    # JsonStore is the only component that mutates chat-data.json. Keeping file
    # access here makes persistence and attachment cleanup easy to audit.
    def __init__(self, path: Path, config: AppConfig):
        """Create the in-memory store and derive its attachment directory."""
        self.path = path
        self.config = config
        self.attachments_path = path.parent / f"{path.stem}-attachments"
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"version": 1, "channels": {}}

    def load(self) -> None:
        """Load persisted channels or create an empty, valid data document.

        Existing histories are trimmed to the configured retention limit during
        startup so lowering max_history takes effect without a migration.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write()
            return
        try:
            with self.path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("channels"), dict):
                raise ValueError("unsupported data format")
            for channel_id, channel in value["channels"].items():
                if not isinstance(channel, dict) or not isinstance(channel.get("messages"), list):
                    raise ValueError("invalid channel record")
                channel["messages"] = channel["messages"][-self.config.max_history :]
            self.data = value
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load chat data: {exc}") from exc

    def _write(self) -> None:
        """Persist the store with an fsynced temporary file and atomic replacement."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Replace the data file atomically so an interrupted write cannot leave
        # the server with a partially written JSON document.
        fd, temporary = tempfile.mkstemp(prefix=".chat-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    self.data,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def channels(self) -> list[dict[str, Any]]:
        """Return public channel summaries without exposing message contents."""
        with self.lock:
            return [self._summary(c) for c in self.data["channels"].values()]

    @staticmethod
    def _summary(channel: dict[str, Any]) -> dict[str, Any]:
        """Build the compact channel representation returned by the API."""
        return {key: channel[key] for key in ("id", "name", "created_at")} | {
            "message_count": len(channel["messages"])
        }

    def create_channel(self, name: str) -> dict[str, Any]:
        """Create and persist a channel after enforcing the configured limit.

        Raises:
            ValueError: If the configured channel limit has been reached.
            FileExistsError: If another channel already uses the name.
        """
        with self.lock:
            if len(self.data["channels"]) >= self.config.max_channels:
                raise ValueError("channel limit reached")
            if any(c["name"] == name for c in self.data["channels"].values()):
                raise FileExistsError("channel already exists")
            channel = {"id": uuid4().hex, "name": name, "created_at": utc_now(), "messages": []}
            self.data["channels"][channel["id"]] = channel
            self._write()
            return self._summary(channel)

    def history(self, channel_id: str) -> list[dict[str, Any]]:
        """Return a copy of a channel's retained message history.

        Raises:
            KeyError: If the requested channel does not exist.
        """
        with self.lock:
            channel = self.data["channels"].get(channel_id)
            if channel is None:
                raise KeyError(channel_id)
            return list(channel["messages"])

    def delete_channel(self, channel_id: str) -> None:
        """Delete a channel and all attachment files referenced by its messages.

        Raises:
            KeyError: If the requested channel does not exist.
        """
        with self.lock:
            if channel_id not in self.data["channels"]:
                raise KeyError(channel_id)
            channel = self.data["channels"][channel_id]
            for message in channel["messages"]:
                for attachment in message.get("attachments", []):
                    self.delete_attachment_file(attachment["id"])
            del self.data["channels"][channel_id]
            self._write()

    def add_message(
        self,
        channel_id: str,
        nickname: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create, retain, and persist a message with server-owned metadata.

        The server assigns the ID and UTC timestamp, then trims the channel to
        the configured history limit before atomically saving the document.

        Raises:
            KeyError: If the requested channel does not exist.
        """
        with self.lock:
            channel = self.data["channels"].get(channel_id)
            if channel is None:
                raise KeyError(channel_id)
            message = {
                "id": uuid4().hex,
                "nickname": nickname,
                "content": content,
                "created_at": utc_now(),
                "attachments": attachments or [],
            }
            channel["messages"].append(message)
            channel["messages"] = channel["messages"][-self.config.max_history :]
            self._write()
            return message

    def save_attachment(self, data: bytes, filename: str, content_type: str) -> dict[str, Any]:
        """Store original attachment bytes under a generated ID and return metadata."""
        attachment_id = uuid4().hex
        self.attachments_path.mkdir(parents=True, exist_ok=True)
        target = self.attachments_path / attachment_id
        target.write_bytes(data)
        return {
            "id": attachment_id,
            "filename": safe_filename(filename),
            "media_type": content_type,
            "size": len(data),
            "download_url": f"/api/attachments/{attachment_id}",
        }

    def delete_attachment_file(self, attachment_id: str) -> None:
        """Remove one generated attachment file when it still exists."""
        target = self.attachments_path / attachment_id
        if target.is_file():
            target.unlink()

    def attachment(self, attachment_id: str) -> tuple[dict[str, Any], Path] | None:
        """Find attachment metadata and its safe generated storage path."""
        for channel in self.data["channels"].values():
            for message in channel["messages"]:
                for item in message.get("attachments", []):
                    if item.get("id") == attachment_id:
                        target = self.attachments_path / attachment_id
                        if target.is_file():
                            return item, target
        return None


class AppState:
    # These values are intentionally transient: subscribers and rate-limit
    # history are rebuilt when the process restarts.
    def __init__(self, store: JsonStore, config: AppConfig):
        """Create subscriber registries and per-client rate-limit state."""
        self.store = store
        self.config = config
        self.subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self.rate: dict[str, deque[float]] = defaultdict(deque)
        self.rate_lock = threading.Lock()

    def allow_send(self, client: str) -> bool:
        """Apply the configured rolling-window message limit for one client."""
        now = time.monotonic()
        with self.rate_lock:
            attempts = self.rate[client]
            while attempts and attempts[0] <= now - self.config.rate_window_seconds:
                attempts.popleft()
            if len(attempts) >= self.config.max_messages_per_client:
                return False
            attempts.append(now)
            return True

    async def publish(self, channel_id: str, message: dict[str, Any]) -> None:
        """Fan a new message out to every active SSE subscriber.

        A full queue is skipped rather than blocking the sender; the client can
        reconnect and reload the bounded history if it fell behind.
        """
        for queue in tuple(self.subscribers.get(channel_id, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow client will reconnect and fetch the bounded history.
                pass


def create_app(
    data_path: str | Path = "chat-data.json",
    config_path: str | Path = "config.json",
) -> FastAPI:
    """Build the FastAPI application around configured storage and runtime state."""
    # The app factory keeps storage paths configurable for tests and embedding,
    # while the normal run.py entrypoint uses the deployment config.json.
    config = AppConfig.load(Path(config_path))
    store = JsonStore(Path(data_path), config)
    state = AppState(store, config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """Load persistent data before serving requests and yield app lifetime."""
        store.load()
        yield

    app = FastAPI(title=config.page_title, version="1.0", lifespan=lifespan)
    app.state.chat = state
    # Static assets are served by FastAPI so the deployment needs no separate
    # frontend server or CDN.
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        """Convert storage validation errors into a JSON 400 response."""
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def index():
        """Serve the browser application shell from the static directory."""
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    async def health():
        """Return a lightweight liveness response for deployment checks."""
        return {"status": "ok"}

    @app.get("/api/config")
    async def public_config():
        """Expose only browser-safe title and warning configuration values."""
        return {
            "page_title": config.page_title,
            "warning_text": config.warning_text,
        }

    @app.get("/api/channels")
    async def list_channels():
        """Return public channel summaries and retained message counts."""
        return {"channels": store.channels()}

    @app.post("/api/channels", status_code=201)
    async def create_channel(payload: ChannelCreate):
        """Validate and persist a public channel, returning its summary."""
        try:
            return store.create_channel(payload.name)
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/channels/{channel_id}/messages")
    async def get_messages(channel_id: str):
        """Return retained messages for a channel or 404 if it is missing."""
        try:
            return {"messages": store.history(channel_id)}
        except KeyError as exc:
            raise HTTPException(404, "channel not found") from exc

    @app.delete("/api/channels/{channel_id}", status_code=204)
    async def delete_channel(channel_id: str):
        """Delete a channel, its messages, and its stored attachments."""
        try:
            store.delete_channel(channel_id)
        except KeyError as exc:
            raise HTTPException(404, "channel not found") from exc

    @app.post("/api/channels/{channel_id}/messages", status_code=201)
    async def send_message(channel_id: str, request: Request):
        """Create a message from JSON or multipart form data.

        Text-only requests use JSON. Multipart requests may include optional
        text plus attachments, including attachment-only messages. Successful
        writes are persisted before publication to channel SSE subscribers.
        """
        client = request.client.host if request.client else "unknown"
        if not state.allow_send(client):
            raise HTTPException(429, "too many messages; try again shortly")
        attachments: list[dict[str, Any]] = []
        saved_ids: list[str] = []
        content_type = request.headers.get("content-type", "").lower()
        if content_type.split(";", 1)[0].strip() == "multipart/form-data":
            # Parse uploads manually because a message may contain text,
            # attachments, or an attachment without any text.
            form = await request.form()
            nickname = str(form.get("nickname", ""))
            content = str(form.get("content", ""))
            uploads = (
                form.getlist("attachments")
                + form.getlist("attachment")
                + form.getlist("files")
            )
            uploads = [item for item in uploads if isinstance(item, StarletteUploadFile)]
            if not uploads and not content.strip():
                raise HTTPException(422, "content must not be blank")
            try:
                validated = MultipartMessageCreate(nickname=nickname, content=content)
            except Exception as exc:
                raise HTTPException(422, "invalid nickname or content") from exc
            for upload in uploads:
                mime = (upload.content_type or "").split(";", 1)[0].strip().lower()
                if not (mime.startswith("text/") or mime.startswith("image/")):
                    raise HTTPException(415, "attachments must be text or image files")
                data = await upload.read(config.max_attachment_size + 1)
                if len(data) > config.max_attachment_size:
                    raise HTTPException(413, "attachment is too large")
                item = store.save_attachment(data, upload.filename or "attachment", mime)
                attachments.append(item)
                saved_ids.append(item["id"])
            nickname, content = validated.nickname, validated.content
        else:
            # Keep the JSON path small and strict for ordinary text messages.
            try:
                payload = MessageCreate.model_validate(await request.json())
            except Exception as exc:
                raise HTTPException(422, "invalid message") from exc
            nickname, content = payload.nickname, payload.content
        try:
            message = store.add_message(channel_id, nickname, content, attachments)
        except KeyError as exc:
            # Remove files if persistence cannot associate them with a channel.
            for attachment_id in saved_ids:
                store.delete_attachment_file(attachment_id)
            raise HTTPException(404, "channel not found") from exc
        await state.publish(channel_id, message)
        return message

    @app.get("/api/attachments/{attachment_id}")
    async def download_attachment(attachment_id: str):
        """Download an attachment by generated ID with safe content headers."""
        found = store.attachment(attachment_id)
        if found is None:
            raise HTTPException(404, "attachment not found")
        metadata, path = found
        # The generated ID is the path; the original name is only a safe display name.
        return FileResponse(
            path,
            media_type=metadata.get("media_type", metadata.get("content_type", "application/octet-stream")),
            filename=metadata["filename"],
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/channels/{channel_id}/events")
    async def events(channel_id: str) -> StreamingResponse:
        """Open an SSE stream for a channel's messages and keepalives."""
        try:
            store.history(channel_id)
        except KeyError as exc:
            raise HTTPException(404, "channel not found") from exc
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        state.subscribers[channel_id].add(queue)

        async def stream() -> AsyncIterator[str]:
            """Yield connection, message, and keepalive frames until disconnect."""
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        # Comment frames keep proxies and browsers from closing
                        # an otherwise healthy but idle SSE connection.
                        yield ": keepalive\n\n"
            finally:
                state.subscribers[channel_id].discard(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
