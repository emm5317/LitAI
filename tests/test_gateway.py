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
