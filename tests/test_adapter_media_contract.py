import asyncio
from pathlib import Path

from gateway.config import PlatformConfig
from gateway.platforms.qq import NapCatQQAdapter


class FakeNapCatClient:
    def __init__(self):
        self.uploads = []
        self.calls = []
        self.segments = []

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
