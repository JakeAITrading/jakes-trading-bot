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
        self.orders = []
        self.calls = []

    def call_tool(self, name, args=None):
        result = self._call(name, args)
        if name == "create_order" and args.get("order_type") == "sl_and_tp":
            # Like Moby: a bracket shows up as live orders; the position flag does NOT change.
            self.orders += [{"token_address": args["token_address"], "order_type": "sl_only"},
                            {"token_address": args["token_address"], "order_type": "tp_only"}]
        return result

    def _call(self, name, args=None):
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
            "get_my_orders": {"orders": self.orders},
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


def test_exits_placed_once_not_every_cycle():
    """Regression: the position flag stays false on Moby, so the bot must check the open orders."""
    moby = FakeMoby(rows=[], tokens=[{"token_address": "X", "symbol": "X", "quantity": 50}])
    eng = TradingEngine(moby, cfg())
    for _ in range(4):
        eng.run_cycle()
    assert [n for n, _ in moby.money_calls()].count("create_order") == 1


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
            "get_my_balances": {"usd_token": {"quantity": 50.0}},
            "get_token_ohlcv": {"o": [1.0, 1.0], "c": [1.05, 1.0], "l": [1.0, 0.95], "h": [1.06, 1.0]},
            "get_token_overview": {"overview": {"view_count": 275}},
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
    s = sniper.Sniper(moby, 5, 25, 20, dry_run=False)
    s.limit_cushion = None   # market-buy mode
    rec = s.trade(whale_row(), time.time())
    buy = [a for n, a in moby.calls if n == "quick_buy"]
    assert len(buy) == 1 and buy[0]["output_token"] == "TOK" and buy[0]["amount_usd"] == 5
    order = [a for n, a in moby.calls if n == "create_order"][0]
    assert order["order_type"] == "tp_only" and order["amount"] == 3990.0
    assert order["take_profit_price"] == pytest.approx(5 / 4000 * 1.25)
    assert not any(n == "quick_sell" for n, _ in moby.calls)  # no stop-loss, no early sell
    assert rec["status"] == "holding, take-profit set"


def test_sniper_sells_now_if_already_past_target(sniper_dir):
    import time
    moby = SniperMoby(tp_reply={"error": "tp_below_market"})
    s = sniper.Sniper(moby, 5, 25, 20, dry_run=False)
    s.limit_cushion = None
    rec = s.trade(whale_row(), time.time())
    assert rec["status"].startswith("sold immediately")


def test_main_bot_ignores_sniper_coins(sniper_dir):
    sniper.save({"trades": [{"token_address": "X"}]})
    moby = FakeMoby(rows=[], tokens=[{"token_address": "X", "symbol": "X", "quantity": 50}])
    report = TradingEngine(moby, cfg()).run_cycle()
    assert moby.money_calls() == [] and report["positions"] == []


def test_sniper_retarget_moves_take_profit_or_sells(sniper_dir):
    sniper.save({"trades": [
        {"mode": "LIVE", "token_address": "A", "symbol": "A", "entry_price": 1.0, "tp_order": {"order_id": "oA"}},
        {"mode": "LIVE", "token_address": "B", "symbol": "B", "entry_price": 0.0005, "tp_order": {"order_id": "oB"}},
    ]})

    class M(SniperMoby):
        def call_tool(self, name, args=None):
            if name in ("get_my_orders", "cancel_order", "get_token_price"):
                self.calls.append((name, args))
            if name == "get_my_orders":
                return {"orders": [{"id": "oA"}, {"id": "oB"}]}
            if name == "cancel_order":
                return {"status": "cancelled"}
            return super().call_tool(name, args) if name != "get_token_price" else {"price_usd": 0.001}
    moby = M()
    s = sniper.Sniper(moby, 5, 20, 20, dry_run=False)
    s.retarget()
    names = [n for n, _ in moby.calls]
    assert names.count("cancel_order") == 2
    new_tp = [a for n, a in moby.calls if n == "create_order"]
    assert len(new_tp) == 1 and new_tp[0]["take_profit_price"] == pytest.approx(1.2)   # A: price 0.001 < 1.2
    assert ("quick_sell", {"token_address": "B", "pct": 100}) in moby.calls              # B: 0.001 >= 0.0006


