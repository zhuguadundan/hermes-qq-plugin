import importlib.util
from pathlib import Path

import pytest
import requests


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "napcat_qq_bridge" / "bridge.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("napcat_qq_bridge_bridge", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, raise_error=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.raise_error = raise_error

    def raise_for_status(self):
        if self.raise_error is not None:
            raise self.raise_error
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def bridge():
    return _load_plugin()


@pytest.fixture
def make_cfg(bridge, tmp_path):
    def _factory(**overrides):
        values = dict(
            onebot_url="http://127.0.0.1:3000",
            onebot_token="token-123",
            onebot_ws_url="ws://127.0.0.1:3001",
            onebot_ws_token="token-123",
            listen_host="127.0.0.1",
            listen_port=8096,
            webhook_path="/napcat",
            receive_mode="ws",
            allowed_user_ids=["10001"],
            allowed_group_ids=["20001"],
            allowed_group_user_ids=[],
            allow_all=False,
            group_chat_all=False,
            hermes_bin="hermes",
            hermes_workdir=str(tmp_path),
            hermes_model="",
            hermes_provider="",
            hermes_toolsets="terminal,file",
            hermes_skills=[],
            temp_dir=str(tmp_path / "tmp"),
            state_dir=str(tmp_path / "state"),
            request_timeout=30,
            chunk_size=65536,
            poll_interval=3.0,
            poll_history_count=20,
            poll_backfill_seconds=180,
            ws_reconnect_delay=3.0,
            verbose=False,
        )
        values.update(overrides)
        return bridge.BridgeConfig(**values)

    return _factory


def test_napcat_client_prefers_path_style(bridge):
    client = bridge.NapCatClient("http://127.0.0.1:3000", token="abc", timeout=12)
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return FakeResponse({"status": "ok", "data": {"message_id": 42}})

    client.session.post = fake_post
    result = client.call("send_private_msg", {"user_id": "10001", "message": "hi"})

    assert result["data"]["message_id"] == 42
    assert calls[0][0] == "http://127.0.0.1:3000/send_private_msg"
    assert calls[0][1] == {"user_id": "10001", "message": "hi"}
    assert client._transport_mode == "path"


def test_napcat_client_falls_back_to_root_payload(bridge):
    client = bridge.NapCatClient("http://127.0.0.1:3000", token="abc", timeout=12)
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        if len(calls) == 1:
            raise requests.ConnectionError("path transport down")
        return FakeResponse({"status": "ok", "data": {"pong": True}})

    client.session.post = fake_post
    result = client.call("get_status", {})

    assert result["data"]["pong"] is True
    assert calls[0][0] == "http://127.0.0.1:3000/get_status"
    assert calls[1][0] == "http://127.0.0.1:3000"
    assert calls[1][1]["action"] == "get_status"
    assert calls[1][1]["params"] == {}
    assert client._transport_mode == "root"


def test_upload_file_stream_uses_chunk_protocol_without_full_buffer(bridge, tmp_path):
    payload = b"abcdefghi"
    local = tmp_path / "sample.bin"
    local.write_bytes(payload)
    client = bridge.NapCatClient("http://127.0.0.1:3000", token="abc", timeout=12, chunk_size=4)
    calls = []

    def fake_call(action, params=None):
        calls.append((action, params))
        if params and params.get("is_complete"):
            return {"status": "ok", "data": {"file_path": "/tmp/remote.bin"}}
        return {"status": "ok", "data": {}}

    client.call = fake_call

    result = client.upload_file_stream(str(local), chunk_size=4)

    assert result == "/tmp/remote.bin"
    assert len(calls) == 4
    first = calls[0][1]
    second = calls[1][1]
    third = calls[2][1]
    assert first["chunk_index"] == 0 and first["total_chunks"] == 3 and first["file_size"] == 9
    assert second["chunk_index"] == 1
    assert third["chunk_index"] == 2
    assert first["expected_sha256"] == second["expected_sha256"] == third["expected_sha256"]
    assert calls[-1][1] == {"stream_id": first["stream_id"], "is_complete": True}


