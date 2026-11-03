import json

import pytest

from trading_bot.engine import TradingEngine, extract_signals
from trading_bot.moby_client import MobyClient, MobyError, _parse_sse, decode_tool_result


class FakeClient:
    def __init__(self, signals):
        self.signals = signals
        self.calls = []

    def call_tool(self, name, args=None):
        self.calls.append((name, args))
        return self.signals if name == "get_signals" else {"ok": True}


def cfg(dry_run=True, **risk):
    base = {"amount_usd_per_trade": 25, "max_trades_per_day": 3}
    base.update(risk)
    return {
        "moby": {"tools": {"signals": "get_signals", "trade": "place_trade"},
                 "trade_arguments": {"symbol": "{symbol}", "side": "{side}", "amount_usd": "{amount_usd}",
                                     "note": "bot {side}"}},
        "trading": {"dry_run": dry_run},
        "risk": base,
    }


def test_extract_signals_handles_common_shapes():
    data = {"signals": [{"ticker": "sol", "action": "BUY"}, {"symbol": "ETH", "direction": "short"},
                        {"symbol": "X", "action": "hold"}, "junk"]}
    sigs = extract_signals(data)
    assert [(s.symbol, s.side) for s in sigs] == [("SOL", "buy"), ("ETH", "sell")]


def test_dry_run_never_calls_trade_tool():
    client = FakeClient([{"symbol": "SOL", "side": "buy"}])
    report = TradingEngine(client, cfg(dry_run=True)).run_cycle()
    assert report["actions"][0]["status"] == "simulated"
    assert all(name != "place_trade" for name, _ in client.calls)


def test_live_places_trade_with_typed_args():
    client = FakeClient([{"symbol": "SOL", "side": "buy"}])
    report = TradingEngine(client, cfg(dry_run=False)).run_cycle()
    assert report["actions"][0]["status"] == "placed"
    assert ("place_trade", {"symbol": "SOL", "side": "buy", "amount_usd": 25.0, "note": "bot buy"}) in client.calls


def test_risk_limits():
    sigs = [{"symbol": s, "side": "buy"} for s in ("A", "B", "C", "D")] + [{"symbol": "A", "side": "buy"}]
    client = FakeClient(sigs)
    eng = TradingEngine(client, cfg(dry_run=False, max_trades_per_day=2, blocked_symbols=["b"]))
    report = eng.run_cycle()
    assert [a["symbol"] for a in report["actions"]] == ["A", "C"]
    reasons = {s["symbol"]: s["reason"] for s in report["skipped"]}
    assert reasons["B"] == "in blocked_symbols" and reasons["D"] == "daily trade limit reached"
    # second cycle same day: nothing new
    assert eng.run_cycle()["actions"] == []


def test_missing_signals_tool_is_reported():
    c = cfg()
    c["moby"]["tools"]["signals"] = ""
    assert "error" in TradingEngine(FakeClient([]), c).run_cycle()


def test_sse_and_tool_result_decoding():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'
    assert _parse_sse(body) == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}]
    assert decode_tool_result({"content": [{"type": "text", "text": json.dumps({"a": 1})}]}) == {"a": 1}
    with pytest.raises(MobyError):
        decode_tool_result({"isError": True, "content": [{"type": "text", "text": "nope"}]})


class FakeResp:
    def __init__(self, payload, headers=None, status=200):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append((json, headers))
        m = json.get("method")
        if m == "initialize":
            return FakeResp({"jsonrpc": "2.0", "id": json["id"], "result": {}}, {"Mcp-Session-Id": "s1"})
        if m == "notifications/initialized":
            return FakeResp(None, status=202)
        if m == "tools/list":
            return FakeResp({"jsonrpc": "2.0", "id": json["id"], "result": {"tools": [{"name": "get_wallet"}]}})
        return FakeResp({"jsonrpc": "2.0", "id": json["id"],
                         "result": {"content": [{"type": "text", "text": '{"usd": 10}'}]}})


def test_client_handshake_session_and_auth():
    http = FakeHTTP()
    c = MobyClient(api_key="moby_agent_test", url="https://example/mcp", session=http)
    assert [t["name"] for t in c.list_tools()] == ["get_wallet"]
    assert c.call_tool("get_wallet") == {"usd": 10}
    methods = [p["method"] for p, _ in http.sent]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert http.sent[0][1]["Authorization"] == "Bearer moby_agent_test"
    assert http.sent[-1][1]["Mcp-Session-Id"] == "s1"