def test_sniper_rebuys_after_take_profit_if_still_rising(sniper_dir):
    sniper.save({"trades": [{"mode": "LIVE", "token_address": "T", "symbol": "T", "entry_price": 0.001,
                             "spent_usd": 5.0, "take_profit_price": 0.0012, "bought_at": "x",
                             "tp_order": {"order_id": "tp1"}}]})

    class M(SniperMoby):
        def __init__(self, green):
            super().__init__(tp_reply={"order_id": "tp2"})
            self.green = green

        def call_tool(self, name, args=None):
            if name == "get_my_orders":
                self.calls.append((name, args))
                if (args or {}).get("status") == "terminal":
                    return {"orders": [{"id": "tp1", "status": "filled_tp", "filled_price": 0.0012,
                                        "filled_output_amount": 6.0}]}
                return {"orders": []}
            if name == "get_token_price":
                return {"price_usd": 0.0013}
            if name == "get_token_ohlcv":
                return {"o": [1.0, 1.0], "c": [1.1 if self.green else 0.9, 1.0]}
            return super().call_tool(name, args)

    moby = M(green=True)
    s = sniper.Sniper(moby, 5, 20, 20, dry_run=False)
    s.limit_cushion = None   # market re-buy mode for this test
    s.check_rebuys()
    t = s.state["trades"]
    assert t[0]["profit_usd"] == 1.0 and len(t) == 2 and t[1]["rebuys"] == 1
    assert [n for n, _ in moby.calls].count("quick_buy") == 1

    sniper.save({"trades": [{**t[0], "closed": None, "rebuy_until": None, "profit_usd": None}]})
    moby = M(green=False)
    s = sniper.Sniper(moby, 5, 20, 20, dry_run=False)
    s.limit_cushion = None
    s.rebuy_always = False
    s.check_rebuys()
    assert not any(n == "quick_buy" for n, _ in moby.calls)   # if-rising mode, falling: no re-buy

    sniper.save({"trades": [{**t[0], "closed": None, "rebuy_until": None, "profit_usd": None}]})
    moby = M(green=False)
    s = sniper.Sniper(moby, 5, 20, 20, dry_run=False)          # default: always re-buy after a sale
    s.limit_cushion = None
    s.check_rebuys()
    assert [n for n, _ in moby.calls].count("quick_buy") == 1


def test_sniper_own_limits(sniper_dir):
    class M(SniperMoby):
        def __init__(self, cash):
            super().__init__()
            self.cash = cash

        def call_tool(self, name, args=None):
            if name == "get_my_balances":
                return {"usd_token": {"quantity": self.cash}}
            return super().call_tool(name, args)

    open_recs = [{"mode": "LIVE", "entry_price": 1, "token_address": str(i)} for i in range(5)]
    sniper.save({"trades": open_recs})
    assert "already held" in sniper.Sniper(M(100), 5, 20, 20, dry_run=False).can_buy()
    sniper.save({"trades": open_recs[:2]})
    assert "floor" in sniper.Sniper(M(14), 5, 20, 20, dry_run=False).can_buy()
    assert sniper.Sniper(M(15), 5, 20, 20, dry_run=False).can_buy() is None


def test_sniper_rebuy_has_no_cap_by_default(sniper_dir):
    s = sniper.Sniper(SniperMoby(), 5, 20, 20, dry_run=False)
    assert s.max_rebuys == 0


def test_sniper_glance_skips_pumped_and_spiking(sniper_dir):
    class M(SniperMoby):
        def __init__(self, candles):
            super().__init__()
            self.candles = candles

        def call_tool(self, name, args=None):
            if name == "get_token_ohlcv":
                return self.candles
            return super().call_tool(name, args)
    calm = {"o": [1.0, 0.98], "c": [1.05, 1.0], "l": [0.97, 0.95]}
    pumped = {"o": [2.4, 2.0], "c": [2.5, 2.4], "l": [2.3, 1.0]}     # +150% above the 5-min low
    spiking = {"o": [1.0, 0.98], "c": [1.3, 1.0], "l": [0.97, 0.95]}  # +30% this minute
    import time
    assert sniper.Sniper(M(calm), 5, 20, 20, dry_run=False).glance("T")[0] is None
    assert sniper.Sniper(M(pumped), 5, 20, 20, dry_run=False).glance("T")[0].startswith("already pumped")
    assert sniper.Sniper(M(spiking), 5, 20, 20, dry_run=False).glance("T")[0].startswith("mid-spike")
    moby = M(pumped)
    rec = sniper.Sniper(moby, 5, 20, 20, dry_run=False).trade(whale_row(), time.time())
    assert rec["status"].startswith("skipped") and not any(n == "quick_buy" for n, _ in moby.calls)


