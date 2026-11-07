"""Sender filtering must happen before model dispatch and context buffering."""

import asyncio
import time

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.qq import NapCatQQAdapter
from test_adapter_media_contract import (
    _feed,
    _group_message,
    _isolated_group_file_root,
    _mention_only_adapter,
)

BOT = "4015317804"


def filtered_adapter(listen_context=15):
    adapter, fake, handled = _mention_only_adapter(listen_context)
    adapter._group_ignore_from = [BOT]
    return adapter, fake, handled


def test_filter_configuration_loads_numeric_qq_ids_as_strings():
    adapter = NapCatQQAdapter(PlatformConfig(enabled=True, extra={"group_ignore_from": [4015317804]}))
    assert adapter._is_ignored_group_user(BOT)
    assert not adapter._is_ignored_group_user("3916381814")


def test_ignored_messages_do_not_consume_context_slots_or_dispatch():
    adapter, fake, handled = filtered_adapter(listen_context=2)
    events = [_group_message("保留这条人的消息", message_id="human-1")]
    events += [
        _group_message("机器人返回的内容", message_id=f"bot-{i}", user=BOT, nickname="小小噜")
        for i in range(20)
    ]
    events += [
        _group_message("同名普通成员", message_id="human-2", user="10002", nickname="小小噜"),
        _group_message("请看上下文", message_id="mention", at_self=True),
    ]
    _feed(adapter, events)
    assert len(handled) == 1
    assert "保留这条人的消息" in handled[0].text
    assert "同名普通成员" in handled[0].text
    assert "机器人返回的内容" not in handled[0].text
    assert "共 2 条" in handled[0].text
    assert fake.calls == []


@pytest.mark.parametrize("text", ["普通消息", "@时也不能触发", "/new"])
@pytest.mark.parametrize("chat_all", [False, True])
def test_ignored_sender_cannot_dispatch_even_with_mention_or_renamed_nickname(text, chat_all):
    adapter, _fake, handled = filtered_adapter()
    adapter.group_chat_all = chat_all
    # Deny takes precedence even when the same account is explicitly allowed.
    adapter._allowed_group_users = [BOT]
    event = _group_message(text, message_id="ignored", user=BOT, nickname="已改昵称", at_self=True)
    _feed(adapter, [event])
    assert handled == []
    assert adapter._passive_backlog == {}
    assert adapter._seen_events == {}


def test_ignored_upload_notice_does_not_download_or_enter_context():
    adapter, fake, handled = filtered_adapter()
    _feed(adapter, [{
        "post_type": "notice", "notice_type": "group_upload", "group_id": "20002",
        "operator_id": BOT, "time": time.time(),
        "file": {"id": "ignored-file", "name": "机器人文件.pdf", "size": 3},
    }])
    assert handled == []
    assert fake.calls == []
    assert adapter._group_file_tasks == {}
    assert adapter._passive_backlog == {}


def test_history_backfill_uses_the_same_sender_filter():
    adapter, fake, handled = filtered_adapter()
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    fake.group_history = [
        _group_message("不应回补", message_id="b1", user=BOT),
        _group_message("机器人命令", message_id="b2", user=BOT, at_self=True),
        _group_message("人的上下文", message_id="h1"),
        _group_message("人的请求", message_id="h2", at_self=True),
    ]

    async def run():
        await adapter._backfill_recent_messages()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(handled) == 1
    assert "人的上下文" in handled[0].text
    assert "人的请求" in handled[0].text
    assert "不应回补" not in handled[0].text
    assert "机器人命令" not in handled[0].text


@pytest.mark.parametrize("already_cached", [False, True])
def test_file_scan_preserves_archiving_but_never_injects_ignored_uploads(tmp_path, already_cached):
    adapter, fake, handled = filtered_adapter()
    _isolated_group_file_root(adapter, tmp_path)
    adapter._group_policy = "allowlist"
    adapter._group_allow_from = ["20002"]
    now = int(time.time())
    fake.group_files = [{
        "file_id": "file-bot", "file_name": "机器人文件.pdf", "size": 3,
        "modify_time": now, "uploader": int(BOT), "uploader_name": "已改昵称",
    }]
    downloads = []

    def fetch(url, dest, max_bytes):
        downloads.append(url)
        dest.write_bytes(b"pdf")
        return 3

    adapter._fetch_group_file = fetch
    if already_cached:
        saved = tmp_path / "20002" / "机器人文件.pdf"
        saved.parent.mkdir(parents=True)
        saved.write_bytes(b"pdf")
        adapter._group_file_index = {
            adapter._group_file_key("20002", "机器人文件.pdf", 3): {
                "path": str(saved), "name": "机器人文件.pdf", "size": 3,
                "group_id": "20002", "src_mtime": now, "ts": now,
            }
        }
    asyncio.run(adapter._backfill_group_files())
    assert len(downloads) == (0 if already_cached else 1)
    assert handled == []
    assert adapter._passive_backlog == {}
    assert len(list((tmp_path / "20002").glob("*.pdf"))) == 1


def test_private_chat_policy_and_unconfigured_filter_are_unchanged():
    adapter, _fake, _handled = filtered_adapter()
    private = adapter._build_qq_source({"user_id": BOT})
    assert adapter._is_allowed(private)
    adapter._dm_policy = "disabled"
    assert not adapter._is_allowed(private)
    adapter._group_ignore_from = []
    assert adapter._is_allowed(adapter._build_qq_source({"user_id": BOT, "group_id": "20002"}))