def test_hermes_runner_uses_resume_and_parses_session_id(bridge, make_cfg, monkeypatch):
    cfg = make_cfg()
    runner = bridge.HermesRunner(cfg)
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            self.returncode = 0

        def communicate(self):
            return ("↻ Resumed session old-session (1 user messages, 2 total messages)\n\n╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────────╮\n回复内容\n\nsession_id: sess-123\n", "")

    monkeypatch.setattr(bridge.subprocess, "Popen", FakePopen)

    proc = runner.start("你好", session_id="old-session")
    output, session_id = runner.collect(proc)

    assert "--resume" in captured["cmd"]
    assert "old-session" in captured["cmd"]
    assert output == "回复内容"
    assert session_id == "sess-123"


def test_sanitize_output_filters_cli_noise_and_duplicate_blocks(bridge, make_cfg):
    runner = bridge.HermesRunner(make_cfg())
    raw = (
        "↻ Resumed session old-session (6 user messages, 12 total messages)\r\n"
        "\r\n"
        "╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────────╮\n"
        "  ┊ 🔎 preparing search_files…\n"
        "  ┊ 💻 $         pwd  0.3s\n"
        "│ streaming token\n"
        "\n"
        "有。\n"
        "\n"
        "看起来 skill 目录在：\n"
        "/home/dawei/.hermes/skills\n"
        "\n"
        "有。\n"
        "\n"
        "看起来 skill 目录在：\n"
        "/home/dawei/.hermes/skills\n"
        "\n"
        "如果你问的是当前这个环境的内置 skill，基本都在这里。\n"
        "╰──────────────────────────────────────────────────────────────────────────────╯\n"
    )

    sanitized = runner._sanitize_output(raw)

    assert sanitized == (
        "有。\n\n"
        "看起来 skill 目录在：\n"
        "/home/dawei/.hermes/skills\n\n"
        "如果你问的是当前这个环境的内置 skill，基本都在这里。"
    )


def test_sanitize_output_prefers_final_tail_after_streaming_overlap(bridge, make_cfg):
    runner = bridge.HermesRunner(make_cfg())
    raw = (
        "目前没有。\n\n"
        "我这边只看到你发过图片，\n"
        "目前没有。\n\n"
        "我这边只看到你发过图片，\n"
        "还没看到语音文件。"
    )

    assert runner._sanitize_output(raw) == "目前没有。\n\n我这边只看到你发过图片，\n还没看到语音文件。"


def test_group_message_requires_mention_or_reply(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    app.bot_user_id = "42"
    app.napcat.get_message = lambda message_id: {"sender": {"user_id": "42"}}
    source = bridge.EventSource(user_id="10001", group_id="20001", message_id="9", self_id="42", raw={})

    plain_event = {"message": [{"type": "text", "data": {"text": "hello"}}]}
    mention_event = {"message": [{"type": "at", "data": {"qq": "42"}}, {"type": "text", "data": {"text": " hello"}}]}
    reply_event = {"message": [{"type": "reply", "data": {"id": "88"}}, {"type": "text", "data": {"text": "继续"}}]}

    assert app.should_process_group_event(plain_event, source) is False
    assert app.should_process_group_event(mention_event, source) is True
    assert app.should_process_group_event(reply_event, source) is True


def test_private_send_file_prefers_online_file_action(bridge):
    client = bridge.NapCatClient("http://127.0.0.1:3000", token="abc", timeout=12)
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="1", self_id="42", raw={})
    calls = []

    def fake_call(action, params=None):
        calls.append((action, params))
        return {"status": "ok", "data": {}}

    client.call = fake_call
    client.upload_file_stream = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not upload for private online-file happy path"))
    client.send_file(source, "/tmp/demo.txt", "demo.txt")

    assert calls == [("send_online_file", {"user_id": "10001", "file_path": "/tmp/demo.txt", "file_name": "demo.txt"})]


def test_build_prompt_resolves_online_file_segments(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="7", self_id="42", raw={})
    app.resolve_online_file = lambda src, data: "/tmp/inbox/report.pdf"
    event = {
        "message": [{"type": "onlinefile", "data": {"msgId": "m1", "elementId": "e1", "fileName": "report.pdf"}}],
        "sender": {"nickname": "tester"},
    }

    prompt = app.build_prompt(event, source)

    assert "用户发送了在线文件：/tmp/inbox/report.pdf" in prompt


