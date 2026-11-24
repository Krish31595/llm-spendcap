"""The proxy: /<provider>/<upstream path>, e.g. /openai/v1/chat/completions."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .core import Config, Key, Ledger, UsageTracker

log = logging.getLogger("spendcap")

HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
               "authorization", "x-api-key", "accept-encoding", "te", "upgrade"}


def _error(status: int, message: str, kind: str = "spendcap_error") -> JSONResponse:
    # Shaped like OpenAI errors so existing SDKs surface the message cleanly.
    return JSONResponse({"error": {"message": message, "type": kind, "code": kind}}, status_code=status)


def _client_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def create_app(cfg: Config, transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    ledger = Ledger(cfg.db)
    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), transport=transport)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    app = FastAPI(title="llm-spendcap", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.ledger = ledger

    @app.get("/spendcap/status")
    async def status(request: Request):
        key = cfg.keys.get(_client_key(request))
        if not key:
            return _error(401, "Unknown spendcap key", "invalid_api_key")
        spent = ledger.spent(key.name, key.period_start())
        return {"name": key.name, "period": key.period, "budget_usd": key.budget_usd,
                "spent_usd": round(spent, 6), "remaining_usd": round(max(0.0, key.budget_usd - spent), 6)}

    @app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(provider: str, path: str, request: Request):
        prov = cfg.providers.get(provider)
        if not prov:
            return _error(404, f"Unknown provider '{provider}'. Configured: {', '.join(cfg.providers)}")
        key: Optional[Key] = cfg.keys.get(_client_key(request))
        if not key:
            return _error(401, "Unknown spendcap key", "invalid_api_key")
        if not key.allows_provider(provider):
            return _error(403, f"Key '{key.name}' may not use provider '{provider}'", "forbidden")

        spent = ledger.spent(key.name, key.period_start())
        if spent >= key.budget_usd:
            return _error(429, f"Budget exhausted for '{key.name}': ${spent:.4f} of "
                               f"${key.budget_usd:.2f} this {key.period}", "budget_exceeded")

        body = await request.body()
        doc = None
        model = ""
        streaming = False
        if body and "json" in request.headers.get("content-type", ""):
            try:
                doc = json.loads(body)
            except ValueError:
                doc = None
        if isinstance(doc, dict):
            model = str(doc.get("model", ""))
            streaming = bool(doc.get("stream"))
            if model and not key.allows_model(model):
                return _error(403, f"Model '{model}' is not allowed for key '{key.name}'", "model_not_allowed")
            if model and cfg.block_unpriced and cfg.price_for(model) is None and provider != "openrouter":
                return _error(403, f"No price configured for model '{model}'. Add it under [prices].",
                              "unpriced_model")
            if streaming and provider in ("openai", "openrouter") and "chat/completions" in path:
                # Ask the API to include token usage in the final stream chunk.
                doc.setdefault("stream_options", {})["include_usage"] = True
            if provider == "openrouter":
                doc.setdefault("usage", {})["include"] = True
            body = json.dumps(doc).encode()

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        if prov.auth == "x-api-key":
            headers["x-api-key"] = prov.api_key
        else:
            headers["authorization"] = f"Bearer {prov.api_key}"
        url = f"{prov.base_url}/{path}"
        if request.url.query:
            url += "?" + request.url.query

        upstream_req = client.build_request(request.method, url, headers=headers, content=body)
        upstream = await client.send(upstream_req, stream=True)
        out_headers = {k: v for k, v in upstream.headers.items()
                       if k.lower() not in HOP_HEADERS and k.lower() != "content-encoding"}
        tracker = UsageTracker()

        def settle():
            used_model = model or tracker.model or "unknown"
            cost = tracker.cost if tracker.cost is not None else cfg.cost(used_model, tracker.tokens_in,
                                                                         tracker.tokens_out)
            if tracker.tokens_in or tracker.tokens_out or cost:
                ledger.record(key.name, provider, used_model, tracker.tokens_in, tracker.tokens_out, cost)
                log.info("%s %s in=%d out=%d $%.6f", key.name, used_model,
                         tracker.tokens_in, tracker.tokens_out, cost)

        ctype = upstream.headers.get("content-type", "")
        if "event-stream" in ctype or streaming:
            async def relay():
                buf = b""
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        for line in lines:
                            line = line.strip()
                            if line.startswith(b"data:"):
                                line = line[5:].strip()
                            if line.startswith(b"{"):
                                try:
                                    tracker.observe(json.loads(line))
                                except ValueError:
                                    pass
                finally:
                    await upstream.aclose()
                    settle()

            return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

        data = await upstream.aread()
        await upstream.aclose()
        if upstream.status_code < 400:
            try:
                tracker.observe(json.loads(data))
            except ValueError:
                pass
            settle()
        return Response(data, status_code=upstream.status_code, headers=out_headers)

    return app
