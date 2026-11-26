import httpx
import pytest

from lpc.ai.gateway import AIRequest, GatewayBlocked, complete
from lpc.matters import create_matter


def _matter(conn, settings, ai):
    return create_matter(conn, {"short_name": f"M{ai}", "caption": "A v. B", "side": "employee",
                                "ai_permitted": ai}, settings.matters_dir)


def _last_call(conn):
    return conn.execute("SELECT * FROM ai_calls ORDER BY id DESC LIMIT 1").fetchone()


def test_blocks_without_client_consent(conn, settings):
    mid = _matter(conn, settings, 0)
    with pytest.raises(GatewayBlocked):
        complete(conn, AIRequest(purpose="qa", prompt="hi", matter_id=mid))
    assert _last_call(conn)["status"] == "blocked"


def test_blocks_models_outside_zdr(conn, settings):
    mid = _matter(conn, settings, 1)
    with pytest.raises(GatewayBlocked, match="zero data retention"):
        complete(conn, AIRequest(purpose="qa", prompt="hi", matter_id=mid, model="claude-fable-5-2"))


def test_successful_call_is_logged(conn, settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("lpc.ai.gateway.get_api_key", lambda: "test-key")
    mid = _matter(conn, settings, 1)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = request.content
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Draft reply"}],
                                         "usage": {"input_tokens": 12, "output_tokens": 3}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = complete(conn, AIRequest(purpose="reply_draft", prompt="Draft a reply", matter_id=mid,
                                   document_ids=[1, 2]), client=client)
    assert out == "Draft reply"
    assert seen["headers"]["x-api-key"] == "test-key"
    row = _last_call(conn)
    assert row["status"] == "ok" and row["input_tokens"] == 12 and row["document_ids"] == "[1, 2]"


def _ok_client(seen, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_effort_is_sent_and_call_id_recorded(conn, settings, monkeypatch):
    monkeypatch.setattr("lpc.ai.gateway.get_api_key", lambda: "k")
    mid = _matter(conn, settings, 1)
    seen = {}
    req = AIRequest(purpose="chronology", prompt="x", matter_id=mid, effort="medium")
    payload = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "[]"}],
               "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 2}}
    assert complete(conn, req, client=_ok_client(seen, payload)) == "[]"
    assert seen["body"]["output_config"] == {"effort": "medium"} and "thinking" not in seen["body"]
    assert seen["body"]["max_tokens"] == 16000
    assert req.call_id == _last_call(conn)["id"] and req.stop_reason == "end_turn"


def test_refusal_is_logged_and_raised(conn, settings, monkeypatch):
    from lpc.ai.gateway import GatewayRefused

    monkeypatch.setattr("lpc.ai.gateway.get_api_key", lambda: "k")
    mid = _matter(conn, settings, 1)
    payload = {"content": [], "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber"},
               "usage": {"input_tokens": 5, "output_tokens": 0}}
    with pytest.raises(GatewayRefused, match="declined"):
        complete(conn, AIRequest(purpose="qa", prompt="x", matter_id=mid), client=_ok_client({}, payload))
    row = _last_call(conn)
    assert row["status"] == "error" and "(cyber)" in row["error"]
