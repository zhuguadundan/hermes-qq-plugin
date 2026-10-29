import asyncio
import base64
import time
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
        self.group_files = []

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
    def get_group_root_files(self, group_id: str):
        self.calls.append(("get_group_root_files", {"group_id": group_id}))
        return {"files": self.group_files, "folders": []}

    def get_group_file_url(self, group_id: str, file_id: str, busid=None):
        self.calls.append(("get_group_file_url", {"group_id": group_id, "file_id": file_id, "busid": busid}))
        return {"url": f"https://cdn.example/{file_id}?busid={busid}"}

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
        # Let background group-file downloads finish (they use to_thread).
        for _ in range(400):
            if not adapter._group_file_tasks:
                break
            await asyncio.sleep(0.01)

    asyncio.run(go())


def _isolated_group_file_root(adapter, tmp_path):
    adapter._group_file_root = lambda: tmp_path  # type: ignore[method-assign]
    return tmp_path


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


def test_group_file_upload_is_collected_without_triggering_a_reply(tmp_path):
    adapter, fake, handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    downloads = []

    def fake_fetch(url, dest, max_bytes):
        downloads.append(url)
        dest.write_bytes(b"%PDF-1.4 weekly report")
        return dest.stat().st_size

    adapter._fetch_group_file = fake_fetch  # type: ignore[method-assign]

    upload = {
        "post_type": "notice",
        "notice_type": "group_upload",
        "group_id": "20002",
        "user_id": "10001",
        "time": 9999999999,
        "file": {"id": "f1", "name": "周报.pdf", "size": 1024, "busid": 102},
        "sender": {"user_id": "10001", "nickname": "张三"},
    }
    _feed(adapter, [upload])

    assert handled == []
    assert fake.calls == [("get_group_file_url", {"group_id": "20002", "file_id": "f1", "busid": 102})]
    assert len(downloads) == 1

    saved = list((tmp_path / "20002").glob("*.pdf"))
    assert len(saved) == 1
    entry = adapter._passive_backlog["20002"][0]
    assert entry.attachment == str(saved[0])

    rendered = adapter._render_passive_context("20002")
    assert "[文件] 周报.pdf" in rendered
    assert str(saved[0]) in rendered
    assert "已存到本地" in rendered


def test_group_file_size_cap_skips_download(tmp_path):
    adapter, fake, handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter.group_file_max_bytes = 1024
    adapter._fetch_group_file = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download"))  # type: ignore[method-assign]

    upload = {
        "post_type": "notice",
        "notice_type": "group_upload",
        "group_id": "20002",
        "user_id": "10001",
        "time": 9999999999,
        "file": {"id": "f2", "name": "录屏.mp4", "size": 80 * 1024 * 1024, "busid": 102},
        "sender": {"user_id": "10001", "nickname": "张三"},
    }
    _feed(adapter, [upload])

    assert handled == []
    assert fake.calls == []
    entry = adapter._passive_backlog["20002"][0]
    assert entry.attachment is None
    assert "超过本地保存上限" in (entry.note or "")
    assert "80.0MB" in (entry.note or "")


def test_group_file_index_dedupes_repeated_uploads(tmp_path):
    adapter, fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    downloads = []

    def fake_fetch(url, dest, max_bytes):
        downloads.append(url)
        dest.write_bytes(b"data")
        return 4

    adapter._fetch_group_file = fake_fetch  # type: ignore[method-assign]

    def upload(file_id: str, name: str, ts: int):
        return {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": "20002",
            "user_id": "10001",
            "time": ts,
            "file": {"id": file_id, "name": name, "size": 10, "busid": 102},
            "sender": {"user_id": "10001", "nickname": "张三"},
        }

    # Two distinct events (different timestamps so transport dedupe does not
    # apply) referencing the same group file id.
    _feed(adapter, [upload("f3", "方案.docx", 9999999999), upload("f3", "方案.docx", 9999999998)])

    assert len(downloads) == 1
    assert [c[0] for c in fake.calls] == ["get_group_file_url"]
    # The index survives on disk, so a fresh adapter reuses the cached path.
    assert (tmp_path / "index.json").exists()
    fresh, fresh_fake, _ = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(fresh, tmp_path)
    fresh._fetch_group_file = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-download"))  # type: ignore[method-assign]
    _feed(fresh, [upload("f3", "方案.docx", 9999999997)])
    assert fresh_fake.calls == []
    assert fresh._passive_backlog["20002"][0].attachment


