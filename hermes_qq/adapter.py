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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        self.enable_online_file = bool(extra.get("enable_online_file", True))
        self.history_backfill_count = int(extra.get("history_backfill_count") or 20)
        self._dm_policy = str(extra.get("dm_policy", "open")).lower()
        self._group_policy = str(extra.get("group_policy", "open")).lower()
        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
        self._allowed_group_users = _coerce_list(extra.get("allowed_group_users"))
        self._seen_events: Dict[str, float] = {}
        self._target_sequences: Dict[str, int] = {}
        self._pending_batches: Dict[str, MessageEvent] = {}
        self._pending_batch_tasks: Dict[str, asyncio.Task] = {}
        self._batch_delay_seconds = float(extra.get("batch_delay_seconds") or 1.2)
        self._split_batch_delay_seconds = float(extra.get("split_batch_delay_seconds") or 2.5)
        self._split_threshold = int(extra.get("split_threshold") or 1800)
        self._client = NapCatClient(self.onebot_url, self.onebot_token, self.request_timeout, self.chunk_size)
        self._listen_task: Optional[asyncio.Task] = None
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
            self._running = True
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._mark_connected()
            logger.info("[QQ] Connected to NapCat as %s (%s)", self.bot_name, self.bot_user_id)
            return True
        except Exception as exc:
            self._set_fatal_error("qq_connect_error", f"QQ startup failed: {exc}", retryable=True)
            logger.error("[QQ] Startup failed: %s", exc, exc_info=True)
            self._running = False
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
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
    def _websocket_connect_kwargs(headers: Optional[Dict[str, str]]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"ping_interval": None, "max_size": 2 * 1024 * 1024}
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
        while self._running:
            try:
                headers = {"Authorization": f"Bearer {self.onebot_ws_token}"} if self.onebot_ws_token else None
                ws_ctx = websockets.connect(
                    self.onebot_ws_url,
                    **self._websocket_connect_kwargs(headers),
                )
                async with ws_ctx as ws:
                    self._mark_connected()
                    logger.info("[QQ] WebSocket connected: %s", self.onebot_ws_url)
                    async for payload in ws:
                        if not self._running:
                            break
                        try:
                            event = json.loads(payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload))
                        except Exception:
                            continue
                        await self._handle_onebot_event(event, origin="ws")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._running:
                    self._mark_disconnected()
                    logger.warning("[QQ] WebSocket error: %s; reconnecting in 3s", exc)
                    await asyncio.sleep(3)

    async def _handle_onebot_event(self, event: dict, origin: str = "ws") -> None:
        normalized = self._normalize_event(event)
        if normalized is None or self._is_self_message(normalized):
            return
        source = self._build_qq_source(normalized)
        if not source.user_id or not self._is_allowed(source):
            return
        if source.group_id and not await self._should_process_group_event(normalized, source):
            return
        dedupe = self._event_dedupe_key(normalized, source)
        if dedupe and not self._remember_event(dedupe):
            return

        text, media_urls, media_types, msg_type = await self._build_message_parts(normalized, source)
        if not text.strip() and not media_urls:
            return
        chat_id = f"group:{source.group_id}" if source.group_id else f"private:{source.user_id}"
        sender = normalized.get("sender") or {}
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
            timestamp=datetime.fromtimestamp(float(normalized.get("time") or time.time())),
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

    def _is_allowed(self, source: QQEventSource) -> bool:
        if source.group_id:
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
                return cache_document_from_bytes(data, f"qq_{prefix}.bin")
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
