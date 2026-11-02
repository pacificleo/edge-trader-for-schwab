"""The poller's +1R watcher for Auto B/E tickets: fires once, only on a fresh quote after the fill."""
from datetime import date, datetime
import time

from companion import poller as poller_mod
from companion import trading
from companion.backends import (
    AccountBalances,
    AccountInfo,
    BracketOrder,
    BrokerBackend,
    BrokerCapabilities,
    Fill,
    MarketHours,
    PendingOrder,
    Position,
    Quote,
)
from companion.poller import Poller
from companion.store import Store


class MockBackend(BrokerBackend):
    def __init__(self, rate_limit: int = 70):
        self.capabilities = BrokerCapabilities(name="mock", rate_limit_per_min=rate_limit)
        self.calls: list[tuple] = []
        self._connected = False

    def connect(self) -> None:
        self.calls.append(("connect",))
        self._connected = True

    def auth_status(self) -> str:
        self.calls.append(("auth_status",))
        return "live"

    def auth_hours_left(self) -> float | None:
        self.calls.append(("auth_hours_left",))
        return 24.0

    def login_cli(self, reauth: bool = False) -> None:
        pass

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.calls.append(("get_quotes", symbols))
        return {
            s: Quote(symbol=s, bid=100.0, ask=100.1, last=100.05, mark=100.05, volume=1000.0)
            for s in symbols
        }

    def get_market_hours(self, d: date) -> MarketHours:
        self.calls.append(("get_market_hours", d))
        from datetime import timezone
        return MarketHours(date=d, is_open=True, open_time=datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc),
                           close_time=datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc))

    def get_accounts(self) -> list[AccountInfo]:
        self.calls.append(("get_accounts",))
        return [AccountInfo(account_id="12345", hash_value="hash_12345", display_name="Test Account")]

    def get_positions(self, account_id: str) -> list[Position]:
        self.calls.append(("get_positions", account_id))
        return [
            Position(
                symbol="CRWV",
                asset_type="EQUITY",
                underlying="CRWV",
                side="SHORT",
                qty=675.0,
                avg_price=58.39,
                market_value=-39413.25,
                open_pnl=0.0,
                day_pnl=0.0,
                held="INTRADAY",
            )
        ]

    def get_balances(self, account_id: str) -> AccountBalances:
        self.calls.append(("get_balances", account_id))
        return AccountBalances(liquidation_value=100_000.0, equity=100_000.0, buying_power=200_000.0)

    def get_orders(
        self, account_id: str, start: datetime, end: datetime, status: str | None = None
    ) -> list[PendingOrder]:
        self.calls.append(("get_orders", account_id, start, end, status))
        return []

    def get_order_details(self, account_id: str, order_id: int) -> PendingOrder | None:
        self.calls.append(("get_order_details", account_id, order_id))
        return None

    def get_fills(self, account_id: str, start: datetime, end: datetime) -> list[Fill]:
        self.calls.append(("get_fills", account_id, start, end))
        return []

    def preview_bracket(self, account_id: str, bracket: BracketOrder) -> dict:
        return {}

    def place_bracket(self, account_id: str, bracket: BracketOrder) -> int:
        return 1

    def cancel_order(self, account_id: str, order_id: int) -> bool:
        return True

    def replace_stop(self, account_id: str, order_id: int, new_stop: float) -> int:
        return 1

    def replace_with_trail(self, account_id: str, order_id: int, trail_spec: dict) -> int:
        return 1

    def replace_target(self, account_id: str, order_id: int, new_target: float) -> int:
        return 1

    def close_market(self, account_id: str, symbol: str, side: str, qty: float) -> int:
        return 1


class _Trader:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.expected_hash = None

    def auto_breakeven(self, *args):
        self.calls.append(args)