def test_sniper_blocked_rebuy_retries_within_window(sniper_dir):
    sniper.save({"trades": [{"mode": "LIVE", "token_address": "T", "symbol": "T", "entry_price": 0.001,
                             "spent_usd": 5.0, "take_profit_price": 0.0012, "bought_at": "x",
                             "tp_order": {"order_id": "tp1"}}]})

    class M(SniperMoby):
        blocked = True

        def call_tool(self, name, args=None):
            if name == "get_my_orders":
                if (args or {}).get("status") == "terminal":
                    return {"orders": [{"id": "tp1", "status": "filled_tp", "filled_price": 0.0012,
                                        "filled_output_amount": 6.0}]}
                return {"orders": []}
            if name == "get_token_price":
                return {"price_usd": 0.0013}
            if name == "get_token_ohlcv":
                return {"o": [1.0], "c": [1.1]}
            if name == "quick_buy" and self.blocked:
                self.calls.append((name, args))
                return {"blocked": True, "reason": "daily_volume"}
            return super().call_tool(name, args)

    moby = M()
    s = sniper.Sniper(moby, 5, 20, 20, dry_run=False)
    s.limit_cushion = None
    s.check_rebuys()
    assert len(s.state["trades"]) == 1 and s.state["trades"][0]["rebuy_until"]   # still eligible
    moby.blocked = False
    s.check_rebuys()
    assert len(s.state["trades"]) == 2 and s.state["trades"][1]["rebuys"] == 1   # retried and bought


def test_sniper_views(sniper_dir):
    assert sniper.Sniper(SniperMoby(), 5, 15, 20, dry_run=False).views("T") == 275


class LimitMoby(SniperMoby):
    """Fake Moby with a resting limit buy that is open, then filled or left to expire."""
    def __init__(self):
        super().__init__()
        self.state = "open"

    def call_tool(self, name, args=None):
        if name == "create_order" and args["order_type"] == "limit_buy":
            self.calls.append((name, args))
            return {"order_id": "L1"}
        if name == "get_my_orders":
            terminal = (args or {}).get("status") == "terminal"
            if self.state == "open":
                return {"orders": [] if terminal else [{"id": "L1"}]}
            if self.state == "filled":
                return {"orders": [{"id": "L1", "status": "filled", "filled_price": 0.00104,
                                    "input_amount_usd": 5.0, "filled_at": "t"}] if terminal else []}
        if name == "cancel_order":
            self.calls.append((name, args))
            self.state = "cancelled"
            return {"status": "cancelled"}
        return super().call_tool(name, args)


def test_limit_buy_fills_then_take_profit(sniper_dir):
    import time
    moby = LimitMoby()
    s = sniper.Sniper(moby, 5, 15, 20, dry_run=False)
    rec = s.trade(whale_row(), time.time())
    assert rec["status"] == "limit pending" and rec["limit_price"] == pytest.approx(0.001 * 1.05)
    lim = [a for n, a in moby.calls if n == "create_order"][0]
    assert lim["order_type"] == "limit_buy" and lim["amount"] == 5
    assert not any(n == "quick_buy" for n, _ in moby.calls)
    s.state["trades"].append(rec)
    assert s.open_count() == 1                     # a pending order counts as a held coin
    moby.state = "filled"
    s.check_pending()
    assert rec["entry_price"] == 0.00104 and rec["entry_vs_seen_pct"] == 4.0
    tp = [a for n, a in moby.calls if n == "create_order" and a["order_type"] == "tp_only"][0]
    assert tp["take_profit_price"] == pytest.approx(0.00104 * 1.15)


def test_limit_buy_cancelled_if_not_filled(sniper_dir):
    import time
    moby = LimitMoby()
    s = sniper.Sniper(moby, 5, 15, 20, dry_run=False)
    rec = s.trade(whale_row(), time.time())
    s.state["trades"].append(rec)
    s.check_pending()
    assert rec["status"] == "limit pending"         # still within the 2 minutes
    rec["limit_expires"] = time.time() - 1
    s.check_pending()
    assert ("cancel_order", {"order_id": "L1"}) in moby.calls and rec["closed"]
    assert s.open_count() == 0


