"""Small quote-only HTTP MCP transport; secrets stay in caller-owned memory."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..core.contracts import ContractError
from .futu_quotes import SYMBOL, NoRedirect

MCP_URL = "https://mcp.futunn.com/mcp"
ALLOWED = frozenset({"quote_history_kline", "quote_trading_days"})


class FutuMCPTransport:
    def __init__(self, token=None, *, opener=None, sleep=time.sleep, interval=7):
        token = token or os.environ.get("FUTU_MCP_ACCESS_TOKEN")
        if not token:
            raise ContractError("Futu MCP quote authorization is missing")
        self._headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._open = opener or urllib.request.build_opener(NoRedirect()).open
        self._sleep, self._interval, self._id = sleep, interval, 0
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "us-research-quotes", "version": "1"},
            },
        )
        self._rpc("notifications/initialized", notification=True)

    def _rpc(self, method, params=None, notification=False):
        self._id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self._id
        if params is not None:
            body["params"] = params
        request = urllib.request.Request(
            MCP_URL, data=json.dumps(body).encode(), headers=self._headers, method="POST"
        )
        try:
            with self._open(request, timeout=30) as response:
                if response.headers.get("Mcp-Session-Id"):
                    self._headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("oversize")
            if notification and not raw:
                return {}
            decoded = raw.decode()
            if decoded.startswith(("event:", "data:")):
                messages = [
                    json.loads(line[5:].strip())
                    for line in decoded.splitlines()
                    if line.startswith("data:")
                ]
                result = next(m for m in messages if m.get("id") == self._id)
            else:
                result = json.loads(decoded)
            if "error" in result or result.get("id") != self._id:
                raise ValueError("RPC failed")
            return result["result"]
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ContractError("Futu MCP authorization expired or permission denied") from None
            raise ContractError("Futu MCP HTTP request failed") from None
        except (OSError, ValueError, KeyError, StopIteration, TypeError):
            raise ContractError("Futu MCP response invalid or connection failed") from None

    def __call__(self, name, arguments):
        if name not in ALLOWED:
            raise ContractError("Only quote history and US calendar MCP tools are allowed")
        if name == "quote_trading_days" and arguments.get("market") != "US":
            raise ContractError("Only US calendar allowed")
        if name == "quote_history_kline" and not SYMBOL.fullmatch(arguments.get("symbol", "")):
            raise ContractError("Only explicit US symbols allowed")
        for attempt in range(3):
            self._sleep(self._interval if attempt == 0 else 30 * attempt)
            result = self._rpc("tools/call", {"name": name, "arguments": arguments})
            # Retry only the observed vendor rate-limit code, never arbitrary failures.
            try:
                texts = [b["text"] for b in result.get("content", []) if b.get("type") == "text"]
                limited = len(texts) == 1 and json.loads(texts[0]).get("ret_code") == -11
            except (ValueError, KeyError, TypeError, AttributeError):
                limited = False
            if not limited:
                return result
        raise ContractError("Futu MCP rate limit persists; collection incomplete")
