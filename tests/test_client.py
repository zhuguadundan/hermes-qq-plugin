from hermes_qq.client import NapCatClient
from hermes_qq.types import QQEventSource


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return FakeResponse({"status": "ok", "data": {"message_id": 42}})


def test_send_private_text_uses_onebot_path_transport():
    client = NapCatClient("http://127.0.0.1:3000", token="secret")
    fake = FakeSession()
    client.session = fake

    result = client.send_text(
        QQEventSource(user_id="10001", group_id=None, message_id=None, self_id="42", raw={}),
        "hello",
    )

    assert result["data"]["message_id"] == 42
    assert fake.headers == {}
    assert fake.calls[0][0] == "http://127.0.0.1:3000/send_private_msg"
    assert fake.calls[0][1] == {"message": "hello", "user_id": "10001"}


def test_send_group_text_sets_group_id():
    client = NapCatClient("http://127.0.0.1:3000")
    fake = FakeSession()
    client.session = fake

    client.send_text(
        QQEventSource(user_id="10001", group_id="20002", message_id=None, self_id="42", raw={}),
        "hello",
    )

    assert fake.calls[0][0].endswith("/send_group_msg")
    assert fake.calls[0][1] == {"message": "hello", "group_id": "20002"}
