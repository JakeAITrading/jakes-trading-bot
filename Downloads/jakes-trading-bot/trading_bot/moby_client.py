"""Minimal client for Moby's MCP server (Streamable HTTP transport).

The API key is read from the MOBY_API_KEY environment variable and is never
written to disk or logged.

CLI (handy for discovering what Moby exposes):
    python -m trading_bot.moby_client tools
    python -m trading_bot.moby_client call <tool_name> '{"arg": "value"}'
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from typing import Any

import requests

DEFAULT_URL = "https://mcp.moby.win/mcp"
PROTOCOL_VERSION = "2025-06-18"


class MobyError(RuntimeError):
    pass


def _parse_sse(text: str) -> list[dict]:
    """Extract JSON-RPC messages from a text/event-stream body."""
    messages, data_lines = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            try:
                messages.append(json.loads("\n".join(data_lines)))
            except json.JSONDecodeError:
                pass
            data_lines = []
    return messages


def decode_tool_result(result: dict) -> Any:
    """Turn an MCP tools/call result into plain Python data."""
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        raise MobyError("Moby tool error: " + " ".join(texts).strip())
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return joined


class MobyClient:
    def __init__(self, api_key: str | None = None, url: str | None = None, timeout: float = 30.0,
                 session: requests.Session | None = None):
        self.api_key = api_key or os.environ.get("MOBY_API_KEY", "")
        if not self.api_key:
            raise MobyError("MOBY_API_KEY is not set")
        self.url = url or os.environ.get("MOBY_MCP_URL", DEFAULT_URL)
        self.timeout = timeout
        self.http = session or requests.Session()
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._initialized = False

    # -- transport -------------------------------------------------------
    def _headers(self) -> dict:
        key = self.api_key
        h = {
            "Authorization": key if key.lower().startswith("bearer ") else f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload: dict) -> requests.Response:
        resp = self.http.post(self.url, json=payload, headers=self._headers(), timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise MobyError(f"Moby rejected the API key (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise MobyError(f"Moby MCP HTTP {resp.status_code}: {resp.text[:300]}")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        return resp

    def _request(self, method: str, params: dict | None = None) -> dict:
        req_id = next(self._ids)
        resp = self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        ctype = resp.headers.get("Content-Type", "")
        messages = _parse_sse(resp.text) if "text/event-stream" in ctype else [resp.json()]
        for msg in messages:
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise MobyError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})
        raise MobyError(f"No response to {method} from Moby")

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "jakes-trading-bot", "version": "1.0"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    # -- public API ------------------------------------------------------
    def list_tools(self) -> list[dict]:
        self._ensure_initialized()
        tools, cursor = [], None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        self._ensure_initialized()
        return decode_tool_result(self._request("tools/call", {"name": name, "arguments": arguments or {}}))


def _main(argv: list[str]) -> int:
    client = MobyClient()
    if len(argv) >= 1 and argv[0] == "tools":
        for t in client.list_tools():
            print(f"- {t['name']}: {t.get('description', '').strip()}")
            print("  input: " + json.dumps(t.get("inputSchema", {}).get("properties", {})))
        return 0
    if len(argv) >= 2 and argv[0] == "call":
        args = json.loads(argv[2]) if len(argv) > 2 else {}
        print(json.dumps(client.call_tool(argv[1], args), indent=2, default=str))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
