import argparse
import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests


MEDIA_RE = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/)\S+(?:[^\S\n]+\S+)*|\S+)[`"']?'''
)
SESSION_RE = re.compile(r"^session_id:\s*(.+)$", re.MULTILINE)
VOICE_DIRECTIVE = "[[audio_as_voice]]"
SESSION_NOT_FOUND_MARKERS = ("Session not found", "No session found")
AUDIO_EXTS = {".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
IMAGE_EXTS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
VIDEO_EXTS = {".avi", ".mkv", ".mov", ".mp4", ".webm"}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class BridgeError(RuntimeError):
    pass


@dataclass
class EventSource:
    user_id: str
    group_id: Optional[str]
    message_id: Optional[str]
    self_id: Optional[str]
    raw: Dict[str, Any]


@dataclass
class BridgeConfig:
    onebot_url: str
    onebot_token: str
    listen_host: str
    listen_port: int
    webhook_path: str
    allowed_user_ids: List[str]
    allowed_group_ids: List[str]
    allow_all: bool
    group_chat_all: bool
    hermes_bin: str
    hermes_workdir: str
    hermes_model: str
    hermes_provider: str
    hermes_toolsets: str
    hermes_skills: List[str]
    temp_dir: str
    state_dir: str
    request_timeout: int
    chunk_size: int
    poll_interval: float
    poll_history_count: int
    poll_backfill_seconds: int
    verbose: bool


@dataclass
class QueuedEvent:
    source: EventSource
    event: Dict[str, Any]
    chat_key: str


@dataclass
class ChatSessionState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    worker_active: bool = False
    active_process: Optional[subprocess.Popen] = None
    active_task: Optional[QueuedEvent] = None
    active_task_requeued: bool = False
    pending_task: Optional[QueuedEvent] = None
    cancel_requested: bool = False
    interrupt_requested: bool = False


class NapCatClient:
    def __init__(self, base_url: str, token: str = "", timeout: int = 60, verbose: bool = False, chunk_size: int = 65536):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self.chunk_size = chunk_size
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._transport_mode: Optional[str] = None

    def call(self, action: str, params: Optional[dict] = None) -> dict:
        payload = params or {}
        errors: List[str] = []
        modes = self._transport_modes()
        for mode in modes:
            try:
                if mode == "path":
                    result = self._call_path(action, payload)
                else:
                    result = self._call_root(action, payload)
                self._transport_mode = mode
                return result
            except (requests.RequestException, ValueError, BridgeError) as exc:
                errors.append(f"{mode}: {exc}")
        raise BridgeError(f"NapCat action failed: {action}: {'; '.join(errors)}")

    def _transport_modes(self) -> List[str]:
        if self._transport_mode == "path":
            return ["path", "root"]
        if self._transport_mode == "root":
            return ["root", "path"]
        return ["path", "root"]

    def _call_path(self, action: str, params: dict) -> dict:
        url = f"{self.base_url}/{action}"
        if self.verbose:
            print(f"[napcat:path] -> {url}", file=sys.stderr)
        resp = self.session.post(url, json=params, timeout=self.timeout)
        return self._parse_response(resp, action, mode="path")

    def _call_root(self, action: str, params: dict) -> dict:
        payload = {"action": action, "params": params, "echo": uuid.uuid4().hex}
        if self.verbose:
            print(f"[napcat:root] -> {self.base_url}", file=sys.stderr)
        resp = self.session.post(self.base_url, json=payload, timeout=self.timeout)
        data = self._parse_response(resp, action, mode="root")
        if data.get("message") == "NapCat4 Is Running":
            raise BridgeError("root endpoint is not an action RPC endpoint")
        return data

    def _parse_response(self, resp: requests.Response, action: str, mode: str) -> dict:
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BridgeError(f"{mode} transport HTTP error for {action}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise BridgeError(f"{mode} transport returned non-JSON for {action}") from exc
        if not isinstance(data, dict):
            raise BridgeError(f"{mode} transport returned unexpected payload for {action}: {data!r}")
        if data.get("status") not in (None, "ok"):
            raise BridgeError(f"{mode} transport returned failure for {action}: {data}")
        return data

    def get_login_info(self) -> dict:
        return self.call("get_login_info").get("data", {})

    def get_message(self, message_id: str) -> dict:
        return self.call("get_msg", {"message_id": message_id}).get("data", {})

    def get_image(self, file_key: str) -> dict:
        return self.call("get_image", {"file": file_key}).get("data", {})

    def get_record(self, file_key: str, out_format: str = "mp3") -> dict:
        return self.call("get_record", {"file": file_key, "out_format": out_format}).get("data", {})

    def get_file(self, file_key: str) -> dict:
        return self.call("get_file", {"file": file_key}).get("data", {})

    def get_private_file_url(self, user_id: str, file_id: str) -> dict:
        return self.call("get_private_file_url", {"user_id": user_id, "file_id": file_id}).get("data", {})

    def get_group_file_url(self, group_id: str, file_id: str, busid: Optional[Any] = None) -> dict:
        params: Dict[str, Any] = {"group_id": group_id, "file_id": file_id}
        if busid is not None:
            params["busid"] = busid
        return self.call("get_group_file_url", params).get("data", {})

    def get_friend_msg_history(self, user_id: str, count: int = 20) -> List[dict]:
        data = self.call("get_friend_msg_history", {"user_id": user_id, "count": count}).get("data", {})
        messages = data.get("messages", data) if isinstance(data, dict) else data
        return messages if isinstance(messages, list) else []

    def get_group_msg_history(self, group_id: str, count: int = 20) -> List[dict]:
        data = self.call("get_group_msg_history", {"group_id": group_id, "count": count}).get("data", {})
        messages = data.get("messages", data) if isinstance(data, dict) else data
        return messages if isinstance(messages, list) else []

    def get_friend_list(self) -> List[dict]:
        data = self.call("get_friend_list").get("data", [])
        return data if isinstance(data, list) else []

    def get_group_list(self) -> List[dict]:
        data = self.call("get_group_list").get("data", [])
        return data if isinstance(data, list) else []

    def send_text(self, source: EventSource, text: str) -> None:
        text = text.strip()
        if not text:
            return
        params: Dict[str, Any] = {"message": text}
        if source.group_id:
            params["group_id"] = source.group_id
            self.call("send_group_msg", params)
        else:
            params["user_id"] = source.user_id
            self.call("send_private_msg", params)

    def send_image(self, source: EventSource, file_path: str) -> None:
        remote_path = self.upload_file_stream(file_path, chunk_size=self.chunk_size)
        self._send_segments(source, [{"type": "image", "data": {"file": remote_path}}])

    def send_voice(self, source: EventSource, file_path: str) -> None:
        remote_path = self.upload_file_stream(file_path, chunk_size=self.chunk_size)
        self._send_segments(source, [{"type": "record", "data": {"file": remote_path}}])

    def send_video(self, source: EventSource, file_path: str) -> None:
        remote_path = self.upload_file_stream(file_path, chunk_size=self.chunk_size)
        self._send_segments(source, [{"type": "video", "data": {"file": remote_path}}])

    def send_file(self, source: EventSource, file_path: str, file_name: Optional[str] = None) -> None:
        resolved = Path(file_path).expanduser().resolve()
        file_name = file_name or resolved.name
        if source.group_id:
            remote_path = self.upload_file_stream(file_path, chunk_size=self.chunk_size)
            self.call("upload_group_file", {"group_id": source.group_id, "file": remote_path, "name": file_name})
        else:
            try:
                self.call("send_online_file", {"user_id": source.user_id, "file_path": str(resolved), "file_name": file_name})
                return
            except Exception:
                remote_path = self.upload_file_stream(file_path, chunk_size=self.chunk_size)
                self.call("upload_private_file", {"user_id": source.user_id, "file": remote_path, "name": file_name})

    def get_online_file_messages(self, user_id: str) -> List[dict]:
        data = self.call("get_online_file_msg", {"user_id": user_id}).get("data", {})
        messages = data.get("msgList", data) if isinstance(data, dict) else data
        return messages if isinstance(messages, list) else []

    def receive_online_file(self, user_id: str, msg_id: str, element_id: str) -> Any:
        return self.call("receive_online_file", {"user_id": user_id, "msg_id": msg_id, "element_id": element_id}).get("data")

    def _send_segments(self, source: EventSource, segments: List[dict]) -> None:
        params: Dict[str, Any] = {"message": segments}
        if source.group_id:
            params["group_id"] = source.group_id
            self.call("send_group_msg", params)
        else:
            params["user_id"] = source.user_id
            self.call("send_private_msg", params)

    def upload_file_stream(self, local_path: str, chunk_size: int = 65536) -> str:
        p = Path(local_path).expanduser().resolve()
        sha256_hasher = hashlib.sha256()
        file_size = 0
        with p.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                file_size += len(chunk)
                sha256_hasher.update(chunk)
        sha256 = sha256_hasher.hexdigest()
        stream_id = str(uuid.uuid4())
        total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)
        with p.open("rb") as handle:
            for idx in range(total_chunks):
                chunk = handle.read(chunk_size)
                if idx < total_chunks - 1 and not chunk:
                    raise BridgeError(f"unexpected EOF while streaming upload: {p}")
                self.call(
                    "upload_file_stream",
                    {
                        "stream_id": stream_id,
                        "chunk_data": base64.b64encode(chunk).decode("ascii"),
                        "chunk_index": idx,
                        "total_chunks": total_chunks,
                        "file_size": file_size,
                        "expected_sha256": sha256,
                        "filename": p.name,
                        "file_retention": 30 * 1000,
                    },
                )
        done = self.call("upload_file_stream", {"stream_id": stream_id, "is_complete": True})
        result = done.get("data") or {}
        file_path = result.get("file_path")
        if file_path:
            return file_path
        raise BridgeError(f"upload_file_stream completion missing file_path: {done}")


class HermesRunner:
    def __init__(self, cfg: BridgeConfig):
        self.cfg = cfg

    def start(self, prompt: str, session_id: Optional[str] = None) -> subprocess.Popen:
        cmd = [self.cfg.hermes_bin, "chat", "-q", prompt, "--quiet", "--source", "tool"]
        if session_id:
            cmd += ["--resume", session_id]
        if self.cfg.hermes_toolsets:
            cmd += ["-t", self.cfg.hermes_toolsets]
        if self.cfg.hermes_model:
            cmd += ["-m", self.cfg.hermes_model]
        if self.cfg.hermes_provider:
            cmd += ["--provider", self.cfg.hermes_provider]
        for skill in self.cfg.hermes_skills:
            cmd += ["-s", skill]
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return subprocess.Popen(
            cmd,
            cwd=self.cfg.hermes_workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def collect(self, proc: subprocess.Popen) -> Tuple[str, Optional[str]]:
        stdout, stderr = proc.communicate()
        combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
        if proc.returncode != 0:
            raise BridgeError(
                f"Hermes failed rc={proc.returncode}\nSTDOUT:\n{stdout.strip()}\nSTDERR:\n{stderr.strip()}"
            )
        output = stdout.strip()
        session_id = None
        match = SESSION_RE.search(output)
        if match:
            session_id = match.group(1).strip()
            output = SESSION_RE.sub("", output).strip()
        output = self._sanitize_output(output)
        if not output and combined:
            output = self._sanitize_output(combined)
        return output, session_id

    def _sanitize_output(self, output: str) -> str:
        output = ANSI_ESCAPE_RE.sub("", output.replace("\r", "\n"))
        cleaned_lines: List[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue
            if self._is_cli_noise_line(stripped):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()
        cleaned = self._collapse_repeated_paragraph_blocks(cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def _is_cli_noise_line(self, stripped: str) -> bool:
        if stripped.startswith("↻ Resumed session "):
            return True
        if stripped.startswith("╭─ ⚕ Hermes"):
            return True
        if stripped.startswith("┊ "):
            return True
        if stripped.startswith("│ "):
            return True
        if stripped.startswith("╰") and "─" in stripped:
            return True
        return False

    def _collapse_repeated_paragraph_blocks(self, text: str) -> str:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        if len(paragraphs) < 2:
            return "\n\n".join(paragraphs)
        collapsed: List[str] = []
        idx = 0
        while idx < len(paragraphs):
            max_block = (len(paragraphs) - idx) // 2
            matched = False
            for block_size in range(max_block, 0, -1):
                current = paragraphs[idx : idx + block_size]
                following = paragraphs[idx + block_size : idx + 2 * block_size]
                if current and current == following:
                    collapsed.extend(current)
                    idx += block_size * 2
                    matched = True
                    break
            if matched:
                continue
            if collapsed and collapsed[-1] == paragraphs[idx]:
                idx += 1
                continue
            collapsed.append(paragraphs[idx])
            idx += 1
        return self._prefer_final_tail_after_streaming("\n\n".join(collapsed))

    def _prefer_final_tail_after_streaming(self, text: str) -> str:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        if len(paragraphs) < 3:
            return "\n\n".join(paragraphs)
        best_start: Optional[int] = None
        best_score = 0
        for later_start in range(1, len(paragraphs)):
            later_tail = paragraphs[later_start:]
            if len(later_tail) < 2:
                continue
            later_text = "\n\n".join(later_tail)
            for earlier_start in range(later_start):
                earlier_block = paragraphs[earlier_start:later_start]
                if len(earlier_block) >= 2:
                    earlier_text = "\n\n".join(earlier_block)
                    if len(later_text) > len(earlier_text) and later_text.startswith(earlier_text):
                        score = len(earlier_block) * 1000 + len(earlier_text)
                        if score > best_score:
                            best_score = score
                            best_start = later_start
                        continue
                overlap = 0
                while (
                    earlier_start + overlap < later_start
                    and later_start + overlap < len(paragraphs)
                    and paragraphs[earlier_start + overlap] == paragraphs[later_start + overlap]
                ):
                    overlap += 1
                if overlap < 2 or earlier_start + overlap != later_start:
                    continue
                score = overlap * 100 + len(later_text)
                if score > best_score:
                    best_score = score
                    best_start = later_start
        result = "\n\n".join(paragraphs[best_start:]) if best_start is not None else "\n\n".join(paragraphs)
        return self._prefer_final_tail_by_lines(result)

    def _prefer_final_tail_by_lines(self, text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines()]
        if len(lines) < 4:
            return "\n".join(lines).strip()
        best_start: Optional[int] = None
        best_score = 0
        for later_start in range(1, len(lines)):
            later_tail = lines[later_start:]
            if len(later_tail) < 2:
                continue
            later_text = "\n".join(later_tail).strip()
            if not later_text:
                continue
            for earlier_start in range(later_start):
                earlier_block = lines[earlier_start:later_start]
                if len(earlier_block) < 2:
                    continue
                earlier_text = "\n".join(earlier_block).strip()
                if not earlier_text:
                    continue
                if len(later_text) > len(earlier_text) and later_text.startswith(earlier_text):
                    score = len(earlier_block) * 1000 + len(earlier_text)
                    if score > best_score:
                        best_score = score
                        best_start = later_start
        if best_start is not None:
            return "\n".join(lines[best_start:]).strip()
        return "\n".join(lines).strip()


class SessionStore:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "sessions.json"
        self._lock = threading.Lock()
        self._cache = self._load()

    def get(self, chat_key: str) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(chat_key) or {}
            return entry.get("session_id")

    def set(self, chat_key: str, session_id: str) -> None:
        with self._lock:
            self._cache[chat_key] = {"session_id": session_id, "updated_at": time.time()}
            self._flush_locked()

    def clear(self, chat_key: str) -> None:
        with self._lock:
            if chat_key in self._cache:
                del self._cache[chat_key]
                self._flush_locked()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _flush_locked(self) -> None:
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)


class BridgeApp:
    def __init__(self, cfg: BridgeConfig):
        self.cfg = cfg
        self.napcat = NapCatClient(
            cfg.onebot_url,
            cfg.onebot_token,
            timeout=cfg.request_timeout,
            verbose=cfg.verbose,
            chunk_size=cfg.chunk_size,
        )
        self.hermes = HermesRunner(cfg)
        self.sessions = SessionStore(cfg.state_dir)
        self.bot_user_id = ""
        self.bot_name = "Hermes"
        self._chat_states: Dict[str, ChatSessionState] = {}
        self._chat_states_lock = threading.Lock()
        self._seen_events: Dict[str, float] = {}
        self._seen_events_lock = threading.Lock()
        self._target_sequences: Dict[str, int] = {}
        self._target_sequences_lock = threading.Lock()
        self._poll_bootstrapped: Set[str] = set()
        self._poll_bootstrapped_lock = threading.Lock()
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self.started_at = time.time()
        Path(cfg.temp_dir).mkdir(parents=True, exist_ok=True)

    def startup_check(self) -> dict:
        try:
            login_info = self.napcat.get_login_info()
        except Exception as exc:
            raise BridgeError(self._format_startup_error(exc)) from exc
        self.bot_user_id = str(login_info.get("user_id") or "")
        self.bot_name = str(login_info.get("nickname") or self.bot_user_id or "Hermes")
        return login_info

    def _format_startup_error(self, exc: Exception) -> str:
        msg = [
            f"cannot reach NapCat OneBot API at {self.cfg.onebot_url}",
            f"detail: {exc}",
            "check that the OneBot HTTP server is enabled and listening on the configured host/port",
            "if NapCat runs in Docker host-network mode, verify it is actually bound to the configured 127.0.0.1/port",
            "if NapCat uses bridge networking, publish the HTTP API port to the host or point --onebot-url at a reachable address",
            "if NapCat requires a token, pass --onebot-token or set NAPCAT_ONEBOT_TOKEN",
        ]
        return "; ".join(msg)

    def log(self, message: str) -> None:
        print(f"[bridge] {message}", file=sys.stderr)

    def auth_configured(self) -> bool:
        return self.cfg.allow_all or bool(self.cfg.allowed_user_ids or self.cfg.allowed_group_ids)

    def allowed(self, source: EventSource) -> bool:
        if self.cfg.allow_all:
            return True
        if source.group_id:
            if source.group_id in self.cfg.allowed_group_ids:
                return True
            if source.user_id in self.cfg.allowed_user_ids:
                return True
            return False
        return source.user_id in self.cfg.allowed_user_ids

    def handle_event(self, event: dict, origin: str = "webhook") -> Tuple[int, dict]:
        normalized = self.normalize_event(event)
        if normalized is None:
            if self.cfg.verbose:
                self.log(f"{origin} ignored unsupported-event")
            return 200, {"ok": True, "ignored": "unsupported-event"}
        if self.is_self_message(normalized):
            if self.cfg.verbose:
                self.log(f"{origin} ignored self-message")
            return 200, {"ok": True, "ignored": "self-message"}
        source = self.build_source(normalized)
        if not source.user_id:
            return 400, {"ok": False, "error": "missing user_id"}
        self.recover_gap_if_needed(normalized, source, origin=origin)
        self._remember_target_sequence(source, normalized)
        dedupe_key = self.event_dedupe_key(normalized, source)
        if dedupe_key and not self._remember_event(dedupe_key):
            if self.cfg.verbose:
                self.log(f"{origin} ignored duplicate message_id={source.message_id} chat={self.chat_key(source)}")
            return 200, {"ok": True, "ignored": "duplicate"}
        if not self.allowed(source):
            if self.cfg.verbose:
                self.log(f"{origin} denied chat={self.chat_key(source)} message_id={source.message_id}")
            return 403, {"ok": False, "error": "not allowed"}
        if source.group_id and not self.should_process_group_event(normalized, source):
            if self.cfg.verbose:
                self.log(f"{origin} ignored group-message-not-directed chat={self.chat_key(source)} message_id={source.message_id}")
            return 200, {"ok": True, "ignored": "group-message-not-directed"}
        command, _arg = self.extract_command(normalized, source)
        if command:
            if self.cfg.verbose:
                self.log(f"{origin} command={command} chat={self.chat_key(source)} message_id={source.message_id}")
            self.handle_control_command(command, source)
            return 200, {"ok": True, "command": command}
        queue_size = self.enqueue_event(source, normalized)
        if self.cfg.verbose:
            self.log(f"{origin} queued chat={self.chat_key(source)} message_id={source.message_id} queue_size={queue_size}")
        return 202, {"ok": True, "queued": True, "queue_size": queue_size}

    def normalize_event(self, event: dict) -> Optional[dict]:
        post_type = event.get("post_type")
        if post_type in ("message", "message_sent"):
            return event
        if post_type != "notice":
            return None
        notice_type = event.get("notice_type")
        if notice_type == "offline_file":
            file_info = event.get("file") or {}
            file_name = file_info.get("name") or "未命名文件"
            return {
                **event,
                "post_type": "message",
                "message_type": "private",
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": file_info.get("file") or file_name,
                            "file_id": file_info.get("id") or file_info.get("file_id"),
                            "url": file_info.get("url"),
                            "name": file_name,
                            "file_size": file_info.get("size") or file_info.get("file_size"),
                        },
                    }
                ],
                "raw_message": f"[文件] {file_name}",
            }
        if notice_type == "group_upload":
            file_info = event.get("file") or {}
            file_name = file_info.get("name") or "未命名文件"
            return {
                **event,
                "user_id": event.get("user_id") or event.get("operator_id"),
                "post_type": "message",
                "message_type": "group",
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": file_info.get("file") or file_name,
                            "file_id": file_info.get("id") or file_info.get("file_id"),
                            "url": file_info.get("url"),
                            "name": file_name,
                            "busid": file_info.get("busid"),
                            "file_size": file_info.get("size") or file_info.get("file_size"),
                        },
                    }
                ],
                "raw_message": f"[群文件] {file_name}",
            }
        return None

    def build_source(self, event: dict) -> EventSource:
        sender = event.get("sender") or {}
        user_id = str(event.get("user_id") or sender.get("user_id") or event.get("operator_id") or "")
        group_id = str(event.get("group_id")) if event.get("group_id") is not None else None
        message_id = str(event.get("message_id")) if event.get("message_id") is not None else None
        self_id = str(event.get("self_id")) if event.get("self_id") is not None else None
        return EventSource(user_id=user_id, group_id=group_id, message_id=message_id, self_id=self_id, raw=event)

    def is_self_message(self, event: dict) -> bool:
        if event.get("sub_type") == "self":
            return True
        sender = str(event.get("user_id") or (event.get("sender") or {}).get("user_id") or "")
        self_id = str(event.get("self_id") or "")
        return bool(sender and self_id and sender == self_id)

    def should_process_group_event(self, event: dict, source: EventSource) -> bool:
        if not source.group_id:
            return True
        if self.cfg.group_chat_all:
            return True
        bot_id = source.self_id or self.bot_user_id
        segments = self.message_segments(event)
        for seg in segments:
            if seg.get("type") != "at":
                continue
            qq = str((seg.get("data") or {}).get("qq") or "")
            if qq and bot_id and qq == str(bot_id):
                return True
        reply_id = self.reply_message_id(segments)
        if reply_id and bot_id:
            try:
                replied = self.napcat.get_message(reply_id)
            except Exception as exc:
                if self.cfg.verbose:
                    self.log(f"failed to resolve reply target {reply_id}: {exc}")
                return False
            sender = str((replied.get("sender") or {}).get("user_id") or replied.get("user_id") or "")
            return bool(sender and sender == str(bot_id))
        return False

    def extract_command(self, event: dict, source: EventSource) -> Tuple[Optional[str], str]:
        text_parts: List[str] = []
        bot_id = source.self_id or self.bot_user_id
        for seg in self.message_segments(event):
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                text_parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                qq = str(data.get("qq") or "")
                if qq and bot_id and qq == str(bot_id):
                    continue
            elif seg_type == "reply":
                continue
        joined = re.sub(r"\s+", " ", "".join(text_parts)).strip()
        if not joined.startswith("/"):
            return None, ""
        command, _, arg = joined.partition(" ")
        return command.lower(), arg.strip()

    def enqueue_event(self, source: EventSource, event: dict) -> int:
        chat_key = self.chat_key(source)
        state = self._chat_state(chat_key)
        task = QueuedEvent(source=source, event=event, chat_key=chat_key)
        proc: Optional[subprocess.Popen] = None
        with state.lock:
            if not state.worker_active:
                state.worker_active = True
                worker = threading.Thread(target=self._worker_loop, args=(task,), daemon=True)
                worker.start()
                return 1
            merged_pending = self.merge_queued_event(state.pending_task, task)
            if state.active_process is not None and state.active_process.poll() is None:
                if state.active_task is not None and not state.active_task_requeued:
                    merged_pending = self.merge_queued_event(state.active_task, merged_pending)
                    state.active_task_requeued = True
                state.interrupt_requested = True
                proc = state.active_process
            state.pending_task = merged_pending
            queue_size = 1 if state.pending_task is not None else 0
        if proc is not None:
            self._terminate_process(proc)
        return queue_size

    def _worker_loop(self, task: QueuedEvent) -> None:
        state = self._chat_state(task.chat_key)
        while True:
            with state.lock:
                state.active_task = task
                state.active_task_requeued = False
            if self._consume_cancel_requested(state):
                if self.cfg.verbose:
                    self.log(f"cancelled chat={task.chat_key} before start")
            else:
                self._process_task(task)
            with state.lock:
                if state.active_task is task:
                    state.active_task = None
                state.active_task_requeued = False
                task = state.pending_task
                state.pending_task = None
                if task is None:
                    state.worker_active = False
                    return

    def _process_task(self, task: QueuedEvent) -> None:
        state = self._chat_state(task.chat_key)
        try:
            prompt = self.build_prompt(task.event, task.source)
            if self._consume_cancel_requested(state):
                if self.cfg.verbose:
                    self.log(f"cancelled chat={task.chat_key} before Hermes start")
                return
            if not prompt.strip():
                self.napcat.send_text(task.source, "没有可处理的消息内容。")
                return
            response, session_id = self._run_hermes_with_recovery(prompt, task.chat_key, state)
            if self._consume_cancel_requested(state):
                if self.cfg.verbose:
                    self.log(f"cancelled chat={task.chat_key} after Hermes exit")
                return
            self._clear_interrupt_requested(state)
            if session_id:
                self.sessions.set(task.chat_key, session_id)
            media, cleaned = self.extract_media(response)
            if cleaned:
                self.napcat.send_text(task.source, cleaned)
            for path, as_voice in media:
                self.dispatch_media(task.source, path, as_voice=as_voice)
            if self.cfg.verbose:
                self.log(f"handled chat={task.chat_key} message_id={task.source.message_id} session_id={session_id}")
        except Exception as exc:
            if self._consume_cancel_requested(state):
                if self.cfg.verbose:
                    self.log(f"cancelled chat={task.chat_key}")
                return
            if self._consume_interrupt_requested(state):
                if self.cfg.verbose:
                    self.log(f"interrupted chat={task.chat_key}")
                return
            self.log(f"error chat={task.chat_key}: {exc}")
            try:
                self.napcat.send_text(task.source, f"处理消息失败：{exc}")
            except Exception as send_exc:
                self.log(f"failed to send error message: {send_exc}")

    def _run_hermes_with_recovery(self, prompt: str, chat_key: str, state: ChatSessionState) -> Tuple[str, Optional[str]]:
        session_id = self.sessions.get(chat_key)
        try:
            return self._run_hermes(prompt, state, session_id=session_id)
        except Exception as exc:
            if session_id and self.is_missing_session_error(exc):
                self.sessions.clear(chat_key)
                return self._run_hermes(prompt, state, session_id=None)
            raise

    def _run_hermes(self, prompt: str, state: ChatSessionState, session_id: Optional[str]) -> Tuple[str, Optional[str]]:
        proc = self.hermes.start(prompt, session_id=session_id)
        with state.lock:
            state.active_process = proc
        try:
            return self.hermes.collect(proc)
        finally:
            with state.lock:
                if state.active_process is proc:
                    state.active_process = None

    def is_missing_session_error(self, exc: Exception) -> bool:
        text = str(exc)
        return any(marker in text for marker in SESSION_NOT_FOUND_MARKERS)

    def handle_control_command(self, command: str, source: EventSource) -> None:
        chat_key = self.chat_key(source)
        if command in ("/new", "/reset"):
            self.sessions.clear(chat_key)
            self.stop_chat(chat_key, clear_queue=True)
            self.napcat.send_text(source, "已重置当前会话。下一条消息会开启新对话。")
            return
        if command == "/stop":
            stopped = self.stop_chat(chat_key, clear_queue=False)
            if stopped:
                self.napcat.send_text(source, "已停止当前处理。")
            else:
                self.napcat.send_text(source, "当前没有正在处理的消息。")
            return
        if command == "/status":
            status = self.chat_status(chat_key)
            session_id = self.sessions.get(chat_key) or "无"
            summary = (
                f"状态：{'处理中' if status['running'] else '空闲'}\n"
                f"排队：{status['queued']}\n"
                f"会话：{session_id}"
            )
            self.napcat.send_text(source, summary)
            return
        if command == "/help":
            self.napcat.send_text(
                source,
                "可用命令：/new /reset 重置会话，/status 查看状态，/stop 停止当前处理，/help 查看帮助。",
            )
            return
        self.napcat.send_text(source, f"不支持的命令：{command}")

    def stop_chat(self, chat_key: str, clear_queue: bool) -> bool:
        state = self._chat_state(chat_key)
        proc: Optional[subprocess.Popen]
        with state.lock:
            proc = state.active_process
            state.cancel_requested = True
            state.interrupt_requested = False
            state.pending_task = None
            if state.worker_active and (proc is None or proc.poll() is not None):
                return True
            if proc is None or proc.poll() is not None:
                state.cancel_requested = False
                return False
        self._terminate_process(proc)
        return True

    def chat_status(self, chat_key: str) -> Dict[str, Any]:
        state = self._chat_state(chat_key)
        with state.lock:
            running = state.worker_active
            queued = 1 if state.pending_task is not None else 0
        return {"running": running, "queued": queued}

    def _consume_cancel_requested(self, state: ChatSessionState) -> bool:
        with state.lock:
            cancelled = state.cancel_requested
            state.cancel_requested = False
            return cancelled

    def _consume_interrupt_requested(self, state: ChatSessionState) -> bool:
        with state.lock:
            interrupted = state.interrupt_requested
            state.interrupt_requested = False
            return interrupted

    def _clear_interrupt_requested(self, state: ChatSessionState) -> None:
        with state.lock:
            state.interrupt_requested = False

    def history_target_key(self, source: EventSource) -> str:
        if source.group_id:
            return f"group:{source.group_id}"
        return f"private:{source.user_id}"

    def event_sequence(self, event: dict) -> Optional[int]:
        for key in ("real_seq", "message_seq"):
            value = event.get(key)
            if value in (None, ""):
                continue
            try:
                return int(str(value))
            except (TypeError, ValueError):
                continue
        return None

    def _remember_target_sequence(self, source: EventSource, event: dict) -> None:
        seq = self.event_sequence(event)
        if seq is None:
            return
        target_key = self.history_target_key(source)
        with self._target_sequences_lock:
            current = self._target_sequences.get(target_key)
            if current is None or seq > current:
                self._target_sequences[target_key] = seq

    def recover_gap_if_needed(self, event: dict, source: EventSource, origin: str) -> None:
        if origin != "webhook":
            return
        seq = self.event_sequence(event)
        if seq is None:
            return
        target_key = self.history_target_key(source)
        with self._target_sequences_lock:
            previous = self._target_sequences.get(target_key)
        if previous is None or seq <= previous + 1:
            return
        missing = seq - previous - 1
        history_count = min(max(self.cfg.poll_history_count, missing + 8, 20), 200)
        self.log(
            f"webhook gap detected target={target_key} prev_seq={previous} current_seq={seq} "
            f"missing={missing}; attempting one-shot history recovery count={history_count}"
        )
        try:
            if source.group_id:
                history = self.napcat.get_group_msg_history(source.group_id, count=history_count)
            else:
                history = self.napcat.get_friend_msg_history(source.user_id, count=history_count)
        except Exception as exc:
            self.log(f"history recovery failed target={target_key}: {exc}")
            return
        recovered = 0
        ordered = sorted(
            (msg for msg in history if isinstance(msg, dict)),
            key=lambda item: (
                self.event_sequence(item) if self.event_sequence(item) is not None else float(item.get("time") or 0),
                str(item.get("message_id") or ""),
            ),
        )
        for message in ordered:
            if self.is_self_message(message):
                continue
            msg_source = self.build_source(message)
            if self.history_target_key(msg_source) != target_key:
                continue
            msg_seq = self.event_sequence(message)
            if msg_seq is None or msg_seq <= previous or msg_seq >= seq:
                continue
            self.handle_event(message, origin="gap-replay")
            recovered += 1
        if recovered:
            self.log(f"history recovery queued {recovered} missing message(s) target={target_key}")
            return
        self.log(
            f"history recovery found no recoverable messages target={target_key} "
            f"prev_seq={previous} current_seq={seq}"
        )

    def event_dedupe_key(self, event: dict, source: EventSource) -> Optional[str]:
        message_id = source.message_id or event.get("message_id")
        if message_id is not None:
            return f"{source.group_id or 'private'}:{source.user_id}:{message_id}"
        raw_message = str(event.get("raw_message") or "").strip()
        message_time = event.get("time")
        if raw_message and message_time is not None:
            return f"{source.group_id or 'private'}:{source.user_id}:{message_time}:{raw_message[:200]}"
        return None

    def _remember_event(self, dedupe_key: str, *, force: bool = False) -> bool:
        now = time.time()
        with self._seen_events_lock:
            self._prune_seen_events_locked(now)
            exists = dedupe_key in self._seen_events
            if exists and not force:
                return False
            self._seen_events[dedupe_key] = now
            return True

    def _prune_seen_events_locked(self, now: float) -> None:
        expiry = now - 6 * 3600
        stale_keys = [key for key, ts in self._seen_events.items() if ts < expiry]
        for key in stale_keys:
            del self._seen_events[key]
        if len(self._seen_events) <= 4096:
            return
        for key, _ in sorted(self._seen_events.items(), key=lambda item: item[1])[: len(self._seen_events) - 4096]:
            self._seen_events.pop(key, None)

    def merge_queued_event(self, existing: Optional[QueuedEvent], new_task: QueuedEvent) -> QueuedEvent:
        if existing is None:
            return new_task
        merged = copy.deepcopy(existing.event)
        merged["message"] = self._merge_messages(existing.event, new_task.event)
        raw_parts = [str(existing.event.get("raw_message") or "").strip(), str(new_task.event.get("raw_message") or "").strip()]
        raw_message = "\n".join(part for part in raw_parts if part)
        if raw_message:
            merged["raw_message"] = raw_message
        sender = new_task.event.get("sender")
        if sender is not None:
            merged["sender"] = copy.deepcopy(sender)
        for key in ("message_id", "time", "post_type", "message_type", "notice_type", "sub_type", "self_id"):
            if key in new_task.event:
                merged[key] = new_task.event[key]
        return QueuedEvent(source=new_task.source, event=merged, chat_key=new_task.chat_key)

    def _merge_messages(self, existing_event: dict, new_event: dict) -> List[dict]:
        existing_segments = copy.deepcopy(self.message_segments(existing_event))
        new_segments = copy.deepcopy(self.message_segments(new_event))
        if existing_segments and new_segments:
            existing_segments.append({"type": "text", "data": {"text": "\n"}})
        return existing_segments + new_segments

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass

    def start_background_polling(self) -> None:
        if self.cfg.poll_interval <= 0:
            if self.cfg.verbose:
                self.log("poller disabled by configuration")
            return
        if self._poll_thread is not None:
            return
        self._poll_thread = threading.Thread(target=self._poll_loop, name="napcat-qq-poller", daemon=True)
        self._poll_thread.start()
        if self.cfg.verbose:
            self.log(
                f"poller started interval={self.cfg.poll_interval}s history={self.cfg.poll_history_count} backfill={self.cfg.poll_backfill_seconds}s"
            )

    def stop_background_polling(self) -> None:
        self._poll_stop.set()
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self.log(f"poller error: {exc}")
            self._poll_stop.wait(self.cfg.poll_interval)

    def poll_once(self) -> None:
        for user_id in self._poll_private_targets():
            messages = self.napcat.get_friend_msg_history(user_id, count=self.cfg.poll_history_count)
            self._process_polled_history(messages, bootstrap_key=f"private:{user_id}", origin="poll")
        for group_id in self._poll_group_targets():
            messages = self.napcat.get_group_msg_history(group_id, count=self.cfg.poll_history_count)
            self._process_polled_history(messages, bootstrap_key=f"group:{group_id}", origin="poll")

    def _poll_private_targets(self) -> List[str]:
        if self.cfg.allow_all:
            try:
                friends = self.napcat.get_friend_list()
                return [str(item.get("user_id")) for item in friends if item.get("user_id") is not None]
            except Exception as exc:
                self.log(f"failed to list friends for polling: {exc}")
                return []
        return list(dict.fromkeys(self.cfg.allowed_user_ids))

    def _poll_group_targets(self) -> List[str]:
        if self.cfg.allow_all:
            try:
                groups = self.napcat.get_group_list()
                return [str(item.get("group_id")) for item in groups if item.get("group_id") is not None]
            except Exception as exc:
                self.log(f"failed to list groups for polling: {exc}")
                return []
        return list(dict.fromkeys(self.cfg.allowed_group_ids))

    def _process_polled_history(self, messages: List[dict], bootstrap_key: str, origin: str) -> None:
        ordered = sorted(
            (msg for msg in messages if isinstance(msg, dict)),
            key=lambda item: (float(item.get("time") or 0), str(item.get("message_id") or "")),
        )
        if not ordered:
            return
        bootstrap_candidates: Optional[Set[str]] = None
        with self._poll_bootstrapped_lock:
            if bootstrap_key not in self._poll_bootstrapped:
                self._poll_bootstrapped.add(bootstrap_key)
                bootstrap_candidates = self._bootstrap_candidates(ordered)
        if bootstrap_candidates is not None:
            for message in ordered:
                source = self.build_source(message)
                dedupe_key = self.event_dedupe_key(message, source)
                if dedupe_key and dedupe_key not in bootstrap_candidates:
                    self._remember_event(dedupe_key, force=True)
        for message in ordered:
            source = self.build_source(message)
            dedupe_key = self.event_dedupe_key(message, source)
            if bootstrap_candidates is not None and dedupe_key and dedupe_key not in bootstrap_candidates:
                continue
            self.handle_event(message, origin=origin)

    def _bootstrap_candidates(self, messages: List[dict]) -> Set[str]:
        cutoff = time.time() - self.cfg.poll_backfill_seconds
        latest_self_time = 0.0
        for message in messages:
            if self.is_self_message(message):
                latest_self_time = max(latest_self_time, float(message.get("time") or 0))
        candidates: Set[str] = set()
        for message in messages:
            if self.is_self_message(message):
                continue
            message_time = float(message.get("time") or 0)
            if message_time < cutoff:
                continue
            if latest_self_time and message_time <= latest_self_time:
                continue
            source = self.build_source(message)
            dedupe_key = self.event_dedupe_key(message, source)
            if dedupe_key:
                candidates.add(dedupe_key)
        return candidates

    def _chat_state(self, chat_key: str) -> ChatSessionState:
        with self._chat_states_lock:
            state = self._chat_states.get(chat_key)
            if state is None:
                state = ChatSessionState()
                self._chat_states[chat_key] = state
            return state

    def chat_key(self, source: EventSource) -> str:
        if source.group_id:
            return f"group:{source.group_id}:user:{source.user_id}"
        return f"private:{source.user_id}"

    def message_segments(self, event: dict) -> List[dict]:
        message = event.get("message")
        if isinstance(message, str):
            return [{"type": "text", "data": {"text": message}}]
        if isinstance(message, list):
            return [seg for seg in message if isinstance(seg, dict)]
        return []

    def reply_message_id(self, segments: List[dict]) -> Optional[str]:
        for seg in segments:
            if seg.get("type") != "reply":
                continue
            reply_id = (seg.get("data") or {}).get("id")
            if reply_id is not None:
                return str(reply_id)
        return None

    def build_prompt(self, event: dict, source: EventSource) -> str:
        segments = self.message_segments(event)
        bot_id = source.self_id or self.bot_user_id
        text_parts: List[str] = []
        attachment_lines: List[str] = []
        sender = event.get("sender") or {}
        reply_to_bot = False

        reply_id = self.reply_message_id(segments)
        if reply_id and bot_id:
            try:
                replied = self.napcat.get_message(reply_id)
                reply_sender = str((replied.get("sender") or {}).get("user_id") or replied.get("user_id") or "")
                reply_to_bot = bool(reply_sender and reply_sender == str(bot_id))
            except Exception:
                reply_to_bot = False

        for seg in segments:
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                text_parts.append(str(data.get("text") or ""))
                continue
            if seg_type == "at":
                qq = str(data.get("qq") or "")
                if qq and bot_id and qq == str(bot_id):
                    continue
                if qq:
                    text_parts.append(f" @{qq} ")
                continue
            if seg_type == "reply":
                continue
            if seg_type == "image":
                local = self.resolve_image(data)
                if local:
                    attachment_lines.append(f"用户发送了图片：{local}")
                continue
            if seg_type in ("record", "voice", "audio"):
                local = self.resolve_record(data)
                if local:
                    attachment_lines.append(f"用户发送了语音/音频：{local}")
                    attachment_lines.append("如果需要理解语音，请优先使用语音转写或音频相关能力。")
                continue
            if seg_type == "video":
                local = self.resolve_video(data)
                if local:
                    attachment_lines.append(f"用户发送了视频：{local}")
                continue
            if seg_type == "file":
                local = self.resolve_file(source, data)
                if local:
                    attachment_lines.append(f"用户发送了文件：{local}")
                continue
            if seg_type == "onlinefile":
                local = self.resolve_online_file(source, data)
                if local:
                    attachment_lines.append(f"用户发送了在线文件：{local}")
                else:
                    attachment_lines.append(f"用户发送了在线文件：{self.guess_name(data, 'online-file.bin')}")
                continue
            raw = json.dumps(seg, ensure_ascii=False)
            text_parts.append(f"[消息段:{seg_type}] {raw}")

        lines: List[str] = []
        scope = f"QQ群 {source.group_id}" if source.group_id else f"QQ私聊 {source.user_id}"
        lines.append(f"你正在通过 QQ 与用户聊天。来源：{scope}。")
        nickname = sender.get("card") or sender.get("nickname") or sender.get("user_id")
        if nickname:
            lines.append(f"发送者：{nickname}")
        if reply_to_bot:
            lines.append("这条消息是用户对你上一条回复的继续。")
        user_text = re.sub(r"\s+", " ", "".join(text_parts)).strip()
        if user_text:
            lines.append("用户文字内容：")
            lines.append(user_text)
        lines.extend(attachment_lines)
        lines.append("请直接回答用户，保持聊天口吻，尽量简洁。")
        lines.append("如果你生成图片、语音、视频或文件，请在最终回复里使用 MEDIA:/绝对路径。")
        lines.append("如果要把音频作为语音消息发送，请在回复里额外包含 [[audio_as_voice]]。")
        lines.append("不要提及桥接、NapCat、OneBot 或内部实现细节。")
        return "\n".join(line for line in lines if line)

    def resolve_image(self, data: dict) -> Optional[str]:
        candidates: List[Optional[str]] = [data.get("url"), data.get("file")]
        file_key = data.get("file")
        if file_key:
            try:
                meta = self.napcat.get_image(str(file_key))
            except Exception:
                meta = {}
            base64_path = self.write_base64_file(meta.get("base64"), "image", self.guess_name(data, "image"))
            if base64_path:
                return base64_path
            candidates = [meta.get("url"), meta.get("file"), data.get("url"), data.get("file")]
        return self.download_first(candidates, prefix="image", fallback_name=self.guess_name(data, "image"))

    def resolve_record(self, data: dict) -> Optional[str]:
        file_key = data.get("file_id") or data.get("file")
        candidates: List[Optional[str]] = [data.get("path"), data.get("url"), data.get("file")]
        if file_key:
            try:
                meta = self.napcat.get_record(str(file_key), out_format="mp3")
            except Exception:
                meta = {}
            base64_path = self.write_base64_file(meta.get("base64"), "audio", self.guess_name(data, "audio.mp3"))
            if base64_path:
                return base64_path
            candidates = [meta.get("file"), meta.get("path"), meta.get("url")] + candidates
        return self.download_first(candidates, prefix="audio", fallback_name=self.guess_name(data, "audio.mp3"))

    def resolve_video(self, data: dict) -> Optional[str]:
        candidates = [data.get("url"), data.get("file")]
        return self.download_first(candidates, prefix="video", fallback_name=self.guess_name(data, "video.mp4"))

    def resolve_file(self, source: EventSource, data: dict) -> Optional[str]:
        file_id = str(data.get("file_id") or "") or None
        file_key = str(data.get("file") or "") or None
        candidates: List[Optional[str]] = [data.get("url"), data.get("file")]
        if file_id:
            try:
                if source.group_id:
                    group_meta = self.napcat.get_group_file_url(source.group_id, file_id, busid=data.get("busid"))
                else:
                    group_meta = self.napcat.get_private_file_url(source.user_id, file_id)
            except Exception:
                group_meta = {}
            candidates = [group_meta.get("url"), group_meta.get("download_url")] + candidates
        if file_key or file_id:
            file_lookup_key = file_id or file_key
            try:
                meta = self.napcat.get_file(str(file_lookup_key))
            except Exception:
                meta = {}
            base64_path = self.write_base64_file(meta.get("base64"), "file", self.guess_name(data, "file.bin"))
            if base64_path:
                return base64_path
            candidates = [meta.get("url"), meta.get("download_url"), meta.get("file")] + candidates
        return self.download_first(candidates, prefix="file", fallback_name=self.guess_name(data, "file.bin"))

    def resolve_online_file(self, source: EventSource, data: dict) -> Optional[str]:
        msg_id = str(data.get("msgId") or data.get("msg_id") or "") or None
        element_id = str(data.get("elementId") or data.get("element_id") or "") or None
        candidates: List[Optional[str]] = [data.get("path"), data.get("file"), data.get("url")]
        if not source.group_id and msg_id and element_id:
            try:
                received = self.napcat.receive_online_file(source.user_id, msg_id, element_id)
            except Exception:
                received = None
            if isinstance(received, dict):
                candidates = [
                    received.get("path"),
                    received.get("file"),
                    received.get("file_path"),
                    received.get("save_path"),
                    received.get("url"),
                ] + candidates
            elif isinstance(received, str):
                candidates = [received] + candidates
        return self.download_first(candidates, prefix="onlinefile", fallback_name=self.guess_name(data, "online-file.bin"))

    def guess_name(self, data: dict, fallback: str) -> str:
        for key in ("name", "file_name", "file", "path"):
            value = data.get(key)
            if value:
                return Path(str(value)).name
        return fallback

    def write_base64_file(self, content: Optional[str], prefix: str, fallback_name: str) -> Optional[str]:
        if not content:
            return None
        payload = content
        if ";base64," in payload:
            payload = payload.split(",", 1)[1]
        try:
            blob = base64.b64decode(payload, validate=False)
        except Exception:
            return None
        name = Path(fallback_name).name or f"{prefix}.bin"
        out = Path(self.cfg.temp_dir) / f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{name}"
        out.write_bytes(blob)
        return str(out.resolve())

    def download_first(self, candidates: List[Optional[str]], prefix: str, fallback_name: Optional[str] = None) -> Optional[str]:
        for item in candidates:
            if not item:
                continue
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value:
                continue
            if value.startswith("file://"):
                local = Path(urlparse(value).path)
                if local.exists():
                    return str(local.resolve())
                continue
            if value.startswith(("http://", "https://")):
                return self.download_url(value, prefix=prefix, fallback_name=fallback_name)
            local = Path(value).expanduser()
            if local.exists():
                return str(local.resolve())
        return None

    def download_url(self, url: str, prefix: str, fallback_name: Optional[str] = None) -> str:
        parsed = urlparse(url)
        name = Path(parsed.path).name or fallback_name or f"{prefix}.bin"
        name = Path(name).name
        if "." not in name:
            ext = mimetypes.guess_extension("application/octet-stream") or ".bin"
            name = f"{name}{ext}"
        out = Path(self.cfg.temp_dir) / f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{name}"
        with requests.get(url, stream=True, timeout=self.cfg.request_timeout) as resp:
            resp.raise_for_status()
            with out.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
        return str(out.resolve())

    def extract_media(self, text: str) -> Tuple[List[Tuple[str, bool]], str]:
        media: List[Tuple[str, bool]] = []
        cleaned = text or ""
        has_voice_tag = VOICE_DIRECTIVE in cleaned
        cleaned = cleaned.replace(VOICE_DIRECTIVE, "")
        for match in MEDIA_RE.finditer(text or ""):
            path = match.group("path").strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
                path = path[1:-1].strip()
            path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
            if path:
                media.append((path, has_voice_tag))
        cleaned = MEDIA_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return media, cleaned

    def dispatch_media(self, source: EventSource, path: str, as_voice: bool = False) -> None:
        resolved = Path(path).expanduser()
        if not resolved.exists():
            self.napcat.send_text(source, f"附件不存在，无法发送：{path}")
            return
        ext = resolved.suffix.lower()
        if ext in IMAGE_EXTS:
            self.napcat.send_image(source, str(resolved))
            return
        if ext in AUDIO_EXTS:
            if as_voice:
                self.napcat.send_voice(source, str(resolved))
            else:
                self.napcat.send_file(source, str(resolved), resolved.name)
            return
        if ext in VIDEO_EXTS:
            self.napcat.send_video(source, str(resolved))
            return
        self.napcat.send_file(source, str(resolved), resolved.name)

    def health_payload(self) -> dict:
        return {
            "ok": True,
            "bot_user_id": self.bot_user_id,
            "bot_name": self.bot_name,
            "onebot_url": self.cfg.onebot_url,
            "transport_mode": self.napcat._transport_mode,
            "auth_configured": self.auth_configured(),
            "group_chat_all": self.cfg.group_chat_all,
            "poll_interval": self.cfg.poll_interval,
            "poll_history_count": self.cfg.poll_history_count,
            "poll_backfill_seconds": self.cfg.poll_backfill_seconds,
            "seen_events": len(self._seen_events),
        }


class WebhookHandler(BaseHTTPRequestHandler):
    app: BridgeApp = None  # type: ignore[assignment]
    webhook_path: str = "/napcat"

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/healthz":
            self.send_json(200, self.app.health_payload())
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != self.webhook_path:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": f"bad json: {exc}"})
            return
        if self.app.cfg.verbose:
            self.app.log(
                f"webhook request path={self.path} post_type={payload.get('post_type')} message_type={payload.get('message_type')} message_id={payload.get('message_id')}"
            )
        code, body = self.app.handle_event(payload, origin="webhook")
        self.send_json(code, body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def build_arg_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run", nargs="?", default="run")
    parser.add_argument("--onebot-url", default=os.getenv("NAPCAT_ONEBOT_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--onebot-token", default=os.getenv("NAPCAT_ONEBOT_TOKEN", ""))
    parser.add_argument("--listen-host", default=os.getenv("NAPCAT_QQ_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("NAPCAT_QQ_BRIDGE_PORT", "8096")))
    parser.add_argument("--webhook-path", default=os.getenv("NAPCAT_QQ_BRIDGE_PATH", "/napcat"))
    parser.add_argument("--allow-user", action="append", default=_env_list("NAPCAT_QQ_BRIDGE_ALLOW_USERS"))
    parser.add_argument("--allow-group", action="append", default=_env_list("NAPCAT_QQ_BRIDGE_ALLOW_GROUPS"))
    parser.add_argument("--allow-all", action="store_true", default=_env_flag("NAPCAT_QQ_BRIDGE_ALLOW_ALL"))
    parser.add_argument("--group-chat-all", action="store_true", default=_env_flag("NAPCAT_QQ_BRIDGE_GROUP_CHAT_ALL"))
    parser.add_argument("--hermes-bin", default=os.getenv("HERMES_BIN", "hermes"))
    parser.add_argument("--hermes-workdir", default=os.getenv("HERMES_WORKDIR", str(Path.home())))
    parser.add_argument("--hermes-model", default=os.getenv("HERMES_MODEL", ""))
    parser.add_argument("--hermes-provider", default=os.getenv("HERMES_PROVIDER", ""))
    parser.add_argument("--hermes-toolsets", default=os.getenv("HERMES_TOOLSETS", ""))
    parser.add_argument("--skill", dest="skills", action="append", default=_env_list("NAPCAT_QQ_BRIDGE_SKILLS"))
    parser.add_argument(
        "--temp-dir",
        default=os.getenv("NAPCAT_QQ_BRIDGE_TEMP", str(Path.home() / ".hermes" / "napcat_qq_bridge" / "tmp")),
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("NAPCAT_QQ_BRIDGE_STATE_DIR", str(Path.home() / ".hermes" / "napcat_qq_bridge" / "state")),
    )
    parser.add_argument("--request-timeout", type=int, default=int(os.getenv("NAPCAT_QQ_BRIDGE_TIMEOUT", "60")))
    parser.add_argument("--chunk-size", type=int, default=int(os.getenv("NAPCAT_QQ_BRIDGE_CHUNK_SIZE", "65536")))
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("NAPCAT_QQ_BRIDGE_POLL_INTERVAL", "3")))
    parser.add_argument("--poll-history-count", type=int, default=int(os.getenv("NAPCAT_QQ_BRIDGE_POLL_HISTORY_COUNT", "20")))
    parser.add_argument("--poll-backfill-seconds", type=int, default=int(os.getenv("NAPCAT_QQ_BRIDGE_POLL_BACKFILL_SECONDS", "600")))
    parser.add_argument("-v", "--verbose", action="store_true")


def _env_list(name: str) -> List[str]:
    value = os.getenv(name, "")
    return [value] if value else []


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_list(values: List[str]) -> List[str]:
    out: List[str] = []
    for item in values or []:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def args_to_config(args: argparse.Namespace) -> BridgeConfig:
    return BridgeConfig(
        onebot_url=args.onebot_url,
        onebot_token=args.onebot_token,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        webhook_path=args.webhook_path,
        allowed_user_ids=_normalize_list(args.allow_user),
        allowed_group_ids=_normalize_list(args.allow_group),
        allow_all=bool(args.allow_all),
        group_chat_all=bool(args.group_chat_all),
        hermes_bin=args.hermes_bin,
        hermes_workdir=args.hermes_workdir,
        hermes_model=args.hermes_model,
        hermes_provider=args.hermes_provider,
        hermes_toolsets=args.hermes_toolsets,
        hermes_skills=_normalize_list(args.skills),
        temp_dir=args.temp_dir,
        state_dir=args.state_dir,
        request_timeout=args.request_timeout,
        chunk_size=args.chunk_size,
        poll_interval=args.poll_interval,
        poll_history_count=args.poll_history_count,
        poll_backfill_seconds=args.poll_backfill_seconds,
        verbose=bool(args.verbose),
    )


def main(args: argparse.Namespace) -> int:
    cfg = args_to_config(args)
    app = BridgeApp(cfg)
    try:
        login_info = app.startup_check()
    except Exception as exc:
        print(f"NapCat QQ bridge startup check failed: {exc}", file=sys.stderr)
        return 1
    if not app.auth_configured():
        print(
            "Warning: no allowlist configured and --allow-all not set. "
            "All inbound chats will be denied until you add --allow-user / --allow-group or --allow-all.",
            file=sys.stderr,
        )
    WebhookHandler.app = app
    WebhookHandler.webhook_path = cfg.webhook_path
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), WebhookHandler)
    print(f"NapCat QQ bridge listening on http://{cfg.listen_host}:{cfg.listen_port}{cfg.webhook_path}")
    print(f"Health check: http://{cfg.listen_host}:{cfg.listen_port}/healthz")
    print(f"OneBot API: {cfg.onebot_url}")
    print(f"Bot self_id: {login_info.get('user_id')} ({login_info.get('nickname') or 'unknown'})")
    app.start_background_polling()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        app.stop_background_polling()
        server.server_close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    build_arg_parser(parser)
    sys.exit(main(parser.parse_args()))