def test_reset_command_clears_session_and_pending_queue(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    app.bot_user_id = "42"
    sent = []
    app.napcat.send_text = lambda source, text: sent.append(text)
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="1", self_id="42", raw={})
    chat_key = app.chat_key(source)
    app.sessions.set(chat_key, "sess-existing")
    state = app._chat_state(chat_key)
    state.worker_active = True
    state.pending_task = bridge.QueuedEvent(source=source, event={"message": []}, chat_key=chat_key)

    event = {"post_type": "message", "user_id": "10001", "self_id": "42", "message": "/reset"}
    code, body = app.handle_event(event)

    assert code == 200
    assert body["command"] == "/reset"
    assert app.sessions.get(chat_key) is None
    assert state.pending_task is None
    assert sent[-1].startswith("已重置当前会话")


def test_enqueue_interrupts_running_process_and_merges_pending_text(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    app.bot_user_id = "42"
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="1", self_id="42", raw={})
    chat_key = app.chat_key(source)
    state = app._chat_state(chat_key)
    state.worker_active = True

    class FakeProc:
        def __init__(self):
            self.running = True
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminate_calls += 1
            self.running = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.kill_calls += 1
            self.running = False

    proc = FakeProc()
    state.active_process = proc

    first = {"message": [{"type": "text", "data": {"text": "第一条"}}], "sender": {"nickname": "tester"}}
    second = {"message": [{"type": "text", "data": {"text": "第二条"}}], "sender": {"nickname": "tester"}}

    app.enqueue_event(source, first)
    app.enqueue_event(source, second)

    assert proc.terminate_calls == 1
    assert state.pending_task is not None
    merged_segments = app.message_segments(state.pending_task.event)
    assert "".join(seg.get("data", {}).get("text", "") for seg in merged_segments if seg.get("type") == "text") == "第一条\n第二条"


def test_enqueue_requeues_interrupted_active_task_once(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="2", self_id="42", raw={})
    chat_key = app.chat_key(source)
    state = app._chat_state(chat_key)
    state.worker_active = True
    state.active_task = bridge.QueuedEvent(
        source=source,
        event={"message": [{"type": "text", "data": {"text": "正在处理"}}]},
        chat_key=chat_key,
    )

    class FakeProc:
        def __init__(self):
            self.running = True
            self.terminate_calls = 0

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminate_calls += 1
            self.running = False

        def wait(self, timeout=None):
            return 0

    proc = FakeProc()
    state.active_process = proc
    event = {"message": [{"type": "text", "data": {"text": "后续补充"}}], "sender": {"nickname": "tester"}}

    app.enqueue_event(source, event)
    assert proc.terminate_calls == 1
    assert state.pending_task is not None
    merged_text = "".join(
        seg.get("data", {}).get("text", "")
        for seg in app.message_segments(state.pending_task.event)
        if seg.get("type") == "text"
    )
    assert merged_text == "正在处理\n后续补充"


def test_group_chat_key_isolated_per_user(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    source = bridge.EventSource(user_id="10001", group_id="20001", message_id="1", self_id="42", raw={})
    assert app.chat_key(source) == "group:20001:user:10001"


def test_handle_event_deduplicates_same_message_id(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    app.bot_user_id = "42"
    queued = []
    app.enqueue_event = lambda source, event: queued.append((source.user_id, event.get("message_id"))) or 1
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": "10001",
        "self_id": "42",
        "message_id": "mid-1",
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "sender": {"user_id": "10001", "nickname": "tester"},
    }

    first = app.handle_event(event, origin="poll")
    second = app.handle_event(event, origin="webhook")

    assert first[0] == 202
    assert second[0] == 200
    assert second[1]["ignored"] == "duplicate"
    assert queued == [("10001", "mid-1")]


def test_bootstrap_candidates_only_recover_recent_unreplied_messages(bridge, make_cfg, monkeypatch):
    app = bridge.BridgeApp(make_cfg(poll_backfill_seconds=180))
    monkeypatch.setattr(bridge.time, "time", lambda: 1000.0)
    messages = [
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "old-user", "time": 700, "message": "old"},
        {"post_type": "message_sent", "message_type": "private", "user_id": "42", "self_id": "42", "message_id": "bot", "time": 930, "message": "reply", "sender": {"user_id": "42"}},
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "fresh-user", "time": 950, "message": "在吗"},
    ]

    candidates = app._bootstrap_candidates(messages)

    assert "private:10001:fresh-user" in candidates
    assert "private:10001:old-user" not in candidates