def test_group_file_autosave_can_be_disabled(tmp_path):
    adapter, fake, handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter.group_file_autosave = False

    upload = {
        "post_type": "notice",
        "notice_type": "group_upload",
        "group_id": "20002",
        "user_id": "10001",
        "time": 9999999999,
        "file": {"id": "f4", "name": "周报.pdf", "size": 10, "busid": 102},
        "sender": {"user_id": "10001", "nickname": "张三"},
    }
    _feed(adapter, [upload])

    assert handled == []
    assert fake.calls == []
    entry = adapter._passive_backlog["20002"][0]
    assert entry.attachment is None
    assert "[文件] 周报.pdf" in adapter._render_passive_context("20002")


def test_group_file_sweep_removes_expired_files(tmp_path):
    import os

    adapter, _fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    old_dir = tmp_path / "20002"
    old_dir.mkdir(parents=True)
    stale = old_dir / "20260101-000000-old.pdf"
    stale.write_bytes(b"old")
    os.utime(stale, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    adapter._group_file_index = {"20002:f9": {"name": "old.pdf", "path": str(stale), "ts": 1}}
    adapter.group_file_retention_days = 7

    adapter._sweep_group_file_cache()

    assert not stale.exists()
    # The entry survives as a tombstone so the connect scan does not fetch it again.
    tomb = adapter._group_file_index["20002:f9"]
    assert "path" not in tomb and tomb["expired_at"] > 0


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


def test_group_file_backfill_collects_recent_files_once(tmp_path):
    adapter, fake, handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    adapter.group_file_backfill_limit = 5
    downloads = []

    def fake_fetch(url, dest, max_bytes):
        downloads.append(url)
        dest.write_bytes(b"xls")
        return 3

    adapter._fetch_group_file = fake_fetch  # type: ignore[method-assign]
    now = int(time.time())
    fake.group_files = [
        {"file_id": "a1", "file_name": "昨天.xlsx", "size": 3, "busid": 102, "modify_time": 100, "uploader_name": "官方瓜", "uploader": 4015317804},
        {"file_id": "a2", "file_name": "刚上传.xlsx", "size": 3, "busid": 102, "modify_time": now - 60, "uploader_name": "大媛", "uploader": 776826706},
        {"file_id": "a3", "file_name": "超大.zip", "size": 999 * 1024 * 1024, "busid": 102, "modify_time": now},
    ]

    asyncio.run(adapter._backfill_group_files())

    assert handled == []
    # Oversized file is skipped; both normal ones are saved to disk.
    assert len(downloads) == 2
    assert len(list((tmp_path / "20002").glob("*"))) == 2
    # Only the file from the recent window becomes mention-time context.
    assert [e.body for e in adapter._passive_backlog["20002"]] == ["[文件] 刚上传.xlsx"]
    entry = adapter._passive_backlog["20002"][0]
    assert entry.attachment and Path(entry.attachment).exists()
    rendered = adapter._render_passive_context("20002")
    assert "已存到本地" in rendered and "刚上传.xlsx" in rendered
    assert "昨天.xlsx" not in rendered

    # A second scan must not re-download anything (index survives restarts).
    downloads.clear()
    asyncio.run(adapter._backfill_group_files())
    assert downloads == []
    assert len(adapter._passive_backlog["20002"]) == 1


def test_group_file_backfill_resurfaces_recently_cached_file_after_restart(tmp_path):
    import json

    adapter, fake, handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    adapter.history_backfill_seconds = 600
    adapter._fetch_group_file = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download"))  # type: ignore[method-assign]

    existing = tmp_path / "20002" / "20261029-084400-周报.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"pdf")
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                adapter._group_file_key("20002", "周报.pdf", 3): {
                    "name": "周报.pdf",
                    "path": str(existing),
                    "size": 3,
                    "ts": time.time(),
                    "group_id": "20002",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake.group_files = [
        {"file_id": "b1", "file_name": "周报.pdf", "size": 3, "busid": 102, "modify_time": int(time.time()), "uploader_name": "张三"},
    ]

    asyncio.run(adapter._backfill_group_files())

    assert handled == []
    entries = adapter._passive_backlog["20002"]
    assert len(entries) == 1
    assert entries[0].attachment == str(existing)
    assert entries[0].body == "[文件] 周报.pdf"


def test_group_file_backfill_dedupes_when_napcat_rotates_file_ids(tmp_path):
    adapter, fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    downloads = []

    def fake_fetch(url, dest, max_bytes):
        downloads.append(url)
        dest.write_bytes(b"pdf")
        return 3

    adapter._fetch_group_file = fake_fetch  # type: ignore[method-assign]

    def listing(file_id: str):
        return [
            {
                "file_id": file_id,  # NapCat hands out a new id on every call
                "file_name": "周报.pdf",
                "size": 3,
                "busid": 102,
                "modify_time": int(time.time()),
                "uploader_name": "张三",
            }
        ]

    fake.group_files = listing("id-first")
    asyncio.run(adapter._backfill_group_files())
    assert len(downloads) == 1

    fake.group_files = listing("id-second")
    asyncio.run(adapter._backfill_group_files())
    assert len(downloads) == 1  # no re-download despite the new file_id
    assert len(adapter._passive_backlog["20002"]) == 1
    assert len(list((tmp_path / "20002").glob("*.pdf"))) == 1


def test_listen_context_expires_after_ttl():
    adapter, _fake, handled = _mention_only_adapter(listen_context=10)
    adapter.group_listen_context_ttl_seconds = 120 * 60

    stale_ts = time.time() - 3 * 3600
    adapter._record_passive_group_event(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": "3860875399",
            "group_id": "20002",
            "user_id": "10001",
            "message_id": "old",
            "time": stale_ts,
            "message": [{"type": "text", "data": {"text": "三小时前的闲聊"}}],
            "sender": {"user_id": "10001", "nickname": "张三"},
        },
        adapter._build_qq_source({"group_id": "20002", "user_id": "10001", "time": stale_ts}),
    )
    # Stale entry is dropped instead of being injected hours later.
    assert adapter._render_passive_context("20002") == ""
    assert "20002" not in adapter._passive_backlog

    _feed(adapter, [_group_message("在吗", message_id="m1", at_self=True)])
    assert handled[0].text == "在吗"


def test_file_sweep_runs_at_most_once_per_interval(tmp_path):
    import os

    adapter, _fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    stale = tmp_path / "20002" / "old.pdf"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    adapter.group_file_retention_days = 7

    adapter._sweep_group_file_cache()
    assert not stale.exists()

    fresh = tmp_path / "20002" / "fresh.pdf"
    fresh.write_bytes(b"new")
    os.utime(fresh, (old, old))
    adapter._sweep_group_file_cache()  # inside the 1h window → no-op
    assert fresh.exists()

    os.utime(fresh, (old, old))
    adapter._sweep_group_file_cache(force=True)
    assert not fresh.exists()


def test_tombstoned_file_is_not_re_collected_until_it_changes(tmp_path):
    import os

    adapter, fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    adapter.group_file_retention_days = 7
    downloads = []

    def fake_fetch(url, dest, max_bytes):
        downloads.append(url)
        dest.write_bytes(b"pdf")
        return 3

    adapter._fetch_group_file = fake_fetch  # type: ignore[method-assign]

    def listing(modify_time: int):
        return [
            {
                "file_id": f"id-{modify_time}",
                "file_name": "周报.pdf",
                "size": 3,
                "busid": 102,
                "modify_time": modify_time,
                "uploader_name": "张三",
            }
        ]

    fake.group_files = listing(1000)
    asyncio.run(adapter._backfill_group_files())
    assert len(downloads) == 1

    saved = next((tmp_path / "20002").glob("*.pdf"))
    old = time.time() - 30 * 86400
    os.utime(saved, (old, old))
    adapter._sweep_group_file_cache(force=True)
    assert not saved.exists()

    # Same version → not re-downloaded.
    fake.group_files = listing(1000)
    asyncio.run(adapter._backfill_group_files())
    assert len(downloads) == 1

    # Genuinely newer upload of the same name+size → collected again.
    fake.group_files = listing(2000)
    asyncio.run(adapter._backfill_group_files())
    assert len(downloads) == 2


def test_cache_size_cap_evicts_oldest_and_keeps_retention_intact(tmp_path):
    import os

    adapter, _fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter.group_file_retention_days = 7
    adapter.group_file_cache_max_bytes = 6  # tiny cap for the test

    group_dir = tmp_path / "20002"
    group_dir.mkdir(parents=True)
    now = time.time()
    oldest = group_dir / "a.bin"
    middle = group_dir / "b.bin"
    newest = group_dir / "c.bin"
    for path, age, payload in ((oldest, 3 * 3600, b"1234"), (middle, 2 * 3600, b"5678"), (newest, 3600, b"9abc")):
        path.write_bytes(payload)
        os.utime(path, (now - age, now - age))

    index = {}
    for path in (oldest, middle, newest):
        index[adapter._group_file_key("20002", path.name, 4)] = {
            "name": path.name,
            "path": str(path),
            "size": 4,
            "ts": path.stat().st_mtime,
            "src_mtime": path.stat().st_mtime,
            "group_id": "20002",
        }
    adapter._group_file_index = index

    adapter._sweep_group_file_cache(force=True)

    # Nothing was old enough for the 7-day rule, so only the size cap acted.
    assert not oldest.exists()
    assert not middle.exists()  # still 8 bytes > 6 cap after the first eviction
    assert newest.exists()
    assert (group_dir / "c.bin").stat().st_size == 4
    tomb = adapter._group_file_index[adapter._group_file_key("20002", "a.bin", 4)]
    assert tomb["reason"] == "size-cap" and "path" not in tomb


def test_cache_size_cap_zero_disables_the_backstop(tmp_path):
    import os

    adapter, _fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter.group_file_retention_days = 7
    adapter.group_file_cache_max_bytes = 0

    group_dir = tmp_path / "20002"
    group_dir.mkdir(parents=True)
    big = group_dir / "a.bin"
    big.write_bytes(b"x" * 4096)
    old = time.time() - 3600
    os.utime(big, (old, old))

    adapter._sweep_group_file_cache(force=True)

    assert big.exists()


def test_retention_still_wins_when_it_is_enough(tmp_path):
    import os

    adapter, _fake, _handled = _mention_only_adapter(listen_context=10)
    _isolated_group_file_root(adapter, tmp_path)
    adapter.group_file_retention_days = 7
    adapter.group_file_cache_max_bytes = 10 * 1024 * 1024

    group_dir = tmp_path / "20002"
    group_dir.mkdir(parents=True)
    expired = group_dir / "old.bin"
    expired.write_bytes(b"x" * 1024)
    ancient = time.time() - 30 * 86400
    os.utime(expired, (ancient, ancient))

    adapter._sweep_group_file_cache(force=True)

    assert not expired.exists()
