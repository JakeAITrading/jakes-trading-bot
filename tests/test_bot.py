import json

import pytest

from trading_bot.engine import MONEY_TOOLS, TradingEngine
from trading_bot.moby_client import MobyClient, MobyError, _parse_sse, decode_tool_result
from trading_bot import run, store
from trading_bot.settings import load_config


GOOD_ROW = {
    "token_address": "GOOD", "symbol": "GOOD", "safety_tier": "green",
    "market_cap_usd": 5e6, "liquidity_usd": 5e5,
    "price_change_pct_h1": 5, "price_change_pct_h24": 20,
    "whale": {"h1": {"whale_net_flow_usd": 5000}, "h4": {"whale_net_flow_usd": 20000, "whale_buyers_count": 5}},
}
GOOD_BRIEF = {
    "safety": {"safety_tier": "green"},
    "smart_money_read": {"tier": "green", "label": "Buying"},
    "smart_money_flow": {"flow": {"h1": {"net_flow_usd": 100}, "h4": {"net_flow_usd": 100}}},
    "risk_signals": {"mint_authority_renounced": True, "freeze_authority_renounced": True},
}


class FakeMoby:
    def __init__(self, rows=None, tokens=None, brief=None, guard=None, cash=72.0, position=None):
        self.rows = rows if rows is not None else [GOOD_ROW]
        self.tokens = tokens or []
        self.brief = brief or GOOD_BRIEF
        self.guard = guard or {"paused": False, "maxTradeUsd": None}
        self.cash = cash
        self.position = position or {"stop_loss_set": False, "cost_basis_usd": 10, "basis_known_token_amount": 100}
        self.calls = []

    def call_tool(self, name, args=None):
        self.calls.append((name, args))
        return {
            "get_my_guardrails": self.guard,
            "get_my_balances": {"usd_token": {"quantity": self.cash}, "tokens": self.tokens},
            "get_screener_leaderboard": {"entries": self.rows},
            "get_token_trade_brief": self.brief,
            "get_my_position": self.position,
            "get_token_price": {"price_usd": 0.12},
            "quick_buy": {"filled_output_amount_ui": 80.0},
            "quick_sell": {"status": "ok"},
            "create_order": {"ok": True},
        }[name]

    def money_calls(self):
        return [(n, a) for n, a in self.calls if n in MONEY_TOOLS]


def cfg(dry_run=False, **risk):
    c = load_config()
    c["trading"]["dry_run"] = dry_run
    c["risk"].update(risk)
    return c


def test_dry_run_sends_no_money_calls():
    moby = FakeMoby()
    report = TradingEngine(moby, cfg(dry_run=True)).run_cycle()
    assert [a["tool"] for a in report["actions"]] == ["quick_buy"]
    assert report["actions"][0]["status"] == "simulated"
    assert moby.money_calls() == []


def test_live_buy_then_bracket_exit():
    moby = FakeMoby()
    TradingEngine(moby, cfg()).run_cycle()
    money = moby.money_calls()
    assert money[0][0] == "quick_buy" and money[0][1]["amount_usd"] == 20
    order = money[1][1]
    assert money[1][0] == "create_order" and order["order_type"] == "sl_and_tp" and order["amount"] == 80.0
    # entry = 10/100 = 0.10, price 0.12 -> SL below the lower, TP above the higher
    assert order["stop_loss_price"] == pytest.approx(0.075)
    assert order["take_profit_price"] == pytest.approx(0.18)


def test_moby_max_trade_guardrail_caps_size():
    moby = FakeMoby(guard={"paused": False, "maxTradeUsd": 10})
    TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls()[0][1]["amount_usd"] == 10


def test_paused_in_moby_stops_everything():
    moby = FakeMoby(guard={"paused": True})
    assert "paused" in TradingEngine(moby, cfg()).run_cycle()["notes"][0]
    assert moby.money_calls() == []