def test_limit_refused_because_price_already_lower_buys_at_market(sniper_dir):
    import time

    class M(SniperMoby):
        def call_tool(self, name, args=None):
            if name == "create_order" and args["order_type"] == "limit_buy":
                raise sniper.MobyError("Moby tool error: limit_above_market")
            return super().call_tool(name, args)
    moby = M()
    rec = sniper.Sniper(moby, 5, 15, 20, dry_run=False).trade(whale_row(), time.time())
    assert any(n == "quick_buy" for n, _ in moby.calls) and rec["status"] == "holding, take-profit set"


def test_rebuy_uses_limit_at_sale_price(sniper_dir):
    sniper.save({"trades": [{"mode": "LIVE", "token_address": "T", "symbol": "T", "entry_price": 0.001,
                             "spent_usd": 5.0, "take_profit_price": 0.00115, "bought_at": "x",
                             "tp_order": {"order_id": "tp1"}}]})

    class M(LimitMoby):
        def call_tool(self, name, args=None):
            if name == "get_my_orders" and (args or {}).get("status") == "terminal" and self.state == "open":
                return {"orders": [{"id": "tp1", "status": "filled_tp", "filled_price": 0.00115,
                                    "filled_output_amount": 5.75}]}
            if name == "get_my_orders" and self.state == "open":
                return {"orders": []}
            if name == "get_token_price":
                return {"price_usd": 0.0014}       # spiking after the sale
            return super().call_tool(name, args)
    moby = M()
    s = sniper.Sniper(moby, 5, 15, 20, dry_run=False)
    s.check_rebuys()
    lim = [a for n, a in moby.calls if n == "create_order" and a["order_type"] == "limit_buy"]
    assert len(lim) == 1 and lim[0]["limit_price"] == pytest.approx(0.00115 * 1.05)
    assert not any(n == "quick_buy" for n, _ in moby.calls)
    assert s.state["trades"][1]["status"] == "limit pending" and s.state["trades"][0]["rebuy_until"] is None


def test_review_what_if():
    from trading_bot.review import what_if
    t0 = 1000
    up = [(1000, 1.0, 1.0, 0.98, 1.0), (1060, 1.0, 1.3, 1.0, 1.25)]
    assert what_if(up, t0, 1.0, 0.15)["outcome"] == "hit +15%"
    runaway = [(1000, 1.6, 2.0, 1.5, 1.9), (1060, 1.9, 3.0, 2.0, 2.9)]   # never back to 1.05
    assert what_if(runaway, t0, 1.0, 0.15)["outcome"] == "limit would not fill"
    crash_candle = [(1000, 1.2, 1.2, 0.3, 0.35), (1060, 0.35, 0.5, 0.3, 0.45)]
    r = what_if(crash_candle, t0, 1.0, 0.15)                          # fills at 1.05 on the way down
    assert r["outcome"] != "hit +15%" and r["pnl_pct"] < -50
    dump = [(1000, 1.0, 1.0, 0.98, 1.0), (1060, 1.0, 1.0, 0.4, 0.5)]
    r = what_if(dump, t0, 1.0, 0.15)
    assert r["outcome"] != "hit +15%" and r["pnl_pct"] < -40


def test_no_rebuy_takes_profit_and_stops(sniper_dir):
    sniper.save({"trades": [{"mode": "LIVE", "token_address": "T", "symbol": "T", "entry_price": 0.001,
                             "spent_usd": 5.0, "take_profit_price": 0.00115, "bought_at": "x",
                             "tp_order": {"order_id": "tp1"}}]})

    class M(SniperMoby):
        def call_tool(self, name, args=None):
            if name == "get_my_orders":
                if (args or {}).get("status") == "terminal":
                    return {"orders": [{"id": "tp1", "status": "filled_tp", "filled_price": 0.00115,
                                        "filled_output_amount": 5.75}]}
                return {"orders": []}
            return super().call_tool(name, args)
    moby = M()
    s = sniper.Sniper(moby, 5, 15, 20, dry_run=False)
    s.rebuy_enabled = False
    s.check_rebuys()
    t = s.state["trades"][0]
    assert t["profit_usd"] == 0.75 and t["rebuy_until"] is None and len(s.state["trades"]) == 1
    assert not any(n in ("quick_buy", "create_order") for n, _ in moby.calls)
    assert not s._watching()
