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


def _group_message(text: str, *, message_id: str, user: str = "10001", nickname: str = "tester", at_self: bool = False):
    segments = []
    if at_self:
        segments.append({"type": "at", "data": {"qq": "3860875399"}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": "3860875399",
        "group_id": "20002",
        "user_id": user,
        "message_id": message_id,
        "time": 9999999999,
        "message": segments,
        "sender": {"user_id": user, "nickname": nickname},
    }


def _mention_only_adapter(listen_context: int):
    adapter, fake = build_adapter()
    adapter.bot_user_id = "3860875399"
    adapter._group_policy = "open"
    adapter.group_chat_all = False
    adapter.group_listen_context = listen_context
    adapter._batch_delay_seconds = 0
    handled = []

    async def handle(event):
        # Stand-in for the gateway turn: proves whether a turn was triggered.
        handled.append(event)

    adapter.handle_message = handle  # type: ignore[method-assign]
    return adapter, fake, handled


def _feed(adapter, events):
    async def go():
        for event in events:
            await adapter._handle_onebot_event(event)
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(go())


def test_listen_mode_does_not_trigger_a_turn_for_plain_group_messages():
    adapter, _fake, handled = _mention_only_adapter(listen_context=10)

    _feed(
        adapter,
        [
            _group_message("今天开会吗", message_id="m1", nickname="张三"),
            _group_message("开，三点", message_id="m2", user="10002", nickname="李四"),
        ],
    )

    assert handled == []
    assert len(adapter._passive_backlog["20002"]) == 2


def test_mention_prepends_context_then_clears_backlog():
    adapter, _fake, handled = _mention_only_adapter(listen_context=10)

    _feed(
        adapter,
        [
            _group_message("今天开会吗", message_id="m1", nickname="张三"),
            _group_message("开，三点", message_id="m2", user="10002", nickname="李四"),
            _group_message("你在吗", message_id="m3", at_self=True),
        ],
    )

    assert len(handled) == 1
    text = handled[0].text
    assert "张三: 今天开会吗" in text
    assert "李四: 开，三点" in text
    assert text.endswith("[当前消息] 你在吗")
    assert adapter._passive_backlog.get("20002") is None

    _feed(adapter, [_group_message("再来一次", message_id="m4", at_self=True)])
    assert handled[1].text == "再来一次"


def test_listen_backlog_respects_configured_window():
    adapter, _fake, handled = _mention_only_adapter(listen_context=3)

    _feed(
        adapter,
        [_group_message(f"第{i}条", message_id=f"m{i}", user="10001") for i in range(6)]
        + [_group_message("在吗", message_id="m9", at_self=True)],
    )

    text = handled[0].text
    assert "第0条" not in text
    assert "第3条" in text and "第5条" in text
    assert "共 3 条" in text


def test_group_file_upload_is_recorded_without_reply_or_download():
    adapter, fake, handled = _mention_only_adapter(listen_context=10)

    upload = {
        "post_type": "notice",
        "notice_type": "group_upload",
        "group_id": "20002",
        "user_id": "10001",
        "time": 9999999999,
        "file": {"id": "f1", "name": "周报.pdf", "size": 1024},
        "sender": {"user_id": "10001", "nickname": "张三"},
    }
    _feed(adapter, [upload])

    assert handled == []
    assert fake.calls == []  # no get_group_file_url / download attempt
    assert "[文件] 周报.pdf" in "".join(adapter._passive_backlog["20002"])


def test_listen_mode_keeps_slash_commands_working():
    adapter, _fake, handled = _mention_only_adapter(listen_context=10)

    _feed(
        adapter,
        [
            _group_message("闲聊一句", message_id="m1"),
            _group_message("/new", message_id="m2", at_self=True),
        ],
    )

    assert [event.text for event in handled] == ["/new"]
    assert handled[0].is_command()
