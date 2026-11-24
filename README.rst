llm-spendcap: spending limits for OpenAI, Anthropic and OpenRouter API keys
============================================================================

.. image:: https://github.com/Krish31595/llm-spendcap/actions/workflows/test.yml/badge.svg
   :target: https://github.com/Krish31595/llm-spendcap/actions
.. image:: https://img.shields.io/github/stars/Krish31595/llm-spendcap?style=social
   :target: https://github.com/Krish31595/llm-spendcap
.. image:: https://img.shields.io/badge/license-MIT-green
   :target: LICENSE

**Hard budgets and cost control for your LLM API keys.** A drop-in proxy for OpenAI,
Anthropic and OpenRouter that gives every app, teammate or student their
own key with a daily or monthly dollar limit, and refuses requests once
it's spent. Your real provider keys never leave the server.

::

   $ spendcap status
   KEY               PERIOD       SPENT    BUDGET      LEFT
   frontend          day         4.8121      5.00    0.1879
   research          month      12.0410     50.00   37.9590

Any request past the limit gets a clean, OpenAI-style error that SDKs
already understand:

.. code:: json

   {"error": {"message": "Budget exhausted for 'frontend': $5.0031 of $5.00 this day", "code": "budget_exceeded"}}

Why
---

-  A runaway agent loop or a leaked key can burn hundreds of dollars
   overnight.
-  Provider dashboards set limits per *account*, not per app, teammate
   or hackathon participant.
-  Sharing one real key across a team means nobody knows who spent what.

Features
--------

-  **Per-key budgets**: daily, monthly (UTC) or lifetime, in USD.
-  **OpenAI, Anthropic and OpenRouter** out of the box; any
   OpenAI-compatible API (Groq, Together, DeepSeek, a local vLLM) with
   three lines of config.
-  **Streaming supported**: usage is read from the stream, with no extra
   latency.
-  **Model and provider allow-lists** per key
   (``models = ["gpt-4o-mini", "claude-haiku-*"]``).
-  **Fails safe**: models with no configured price are refused rather
   than tracked as free.
-  **Spend reports** per key and model, stored in SQLite.
-  One small Python package: FastAPI + httpx. Docker image included.

Quick start
-----------

.. code:: sh

   pip install git+https://github.com/Krish31595/llm-spendcap
   curl -O https://raw.githubusercontent.com/Krish31595/llm-spendcap/main/spendcap.example.toml
   mv spendcap.example.toml spendcap.toml        # edit keys, budgets and prices
   export OPENAI_API_KEY=sk-...  ANTHROPIC_API_KEY=sk-ant-...  OPENROUTER_API_KEY=sk-or-...
   spendcap serve

Point your apps at the proxy and give them a spendcap key instead of the
real one:

.. code:: python

   from openai import OpenAI
   client = OpenAI(base_url="http://localhost:8787/openai/v1", api_key="cap-frontend-7f3a9c")

   import anthropic
   client = anthropic.Anthropic(base_url="http://localhost:8787/anthropic", api_key="cap-frontend-7f3a9c")

   # OpenRouter
   client = OpenAI(base_url="http://localhost:8787/openrouter/v1", api_key="cap-research-b81e44")

Routes are ``/<provider>/<upstream path>``, so anything the provider's
API offers works.

Docker
~~~~~~

Multi-arch images (amd64, arm64) are published to GitHub Container Registry:

.. code:: sh

   docker run -d -p 8787:8787 -v $PWD:/config \
     -e OPENAI_API_KEY -e ANTHROPIC_API_KEY -e OPENROUTER_API_KEY \
     ghcr.io/krish31595/llm-spendcap:latest

Put ``spendcap.toml`` in the current directory and set ``db = "/config/spendcap.db"`` so usage survives restarts.

Configuration
-------------

See ```spendcap.example.toml`` <spendcap.example.toml>`__.

.. code:: toml

   [[keys]]
   key = "cap-frontend-7f3a9c"   # what the app sends
   name = "frontend"
   budget_usd = 5.00
   period = "day"                # day | month | total
   models = ["gpt-4o-mini"]      # optional
   providers = ["openai"]        # optional

   [prices]                      # USD per 1M tokens: [input, output], globs allowed
   "gpt-4o-mini" = [0.15, 0.60]

Prices in the example file are examples. **Check your provider's pricing
page and keep ``[prices]`` current**; the caps are only as accurate as
the prices. OpenRouter reports the exact cost of each call, and spendcap
uses that when present.

Commands
--------

============================ ==========================================
Command                      What it does
============================ ==========================================
``spendcap serve``           Run the proxy (default ``127.0.0.1:8787``)
``spendcap status``          Budget spent and left for every key
``spendcap report --days 7`` Calls, tokens and cost per key and model
``GET /spendcap/status``     Same, for the calling key, as JSON
============================ ==========================================

How accurate is it?
-------------------

-  Spend is recorded from the token counts the provider returns,
   multiplied by your prices.
-  The cap is checked **before** each request, so the request that
   crosses the limit is allowed to finish; the next one is refused. With
   many parallel requests, overshoot is at most those in flight.
-  Anthropic prompt-cache tokens are counted at the input price (cache
   reads are actually cheaper, so this errs on the safe side).
-  Requests the provider rejects (4xx/5xx) cost nothing and aren't
   recorded.

FAQ
---

How do I set a spending limit on my OpenAI API key?
   OpenAI's own limits apply to the whole account. Put llm-spendcap in front of the API, give each
   app or person a spendcap key with ``budget_usd`` and ``period = "day"`` or ``"month"``, and
   requests are refused with a clear error once that key's budget is used.

How do I stop a runaway AI agent from burning my API credits?
   Give the agent its own spendcap key with a small daily budget. When an agent loop goes wrong,
   it hits the cap and stops instead of running all night.

Can I share one OpenAI or Anthropic key with my team safely?
   Yes. The real key stays on the server; each teammate gets their own spendcap key with its own
   budget, model allow-list and usage report, and you can see who spent what.

Does it work with the OpenAI Python SDK, LangChain, LlamaIndex and Cursor?
   Anything that lets you set a base URL and API key works: point it at
   ``http://<host>:8787/openai/v1`` (or ``/anthropic``, ``/openrouter/v1``).

Is it an alternative to LiteLLM or an AI gateway?
   It does one job: budgets. It is a single small package with a config file and no database
   server, for when a full AI gateway is more than you need.

Security
--------

-  Bind to ``127.0.0.1`` (the default) unless you need LAN or team
   access; put it behind HTTPS (Caddy, nginx) if it's reachable from
   other machines.
-  Treat spendcap keys like passwords. Anyone holding one can spend up
   to its budget.

Development
-----------

.. code:: sh

   pip install -e ".[test]"
   pytest -q

License
-------

MIT © Krish31595