def test_gap_recovery_replays_missing_history_messages(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="current", self_id="42", raw={})
    app._target_sequences[app.history_target_key(source)] = 10
    handled = []
    app.handle_event = lambda event, origin="webhook": handled.append((origin, event.get("real_seq")))
    app.napcat.get_friend_msg_history = lambda user_id, count=20: [
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "m11", "real_seq": "11", "message": "a"},
        {"post_type": "message_sent", "message_type": "private", "user_id": "42", "self_id": "42", "message_id": "m12", "real_seq": "12", "message": "self"},
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "m13", "real_seq": "13", "message": "b"},
    ]

    app.recover_gap_if_needed(
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "m14", "real_seq": "14", "message": "current"},
        source,
        origin="webhook",
    )

    assert handled == [("gap-replay", "11"), ("gap-replay", "13")]


def test_gap_recovery_also_runs_for_ws_origin(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg())
    source = bridge.EventSource(user_id="10001", group_id=None, message_id="current", self_id="42", raw={})
    app._target_sequences[app.history_target_key(source)] = 10
    handled = []
    app.handle_event = lambda event, origin="webhook": handled.append((origin, event.get("real_seq")))
    app.napcat.get_friend_msg_history = lambda user_id, count=20: [
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "m11", "real_seq": "11", "message": "a"},
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "m12", "real_seq": "12", "message": "b"},
    ]

    app.recover_gap_if_needed(
        {"post_type": "message", "message_type": "private", "user_id": "10001", "self_id": "42", "message_id": "m13", "real_seq": "13", "message": "current"},
        source,
        origin="ws",
    )

    assert handled == [("gap-replay", "11"), ("gap-replay", "12")]


def test_build_arg_parser_defaults_to_ws_mode_and_no_polling(bridge, monkeypatch):
    for key in list(bridge.os.environ):
        if key.startswith("NAPCAT_QQ_BRIDGE_POLL_"):
            monkeypatch.delenv(key, raising=False)
        if key.startswith("NAPCAT_QQ_BRIDGE_RECEIVE_MODE"):
            monkeypatch.delenv(key, raising=False)
    parser = bridge.argparse.ArgumentParser()
    bridge.build_arg_parser(parser)
    args = parser.parse_args([])

    assert args.receive_mode == "ws"
    assert args.poll_interval == 0.0


def test_args_to_config_reads_ws_fields_from_config_file(bridge, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "onebot": {
            "url": "http://127.0.0.1:3000",
            "token": "http-token",
            "ws_url": "ws://127.0.0.1:3001",
            "ws_token": "ws-token"
          },
          "bridge": {
            "receive_mode": "ws",
            "ws_reconnect_delay": 5
          }
        }
        """,
        encoding="utf-8",
    )

    parser = bridge.argparse.ArgumentParser()
    bridge.build_arg_parser(parser)
    args = parser.parse_args(["--config-file", str(config_path)])
    cfg = bridge.args_to_config(args)

    assert cfg.onebot_ws_url == "ws://127.0.0.1:3001"
    assert cfg.onebot_ws_token == "ws-token"
    assert cfg.receive_mode == "ws"
    assert cfg.ws_reconnect_delay == 5.0


def test_startup_check_runs_websocket_preflight_in_ws_mode(bridge, make_cfg):
    app = bridge.BridgeApp(make_cfg(receive_mode="ws"))
    app.napcat.get_login_info = lambda: {"user_id": "42", "nickname": "bot"}
    called = []
    app.websocket_preflight_check = lambda: called.append(True)

    result = app.startup_check()

    assert result["user_id"] == "42"
    assert called == [True]
