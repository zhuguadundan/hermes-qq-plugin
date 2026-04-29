"""NapCat/OneBot HTTP client used by the Hermes QQ native adapter."""

from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .types import QQEventSource


class QQBridgeError(RuntimeError):
    pass


class NapCatClient:
    def __init__(self, base_url: str, token: str = "", timeout: int = 60, chunk_size: int = 65536):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.chunk_size = chunk_size
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._transport_mode: Optional[str] = None

    def call(self, action: str, params: Optional[dict] = None) -> dict:
        payload = params or {}
        errors: List[str] = []
        modes = [self._transport_mode] if self._transport_mode else []
        modes += [mode for mode in ("path", "root") if mode not in modes]
        for mode in modes:
            try:
                if mode == "path":
                    resp = self.session.post(f"{self.base_url}/{action}", json=payload, timeout=self.timeout)
                    data = self._parse_response(resp, action, mode)
                else:
                    resp = self.session.post(
                        self.base_url,
                        json={"action": action, "params": payload, "echo": uuid.uuid4().hex},
                        timeout=self.timeout,
                    )
                    data = self._parse_response(resp, action, mode)
                    if data.get("message") == "NapCat4 Is Running":
                        raise QQBridgeError("root endpoint is not an action RPC endpoint")
                self._transport_mode = mode
                return data
            except (requests.RequestException, ValueError, QQBridgeError) as exc:
                errors.append(f"{mode}: {exc}")
        raise QQBridgeError(f"NapCat action failed: {action}: {'; '.join(errors)}")

    @staticmethod
    def _parse_response(resp: requests.Response, action: str, mode: str) -> dict:
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise QQBridgeError(f"{mode} transport returned unexpected payload for {action}: {data!r}")
        if data.get("status") not in (None, "ok"):
            raise QQBridgeError(f"{mode} transport returned failure for {action}: {data}")
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

    def get_group_file_url(self, group_id: str, file_id: str, busid: Any = None) -> dict:
        params: Dict[str, Any] = {"group_id": group_id, "file_id": file_id}
        if busid is not None:
            params["busid"] = busid
        return self.call("get_group_file_url", params).get("data", {})

    def get_private_file_url(self, user_id: str, file_id: str) -> dict:
        return self.call("get_private_file_url", {"user_id": user_id, "file_id": file_id}).get("data", {})

    def get_friend_msg_history(self, user_id: str, count: int = 20) -> List[dict]:
        data = self.call("get_friend_msg_history", {"user_id": user_id, "count": count}).get("data", {})
        messages = data.get("messages", data) if isinstance(data, dict) else data
        return messages if isinstance(messages, list) else []

    def get_group_msg_history(self, group_id: str, count: int = 20) -> List[dict]:
        data = self.call("get_group_msg_history", {"group_id": group_id, "count": count}).get("data", {})
        messages = data.get("messages", data) if isinstance(data, dict) else data
        return messages if isinstance(messages, list) else []

    def send_text(self, source: QQEventSource, text: str) -> dict:
        params: Dict[str, Any] = {"message": text}
        if source.group_id:
            params["group_id"] = source.group_id
            return self.call("send_group_msg", params)
        params["user_id"] = source.user_id
        return self.call("send_private_msg", params)

    def send_segments(self, source: QQEventSource, segments: List[dict]) -> dict:
        params: Dict[str, Any] = {"message": segments}
        if source.group_id:
            params["group_id"] = source.group_id
            return self.call("send_group_msg", params)
        params["user_id"] = source.user_id
        return self.call("send_private_msg", params)

    def upload_file_stream(self, local_path: str) -> str:
        p = Path(local_path).expanduser().resolve()
        sha256_hasher = hashlib.sha256()
        file_size = 0
        with p.open("rb") as handle:
            while chunk := handle.read(self.chunk_size):
                file_size += len(chunk)
                sha256_hasher.update(chunk)
        stream_id = str(uuid.uuid4())
        total_chunks = max(1, (file_size + self.chunk_size - 1) // self.chunk_size)
        with p.open("rb") as handle:
            for idx in range(total_chunks):
                chunk = handle.read(self.chunk_size)
                self.call(
                    "upload_file_stream",
                    {
                        "stream_id": stream_id,
                        "chunk_data": base64.b64encode(chunk).decode("ascii"),
                        "chunk_index": idx,
                        "total_chunks": total_chunks,
                        "file_size": file_size,
                        "expected_sha256": sha256_hasher.hexdigest(),
                        "filename": p.name,
                        "file_retention": 30 * 1000,
                    },
                )
        done = self.call("upload_file_stream", {"stream_id": stream_id, "is_complete": True})
        file_path = (done.get("data") or {}).get("file_path")
        if not file_path:
            raise QQBridgeError(f"upload_file_stream completion missing file_path: {done}")
        return file_path