class _NowThread:
    def __init__(self, target, args, **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _setup(tmp_path, monkeypatch, backend=None):
    store = Store(tmp_path / "p.db")
    backend = backend or MockBackend()
    p = Poller(backend=backend, store=store)
    p.trader = _Trader(store)
    monkeypatch.setattr(p, "_fresh", lambda name: True)
    monkeypatch.setattr(p, "_orders_fresh", lambda: True)
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(poller_mod.threading, "Thread", _NowThread)
    store.add_auto_be("t1", "CRWV")
    return p


def _state(mark, quote_ms=None):
    quote_ms = quote_ms if quote_ms is not None else time.time() * 1000
    # Short 675 CRWV filled 58.39, stop 59.13: +1R = 57.65.
    pos = {"symbol": "CRWV", "side": "SHORT", "qty": 675,
           "r": {"ticket_id": "t1", "side": "SHORT", "entry": 58.39, "stop": 59.13, "rps": 0.74,
                 "first_fill": "2026-10-27T15:33:19+0000"}}
    return {"positions": [pos]}, {"CRWV": {"quote": {"mark": mark, "quoteTime": quote_ms}}}


def test_fires_once_when_the_mark_touches_1r(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch)
    st, p.quotes = _state(57.70)
    p._auto_be(st)
    assert p.trader.calls == [] and st["positions"][0]["auto_be"]["state"] == "PENDING"
    assert st["positions"][0]["auto_be"]["trigger"] == 57.65
    st, p.quotes = _state(57.65)
    p._auto_be(st)
    assert len(p.trader.calls) == 1 and p.trader.calls[0][:4] == ("t1", "CRWV", "SHORT", 58.39)
    st, p.quotes = _state(57.50)
    p._auto_be(st)
    assert len(p.trader.calls) == 1                     # claimed: never a second move


def test_ignores_a_quote_from_before_the_fill_and_stale_data(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch)
    st, p.quotes = _state(57.00, quote_ms=1_000)        # quote older than the entry fill
    p._auto_be(st)
    assert p.trader.calls == []
    st, p.quotes = _state(57.00, quote_ms=time.time() * 1000 - 60_000)   # after the fill but a minute old
    p._auto_be(st)
    assert p.trader.calls == []
    st, p.quotes = _state(57.00)
    p.quotes["CRWV"]["realtime"] = False                # delayed quote
    p._auto_be(st)
    assert p.trader.calls == []
    monkeypatch.setattr(p, "_orders_fresh", lambda: False)
    st, p.quotes = _state(57.00)
    p._auto_be(st)
    assert p.trader.calls == [] and p.trader.store.auto_be("t1")["state"] == "PENDING"


def test_poller_initialization_with_backend(tmp_path):
    backend = MockBackend(rate_limit=45)
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    assert p.backend is backend
    assert p.store is store
    assert p.rate_limit_per_min == 45


def test_poller_connect_sets_account_and_trader_hash(tmp_path):
    backend = MockBackend()
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    p.trader = _Trader(store)
    assert p._connect() is True
    assert ("connect",) in backend.calls
    assert p.account_id == "12345"
    assert p.account_hash == "hash_12345"
    assert p.trader.expected_hash == "hash_12345"


def test_poller_poll_methods_call_backend(tmp_path):
    backend = MockBackend()
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    assert p._connect() is True

    # Account poll
    assert p._poll("account") is True
    call_names = [c[0] for c in backend.calls]
    assert "get_positions" in call_names
    assert "get_balances" in call_names

    # Orders today poll
    assert p._poll("orders_today") is True
    assert "get_orders" in [c[0] for c in backend.calls]

    # Orders sweep poll
    assert p._poll("orders_sweep") is True
    sweep_calls = [c for c in backend.calls if c[0] == "get_orders" and c[4] is not None]
    assert len(sweep_calls) == 2  # WORKING and PENDING_ACTIVATION

    # Transactions poll
    assert p._poll("transactions") is True
    assert "get_fills" in [c[0] for c in backend.calls]

    # Market hours poll
    assert p._poll("hours") is True
    assert "get_market_hours" in [c[0] for c in backend.calls]

    # Quotes poll
    p.account = {"securitiesAccount": {"positions": [{"instrument": {"symbol": "AAPL"}}]}}
    assert p._poll("quotes") is True
    assert any(c[0] == "get_quotes" for c in backend.calls)


def test_poller_follow_order_calls_backend_order_details(tmp_path):
    backend = MockBackend()
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    assert p._connect() is True
    terminal_order = PendingOrder(
        order_id=999, leg_id=1, legs=1, group_id=999, group_type="SINGLE",
        parent_id=None, oco_id=None, status="FILLED", symbol="AAPL",
        underlying="AAPL", asset_type="EQUITY", instruction="BUY",
        effect="OPENING", order_type="LIMIT", qty=100.0, filled=100.0,
        price=150.0, stop=None, duration="DAY", entered="2026-11-02T09:30:00Z",
        tag=None, role="ENTRY"
    )
    backend.get_order_details = lambda acct, oid: (backend.calls.append(("get_order_details", acct, oid)) or terminal_order)
    p.orders_followed[999] = {"orderId": 999, "status": "WORKING"}
    p._follow([999])
    details_calls = [c for c in backend.calls if c[0] == "get_order_details"]
    assert len(details_calls) == 1
    assert details_calls[0][2] == 999
    assert 999 in p.terminal
    assert 999 not in p.orders_followed
    saved = store.all_orders()
    assert any(o.get("orderId") == 999 and o.get("status") == "FILLED" for o in saved)


def test_poller_multileg_orders_retain_all_legs_in_today_and_sweep(tmp_path):
    backend = MockBackend()
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    assert p._connect() is True

    leg1 = PendingOrder(
        order_id=501, leg_id=1, legs=2, group_id=501, group_type="SPREAD",
        parent_id=None, oco_id=None, status="WORKING", symbol="SPY_C500",
        underlying="SPY", asset_type="OPTION", instruction="BUY_TO_OPEN",
        effect="OPENING", order_type="NET_DEBIT", qty=10.0, filled=0.0,
        price=2.50, stop=None, duration="DAY", entered="2026-11-02T10:00:00Z",
        tag=None, role="ENTRY"
    )
    leg2 = PendingOrder(
        order_id=501, leg_id=2, legs=2, group_id=501, group_type="SPREAD",
        parent_id=None, oco_id=None, status="WORKING", symbol="SPY_C505",
        underlying="SPY", asset_type="OPTION", instruction="SELL_TO_OPEN",
        effect="OPENING", order_type="NET_DEBIT", qty=10.0, filled=0.0,
        price=2.50, stop=None, duration="DAY", entered="2026-11-02T10:00:00Z",
        tag=None, role="ENTRY"
    )
    backend.get_orders = lambda acct, start, end, status=None: [leg1, leg2]

    # Test orders_today
    assert p._poll("orders_today") is True
    assert 501 in p.orders_today
    legs_today = p.orders_today[501]["orderLegCollection"]
    assert len(legs_today) == 2
    assert legs_today[0]["legId"] == 1 and legs_today[0]["instrument"]["symbol"] == "SPY_C500"
    assert legs_today[1]["legId"] == 2 and legs_today[1]["instrument"]["symbol"] == "SPY_C505"

    # Test orders_sweep
    assert p._poll("orders_sweep") is True
    assert 501 in p.orders_sweep
    legs_sweep = p.orders_sweep[501]["orderLegCollection"]
    assert len(legs_sweep) == 2
    assert legs_sweep[0]["legId"] == 1 and legs_sweep[0]["instrument"]["symbol"] == "SPY_C500"
    assert legs_sweep[1]["legId"] == 2 and legs_sweep[1]["instrument"]["symbol"] == "SPY_C505"


def test_poller_multifill_transactions_do_not_overwrite_in_sqlite(tmp_path):
    from datetime import timezone
    from companion.pnl import fills_from_transactions
    backend = MockBackend()
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    assert p._connect() is True

    now_dt = datetime(2026, 11, 2, 10, 5, tzinfo=timezone.utc)
    f1 = Fill(
        id="777123:0", order_id=501, time=now_dt,
        symbol="SPY_C500", asset_type="OPTION", underlying="SPY",
        qty=10.0, price=5.0, multiplier=100.0, fees=-1.50, effect="OPENING"
    )
    f2 = Fill(
        id="777123:1", order_id=501, time=now_dt,
        symbol="SPY_C505", asset_type="OPTION", underlying="SPY",
        qty=-10.0, price=2.5, multiplier=100.0, fees=0.0, effect="OPENING"
    )
    backend.get_fills = lambda acct, start, end: [f1, f2]

    assert p._poll("transactions") is True

    # Verify in SQLite store
    txns = store.transactions_since("2026-11-02T00:00:00")
    assert len(txns) == 1
    assert txns[0]["activityId"] == 777123
    # Both fill items are present in transferItems
    non_currency_items = [it for it in txns[0]["transferItems"] if (it.get("instrument") or {}).get("assetType") != "CURRENCY"]
    assert len(non_currency_items) == 2

    # Reconstructed fills from transaction must yield both fills
    reconstructed = fills_from_transactions(txns)
    assert len(reconstructed) == 2
    symbols = {f.symbol for f in reconstructed}
    assert symbols == {"SPY_C500", "SPY_C505"}


def test_poller_backfill_calls_get_fills(tmp_path):
    backend = MockBackend()
    store = Store(tmp_path / "p.db")
    p = Poller(backend=backend, store=store)
    assert p._connect() is True
    assert p._backfill() is True
    fills_calls = [c for c in backend.calls if c[0] == "get_fills"]
    assert len(fills_calls) > 0


def test_poller_module_has_no_schwab_imports():
    import inspect
    import companion.poller as pm
    source = inspect.getsource(pm)
    assert "from . import schwab" not in source
    assert "import schwab" not in source
