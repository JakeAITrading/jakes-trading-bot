import json

import pytest

from trading_bot.engine import MONEY_TOOLS, TradingEngine
from trading_bot.moby_client import MobyClient, MobyError, _parse_sse, decode_tool_result
from trading_bot import run, sniper, store
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
    c["risk"].update({"usd_per_trade": 20, "max_open_positions": 3, "max_buys_per_cycle": 1,
                      "max_daily_buy_usd": 40, "min_cash_reserve_usd": 5})
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
    monkeypatch.setattr(run, "load_env", lambda: None)
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
        self.content = self.text.encode()

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


def test_sse_with_unicode_token_names():
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"name": "\u2028 ቬ\x85 coin"}}
    body = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    assert _parse_sse(body) == [payload]


def test_client_handshake_session_and_auth():
    http = FakeHTTP()
    c = MobyClient(api_key="moby_agent_test", url="https://example/mcp", session=http)
    assert [t["name"] for t in c.list_tools()] == ["get_wallet"]
    assert c.call_tool("get_wallet") == {"usd": 10}
    methods = [p["method"] for p, _ in http.sent]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert http.sent[0][1]["Authorization"] == "Bearer moby_agent_test"
    assert http.sent[-1][1]["Mcp-Session-Id"] == "s1"


class SniperMoby:
    def __init__(self, tp_reply=None):
        self.calls = []
        self.tp_reply = tp_reply or {"order_id": "o1"}

    def call_tool(self, name, args=None):
        self.calls.append((name, args))
        return {
            "get_token_price": {"price_usd": 0.001},
            "quick_buy": {"quote_id": "q1", "fill_pending": False, "filled_output_amount_ui": 4000.0,
                          "filled_input_amount_ui": 5.0},
            "get_my_token_balance": {"balance_token_amount": 3990.0},
            "create_order": self.tp_reply,
            "quick_sell": {"status": "ok"},
        }[name]


def whale_row(age_s=3, **kw):
    import datetime as dt
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S")
    row = {"id": "w1", "timestamp": ts, "transaction_type": "buy", "action": "first_buy", "amount_usd": 900,
           "token_market_cap": 300_000, "wallet_list_type": "whale",
           "token": {"token_address": "TOK", "symbol": "TOK", "safety_tier": "grey"}}
    row.update(kw)
    return row


@pytest.fixture
def sniper_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DIR", str(tmp_path))
    return tmp_path


def test_sniper_filters(sniper_dir):
    import time
    s = sniper.Sniper(SniperMoby(), 5, 25, 20, dry_run=False)
    now = time.time()
    assert s.qualifies(whale_row(), now) is None
    assert s.qualifies(whale_row(age_s=60), now).startswith("too old")
    assert s.qualifies(whale_row(action="add"), now) == "not an opening buy"
    assert s.qualifies(whale_row(token_market_cap=5e6), now) == "market cap out of range"
    assert s.qualifies(whale_row(amount_usd=50), now) == "whale buy too small"
    red = whale_row(); red["token"] = {**red["token"], "safety_tier": "red"}
    assert s.qualifies(red, now) == "red safety"


def test_sniper_buys_5_and_sets_take_profit_25(sniper_dir):
    import time
    moby = SniperMoby()
    rec = sniper.Sniper(moby, 5, 25, 20, dry_run=False).trade(whale_row(), time.time())
    assert ("quick_buy", {"output_token": "TOK", "amount_usd": 5,
                          "rationale": moby.calls[1][1]["rationale"]}) in moby.calls
    order = [a for n, a in moby.calls if n == "create_order"][0]
    assert order["order_type"] == "tp_only" and order["amount"] == 3990.0
    assert order["take_profit_price"] == pytest.approx(5 / 4000 * 1.25)
    assert not any(n == "quick_sell" for n, _ in moby.calls)  # no stop-loss, no early sell
    assert rec["status"] == "holding, take-profit set"


def test_sniper_sells_now_if_already_past_target(sniper_dir):
    import time
    moby = SniperMoby(tp_reply={"error": "tp_below_market"})
    rec = sniper.Sniper(moby, 5, 25, 20, dry_run=False).trade(whale_row(), time.time())
    assert rec["status"].startswith("sold immediately")


def test_main_bot_ignores_sniper_coins(sniper_dir):
    sniper.save({"trades": [{"token_address": "X"}]})
    moby = FakeMoby(rows=[], tokens=[{"token_address": "X", "symbol": "X", "quantity": 50}])
    report = TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls() == [] and report["positions"] == []
