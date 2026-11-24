"""Config, pricing and the usage ledger."""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    auth: str = "bearer"  # "bearer" or "x-api-key"

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class Key:
    key: str
    name: str
    budget_usd: float
    period: str = "month"  # day | month | total
    models: List[str] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)

    def allows_model(self, model: str) -> bool:
        return not self.models or any(fnmatch.fnmatch(model, m) for m in self.models)

    def allows_provider(self, provider: str) -> bool:
        return not self.providers or provider in self.providers

    def period_start(self, now: Optional[float] = None) -> float:
        dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
        if self.period == "day":
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        elif self.period == "month":
            dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            return 0.0
        return dt.timestamp()


@dataclass
class Config:
    providers: Dict[str, Provider]
    keys: Dict[str, Key]
    prices: Dict[str, Tuple[float, float]]
    db: str = "spendcap.db"
    host: str = "127.0.0.1"
    port: int = 8787
    block_unpriced: bool = True

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        server = raw.get("server", {})
        providers = {n: Provider(n, p["base_url"].rstrip("/"), p.get("api_key_env", ""), p.get("auth", "bearer"))
                     for n, p in raw.get("providers", {}).items()}
        keys = {}
        for k in raw.get("keys", []):
            if k.get("period", "month") not in ("day", "month", "total"):
                raise ValueError(f"key {k.get('name')}: period must be day, month or total")
            keys[k["key"]] = Key(k["key"], k.get("name", k["key"][:8]), float(k["budget_usd"]),
                                 k.get("period", "month"), k.get("models", []), k.get("providers", []))
        prices = {m: (float(v[0]), float(v[1])) for m, v in raw.get("prices", {}).items()}
        return cls(providers, keys, prices, server.get("db", "spendcap.db"),
                   server.get("host", "127.0.0.1"), int(server.get("port", 8787)),
                   bool(server.get("block_unpriced", True)))

    def price_for(self, model: str) -> Optional[Tuple[float, float]]:
        if model in self.prices:
            return self.prices[model]
        # longest matching glob wins, so "anthropic/claude-*" beats "*"
        for pattern in sorted(self.prices, key=len, reverse=True):
            if fnmatch.fnmatch(model, pattern):
                return self.prices[pattern]
        return None

    def cost(self, model: str, tokens_in: int, tokens_out: int) -> float:
        p = self.price_for(model)
        if not p:
            return 0.0
        return (tokens_in * p[0] + tokens_out * p[1]) / 1_000_000


class Ledger:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS usage (
            ts REAL, key_name TEXT, provider TEXT, model TEXT,
            tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS usage_key_ts ON usage(key_name, ts)")
        self.db.commit()

    def record(self, key_name: str, provider: str, model: str, tin: int, tout: int, cost: float):
        with self._lock:
            self.db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?)",
                            (time.time(), key_name, provider, model, tin, tout, cost))
            self.db.commit()

    def spent(self, key_name: str, since: float = 0.0) -> float:
        with self._lock:
            row = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE key_name=? AND ts>=?",
                                  (key_name, since)).fetchone()
        return float(row[0])

    def report(self, since: float = 0.0):
        with self._lock:
            return self.db.execute(
                """SELECT key_name, model, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost_usd)
                   FROM usage WHERE ts>=? GROUP BY key_name, model ORDER BY SUM(cost_usd) DESC""",
                (since,)).fetchall()


class UsageTracker:
    """Pulls token counts out of OpenAI, Anthropic and OpenRouter responses (JSON or streamed)."""

    def __init__(self):
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost: Optional[float] = None  # provider-reported cost (OpenRouter)
        self.model: Optional[str] = None

    def observe(self, doc) -> None:
        if isinstance(doc, dict):
            if isinstance(doc.get("model"), str) and not self.model:
                self.model = doc["model"]
            u = doc.get("usage")
            if isinstance(u, dict):
                tin = u.get("prompt_tokens", u.get("input_tokens"))
                tout = u.get("completion_tokens", u.get("output_tokens"))
                # Anthropic counts cache reads/writes separately from input_tokens.
                for extra in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                    if isinstance(u.get(extra), int) and isinstance(tin, int):
                        tin += u[extra]
                if isinstance(tin, int):
                    self.tokens_in = max(self.tokens_in, tin)
                if isinstance(tout, int):
                    self.tokens_out = max(self.tokens_out, tout)
                if isinstance(u.get("cost"), (int, float)):
                    self.cost = float(u["cost"])
            for v in doc.values():
                if isinstance(v, (dict, list)):
                    self.observe(v)
        elif isinstance(doc, list):
            for v in doc:
                self.observe(v)
