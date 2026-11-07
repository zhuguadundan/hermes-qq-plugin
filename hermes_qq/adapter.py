"""Native NapCat/OneBot personal QQ adapter for Hermes Gateway.

This external package is loaded through a tiny `gateway/platforms/qq.py` shim
until Hermes exposes a stable third-party platform adapter registry.  The
adapter itself is native Gateway code: it does not shell out to `HermesCLI`, and
it converts NapCat/OneBot events into the same `MessageEvent` shape used by
Telegram, Discord, Weixin, and other Hermes messaging platforms.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    import websockets  # type: ignore

    WEBSOCKETS_AVAILABLE = True
except Exception:  # pragma: no cover - exercised by requirement checks
    websockets = None  # type: ignore
    WEBSOCKETS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
    get_document_cache_dir,
    get_image_cache_dir,
)

from .client import NapCatClient
from .types import QQEventSource
from .reliability import EventJournal, number, stream_download

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
VIDEO_EXTS = {".avi", ".mkv", ".mov", ".mp4", ".webm"}
AUDIO_EXTS = {".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}


def check_qq_requirements() -> bool:
    return WEBSOCKETS_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _entry_matches(entries: List[str], value: str) -> bool:
    return "*" in entries or (bool(value) and value in entries)


@dataclass
class _BacklogEntry:
    """One passive group message kept for mention-time context.

    Attributes are filled in out of band: a group file upload creates the
    entry immediately and a background download later attaches either the
    local path or a short failure note.
    """

    stamp: str
    who: str
    body: str
    attachment: Optional[str] = None
    note: Optional[str] = None
    ts: float = 0.0
    task: Optional[asyncio.Task] = None

    def render(self) -> str:
        line = f"{self.stamp} {self.who}: {self.body}"
        if self.attachment:
            if not Path(self.attachment).is_file():
                return f"{line} (文件已清理或不可用，可按文件名查询归档)"
            return f"{line} → 已存到本地: {self.attachment}"
        if self.note:
            return f"{line} {self.note}"
        return line


class NapCatQQAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = 3500

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.QQ)
        extra = config.extra or {}
        self.onebot_url = str(extra.get("onebot_url") or os.getenv("NAPCAT_ONEBOT_URL", "http://127.0.0.1:3000"))
        self.onebot_token = str(extra.get("onebot_token") or os.getenv("NAPCAT_ONEBOT_TOKEN", ""))
        self.onebot_ws_url = str(extra.get("onebot_ws_url") or os.getenv("NAPCAT_ONEBOT_WS_URL", "ws://127.0.0.1:3001"))
        self.onebot_ws_token = str(extra.get("onebot_ws_token") or self.onebot_token or os.getenv("NAPCAT_ONEBOT_WS_TOKEN", ""))
        self.request_timeout = number(extra, "request_timeout", 60, 0.1)
        self.chunk_size = int(number(extra, "chunk_size", 65536, 1))
        self.group_chat_all = bool(extra.get("group_chat_all", False))
        # Passive group backlog: number of recent non-mentioned group messages
        # kept in memory and prepended as read-only context when the bot is
        # finally mentioned.  0 disables listening entirely.
        self.group_listen_context = int(number(extra, "group_listen_context", 0))
        self.group_listen_context_ttl_seconds = number(extra, "group_listen_context_ttl_minutes", 120) * 60
        self.group_file_wait_seconds = number(extra, "group_file_wait_seconds", 5)
        self._passive_backlog: Dict[str, Deque["_BacklogEntry"]] = {}
        # Passive group-file collection.  QQ groups cannot mention-attach a
        # file, so a bare upload is the only signal; we save it locally while
        # nobody is talking to us so a later mention can actually read it.
        self.group_file_autosave = bool(extra.get("group_file_autosave", True))
        self.group_file_backfill = bool(extra.get("group_file_backfill", True))
        self.group_file_backfill_limit = int(number(extra, "group_file_backfill_limit", 50))
        self.group_file_max_bytes = int(number(extra, "group_file_max_mb", 200) * 1024 * 1024)
        self.group_file_retention_days = number(extra, "group_file_retention_days", 7)
        # Post-retention capacity backstop; it can evict younger files when
        # necessary, but never disables the periodic retention pass.
        self.group_file_cache_max_bytes = int(number(extra, "group_file_cache_max_gb", 5) * 1073741824)
        self._download_slots = asyncio.Semaphore(int(number(extra, "group_file_concurrency", 2, 1)))
        self._version_locks = [asyncio.Lock() for _ in range(64)]
        self._maintenance_lock = asyncio.Lock()
        self._download_stop = threading.Event()
        self._download_workers: set[asyncio.Task] = set()
        self._download_reservations: Dict[str, int] = {}
        self._group_download_tasks: Dict[str, asyncio.Task] = {}
        self._context_tasks: set[asyncio.Task] = set()
        self._history_task: Optional[asyncio.Task] = None
        self._live_task: Optional[asyncio.Task] = None
        self._live_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._journal: Optional[EventJournal] = None
        self._event_inflight: set[str] = set()
        self._group_file_index: Optional[Dict[str, dict]] = None
        self._group_file_tasks: Dict[str, asyncio.Task] = {}
        self._group_file_backfill_task: Optional[asyncio.Task] = None
        self._last_group_backfill = 0.0
        self._file_backfill_pending = False
        self._last_file_sweep = 0.0
        self.enable_online_file = bool(extra.get("enable_online_file", True))
        self.history_backfill_count = int(number(extra, "history_backfill_count", 20))
        self.history_backfill_seconds = number(extra, "history_backfill_seconds", 600)
        self.resync_interval_seconds = number(extra, "resync_interval_seconds", 3, 0.1)
        self._ws_reconnect_delay_seconds = number(extra, "ws_reconnect_delay_seconds", 3, 0.1)
        self._ws_ping_interval_seconds = number(extra, "ws_ping_interval_seconds", 25, 0.1)
        self._ws_ping_timeout_seconds = number(extra, "ws_ping_timeout_seconds", 15, 0.1)
        self._health_check_interval_seconds = number(extra, "health_check_interval_seconds", max(15, self.resync_interval_seconds), 0.1)
        self._dm_policy = str(extra.get("dm_policy", "open")).lower()
        self._group_policy = str(extra.get("group_policy", "open")).lower()
        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
        self._allowed_group_users = _coerce_list(extra.get("allowed_group_users"))
        self._group_ignore_from = _coerce_list(extra.get("group_ignore_from"))
        self._seen_events: Dict[str, float] = {}
        self._target_sequences: Dict[str, int] = {}
        self._pending_batches: Dict[str, MessageEvent] = {}
        self._pending_batch_tasks: Dict[str, asyncio.Task] = {}
        self._batch_delay_seconds = number(extra, "batch_delay_seconds", 1.2)
        self._split_batch_delay_seconds = number(extra, "split_batch_delay_seconds", 2.5)
        self._split_threshold = int(number(extra, "split_threshold", 1800, 1))
        self._client = NapCatClient(self.onebot_url, self.onebot_token, self.request_timeout, self.chunk_size)
        self._listen_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        # Connection state and desired lifecycle are separate so a transient
        # WebSocket failure does not terminate the reconnect loop.
        self._qq_should_run = False
        self.bot_user_id = ""
        self.bot_name = "Hermes"

    @property
    def name(self) -> str:
        return "QQ"

    async def connect(self) -> bool:
        try:
            login = await asyncio.to_thread(self._client.get_login_info)
            self.bot_user_id = str(login.get("user_id") or "")
            self.bot_name = str(login.get("nickname") or self.bot_user_id or "Hermes")
            self._download_stop.clear()
            self._journal = EventJournal(self._group_file_root().parent / "qq_state" / f"events-{self.bot_user_id}.sqlite3")
            self._qq_should_run = True
            self._live_task = self._spawn_task(self._process_live_events())
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._health_task = asyncio.create_task(self._health_loop())
            self._mark_connected()
            logger.info("[QQ] Connected to NapCat as %s (%s)", self.bot_name, self.bot_user_id)
            return True
        except Exception as exc:
            self._set_fatal_error("qq_connect_error", f"QQ startup failed: {exc}", retryable=True)
            logger.error("[QQ] Startup failed: %s", exc, exc_info=True)
            self._running = False
            return False

    async def disconnect(self) -> None:
        self._qq_should_run = False
        self._download_stop.set()
        self._mark_disconnected()
        tasks = set(self._pending_batch_tasks.values()) | set(self._group_file_tasks.values())
        tasks |= set(self._group_download_tasks.values()) | self._context_tasks
        tasks.update(t for t in (self._listen_task, self._health_task, self._history_task, self._live_task, self._group_file_backfill_task) if t)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Cancelling to_thread alone cannot stop a worker. Streaming workers
        # check the stop flag between chunks and use short socket timeouts.
        if self._download_workers:
            await asyncio.gather(*list(self._download_workers), return_exceptions=True)
        self._listen_task = self._health_task = self._history_task = self._live_task = None
        while not self._live_queue.empty():
            self._live_queue.get_nowait()
            self._live_queue.task_done()
        self._group_file_backfill_task = None
        self._pending_batch_tasks.clear()
        self._pending_batches.clear()
        self._group_file_tasks.clear()
        self._group_download_tasks.clear()
        self._context_tasks.clear()
        self._event_inflight.clear()
        if self._journal:
            self._journal.close()
            self._journal = None
        self._client.session.close() if hasattr(self._client, "session") else None

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        source = self._source_from_chat_id(chat_id)
        try:
            result = await asyncio.to_thread(self._client.send_text, source, content)
            data = result.get("data") or {}
            return SendResult(success=True, message_id=str(data.get("message_id") or ""), raw_response=result)
        except Exception as exc:
            logger.warning("[QQ] send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _send_with_retry(self, chat_id: str, content: str, reply_to=None, metadata=None, **kwargs) -> SendResult:
        # The base adapter also has a plain-text fallback. That second send
        # is unsafe when QQ accepted the first request but its reply was lost.
        return await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def send_image(
        self,
        chat_id: str,
        image_url: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        file_path: Optional[str] = None,
        image_path: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        media_ref = self._first_media_ref(image_url, image_path, file_path)
        if not media_ref:
            return SendResult(success=False, error="QQ image send missing image_url/image_path/file_path")
        return await self._send_file_segment(chat_id, media_ref, "image", caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        return await self.send_image(chat_id=chat_id, image_path=image_path, caption=caption)

    async def send_voice(
        self,
        chat_id: str,
        voice_url: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        audio_path: Optional[str] = None,
        file_path: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        media_ref = self._first_media_ref(audio_path, voice_url, file_path)
        if not media_ref:
            return SendResult(success=False, error="QQ voice send missing audio_path/voice_url/file_path")
        return await self._send_file_segment(chat_id, media_ref, "record", caption)

    async def send_video(
        self,
        chat_id: str,
        video_url: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        video_path: Optional[str] = None,
        file_path: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        media_ref = self._first_media_ref(video_path, video_url, file_path)
        if not media_ref:
            return SendResult(success=False, error="QQ video send missing video_path/video_url/file_path")
        return await self._send_file_segment(chat_id, media_ref, "video", caption)

    async def send_document(
        self,
        chat_id: str,
        document_url: Optional[str] = None,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        file_path: Optional[str] = None,
        file_name: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        media_ref = self._first_media_ref(file_path, document_url)
        if not media_ref:
            return SendResult(success=False, error="QQ document send missing file_path/document_url")
        if self._is_remote_ref(media_ref):
            text = f"{caption}\n{media_ref}" if caption else media_ref
            return await self.send(chat_id=chat_id, content=text)

        source = self._source_from_chat_id(chat_id)
        try:
            path = self._local_path(media_ref)
            if not path.exists():
                return SendResult(success=False, error=f"QQ document file not found: {path}")
            display_name = file_name or filename or path.name
            if source.group_id:
                remote = await self._upload_local_file_with_fallback(path)
                result = await asyncio.to_thread(
                    self._client.call,
                    "upload_group_file",
                    {"group_id": source.group_id, "file": remote, "name": display_name},
                )
            else:
                result = await asyncio.to_thread(
                    self._client.call,
                    "send_online_file",
                    {"user_id": source.user_id, "file_path": str(path), "file_name": display_name},
                )
            return SendResult(success=True, raw_response=result)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"id": chat_id, "name": chat_id, "type": "group" if chat_id.startswith("group:") else "dm"}

    async def _send_file_segment(self, chat_id: str, path_or_url: str, segment_type: str, caption: Optional[str]) -> SendResult:
        source = self._source_from_chat_id(chat_id)
        try:
            remote = path_or_url
            if not self._is_remote_ref(path_or_url):
                path = self._local_path(path_or_url)
                if not path.exists():
                    return SendResult(success=False, error=f"QQ {segment_type} file not found: {path}")
                remote = await self._upload_local_file_with_fallback(path)
            segments = []
            if caption:
                segments.append({"type": "text", "data": {"text": caption}})
            segments.append({"type": segment_type, "data": {"file": remote}})
            result = await asyncio.to_thread(self._client.send_segments, source, segments)
            return SendResult(success=True, raw_response=result)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    @staticmethod
    def _first_media_ref(*values: Optional[str]) -> Optional[str]:
        for value in values:
            if value:
                return str(value)
        return None

    @staticmethod
    def _is_remote_ref(value: str) -> bool:
        return bool(re.match(r"^(?:https?|base64)://|^data:", value, flags=re.IGNORECASE))

    @staticmethod
    def _local_path(value: str) -> Path:
        if value.startswith("file://"):
            value = value[len("file://") :]
        return Path(value).expanduser().resolve()

    async def _upload_local_file_with_fallback(self, path: Path) -> str:
        try:
            return await asyncio.to_thread(self._client.upload_file_stream, str(path))
        except Exception as exc:
            logger.warning(
                "[QQ] upload_file_stream failed for %s; falling back to direct local path: %s",
                path.name,
                exc,
            )
            return str(path)

    @staticmethod
    def _websocket_connect_kwargs(
        headers: Optional[Dict[str, str]],
        *,
        ping_interval: float = 25.0,
        ping_timeout: float = 15.0,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "ping_interval": ping_interval,
            "ping_timeout": ping_timeout,
            "max_size": 2 * 1024 * 1024,
        }
        if not headers:
            return kwargs
        try:
            parameters = inspect.signature(websockets.connect).parameters if websockets else {}
        except (TypeError, ValueError):
            parameters = {}
        header_param = "additional_headers" if "additional_headers" in parameters else "extra_headers"
        kwargs[header_param] = headers
        return kwargs

    def _source_from_chat_id(self, chat_id: str) -> QQEventSource:
        if str(chat_id).startswith("group:"):
            group_id = str(chat_id).split(":", 1)[1]
            return QQEventSource(user_id="", group_id=group_id, message_id=None, self_id=self.bot_user_id, raw={})
        if str(chat_id).startswith("private:"):
            user_id = str(chat_id).split(":", 1)[1]
        else:
            user_id = str(chat_id)
        return QQEventSource(user_id=user_id, group_id=None, message_id=None, self_id=self.bot_user_id, raw={})

    async def _listen_loop(self) -> None:
        while self._qq_should_run:
            try:
                headers = {"Authorization": f"Bearer {self.onebot_ws_token}"} if self.onebot_ws_token else None
                ws_ctx = websockets.connect(
                    self.onebot_ws_url,
                    **self._websocket_connect_kwargs(
                        headers,
                        ping_interval=self._ws_ping_interval_seconds,
                        ping_timeout=self._ws_ping_timeout_seconds,
                    ),
                )
                async with ws_ctx as ws:
                    self._mark_connected()
                    logger.info("[QQ] WebSocket connected: %s", self.onebot_ws_url)
                    if self._history_task is None or self._history_task.done():
                        self._history_task = self._spawn_task(self._backfill_recent_messages())
                    self._schedule_group_file_backfill()
                    async for payload in ws:
                        if not self._qq_should_run:
                            break
                        try:
                            event = json.loads(payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload))
                        except Exception:
                            continue
                        if isinstance(event, dict):
                            await self._live_queue.put(event)
                    if self._qq_should_run:
                        raise RuntimeError("WebSocket closed")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._qq_should_run:
                    self._mark_stream_disconnected()
                    logger.warning(
                        "[QQ] WebSocket error: %s; reconnecting in %ss",
                        exc,
                        self._ws_reconnect_delay_seconds,
                    )
                    await asyncio.sleep(self._ws_reconnect_delay_seconds)

    async def _process_live_events(self) -> None:
        # Receive WS frames immediately, but preserve history-before-live order
        # in one bounded dispatcher rather than spawning unbounded handlers.
        while self._qq_should_run:
            event = await self._live_queue.get()
            try:
                history = self._history_task
                if history and not history.done():
                    await asyncio.shield(history)
                await self._handle_onebot_event(event, origin="ws")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[QQ] Failed to process OneBot event: %s", exc, exc_info=True)
            finally:
                self._live_queue.task_done()

    def _mark_stream_disconnected(self) -> None:
        self._running = False
        try:
            from gateway.status import write_runtime_status

            write_runtime_status(
                platform=self.platform.value,
                platform_state="disconnected",
                error_code=None,
                error_message=None,
            )
        except Exception:
            pass

    async def _health_loop(self) -> None:
        try:
            while self._qq_should_run:
                await asyncio.sleep(self._health_check_interval_seconds)
                if not self._qq_should_run:
                    return
                try:
                    status = await asyncio.to_thread(self._client.get_status)
                    if isinstance(status, dict) and status.get("online") is False:
                        logger.warning("[QQ] NapCat reports account offline: %s", status)
                except Exception as exc:
                    logger.warning("[QQ] NapCat health check failed: %s", exc)
                # Expire collected group files / stale context once per hour
                # even if nobody ever uploads again.
                try:
                    async with self._maintenance_lock:
                        await self._disk_op(self._sweep_group_file_cache)
                    self._prune_passive_backlog()
                    if self._journal:
                        self._journal.prune()
                    if time.time() - self._last_group_backfill >= (60 if self._file_backfill_pending else 3600):
                        self._schedule_group_file_backfill()
                except Exception as exc:
                    logger.debug("[QQ] cache maintenance skipped: %s", exc)
        except asyncio.CancelledError:
            return

    async def _backfill_recent_messages(self) -> None:
        if self.history_backfill_count <= 0 or self.history_backfill_seconds <= 0:
            return
        cutoff = time.time() - self.history_backfill_seconds
        for chat_type, target_id in self._backfill_targets():
            try:
                if chat_type == "group":
                    messages = await asyncio.to_thread(
                        self._client.get_group_msg_history,
                        target_id,
                        self.history_backfill_count,
                    )
                else:
                    messages = await asyncio.to_thread(
                        self._client.get_friend_msg_history,
                        target_id,
                        self.history_backfill_count,
                    )
            except Exception as exc:
                logger.debug("[QQ] History backfill failed for %s:%s: %s", chat_type, target_id, exc)
                continue

            if not isinstance(messages, list):
                continue
            recent = (message for message in messages if isinstance(message, dict))
            for raw in sorted(recent, key=self._event_timestamp):
                timestamp = self._event_timestamp(raw)
                if timestamp and timestamp < cutoff:
                    continue
                event = self._history_event_to_onebot(raw, chat_type, target_id)
                try:
                    await self._handle_onebot_event(event, origin="backfill")
                except Exception as exc:
                    logger.debug("[QQ] History backfill event skipped: %s", exc)

    def _backfill_targets(self) -> List[Tuple[str, str]]:
        targets: List[Tuple[str, str]] = []
        if self._dm_policy == "allowlist":
            targets.extend(("private", value) for value in self._allow_from if value != "*")
        if self._group_policy == "allowlist":
            targets.extend(("group", value) for value in self._group_allow_from if value != "*")
        return targets

    @staticmethod
    def _event_timestamp(event: dict, default: float = 0) -> float:
        try:
            return float(event.get("time") or default)
        except (TypeError, ValueError):
            return default

    def _history_event_to_onebot(self, raw: dict, chat_type: str, target_id: str) -> dict:
        event = dict(raw)
        event.setdefault("post_type", "message")
        event.setdefault("message_type", "group" if chat_type == "group" else "private")
        event.setdefault("self_id", self.bot_user_id)
        if chat_type == "group":
            event.setdefault("group_id", target_id)
        else:
            event.setdefault("user_id", target_id)
        sender = event.get("sender")
        if isinstance(sender, dict) and not event.get("user_id"):
            event["user_id"] = sender.get("user_id")
        if "message" not in event:
            event["message"] = event.get("raw_message") or ""
        return event

    async def _handle_onebot_event(self, event: dict, origin: str = "ws") -> None:
        normalized = self._normalize_event(event)
        if normalized is None or self._is_self_message(normalized):
            return
        source = self._build_qq_source(normalized)
        if not source.user_id or not self._is_allowed(source):
            return
        addressed = not source.group_id or await self._should_process_group_event(normalized, source)
        dedupe = self._event_dedupe_key(normalized, source)
        if dedupe and not self._remember_event(dedupe, persistent=addressed):
            return
        try:
            raw_text = ''.join(str((s.get('data') or {}).get('text') or '')
                               for s in self._message_segments(normalized) if s.get('type') == 'text')
            if origin == 'backfill' and raw_text.lstrip().startswith('/'):
                self._finish_events([dedupe] if dedupe else [])
                return
            # A first installation cannot infer which older requests ran.
            cold_history = (origin == "backfill" and self._journal is not None
                            and self._event_timestamp(normalized) < self._journal.created_at)
            if not addressed or cold_history:
                if source.group_id:
                    self._record_passive_group_event(normalized, source)
                self._finish_events([dedupe] if dedupe else [])
                return
            text, media_urls, media_types, msg_type = await self._build_message_parts(normalized, source)
            if not text.strip() and not media_urls:
                if any(s.get("type") in {"file", "onlinefile", "image", "record", "voice", "audio", "video"}
                       for s in self._message_segments(normalized)):
                    raise RuntimeError("QQ message media unavailable; message remains retryable")
                self._finish_events([dedupe] if dedupe else [])
                return
            if origin == "backfill" and msg_type == MessageType.COMMAND:
                self._finish_events([dedupe] if dedupe else [])
                return
            chat_id = f"group:{source.group_id}" if source.group_id else f"private:{source.user_id}"
            sender = normalized.get("sender") or {}
            event_obj = MessageEvent(
                text=text, message_type=msg_type,
                source=self.build_source(
                    chat_id=chat_id,
                    chat_name=str(source.group_id) if source.group_id else str(source.user_id),
                    chat_type="group" if source.group_id else "dm",
                    user_id=source.user_id,
                    user_name=str(sender.get("card") or sender.get("nickname") or source.user_id),
                    message_id=source.message_id,
                ),
                raw_message=normalized, message_id=source.message_id,
                media_urls=media_urls, media_types=media_types,
                timestamp=datetime.fromtimestamp(self._event_timestamp(normalized, time.time())),
            )
            event_obj._qq_dedupe_keys = [dedupe] if dedupe else []
            if event_obj.is_command():
                await self._dispatch_qq_message(event_obj)
                return
            snapshot = []
            if source.group_id:
                self._prune_passive_backlog(str(source.group_id))
                snapshot = list(self._passive_backlog.pop(str(source.group_id), []))
            pending = {entry.task for entry in snapshot if entry.task and not entry.task.done()}
            if pending:
                self._spawn_task(self._prepare_mention(event_obj, source.group_id, snapshot, pending))
            else:
                self._add_context(event_obj, source.group_id, snapshot)
                self._enqueue_batched_event(event_obj)
        except BaseException:
            if dedupe:
                self._event_inflight.discard(dedupe)
            raise

    def _spawn_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._context_tasks.add(task)
        def done(t):
            self._context_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error("[QQ] background task failed: %s", t.exception())
        task.add_done_callback(done)
        return task

    async def _disk_op(self, func, *args):
        # Keep the surrounding lock until the thread has stopped mutating
        # shared state, even if its awaiting coroutine was cancelled.
        worker = asyncio.create_task(asyncio.to_thread(func, *args))
        self._download_workers.add(worker)
        worker.add_done_callback(self._worker_done)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.shield(worker)
            raise

    def _worker_done(self, task):
        self._download_workers.discard(task)
        if not task.cancelled() and task.exception():
            logger.debug("[QQ] file worker ended: %s", task.exception())

    async def _prepare_mention(self, event, group_id, snapshot, pending) -> None:
        try:
            await asyncio.wait(pending, timeout=self.group_file_wait_seconds)
            for entry in snapshot:
                if entry.task and not entry.task.done():
                    entry.note = "(正在下载，可稍后按文件名查本群文件目录)"
            self._add_context(event, group_id, snapshot)
            self._enqueue_batched_event(event)
        except BaseException:
            self._event_inflight.difference_update(event._qq_dedupe_keys)
            raise

    def _add_context(self, event, group_id, snapshot) -> None:
        if group_id:
            context = self._render_passive_context(str(group_id), snapshot)
            if context:
                event.text = f"{context}\n[当前消息] {event.text}"

    def _finish_events(self, keys) -> None:
        if self._journal and keys:
            self._journal.finish(keys)
        now = time.time()
        for key in keys:
            self._event_inflight.discard(key)
            self._seen_events[key] = now

    async def _dispatch_qq_message(self, event) -> None:
        keys = getattr(event, "_qq_dedupe_keys", [])
        try:
            await self.handle_message(event)
            self._finish_events(keys)
        except BaseException:
            self._event_inflight.difference_update(keys)
            raise

    def _batch_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_batched_event(self, event: MessageEvent) -> None:
        if self._batch_delay_seconds <= 0:
            self._spawn_task(self._dispatch_qq_message(event))
            return

        key = self._batch_key(event)
        existing = self._pending_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_batches[key] = event
        else:
            existing._qq_dedupe_keys = getattr(existing, "_qq_dedupe_keys", []) + getattr(event, "_qq_dedupe_keys", [])
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
                if event.message_type != MessageType.TEXT:
                    existing.message_type = event.message_type
            existing.raw_message = event.raw_message
            existing.message_id = event.message_id or existing.message_id

        prior_task = self._pending_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_batch_tasks[key] = asyncio.create_task(self._flush_batched_event(key))

    async def _flush_batched_event(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            pending = self._pending_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            delay = self._split_batch_delay_seconds if last_len >= self._split_threshold else self._batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_batches.pop(key, None)
            if event:
                logger.info(
                    "[QQ] Flushing inbound batch %s (%d chars, %d media)",
                    key,
                    len(event.text or ""),
                    len(event.media_urls or []),
                )
                await self._dispatch_qq_message(event)
        except asyncio.CancelledError:
            pass
        finally:
            if self._pending_batch_tasks.get(key) is current_task:
                self._pending_batch_tasks.pop(key, None)

    def _normalize_event(self, event: dict) -> Optional[dict]:
        post_type = event.get("post_type")
        # OneBot/NapCat emits ``message_sent`` for messages sent by the QQ
        # account itself.  Native gateway adapters must only inject inbound
        # user messages into GatewayRunner; processing outbound echoes can
        # create self-replies and duplicate turns.
        if post_type == "message_sent":
            return None
        if post_type == "message":
            return event
        if post_type != "notice":
            return None
        notice_type = event.get("notice_type")
        if notice_type not in {"offline_file", "group_upload"}:
            return None
        file_info = event.get("file") or {}
        file_name = file_info.get("name") or "未命名文件"
        return {
            **event,
            "user_id": event.get("user_id") or event.get("operator_id"),
            "post_type": "message",
            "message_type": "group" if notice_type == "group_upload" else "private",
            "message": [{"type": "file", "data": {**file_info, "name": file_name}}],
            "raw_message": f"[文件] {file_name}",
        }

    @staticmethod
    def _message_segments(event: dict) -> List[dict]:
        msg = event.get("message")
        if isinstance(msg, str):
            return [{"type": "text", "data": {"text": msg}}]
        if isinstance(msg, list):
            return [seg for seg in msg if isinstance(seg, dict)]
        return []

    def _build_qq_source(self, event: dict) -> QQEventSource:
        sender = event.get("sender") or {}
        return QQEventSource(
            user_id=str(event.get("user_id") or sender.get("user_id") or event.get("operator_id") or ""),
            group_id=str(event.get("group_id")) if event.get("group_id") is not None else None,
            message_id=str(event.get("message_id")) if event.get("message_id") is not None else None,
            self_id=str(event.get("self_id")) if event.get("self_id") is not None else self.bot_user_id,
            raw=event,
        )

    @staticmethod
    def _is_self_message(event: dict) -> bool:
        if event.get("sub_type") == "self":
            return True
        sender = str(event.get("user_id") or (event.get("sender") or {}).get("user_id") or "")
        self_id = str(event.get("self_id") or "")
        return bool(sender and self_id and sender == self_id)

    def _is_ignored_group_user(self, user_id: str) -> bool:
        return _entry_matches(self._group_ignore_from, str(user_id))

    def _is_allowed(self, source: QQEventSource) -> bool:
        if source.group_id:
            # Reject before mention detection, dedupe, context, or media work.
            if self._is_ignored_group_user(source.user_id):
                return False
            if self._group_policy == "disabled":
                return False
            if self._group_policy == "allowlist" and not _entry_matches(self._group_allow_from, source.group_id):
                return False
            if self._allowed_group_users and not _entry_matches(self._allowed_group_users, source.user_id):
                return False
            return True
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, source.user_id)
        return True

    async def _should_process_group_event(self, event: dict, source: QQEventSource) -> bool:
        if self.group_chat_all:
            return True
        for seg in self._message_segments(event):
            if seg.get("type") == "at":
                qq = str((seg.get("data") or {}).get("qq") or "")
                if qq and qq == str(source.self_id or self.bot_user_id):
                    return True
        return False

    def _prune_passive_backlog(self, group_id: Optional[str] = None) -> None:
        """Drop context messages older than the TTL so a quiet group goes quiet."""
        if self.group_listen_context_ttl_seconds <= 0:
            return
        cutoff = time.time() - self.group_listen_context_ttl_seconds
        keys = [group_id] if group_id else list(self._passive_backlog)
        for key in keys:
            backlog = self._passive_backlog.get(key)
            if backlog is None:
                continue
            fresh = [entry for entry in backlog if entry.ts >= cutoff]
            backlog.clear()
            backlog.extend(fresh)
            if not backlog:
                self._passive_backlog.pop(key, None)

    def _render_passive_context(self, group_id: str, snapshot=None) -> str:
        if snapshot is None:
            self._prune_passive_backlog(group_id)
            backlog = list(self._passive_backlog.get(group_id, []))
        else:
            cutoff = time.time() - self.group_listen_context_ttl_seconds
            backlog = [e for e in snapshot if self.group_listen_context_ttl_seconds <= 0 or e.ts >= cutoff]
        group_dir = self._group_file_root() / group_id
        if not backlog:
            return f"[本群文件目录：{group_dir}/；可按文件名查找已收下的文件]" if group_dir.is_dir() else ""
        lines: List[str] = []
        budget = 2500
        for entry in reversed(list(backlog)):
            line = entry.render()
            # Always keep at least one line, even if it is long (file paths matter).
            if lines and len(line) > budget:
                break
            lines.append(line)
            budget -= len(line) + 1
        if not lines:
            return ""
        lines.reverse()
        group_dir = self._group_file_root() / group_id
        header = (
            f"[群聊上下文，共 {len(lines)} 条，仅供参考：不要逐条回复，也不要声称自己参与了这些发言；"
            f"标了本地路径的文件可以直接用文件/终端工具读取；本群已收下的群文件都在 {group_dir}/]"
        )
        return "\n".join([header] + lines)

    def _passive_summary(self, event: dict, source: QQEventSource) -> str:
        parts: List[str] = []
        for seg in self._message_segments(event):
            typ = seg.get("type")
            data = seg.get("data") or {}
            if typ == "text":
                parts.append(str(data.get("text") or ""))
            elif typ == "at":
                qq = str(data.get("qq") or "")
                if qq and qq != str(source.self_id or self.bot_user_id):
                    parts.append(f"@{qq}")
            elif typ == "image":
                parts.append("[图片]")
            elif typ in ("record", "voice", "audio"):
                parts.append("[语音]")
            elif typ == "video":
                parts.append("[视频]")
            elif typ in ("file", "onlinefile"):
                parts.append(f"[文件] {data.get('name') or '未命名文件'}")
            elif typ == "face":
                parts.append("[表情]")
        body = re.sub(r"\s+", " ", "".join(parts)).strip()
        return body[:200]

    def _record_passive_group_event(self, event: dict, source: QQEventSource) -> None:
        """Remember a group message that was not addressed to the bot.

        No Hermes turn is created and no reply is produced; the message only
        becomes read-only context for a later mention.  Media stays a
        placeholder, except group files, which are pulled down in the
        background so a later mention can actually open them.
        """
        if self.group_listen_context <= 0 or not source.group_id:
            return
        body = self._passive_summary(event, source)
        if not body:
            return
        sender = event.get("sender") or {}
        who = str(sender.get("card") or sender.get("nickname") or source.user_id or "?")
        ts = self._event_timestamp(event, time.time())
        entry = _BacklogEntry(
            stamp=datetime.fromtimestamp(ts).strftime("%H:%M"), who=who, body=body, ts=ts
        )
        group_id = str(source.group_id)
        self._prune_passive_backlog(group_id)
        backlog = self._passive_backlog.get(group_id)
        if backlog is None:
            backlog = deque(maxlen=self.group_listen_context)
            self._passive_backlog[group_id] = backlog
        backlog.append(entry)
        self._collect_files_for_entry(event, source, entry)

    # ------------------------------------------------------------------
    # Passive group-file collection
    # ------------------------------------------------------------------

    def _group_file_root(self) -> Path:
        try:
            from hermes_constants import get_hermes_dir

            root = get_hermes_dir("cache/qq_group_files", "qq_group_files")
        except Exception:
            root = Path.home() / ".hermes" / "cache" / "qq_group_files"
        return root

    def _group_file_index_path(self) -> Path:
        return self._group_file_root() / "index.json"

    def _load_group_file_index(self) -> Dict[str, dict]:
        if self._group_file_index is not None:
            return self._group_file_index
        index: Dict[str, dict] = {}
        try:
            path = self._group_file_index_path()
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    index = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:
            logger.debug("[QQ] group file index unreadable: %s", exc)
        self._group_file_index = index
        return index

    def _save_group_file_index(self) -> None:
        index = self._group_file_index or {}
        try:
            path = self._group_file_index_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.debug("[QQ] group file index write failed: %s", exc)

    @staticmethod
    def _safe_file_name(name: str, max_stem: int = 80) -> str:
        base = Path(str(name or "file")).name
        base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", base).strip(" .") or "file"
        stem, dot, ext = base.rpartition(".")
        # Reserve bytes for timestamp, UUID, extension, and .part. A Chinese
        # character uses multiple bytes on the filesystem.
        if dot and 0 < len(ext.encode("utf-8")) <= 20 and stem:
            clipped = stem[:max_stem].encode("utf-8")[:160].decode("utf-8", errors="ignore")
            return f"{clipped}.{ext}"
        return base[:max_stem].encode("utf-8")[:180].decode("utf-8", errors="ignore")

    _SWEEP_INTERVAL_SECONDS = 3600
    _TOMBSTONE_TTL_SECONDS = 180 * 86400

    def _sweep_group_file_cache(self, force: bool = False) -> None:
        """Delete collected files older than the retention window.

        Runs at most once per ``_SWEEP_INTERVAL_SECONDS`` so a long-lived
        gateway still expires files without waiting for a restart.
        """
        now = time.time()
        if not force and now - self._last_file_sweep < self._SWEEP_INTERVAL_SECONDS:
            return
        self._last_file_sweep = now
        cutoff = now - self.group_file_retention_days * 86400 if self.group_file_retention_days > 0 else float("-inf")
        index = self._load_group_file_index()
        removed = 0
        for key, meta in list(index.items()):
            if not meta.get("path"):
                # Tombstone from an earlier sweep: keep it so the connect scan
                # does not re-download a file we deliberately dropped.
                if now - float(meta.get("expired_at") or 0) > self._TOMBSTONE_TTL_SECONDS:
                    index.pop(key, None)
                continue
            path = Path(str(meta.get("path") or ""))
            try:
                if not path.exists() or path.stat().st_mtime < cutoff:
                    if path.exists():
                        path.unlink(missing_ok=True)
                        removed += 1
                    index[key] = {
                        "name": meta.get("name"),
                        "size": meta.get("size"),
                        "group_id": meta.get("group_id"),
                        "src_mtime": meta.get("src_mtime") or meta.get("ts"),
                        "expired_at": now,
                    }
            except OSError:
                index.pop(key, None)
        try:
            stale_parts_cutoff = time.time() - 3600
            for stale in self._group_file_root().rglob("*"):
                if not stale.is_file() or stale.name in {"index.json", "index.tmp"}:
                    continue
                if str(stale) in self._download_reservations or str(stale).removesuffix(".part") in self._download_reservations:
                    continue
                mtime = stale.stat().st_mtime
                if stale.name.endswith(".part"):
                    # Leftovers from an interrupted download.
                    if mtime < stale_parts_cutoff:
                        stale.unlink(missing_ok=True)
                    continue
                if mtime < cutoff:
                    stale.unlink(missing_ok=True)
                    removed += 1
        except OSError as exc:
            logger.debug("[QQ] group file sweep skipped: %s", exc)
        if removed:
            logger.info("[QQ] Group file cache sweep removed %d expired file(s)", removed)
        self._save_group_file_index()
        # Hard backstop, applied after the normal retention pass: retention is
        # the primary rule, the size cap only kicks in when it is not enough.
        self._enforce_cache_size_cap()

    def _cache_index_key_for_path(self, path: Path) -> Optional[str]:
        index = self._load_group_file_index()
        target = str(path)
        for key, meta in index.items():
            if str(meta.get("path") or "") == target:
                return key
        return None

    def _enforce_cache_size_cap(self) -> bool:
        """Evict the oldest collected files while the cache exceeds its cap.

        Retention is the maximum age, not a guaranteed minimum. Capacity may
        remove younger files after the retention pass. Download reservations
        count toward the limit. Evicted files get the same tombstone as
        retention expiries so the connect scan does not fetch them straight
        back.
        """
        cap = self.group_file_cache_max_bytes
        if cap <= 0:
            return True
        root = self._group_file_root()
        files: List[Tuple[float, int, Path]] = []
        reservations = self._download_reservations
        total = sum(reservations.values())
        reserved_parts = {p + ".part" for p in reservations}
        try:
            for path in root.rglob("*"):
                if not path.is_file() or path.name in {"index.json", "index.tmp"}:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if str(path) in reservations or str(path) in reserved_parts:
                    continue
                total += stat.st_size
                if not path.name.endswith(".part"):
                    files.append((stat.st_mtime, stat.st_size, path))
        except OSError as exc:
            logger.debug("[QQ] cache size scan failed: %s", exc)
            return False
        if total <= cap:
            return True

        files.sort(key=lambda item: item[0])
        index = self._load_group_file_index()
        now = time.time()
        evicted = 0
        freed = 0
        for mtime, size, path in files:
            if total <= cap:
                break
            key = self._cache_index_key_for_path(path)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("[QQ] cache eviction failed for %s: %s", path, exc)
                continue
            total -= size
            freed += size
            evicted += 1
            if key:
                meta = index.get(key) or {}
                index[key] = {
                    "name": meta.get("name") or path.name,
                    "size": meta.get("size"),
                    "group_id": meta.get("group_id"),
                    "src_mtime": meta.get("src_mtime") or meta.get("ts") or mtime,
                    "expired_at": now,
                    "reason": "size-cap",
                }
        if evicted:
            self._save_group_file_index()
            logger.warning(
                "[QQ] Group file cache exceeded %.1fGB → evicted %d oldest file(s), freed %.1fMB",
                cap / 1073741824,
                evicted,
                freed / 1048576,
            )
        return total <= cap

    @staticmethod
    def _group_file_key(group_id: str, name: str, size: Any, folder: str = "", uploader: str = "") -> str:
        """Metadata identity; version time and downloaded SHA-256 are separate.

        NapCat rotates file_id on list calls, so it is not a durable identity.
        Uploader and folder distinguish otherwise identical names and sizes.
        """
        try:
            size_part = str(int(size)) if size is not None else "?"
        except (TypeError, ValueError):
            size_part = "?"
        prefix = f"{group_id}/{folder}" if folder else group_id
        identity = f"{prefix}|{Path(str(name or '')).name}|{size_part}"
        return f"{identity}|u:{uploader}" if uploader else identity

    def _cached_group_file(self, source, data, index_key):
        index = self._load_group_file_index()
        incoming = self._event_timestamp({"time": data.get("modify_time") or data.get("upload_time")})
        cached = index.get(f"{index_key}|v:{incoming}") or index.get(index_key)
        if not cached:
            # Preserve valid caches/tombstones from pre-versioned installs.
            legacy = self._group_file_key(str(source.group_id),
                str(data.get("name") or data.get("file_name") or ""),
                data.get("size") or data.get("file_size"), str(data.get("folder_id") or ""))
            old = index.get(legacy)
            if old and (not old.get("user_id") or str(old["user_id"]) == source.user_id):
                cached = old
                index[index_key] = index.pop(legacy)
        if not cached:
            return None, False
        version = self._event_timestamp({"time": cached.get("src_mtime") or cached.get("ts")})
        if incoming and (incoming != version if cached.get("src_mtime") else incoming > version):
            return None, False
        if cached.get("path") and Path(cached["path"]).is_file():
            if not incoming and cached.get("user_id") and source.user_id and str(cached["user_id"]) != source.user_id:
                return None, False
            return str(cached["path"]), False
        if not cached.get("path") and cached.get("expired_at"):
            return None, True
        return None, False

    def _collect_files_for_entry(self, event: dict, source: QQEventSource, entry: _BacklogEntry) -> None:
        if not self.group_file_autosave or not source.group_id:
            return
        for seg in self._message_segments(event):
            if seg.get("type") not in ("file", "onlinefile"):
                continue
            data = dict(seg.get("data") or {})
            data.setdefault("modify_time", self._event_timestamp(event, time.time()))
            file_key = str(data.get("file_id") or data.get("id") or data.get("file") or "") or None
            if not file_key:
                entry.note = entry.note or "(无法定位文件)"
                continue
            name = str(data.get("name") or data.get("file_name") or "")
            index_key = self._group_file_key(
                str(source.group_id), name, data.get("size") or data.get("file_size"), str(data.get("folder_id") or ""), source.user_id
            )
            task_key = f"{index_key}|{data.get('modify_time', '')}"
            task = self._spawn_task(self._collect_into_entry(source, data, file_key, index_key, entry))
            entry.task = task
            self._group_file_tasks[task_key] = task
            task.add_done_callback(lambda _t, key=task_key: self._group_file_tasks.pop(key, None))

    async def _collect_into_entry(
        self,
        source: QQEventSource,
        data: dict,
        file_key: str,
        index_key: str,
        entry: _BacklogEntry,
    ) -> None:
        path, note = await self._download_group_file(source, data, file_key, index_key)
        if path:
            entry.attachment = path
            entry.note = None
        elif note and not entry.note:
            entry.note = note

    # ------------------------------------------------------------------
    # Group file backfill (files uploaded while the gateway was offline)
    # ------------------------------------------------------------------

    _BACKFILL_TOTAL_BUDGET = 1024 * 1024 * 1024

    def _schedule_group_file_backfill(self) -> None:
        if not self.group_file_autosave or not self.group_file_backfill:
            return
        if self.group_file_backfill_limit <= 0 or self._group_policy != "allowlist":
            return
        existing = self._group_file_backfill_task
        if existing and not existing.done():
            return
        self._group_file_backfill_task = self._spawn_task(self._backfill_group_files())

    async def _list_group_files(self, group_id: str) -> List[dict]:
        listing = await asyncio.to_thread(self._client.get_group_root_files, group_id)
        pending = [("", listing)]
        visited = {""}
        files = []
        while pending:
            folder, listing = pending.pop(0)
            if not isinstance(listing, dict):
                continue
            for info in listing.get("files", []):
                if isinstance(info, dict):
                    files.append({**info, "folder_id": folder})
            for info in listing.get("folders", []):
                child = str(info.get("folder_id") or info.get("id") or "")
                if not child or child in visited:
                    continue
                if len(visited) >= 10000:
                    raise ValueError("QQ group folder enumeration limit reached")
                visited.add(child)
                contents = await asyncio.to_thread(self._client.get_group_files_by_folder, group_id, child)
                pending.append((child, contents))
        return files

    async def _backfill_group_files(self) -> None:
        """Enumerate all files; only downloads are subject to per-pass budgets."""
        self._last_group_backfill = time.time()
        self._file_backfill_pending = False
        budget = self._BACKFILL_TOTAL_BUDGET
        recent_cutoff = time.time() - max(self.history_backfill_seconds, 0)
        for group_id in [g for g in self._group_allow_from if g != "*"]:
            try:
                candidates = await self._list_group_files(group_id)
            except Exception as exc:
                logger.warning("[QQ] group file enumeration failed for %s: %s", group_id, exc)
                continue
            candidates.sort(key=lambda f: self._event_timestamp({"time": f.get("modify_time") or f.get("upload_time")}), reverse=True)
            added = 0
            for info in candidates:
                source = QQEventSource(user_id=str(info.get("uploader") or ""), group_id=str(group_id),
                                       message_id=None, self_id=self.bot_user_id, raw=info)
                file_key = str(info.get("file_id") or info.get("id") or "")
                if not file_key:
                    continue
                name = str(info.get("file_name") or info.get("name") or "文件")
                size = info.get("size") or info.get("file_size")
                data = {**info, "name": name, "size": size}
                index_key = self._group_file_key(str(group_id), name, size, str(info.get("folder_id") or ""), source.user_id)
                async with self._maintenance_lock:
                    path, expired = self._cached_group_file(source, data, index_key)
                if expired:
                    continue
                if not path:
                    if added >= self.group_file_backfill_limit or budget <= 0:
                        self._file_backfill_pending = True
                        continue
                    try:
                        declared = int(size or 0)
                    except (TypeError, ValueError):
                        declared = 0
                    if declared > budget:
                        self._file_backfill_pending = True
                        continue
                    data["download_limit"] = budget
                    path, _note = await self._download_group_file(source, data, file_key, index_key)
                    if not path or not Path(path).is_file():
                        continue
                    budget -= Path(path).stat().st_size
                    added += 1
                modified = self._event_timestamp({"time": info.get("modify_time") or info.get("upload_time")}, time.time())
                if not self._is_ignored_group_user(source.user_id) and modified >= recent_cutoff:
                    self._append_backlog_file_entry(
                        group_id=str(group_id), stamp=datetime.fromtimestamp(modified).strftime("%H:%M"),
                        who=str(info.get("uploader_name") or source.user_id or "群文件"), name=name,
                        path=path, ts=modified,
                    )
            logger.info("[QQ] Group file scan %s: enumerated=%d collected=%d more=%s",
                        group_id, len(candidates), added, self._file_backfill_pending)

    def _append_backlog_file_entry(
        self,
        *,
        group_id: str,
        stamp: str,
        who: str,
        name: str,
        path: str,
        ts: Optional[float] = None,
    ) -> None:
        if self.group_listen_context <= 0:
            return
        self._prune_passive_backlog(group_id)
        backlog = self._passive_backlog.get(group_id)
        if backlog is None:
            backlog = deque(maxlen=self.group_listen_context)
            self._passive_backlog[group_id] = backlog
        if any(entry.attachment == path for entry in backlog):
            return
        backlog.append(
            _BacklogEntry(
                stamp=stamp,
                who=who,
                body=f"[文件] {name}",
                attachment=path,
                ts=time.time() if ts is None else ts,
            )
        )

    async def _download_group_file(self, source, data, file_key, index_key):
        # Both live notices and directory scans join the same versioned job.
        version = data.get("modify_time") or data.get("upload_time") or 0
        token = f"{index_key}|{version}"
        task = self._group_download_tasks.get(token)
        if task is None:
            task = self._spawn_task(self._download_group_file_impl(source, dict(data), file_key, index_key))
            self._group_download_tasks[token] = task
            task.add_done_callback(lambda t, key=token: self._group_download_tasks.pop(key, None))
        return await asyncio.shield(task)

    async def _download_group_file_impl(
        self,
        source: QQEventSource,
        data: dict,
        file_key: str,
        index_key: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Save one group file locally.

        Returns ``(path, note)``: exactly one of them is set.  Never raises for
        ordinary failures — callers record the note in the backlog instead.
        """
        name = str(data.get("name") or data.get("file_name") or f"qq_file_{file_key[:12]}")
        declared = data.get("size") or data.get("file_size")
        try:
            declared_bytes = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            declared_bytes = None
        if declared_bytes and self.group_file_max_bytes and declared_bytes > self.group_file_max_bytes:
            return None, f"({declared_bytes / 1048576:.1f}MB，超过本地保存上限)"

        async with self._version_locks[hash(index_key) % len(self._version_locks)], self._download_slots:
            async with self._maintenance_lock:
                path, expired = self._cached_group_file(source, data, index_key)
                if path or expired:
                    return path, "(文件已按缓存策略清理)" if expired else None
            return await self._save_group_download(source, data, file_key, index_key, name, declared_bytes)

    async def _save_group_download(self, source, data, file_key, index_key, name, declared_bytes):
        group_id = str(source.group_id)
        target_dir = self._group_file_root() / group_id
        target_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        dest = target_dir / f"{datetime.fromtimestamp(ts).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}-{self._safe_file_name(name)}"
        limits = [n for n in (self.group_file_max_bytes, self.group_file_cache_max_bytes, int(data.get("download_limit") or 0)) if n > 0]
        limit = min(limits) if limits else 0
        if declared_bytes and limit and declared_bytes > limit:
            return None, "(文件超过本次下载/缓存上限)"
        if declared_bytes and declared_bytes > 0:
            limit = min(limit, declared_bytes) if limit else declared_bytes
        reservation = limit
        async with self._maintenance_lock:
            self._download_reservations[str(dest)] = reservation
            await self._disk_op(self._sweep_group_file_cache, True)
            fits = await self._disk_op(self._enforce_cache_size_cap)
            if not fits:
                self._download_reservations.pop(str(dest), None)
                return None, "(缓存空间暂不足，请稍后重试)"
        try:
            urls: List[str] = []
            try:
                meta = await asyncio.to_thread(self._client.get_group_file_url, group_id, file_key, data.get("busid"))
                if isinstance(meta, dict):
                    urls.extend(str(meta[k]) for k in ("url", "download_url") if meta.get(k))
            except Exception as exc:
                logger.debug("[QQ] get_group_file_url failed for %s: %s", name, exc)
            urls.extend(str(data[k]) for k in ("url", "path") if data.get(k))
            for url in urls:
                if not url.startswith(("http://", "https://")):
                    continue
                try:
                    # Keep existing fetch signature for embedders; the limit is
                    # enforced from the actual byte stream, not just metadata.
                    worker = asyncio.create_task(asyncio.to_thread(self._fetch_group_file, url, dest, limit))
                    self._download_workers.add(worker)
                    worker.add_done_callback(self._worker_done)
                    size = await asyncio.shield(worker)
                except Exception as exc:
                    logger.debug("[QQ] group file download failed for %s: %s", name, exc)
                    continue
                if self._download_stop.is_set():
                    dest.unlink(missing_ok=True)
                    return None, "(下载已停止)"
                digest = await asyncio.to_thread(self._file_digest, dest)
                async with self._maintenance_lock:
                    index = self._load_group_file_index()
                    prior = index.get(index_key) or {}
                    incoming = data.get("modify_time") or data.get("upload_time") or ts
                    record = {
                        "name": name, "path": str(dest), "size": size, "sha256": digest,
                        "ts": ts, "group_id": group_id, "user_id": source.user_id,
                        "file_id": file_key, "folder_id": data.get("folder_id") or "",
                        "src_mtime": incoming,
                    }
                    # Keep previous versions addressable without replacing the
                    # newest index with a slower/older listing result.
                    prior_time = self._event_timestamp({"time": prior.get("src_mtime")})
                    if prior and prior_time and prior_time < float(incoming):
                        index[f"{index_key}|v:{prior_time}"] = prior
                    if prior_time <= float(incoming):
                        index[index_key] = record
                    else:
                        index[f"{index_key}|v:{float(incoming)}"] = record
                    self._download_reservations.pop(str(dest), None)
                    await self._disk_op(self._sweep_group_file_cache, True)
                    await self._disk_op(self._enforce_cache_size_cap)
                logger.info("[QQ] Collected group file %s (%d bytes)", name, size)
                if dest.is_file():
                    return str(dest), None
                return None, "(文件已按缓存策略清理)"
            return None, "(未能保存到本地)"
        finally:
            async with self._maintenance_lock:
                self._download_reservations.pop(str(dest), None)

    @staticmethod
    def _file_digest(path: Path) -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def _fetch_group_file(self, url: str, dest: Path, max_bytes: int) -> int:
        return stream_download(url, dest, max_bytes, self.request_timeout, self._download_stop)

    def _event_dedupe_key(self, event: dict, source: QQEventSource) -> Optional[str]:
        if source.message_id:
            return f"{source.group_id or 'private'}:{source.user_id}:{source.message_id}"
        raw = str(event.get("raw_message") or "").strip()
        ts = event.get("time")
        if raw and ts is not None:
            return f"{source.group_id or 'private'}:{source.user_id}:{ts}:{raw[:200]}"
        return None

    def _remember_event(self, key: str, persistent: bool = True) -> bool:
        now = time.time()
        expiry = now - 6 * 3600
        for stale in [k for k, ts in self._seen_events.items() if ts < expiry]:
            self._seen_events.pop(stale, None)
        if key in self._seen_events or key in self._event_inflight:
            return False
        if persistent and self._journal and self._journal.contains(key):
            return False
        self._event_inflight.add(key)
        return True

    async def _build_message_parts(self, event: dict, source: QQEventSource) -> Tuple[str, List[str], List[str], MessageType]:
        text_parts: List[str] = []
        media_urls: List[str] = []
        media_types: List[str] = []
        for seg in self._message_segments(event):
            typ = seg.get("type")
            data = seg.get("data") or {}
            if typ == "text":
                text_parts.append(str(data.get("text") or ""))
            elif typ == "at":
                qq = str(data.get("qq") or "")
                if qq and qq != str(source.self_id or self.bot_user_id):
                    text_parts.append(f"@{qq}")
            elif typ == "image":
                path = await self._resolve_media(data, "image")
                if path:
                    media_urls.append(path)
                    media_types.append("image/jpeg")
                else:
                    raise RuntimeError("QQ image download unavailable; message remains retryable")
            elif typ in ("record", "voice", "audio"):
                path = await self._resolve_record(data)
                if path:
                    media_urls.append(path)
                    media_types.append("audio/mpeg")
                else:
                    raise RuntimeError("QQ audio download unavailable; message remains retryable")
            elif typ == "video":
                path = await self._resolve_media(data, "video")
                if path:
                    media_urls.append(path)
                    media_types.append("video/mp4")
                else:
                    raise RuntimeError("QQ video download unavailable; message remains retryable")
            elif typ in ("file", "onlinefile"):
                path = await self._resolve_file(source, data)
                if path:
                    text_parts.append(f"[Attachment: {Path(path).name}]")
                    media_urls.append(path)
                    media_types.append("application/octet-stream")
                else:
                    raise RuntimeError("QQ attachment download unavailable; retry remains possible")

        text = re.sub(r"\s+", " ", "".join(text_parts)).strip()
        if media_types:
            if any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO
            elif any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("video/") for mt in media_types):
                msg_type = MessageType.VIDEO
            else:
                msg_type = MessageType.DOCUMENT
        else:
            msg_type = MessageType.COMMAND if text.startswith("/") else MessageType.TEXT
        return text, media_urls, media_types, msg_type

    async def _resolve_record(self, data: dict) -> Optional[str]:
        file_key = data.get("file_id") or data.get("file")
        if file_key:
            try:
                meta = await asyncio.to_thread(self._client.get_record, str(file_key), "mp3")
                return await self._cache_media_from_meta(meta, "audio")
            except Exception:
                pass
        return await self._download_first([data.get("path"), data.get("url"), data.get("file")], "audio")

    async def _resolve_media(self, data: dict, prefix: str) -> Optional[str]:
        file_key = data.get("file")
        if prefix == "image" and file_key:
            try:
                meta = await asyncio.to_thread(self._client.get_image, str(file_key))
                cached = await self._cache_media_from_meta(meta, prefix)
                if cached:
                    return cached
            except Exception:
                pass
        return await self._download_first([data.get("url"), data.get("file"), data.get("path")], prefix)

    async def _resolve_file(self, source: QQEventSource, data: dict) -> Optional[str]:
        candidates = [data.get("url"), data.get("file"), data.get("path")]
        file_id = str(data.get("file_id") or data.get("id") or "") or None
        if file_id:
            try:
                if source.group_id:
                    meta = await asyncio.to_thread(self._client.get_group_file_url, source.group_id, file_id, data.get("busid"))
                else:
                    meta = await asyncio.to_thread(self._client.get_private_file_url, source.user_id, file_id)
                candidates = [meta.get("url"), meta.get("download_url")] + candidates
            except Exception:
                pass
        return await self._download_first(candidates, "file")

    async def _cache_media_from_meta(self, meta: dict, prefix: str) -> Optional[str]:
        if not isinstance(meta, dict):
            return None
        b64 = meta.get("base64")
        if isinstance(b64, str) and b64:
            try:
                if self.group_file_max_bytes and len(b64) > (self.group_file_max_bytes + 2) // 3 * 4:
                    raise ValueError("QQ base64 media exceeds single-file limit")
                data = base64.b64decode(b64, validate=True)
                if self.group_file_max_bytes and len(data) > self.group_file_max_bytes:
                    raise ValueError("QQ media exceeds single-file limit")
                if prefix == "image":
                    return cache_image_from_bytes(data, ".jpg")
                suffix = ".mp3" if prefix == "audio" else ".bin"
                return cache_document_from_bytes(data, f"qq_{prefix}{suffix}")
            except Exception:
                pass
        return await self._download_first([meta.get("url"), meta.get("download_url"), meta.get("file"), meta.get("path")], prefix)

    async def _download_first(self, candidates: List[Any], prefix: str) -> Optional[str]:
        for raw in candidates:
            if not raw:
                continue
            value = str(raw)
            path = Path(value).expanduser()
            if path.is_file():
                if self.group_file_max_bytes and path.stat().st_size > self.group_file_max_bytes:
                    continue
                return str(path.resolve())
            if not value.startswith(("http://", "https://")):
                continue
            try:
                suffix = Path(value.split("?", 1)[0]).suffix
                root = get_image_cache_dir() if prefix == "image" else get_document_cache_dir()
                name = Path(value.split("?", 1)[0]).name or f"qq_{prefix}{suffix or '.bin'}"
                dest = root / f"qq_{uuid.uuid4().hex[:12]}-{self._safe_file_name(name)}"
                async with self._download_slots:
                    worker = asyncio.create_task(asyncio.to_thread(self._fetch_group_file, value, dest, self.group_file_max_bytes))
                    self._download_workers.add(worker)
                    worker.add_done_callback(self._worker_done)
                    await asyncio.shield(worker)
                return str(dest)
            except Exception as exc:
                logger.debug("[QQ] media download failed: %s", type(exc).__name__)
        return None
