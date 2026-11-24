import json

import httpx
import pytest
from fastapi.testclient import TestClient

from spendcap.app import create_app
from spendcap.core import Config, Key, UsageTracker

SEEN = []


def upstream(request: httpx.Request) -> httpx.Response:
    SEEN.append(request)
    body = json.loads(request.content or b"{}")
    if request.url.host == "api.anthropic.com":
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": {"model": body["model"], "usage": {"input_tokens": 1000, "output_tokens": 1}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
                {"type": "message_delta", "usage": {"output_tokens": 500}},
            ]
            sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
        return httpx.Response(200, json={"model": body["model"], "usage": {"input_tokens": 1000, "output_tokens": 1000}})
    if request.url.host == "openrouter.ai":
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.75}})
    if body.get("stream"):
        chunks = [{"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
                  {"choices": [], "usage": {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000}}]
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
    return httpx.Response(200, json={"model": body.get("model"), "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})


@pytest.fixture
def client(tmp_path, monkeypatch):
    SEEN.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-anthropic")
    cfg = Config.from_dict({
        "server": {"db": str(tmp_path / "t.db")},
        "providers": {
            "openai": {"base_url": "https://api.openai.com", "api_key_env": "OPENAI_API_KEY"},
            "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY", "auth": "x-api-key"},
            "openrouter": {"base_url": "https://openrouter.ai/api", "api_key_env": "OPENROUTER_API_KEY"},
        },
        "keys": [
            {"key": "cap-a", "name": "a", "budget_usd": 1.0, "period": "day"},
            {"key": "cap-b", "name": "b", "budget_usd": 100, "models": ["gpt-4o-mini"], "providers": ["openai"]},
        ],
        "prices": {"gpt-4o-mini": [0.5, 1.0], "claude-haiku-*": [1.0, 5.0]},
    })
    return TestClient(create_app(cfg, transport=httpx.MockTransport(upstream)))


def chat(client, key, model="gpt-4o-mini", provider="openai", stream=False, path="v1/chat/completions"):
    return client.post(f"/{provider}/{path}", headers={"authorization": f"Bearer {key}"},
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream})


def status(client, key):
    return client.get("/spendcap/status", headers={"authorization": f"Bearer {key}"}).json()


def test_unknown_key(client):
    assert chat(client, "nope").status_code == 401


def test_real_key_swapped_in(client):
    assert chat(client, "cap-a").status_code == 200
    assert SEEN[-1].headers["authorization"] == "Bearer sk-real-openai"


def test_budget_enforced(client):
    assert chat(client, "cap-a").status_code == 200          # 1M in * 0.5 = $0.50
    assert status(client, "cap-a")["spent_usd"] == 0.5
    assert chat(client, "cap-a").status_code == 200          # now $1.00
    r = chat(client, "cap-a")
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded"


def test_openai_stream_usage_counted(client):
    r = chat(client, "cap-a", stream=True)
    assert r.status_code == 200 and "Hel" in r.text and "[DONE]" in r.text
    assert json.loads(SEEN[-1].content)["stream_options"]["include_usage"] is True
    assert status(client, "cap-a")["spent_usd"] == pytest.approx(2.0)  # 2M*0.5 + 1M*1.0


def test_anthropic_stream_and_auth(client):
    r = chat(client, "cap-a", model="claude-haiku-4-5", provider="anthropic", stream=True, path="v1/messages")
    assert r.status_code == 200
    assert SEEN[-1].headers["x-api-key"] == "sk-real-anthropic"
    assert "authorization" not in SEEN[-1].headers
    assert status(client, "cap-a")["spent_usd"] == pytest.approx((1000 * 1 + 500 * 5) / 1e6)


def test_openrouter_reported_cost(client):
    assert chat(client, "cap-a", model="meta/llama-x", provider="openrouter", path="v1/chat/completions").status_code == 200
    assert status(client, "cap-a")["spent_usd"] == 0.75


def test_model_and_provider_allowlist(client):
    assert chat(client, "cap-b", model="gpt-4o").status_code == 403
    assert chat(client, "cap-b", model="claude-haiku-4-5", provider="anthropic", path="v1/messages").status_code == 403


def test_unpriced_model_blocked(client):
    r = chat(client, "cap-a", model="gpt-9-ultra")
    assert r.status_code == 403 and r.json()["error"]["code"] == "unpriced_model"
    assert not SEEN


def test_tracker_anthropic_cache_tokens():
    t = UsageTracker()
    t.observe({"usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}})
    assert (t.tokens_in, t.tokens_out) == (100, 5)


def test_period_start():
    k = Key("x", "x", 1, "total")
    assert k.period_start() == 0.0