@pytest.mark.parametrize("change,reason", [
    ({"safety_tier": "red"}, "safety"),
    ({"market_cap_usd": 5e9}, "market cap"),
    ({"liquidity_usd": 1000}, "liquidity"),
    ({"price_change_pct_h1": -2}, "trending"),
    ({"price_change_pct_h24": 400}, "ran too far"),
    ({"whale": {"h1": {"whale_net_flow_usd": -1}, "h4": {"whale_net_flow_usd": 9e4, "whale_buyers_count": 9}}}, "1h"),
])
def test_screen_rejects(change, reason):
    moby = FakeMoby(rows=[{**GOOD_ROW, **change}])
    report = TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls() == []
    assert reason in report["screened_out"][0]


def test_brief_rejects_smart_money_not_buying():
    moby = FakeMoby(brief={**GOOD_BRIEF, "smart_money_read": {"tier": "red"}})
    TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls() == []


def test_limits_cash_slots_and_cooldown():
    moby = FakeMoby(cash=20)
    assert "not enough cash" in TradingEngine(moby, cfg()).run_cycle()["notes"][0]
    held = [{"token_address": t, "symbol": t, "quantity": 1} for t in ("A", "B", "C")]
    moby = FakeMoby(tokens=held, position={"stop_loss_set": True})
    assert "max open" in TradingEngine(moby, cfg()).run_cycle()["notes"][-1]
    moby = FakeMoby()
    eng = TradingEngine(moby, cfg())
    eng.run_cycle(); eng.run_cycle()
    assert [n for n, _ in moby.money_calls()].count("quick_buy") == 1


def test_sells_when_smart_money_flips():
    selling = {**GOOD_BRIEF, "smart_money_flow": {"flow": {"h1": {"net_flow_usd": -5}, "h4": {"net_flow_usd": -9}}}}
    moby = FakeMoby(rows=[], tokens=[{"token_address": "X", "symbol": "X", "quantity": 50}], brief=selling)
    TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls() == [("quick_sell", {"token_address": "X", "pct": 100})]


def test_existing_position_without_stop_gets_one():
    moby = FakeMoby(rows=[], tokens=[{"token_address": "X", "symbol": "X", "quantity": 50}])
    TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls()[0][0] == "create_order"


def test_paper_position_opens_and_closes_on_take_profit():
    moby = FakeMoby(rows=[{**GOOD_ROW, "price_usd": 0.10}])
    eng = TradingEngine(moby, cfg(dry_run=True))
    eng.run_cycle()
    assert eng.state["paper"]["GOOD"]["entry_price"] == 0.10
    moby.rows = []
    moby.call_tool = (lambda orig: lambda n, a=None: {"price_usd": 0.16} if n == "get_token_price" else orig(n, a))(moby.call_tool)
    report = eng.run_cycle()
    closed = eng.state["paper_closed"][0]
    assert closed["exit_reason"] == "take-profit" and closed["pnl_usd"] == pytest.approx(12.0)
    assert "paper exit" in report["actions"][0]["why"]
    assert moby.money_calls() == []


def test_paper_positions_use_up_slots_and_cash():
    held = {t: {"symbol": t, "usd": 20, "entry_price": 1, "stop_loss": 0.5, "take_profit": 9} for t in "ABC"}
    moby = FakeMoby()
    eng = TradingEngine(moby, cfg(dry_run=True), {"paper": held})
    moby.call_tool = (lambda orig: lambda n, a=None: {"price_usd": 1} if n == "get_token_price" else orig(n, a))(moby.call_tool)
    assert "max open" in eng.run_cycle()["notes"][-1]


def test_state_survives_restart_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DIR", str(tmp_path))
    moby = FakeMoby()
    eng = TradingEngine(moby, cfg())
    eng.run_cycle()
    store.save_state(eng.state)
    TradingEngine(moby, cfg(), store.load_state()).run_cycle()
    assert [n for n, _ in moby.money_calls()].count("quick_buy") == 1


def test_run_respects_stop_files(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    (tmp_path / "REMOTE_STOP").write_text("")
    monkeypatch.setattr(run, "MobyClient", lambda **k: (_ for _ in ()).throw(AssertionError("must not connect")))
    assert run.main() == 0
    assert "REMOTE_STOP" in (tmp_path / "last_run.txt").read_text()


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
