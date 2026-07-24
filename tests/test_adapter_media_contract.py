import asyncio
import base64
from pathlib import Path

from gateway.config import PlatformConfig
from gateway.platforms.qq import NapCatQQAdapter


class FakeNapCatClient:
    def __init__(self):
        self.uploads = []
        self.calls = []
        self.segments = []
        self.group_history = []
        self.friend_history = []

    def upload_file_stream(self, local_path: str) -> str:
        self.uploads.append(local_path)
        return f"remote:{Path(local_path).name}"

    def call(self, action: str, params=None):
        self.calls.append((action, params or {}))
        return {"status": "ok", "data": {"message_id": 123}}

    def send_segments(self, source, segments):
        self.segments.append((source, segments))
        return {"status": "ok", "data": {"message_id": 456}}

    def send_text(self, source, text: str):
        self.calls.append(("send_text", {"source": source, "text": text}))
        return {"status": "ok", "data": {"message_id": 789}}

    def get_status(self):
        return {"online": True, "good": True}

    def get_group_msg_history(self, group_id: str, count: int = 20):
        self.calls.append(("get_group_msg_history", {"group_id": group_id, "count": count}))
        return self.group_history[-count:]

    def get_friend_msg_history(self, user_id: str, count: int = 20):
        self.calls.append(("get_friend_msg_history", {"user_id": user_id, "count": count}))
        return self.friend_history[-count:]


def build_adapter() -> tuple[NapCatQQAdapter, FakeNapCatClient]:
    adapter = NapCatQQAdapter(PlatformConfig(enabled=True, extra={}))
    fake = FakeNapCatClient()
    adapter._client = fake
    return adapter, fake


def test_send_document_accepts_hermes_file_path_keyword_for_groups(tmp_path):
    adapter, fake = build_adapter()
    report = tmp_path / "report.txt"
    report.write_text("hello", encoding="utf-8")

    result = asyncio.run(
        adapter.send_document(
            chat_id="group:20002",
            file_path=str(report),
            file_name="报告.txt",
        )
    )

    assert result.success is True
    assert fake.uploads == [str(report.resolve())]
    assert fake.calls == [
        (
            "upload_group_file",
            {"group_id": "20002", "file": "remote:report.txt", "name": "报告.txt"},
        )
    ]


def test_send_document_accepts_legacy_document_url_and_filename(tmp_path):
    adapter, fake = build_adapter()
    report = tmp_path / "legacy.txt"
    report.write_text("hello", encoding="utf-8")

    result = asyncio.run(
        adapter.send_document(
            chat_id="private:10001",
            document_url=str(report),
            filename="legacy-name.txt",
        )
    )

    assert result.success is True
    assert fake.calls == [
        (
            "send_online_file",
            {
                "user_id": "10001",
                "file_path": str(report.resolve()),
                "file_name": "legacy-name.txt",
            },
        )
    ]


def test_media_methods_accept_native_hermes_path_keywords(tmp_path):
    adapter, fake = build_adapter()
    image = tmp_path / "image.png"
    audio = tmp_path / "voice.ogg"
    video = tmp_path / "video.mp4"
    for path in (image, audio, video):
        path.write_bytes(b"fake")

    assert asyncio.run(adapter.send_image_file(chat_id="group:20002", image_path=str(image))).success
    assert asyncio.run(adapter.send_voice(chat_id="group:20002", audio_path=str(audio))).success
    assert asyncio.run(adapter.send_video(chat_id="group:20002", video_path=str(video))).success

    segment_types = [segments[-1]["type"] for _, segments in fake.segments]
    segment_files = [segments[-1]["data"]["file"] for _, segments in fake.segments]
    assert segment_types == ["image", "record", "video"]
    assert segment_files == ["remote:image.png", "remote:voice.ogg", "remote:video.mp4"]


def test_remote_image_url_is_sent_as_segment_without_local_upload():
    adapter, fake = build_adapter()

    result = asyncio.run(
        adapter.send_image(
            chat_id="group:20002",
            image_url="https://example.invalid/picture.png",
            caption="配图",
        )
    )

    assert result.success is True
    assert fake.uploads == []
    assert fake.segments[0][1] == [
        {"type": "text", "data": {"text": "配图"}},
        {"type": "image", "data": {"file": "https://example.invalid/picture.png"}},
    ]


def test_websocket_header_keyword_matches_installed_websockets_signature():
    import inspect
    import websockets

    kwargs = NapCatQQAdapter._websocket_connect_kwargs({"Authorization": "Bearer token"})
    header_param = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"

    assert kwargs[header_param] == {"Authorization": "Bearer token"}
    assert {"additional_headers", "extra_headers"}.intersection(kwargs) == {header_param}
    assert kwargs["ping_interval"] > 0
    assert kwargs["ping_timeout"] > 0


def test_base64_record_is_cached_with_mp3_extension(monkeypatch):
    adapter, fake = build_adapter()
    fake.get_record = lambda file_key, out_format: {
        "base64": base64.b64encode(b"fake mp3").decode("ascii")
    }
    monkeypatch.setattr(
        "hermes_qq.adapter.cache_document_from_bytes",
        lambda data, filename: f"/cache/{filename}",
    )

    path = asyncio.run(adapter._resolve_record({"file": "record-key"}))

    assert path == "/cache/qq_audio.mp3"


def test_stream_disconnect_does_not_stop_qq_adapter_reconnect_loop():
    adapter, _fake = build_adapter()
    adapter._qq_should_run = True
    adapter._running = True

    adapter._mark_stream_disconnected()

    assert adapter._qq_should_run is True
    assert adapter.is_connected is False


def test_backfill_recent_group_messages_after_reconnect():
    adapter, fake = build_adapter()
    adapter.bot_user_id = "3860875399"
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    adapter.group_chat_all = True
    adapter.history_backfill_count = 5
    adapter.history_backfill_seconds = 600
    adapter._batch_delay_seconds = 0
    fake.group_history = [
        {
            "time": "not-a-number",
            "message_id": "malformed-time",
            "group_id": "20002",
            "user_id": "10001",
            "message": "",
            "raw_message": "",
        },
        {
            "time": 1,
            "message_id": "old",
            "group_id": "20002",
            "user_id": "10001",
            "message": "old",
            "raw_message": "old",
        },
        {
            "time": 9999999999,
            "message_id": "new",
            "group_id": "20002",
            "user_id": "10001",
            "message": "在吗",
            "raw_message": "在吗",
            "sender": {"user_id": "10001", "nickname": "tester"},
        },
    ]
    handled = []

    async def handle(event):
        handled.append(event)
        return None

    adapter.set_message_handler(handle)

    async def run_backfill():
        await adapter._backfill_recent_messages()
        await asyncio.sleep(0)
        if adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))

    asyncio.run(run_backfill())

    assert [event.text for event in handled] == ["在吗"]
    assert fake.calls == [("get_group_msg_history", {"group_id": "20002", "count": 5})]
