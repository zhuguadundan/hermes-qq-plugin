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
import inspect
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests

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
)

from .client import NapCatClient
from .types import QQEventSource

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

    def render(self) -> str:
        line = f"{self.stamp} {self.who}: {self.body}"
        if self.attachment:
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
        self.request_timeout = int(extra.get("request_timeout") or 60)
        self.chunk_size = int(extra.get("chunk_size") or 65536)
        self.group_chat_all = bool(extra.get("group_chat_all", False))
        # Passive group backlog: number of recent non-mentioned group messages
        # kept in memory and prepended as read-only context when the bot is
        # finally mentioned.  0 disables listening entirely.
        self.group_listen_context = max(0, int(extra.get("group_listen_context") or 0))
        # How long a passive message stays usable as context.  Without this a
        # quiet group would still inject week-old chatter on the next mention.
        self.group_listen_context_ttl_seconds = max(
            0.0, float(extra.get("group_listen_context_ttl_minutes", 120)) * 60
        )
        self._passive_backlog: Dict[str, Deque["_BacklogEntry"]] = {}
        # Passive group-file collection.  QQ groups cannot mention-attach a
        # file, so a bare upload is the only signal; we save it locally while
        # nobody is talking to us so a later mention can actually read it.
        self.group_file_autosave = bool(extra.get("group_file_autosave", True))
        self.group_file_backfill = bool(extra.get("group_file_backfill", True))
        self.group_file_backfill_limit = max(0, int(extra.get("group_file_backfill_limit", 10)))
        self.group_file_max_bytes = int(float(extra.get("group_file_max_mb", 30)) * 1024 * 1024)
        self.group_file_retention_days = float(extra.get("group_file_retention_days", 7))
        # Backstop on total cache size.  0 disables it; it never replaces the
        # retention window, it only evicts oldest-first when retention is not
        # enough (e.g. a burst of large uploads).
        self.group_file_cache_max_bytes = int(float(extra.get("group_file_cache_max_gb", 5)) * 1073741824)
        self._group_file_index: Optional[Dict[str, dict]] = None
        self._group_file_tasks: Dict[str, asyncio.Task] = {}
        self._group_file_backfill_task: Optional[asyncio.Task] = None
        self._last_file_sweep = 0.0
        self.enable_online_file = bool(extra.get("enable_online_file", True))
        self.history_backfill_count = int(extra.get("history_backfill_count") or 20)
        self.history_backfill_seconds = int(extra.get("history_backfill_seconds") or 600)
        self.resync_interval_seconds = float(extra.get("resync_interval_seconds") or 3)
        self._ws_reconnect_delay_seconds = float(extra.get("ws_reconnect_delay_seconds") or 3)
        self._ws_ping_interval_seconds = float(extra.get("ws_ping_interval_seconds") or 25)
        self._ws_ping_timeout_seconds = float(extra.get("ws_ping_timeout_seconds") or 15)
        self._health_check_interval_seconds = float(
            extra.get("health_check_interval_seconds") or max(15.0, self.resync_interval_seconds)
        )
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
        self._batch_delay_seconds = float(extra.get("batch_delay_seconds") or 1.2)
        self._split_batch_delay_seconds = float(extra.get("split_batch_delay_seconds") or 2.5)
        self._split_threshold = int(extra.get("split_threshold") or 1800)
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
            self._qq_should_run = True
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
        self._mark_disconnected()
        for task in (self._listen_task, self._health_task):
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._listen_task = None
        self._health_task = None
        for task in list(self._pending_batch_tasks.values()):
            if not task.done():
                task.cancel()
        self._pending_batch_tasks.clear()
        self._pending_batches.clear()

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        source = self._source_from_chat_id(chat_id)
        try:
            result = await asyncio.to_thread(self._client.send_text, source, content)
            data = result.get("data") or {}
            return SendResult(success=True, message_id=str(data.get("message_id") or ""), raw_response=result)
        except Exception as exc:
            logger.warning("[QQ] send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

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
                    await self._backfill_recent_messages()
                    self._schedule_group_file_backfill()
                    async for payload in ws:
                        if not self._qq_should_run:
                            break
                        try:
                            event = json.loads(payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload))
                        except Exception:
                            continue
                        try:
                            await self._handle_onebot_event(event, origin="ws")
                        except Exception as event_exc:
                            logger.warning("[QQ] Failed to process OneBot event: %s", event_exc, exc_info=True)
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
                    self._sweep_group_file_cache()
                    self._prune_passive_backlog()
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
        if dedupe and not self._remember_event(dedupe):
            return
        if not addressed:
            self._record_passive_group_event(normalized, source)
            return

        text, media_urls, media_types, msg_type = await self._build_message_parts(normalized, source)
        if not text.strip() and not media_urls:
            return
        chat_id = f"group:{source.group_id}" if source.group_id else f"private:{source.user_id}"
        sender = normalized.get("sender") or {}
        if source.group_id and self.group_listen_context > 0 and msg_type != MessageType.COMMAND:
            context = self._render_passive_context(str(source.group_id))
            if context:
                text = f"{context}\n[当前消息] {text}" if text else context
                self._passive_backlog.pop(str(source.group_id), None)

        event_obj = MessageEvent(
            text=text,
            message_type=msg_type,
            source=self.build_source(
                chat_id=chat_id,
                chat_name=str(source.group_id) if source.group_id else str(source.user_id),
                chat_type="group" if source.group_id else "dm",
                user_id=source.user_id,
                user_name=str(sender.get("card") or sender.get("nickname") or source.user_id),
                message_id=source.message_id,
            ),
            raw_message=normalized,
            message_id=source.message_id,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=datetime.fromtimestamp(self._event_timestamp(normalized, time.time())),
        )
        if event_obj.is_command():
            await self.handle_message(event_obj)
            return
        self._enqueue_batched_event(event_obj)

    def _batch_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_batched_event(self, event: MessageEvent) -> None:
        if self._batch_delay_seconds <= 0:
            asyncio.create_task(self.handle_message(event))
            return

        key = self._batch_key(event)
        existing = self._pending_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_batches[key] = event
        else:
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
                await asyncio.shield(self.handle_message(event))
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

    def _render_passive_context(self, group_id: str) -> str:
        self._prune_passive_backlog(group_id)
        backlog = self._passive_backlog.get(group_id)
        if not backlog:
            return ""
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
        if dot and 0 < len(ext) <= 10 and stem:
            return f"{stem[:max_stem]}.{ext}"
        return base[:max_stem]

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
        if self.group_file_retention_days <= 0:
            return
        cutoff = now - self.group_file_retention_days * 86400
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
                if not stale.is_file() or stale.name == "index.json":
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

    def _enforce_cache_size_cap(self) -> None:
        """Evict the oldest collected files while the cache exceeds its cap.

        This is a last-resort guard on top of the retention window: it never
        shortens retention, it only stops a burst of large uploads from growing
        the directory without bound.  Evicted files get the same tombstone as
        retention expiries so the connect scan does not fetch them straight
        back.
        """
        cap = self.group_file_cache_max_bytes
        if cap <= 0:
            return
        root = self._group_file_root()
        files: List[Tuple[float, int, Path]] = []
        total = 0
        try:
            for path in root.rglob("*"):
                if not path.is_file() or path.name == "index.json" or path.name.endswith(".part"):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
        except OSError as exc:
            logger.debug("[QQ] cache size scan failed: %s", exc)
            return
        if total <= cap:
            return

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

    @staticmethod
    def _group_file_key(group_id: str, name: str, size: Any) -> str:
        """Content-stable cache key for a group file.

        NapCat hands out a fresh ``file_id`` on every ``get_group_root_files``
        call, so ids cannot be used for dedupe.  Name + size stays stable across
        calls and across the notice-vs-scan paths.
        """
        try:
            size_part = str(int(size)) if size is not None else "?"
        except (TypeError, ValueError):
            size_part = "?"
        return f"{group_id}|{Path(str(name or '')).name}|{size_part}"

    def _collect_files_for_entry(self, event: dict, source: QQEventSource, entry: _BacklogEntry) -> None:
        if not self.group_file_autosave or not source.group_id:
            return
        for seg in self._message_segments(event):
            if seg.get("type") not in ("file", "onlinefile"):
                continue
            data = seg.get("data") or {}
            file_key = str(data.get("file_id") or data.get("id") or data.get("file") or "") or None
            if not file_key:
                entry.note = entry.note or "(无法定位文件)"
                continue
            name = str(data.get("name") or data.get("file_name") or "")
            index_key = self._group_file_key(
                str(source.group_id), name, data.get("size") or data.get("file_size")
            )
            index = self._load_group_file_index()
            cached = index.get(index_key)
            if cached and cached.get("path") and Path(str(cached["path"])).exists():
                entry.attachment = str(cached["path"])
                continue
            if index_key in self._group_file_tasks:
                continue
            task = asyncio.create_task(self._collect_into_entry(source, data, file_key, index_key, entry))
            self._group_file_tasks[index_key] = task
            task.add_done_callback(lambda _t, key=index_key: self._group_file_tasks.pop(key, None))

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
        self._group_file_backfill_task = asyncio.create_task(self._backfill_group_files())

    async def _backfill_group_files(self) -> None:
        """Pull recent files that are already sitting in the group file list.

        ``group_upload`` notices are only delivered while connected, so anything
        uploaded during a restart window would otherwise never be seen.
        """
        budget = self._BACKFILL_TOTAL_BUDGET
        recent_cutoff = time.time() - max(self.history_backfill_seconds, 0)
        for group_id in [g for g in self._group_allow_from if g != "*"]:
            try:
                listing = await asyncio.to_thread(self._client.get_group_root_files, group_id)
            except Exception as exc:
                logger.debug("[QQ] group file listing failed for %s: %s", group_id, exc)
                continue
            files = listing.get("files") if isinstance(listing, dict) else listing
            if not isinstance(files, list):
                continue
            candidates = [f for f in files if isinstance(f, dict)]
            candidates.sort(key=lambda f: self._event_timestamp({"time": f.get("modify_time") or f.get("upload_time")}), reverse=True)
            added = 0
            for info in candidates:
                if added >= self.group_file_backfill_limit or budget <= 0:
                    break
                file_key = str(info.get("file_id") or info.get("id") or "") or None
                if not file_key:
                    continue
                index_key = self._group_file_key(
                    str(group_id),
                    str(info.get("file_name") or info.get("name") or ""),
                    info.get("size") or info.get("file_size"),
                )
                index = self._load_group_file_index()
                cached = index.get(index_key)
                # Keep file archiving independent, but do not reintroduce an
                # ignored sender's uploads through the file-list context path.
                include_context = not self._is_ignored_group_user(str(info.get("uploader") or ""))
                modified = self._event_timestamp(
                    {"time": info.get("modify_time") or info.get("upload_time")}, time.time()
                )
                if cached and not cached.get("path"):
                    # Tombstoned by the retention sweep.  Skip the exact same
                    # version; a genuinely newer upload of the same name+size
                    # gets collected again.
                    if self._event_timestamp({"time": cached.get("src_mtime")}) >= modified:
                        continue
                if cached and cached.get("path") and Path(str(cached["path"])).exists():
                    # Already collected: surface it as recent context (so a
                    # restart does not hide a file uploaded moments ago), but
                    # never let old files crowd out real chat context.
                    if include_context and modified >= recent_cutoff:
                        self._append_backlog_file_entry(
                            group_id=str(group_id),
                            stamp=datetime.fromtimestamp(modified).strftime("%H:%M"),
                            who=str(info.get("uploader_name") or info.get("uploader") or "群文件"),
                            name=str(info.get("file_name") or cached.get("name") or "文件"),
                            path=str(cached["path"]),
                            ts=modified,
                        )
                    continue
                size = info.get("size") or info.get("file_size")
                try:
                    size_bytes = int(size) if size is not None else 0
                except (TypeError, ValueError):
                    size_bytes = 0
                if size_bytes and size_bytes > budget:
                    continue
                data = {
                    "name": info.get("file_name") or info.get("name"),
                    "size": size,
                    "busid": info.get("busid"),
                    "file_id": file_key,
                    "modify_time": info.get("modify_time") or info.get("upload_time"),
                }
                source = QQEventSource(
                    user_id=str(info.get("uploader") or ""),
                    group_id=str(group_id),
                    message_id=None,
                    self_id=self.bot_user_id,
                    raw=info,
                )
                path, _note = await self._download_group_file(source, data, file_key, index_key)
                if not path:
                    continue
                budget -= max(size_bytes, 0)
                added += 1
                if include_context and modified >= recent_cutoff:
                    self._append_backlog_file_entry(
                        group_id=str(group_id),
                        stamp=datetime.fromtimestamp(modified).strftime("%H:%M"),
                        who=str(info.get("uploader_name") or info.get("uploader") or "群文件"),
                        name=str(data.get("name") or "文件"),
                        path=path,
                        ts=modified,
                    )
            if added:
                logger.info("[QQ] Group file backfill collected %d file(s) for %s", added, group_id)

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

    async def _download_group_file(
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

        self._sweep_group_file_cache()
        group_id = str(source.group_id)
        target_dir = self._group_file_root() / group_id
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("[QQ] group file dir unavailable: %s", exc)
            return None, "(本地保存目录不可写)"

        ts = self._event_timestamp(data, time.time())
        dest = target_dir / f"{datetime.fromtimestamp(ts).strftime('%Y%m%d-%H%M%S')}-{self._safe_file_name(name)}"
        suffix = 1
        while dest.exists():
            dest = target_dir / (
                f"{datetime.fromtimestamp(ts).strftime('%Y%m%d-%H%M%S')}-{suffix}-{self._safe_file_name(name)}"
            )
            suffix += 1

        urls: List[str] = []
        try:
            meta = await asyncio.to_thread(
                self._client.get_group_file_url, group_id, file_key, data.get("busid")
            )
            if isinstance(meta, dict):
                urls.extend(str(meta.get(k)) for k in ("url", "download_url") if meta.get(k))
        except Exception as exc:
            logger.debug("[QQ] get_group_file_url failed for %s: %s", name, exc)
        urls.extend(str(data.get(k)) for k in ("url", "path") if data.get(k))

        for url in urls:
            if not url.startswith(("http://", "https://")):
                continue
            try:
                size = await asyncio.to_thread(
                    self._fetch_group_file, url, dest, self.group_file_max_bytes
                )
            except Exception as exc:
                logger.debug("[QQ] group file download failed for %s: %s", name, exc)
                continue
            index = self._load_group_file_index()
            index[index_key] = {
                "name": name,
                "path": str(dest),
                "size": size,
                "ts": ts,
                "group_id": group_id,
                "user_id": source.user_id,
                "file_id": file_key,
                "src_mtime": data.get("modify_time") or ts,
            }
            self._save_group_file_index()
            logger.info("[QQ] Collected group file %s → %s (%d bytes)", name, dest, size)
            try:
                self._enforce_cache_size_cap()
            except Exception as exc:  # never fail a collection because of eviction
                logger.debug("[QQ] cache size cap check failed: %s", exc)
            return str(dest), None

        return None, "(未能保存到本地)"

    def _fetch_group_file(self, url: str, dest: Path, max_bytes: int) -> int:
        """Stream *url* into *dest*, aborting once *max_bytes* is exceeded."""
        tmp = dest.with_name(dest.name + ".part")
        total = 0
        try:
            with requests.get(url, timeout=self.request_timeout, stream=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as handle:
                    for chunk in resp.iter_content(65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if max_bytes and total > max_bytes:
                            raise ValueError(f"file exceeds local limit of {max_bytes} bytes")
                        handle.write(chunk)
            tmp.replace(dest)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return total

    def _event_dedupe_key(self, event: dict, source: QQEventSource) -> Optional[str]:
        if source.message_id:
            return f"{source.group_id or 'private'}:{source.user_id}:{source.message_id}"
        raw = str(event.get("raw_message") or "").strip()
        ts = event.get("time")
        if raw and ts is not None:
            return f"{source.group_id or 'private'}:{source.user_id}:{ts}:{raw[:200]}"
        return None

    def _remember_event(self, key: str) -> bool:
        now = time.time()
        expiry = now - 6 * 3600
        for stale in [k for k, ts in self._seen_events.items() if ts < expiry]:
            self._seen_events.pop(stale, None)
        if key in self._seen_events:
            return False
        self._seen_events[key] = now
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
            elif typ in ("record", "voice", "audio"):
                path = await self._resolve_record(data)
                if path:
                    media_urls.append(path)
                    media_types.append("audio/mpeg")
            elif typ == "video":
                path = await self._resolve_media(data, "video")
                if path:
                    media_urls.append(path)
                    media_types.append("video/mp4")
            elif typ in ("file", "onlinefile"):
                path = await self._resolve_file(source, data)
                if path:
                    text_parts.append(f"[Attachment: {Path(path).name}]")
                    media_urls.append(path)
                    media_types.append("application/octet-stream")

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
        file_id = str(data.get("file_id") or "") or None
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
                data = base64.b64decode(b64)
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
            if path.exists():
                return str(path.resolve())
            if not value.startswith(("http://", "https://")):
                continue
            try:
                resp = await asyncio.to_thread(requests.get, value, timeout=self.request_timeout)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                suffix = Path(value.split("?", 1)[0]).suffix
                if prefix == "image":
                    return cache_image_from_bytes(resp.content, suffix if suffix.lower() in IMAGE_EXTS else ".jpg")
                filename = Path(value.split("?", 1)[0]).name or f"qq_{prefix}{suffix or '.bin'}"
                return cache_document_from_bytes(resp.content, filename)
            except Exception as exc:
                logger.debug("[QQ] media download failed for %s: %s", value[:80], exc)
        return None
