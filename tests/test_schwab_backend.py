"""Tests for SchwabBackend order translation, payload parsing, and broker methods."""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from companion import schwab
from companion.backends import (
    AccountBalances,
    AccountInfo,
    BracketOrder,
    BrokerCapabilities,
    Fill,
    MarketHours,
    PendingOrder,
    Position,
    Quote,
    StopSpec,
    TargetSpec,
    get_backend,
)
from companion.backends.schwab import SchwabBackend
from companion.schwab import _BACKEND_ALLOWED, ReadOnlyClient, make_backend_client


# ---------------------------------------------------------------------------
# Backend Instantiation & Capabilities
# ---------------------------------------------------------------------------


def test_schwab_backend_instantiation_offline():
    backend = SchwabBackend(auto_connect=False)
    assert backend is not None
    assert isinstance(backend.capabilities, BrokerCapabilities)
    assert backend.capabilities.name == "schwab"
    assert backend.capabilities.supports_two_targets is True
    assert backend.capabilities.supports_native_trailing_stop is True
    assert backend.capabilities.rate_limit_per_min == 70
    assert backend.capabilities.has_market_hours_api is True


def test_registry_retrieves_schwab_backend():
    backend = get_backend("schwab", auto_connect=False)
    assert isinstance(backend, SchwabBackend)


# ---------------------------------------------------------------------------
# Order Building (_build_schwab_order)
# ---------------------------------------------------------------------------


def test_build_schwab_order_single_target_limit():
    backend = SchwabBackend(auto_connect=False)
    bracket = BracketOrder(
        symbol="AAPL",
        side="LONG",
        qty=100,
        entry_type="LIMIT",
        limit_price=Decimal("150.25"),
        stop=StopSpec(order_type="STOP", price=Decimal("148.00")),
        target=TargetSpec(price=Decimal("155.00")),
        duration="DAY",
    )
    payload = backend._build_schwab_order(bracket)

    assert payload["orderStrategyType"] == "TRIGGER"
    assert payload["orderType"] == "LIMIT"
    assert payload["price"] == "150.25"
    assert payload["duration"] == "DAY"
    assert payload["orderLegCollection"] == [
        {
            "instruction": "BUY",
            "quantity": 100,
            "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
        }
    ]

    # Child exits: 1 OCO
    assert len(payload["childOrderStrategies"]) == 1
    oco = payload["childOrderStrategies"][0]
    assert oco["orderStrategyType"] == "OCO"
    children = oco["childOrderStrategies"]
    assert len(children) == 2

    # Target LIMIT
    target_child = next(c for c in children if c["orderType"] == "LIMIT")
    assert target_child["price"] == "155.00"
    assert target_child["orderLegCollection"][0]["instruction"] == "SELL"
    assert target_child["orderLegCollection"][0]["quantity"] == 100

    # Stop STOP
    stop_child = next(c for c in children if c["orderType"] == "STOP")
    assert stop_child["stopPrice"] == "148.00"
    assert stop_child["orderLegCollection"][0]["instruction"] == "SELL"
    assert stop_child["orderLegCollection"][0]["quantity"] == 100


def test_build_schwab_order_single_target_market_short():
    backend = SchwabBackend(auto_connect=False)
    bracket = BracketOrder(
        symbol="TSLA",
        side="SHORT",
        qty=50,
        entry_type="MARKET",
        limit_price=None,
        stop=StopSpec(order_type="STOP", price=Decimal("210.50")),
        target=TargetSpec(price=Decimal("190.00")),
        duration="DAY",
    )
    payload = backend._build_schwab_order(bracket)

    assert payload["orderStrategyType"] == "TRIGGER"
    assert payload["orderType"] == "MARKET"
    assert "price" not in payload
    assert payload["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    assert payload["orderLegCollection"][0]["quantity"] == 50

    oco = payload["childOrderStrategies"][0]
    target_child = next(c for c in oco["childOrderStrategies"] if c["orderType"] == "LIMIT")
    assert target_child["price"] == "190.00"
    assert target_child["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    assert target_child["orderLegCollection"][0]["quantity"] == 50

    stop_child = next(c for c in oco["childOrderStrategies"] if c["orderType"] == "STOP")
    assert stop_child["stopPrice"] == "210.50"
    assert stop_child["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    assert stop_child["orderLegCollection"][0]["quantity"] == 50


def test_build_schwab_order_two_targets_split():
    backend = SchwabBackend(auto_connect=False)
    bracket = BracketOrder(
        symbol="NVDA",
        side="LONG",
        qty=100,
        entry_type="LIMIT",
        limit_price=Decimal("120.00"),
        stop=StopSpec(order_type="STOP", price=Decimal("115.00")),
        target=TargetSpec(price=Decimal("130.00")),
        qty1=40,
        target1=TargetSpec(price=Decimal("125.00")),
        duration="DAY",
    )
    payload = backend._build_schwab_order(bracket)

    assert payload["orderStrategyType"] == "TRIGGER"
    assert len(payload["childOrderStrategies"]) == 2

    # First OCO: qty1 = 40, target1 = 125.00
    oco1 = payload["childOrderStrategies"][0]
    t1 = next(c for c in oco1["childOrderStrategies"] if c["orderType"] == "LIMIT")
    s1 = next(c for c in oco1["childOrderStrategies"] if c["orderType"] == "STOP")
    assert t1["orderLegCollection"][0]["quantity"] == 40
    assert t1["price"] == "125.00"
    assert s1["orderLegCollection"][0]["quantity"] == 40
    assert s1["stopPrice"] == "115.00"

    # Second OCO: qty - qty1 = 60, runner target = 130.00
    oco2 = payload["childOrderStrategies"][1]
    t2 = next(c for c in oco2["childOrderStrategies"] if c["orderType"] == "LIMIT")
    s2 = next(c for c in oco2["childOrderStrategies"] if c["orderType"] == "STOP")
    assert t2["orderLegCollection"][0]["quantity"] == 60
    assert t2["price"] == "130.00"
    assert s2["orderLegCollection"][0]["quantity"] == 60
    assert s2["stopPrice"] == "115.00"


def test_build_schwab_order_trailing_stop():
    backend = SchwabBackend(auto_connect=False)
    bracket = BracketOrder(
        symbol="MSFT",
        side="LONG",
        qty=80,
        entry_type="LIMIT",
        limit_price=Decimal("400.00"),
        stop=StopSpec(
            order_type="TRAILING_STOP",
            trail_offset=Decimal("4.50"),
            trail_link="VALUE",
            trail_basis="MARK",
        ),
        target=TargetSpec(price=Decimal("420.00")),
        duration="DAY",
    )
    payload = backend._build_schwab_order(bracket)

    oco = payload["childOrderStrategies"][0]
    trail_child = next(c for c in oco["childOrderStrategies"] if c["orderType"] == "TRAILING_STOP")
    assert trail_child["orderType"] == "TRAILING_STOP"
    assert trail_child["stopPriceOffset"] == 4.50
    assert trail_child["stopPriceLinkType"] == "VALUE"
    assert trail_child["stopPriceLinkBasis"] == "MARK"


# ---------------------------------------------------------------------------
# Payload Parsing Tests
# ---------------------------------------------------------------------------


def test_parse_positions():
    backend = SchwabBackend(auto_connect=False)
    account_data = {
        "securitiesAccount": {
            "positions": [
                {
                    "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                    "longQuantity": 100.0,
                    "shortQuantity": 0.0,
                    "averagePrice": 150.0,
                    "marketValue": 15500.0,
                    "longOpenProfitLoss": 500.0,
                    "currentDayProfitLoss": 200.0,
                    "previousSessionLongQuantity": 0.0,
                    "previousSessionShortQuantity": 0.0,
                    "currentDayCost": 15000.0,
                },
                {
                    "instrument": {"symbol": "TSLA", "assetType": "EQUITY"},
                    "longQuantity": 0.0,
                    "shortQuantity": 50.0,
                    "averagePrice": 200.0,
                    "marketValue": -9800.0,
                    "shortOpenProfitLoss": 200.0,
                    "currentDayProfitLoss": 50.0,
                    "previousSessionLongQuantity": 0.0,
                    "previousSessionShortQuantity": 50.0,
                    "currentDayCost": 0.0,
                },
            ]
        }
    }
    positions = backend._parse_positions(account_data)
    assert len(positions) == 2

    p_aapl = next(p for p in positions if p.symbol == "AAPL")
    assert p_aapl.side == "LONG"
    assert p_aapl.qty == 100.0
    assert p_aapl.avg_price == 150.0
    assert p_aapl.market_value == 15500.0
    assert p_aapl.open_pnl == 500.0
    assert p_aapl.day_pnl == 200.0
    assert p_aapl.held == "INTRADAY"
    assert p_aapl.previous_session_qty == 0.0

    p_tsla = next(p for p in positions if p.symbol == "TSLA")
    assert p_tsla.side == "SHORT"
    assert p_tsla.qty == 50.0
    assert p_tsla.avg_price == 200.0
    assert p_tsla.held == "CARRIED"
    assert p_tsla.previous_session_qty == 50.0


def test_parse_balances():
    backend = SchwabBackend(auto_connect=False)
    account_data = {
        "securitiesAccount": {
            "currentBalances": {
                "liquidationValue": 100500.25,
                "equity": 100500.25,
                "buyingPower": 200000.0,
                "dayTradingBuyingPower": 400000.0,
                "availableFunds": 75000.0,
                "maintenanceRequirement": 25500.25,
            }
        }
    }
    balances = backend._parse_balances(account_data)
    assert isinstance(balances, AccountBalances)
    assert balances.liquidation_value == 100500.25
    assert balances.equity == 100500.25
    assert balances.buying_power == 200000.0
    assert balances.day_trading_buying_power == 400000.0
    assert balances.available_funds == 75000.0
    assert balances.maintenance_requirement == 25500.25


def test_parse_orders():
    backend = SchwabBackend(auto_connect=False)
    orders_data = [
        {
            "orderId": 101,
            "orderStrategyType": "TRIGGER",
            "orderType": "LIMIT",
            "quantity": 100,
            "remainingQuantity": 100,
            "filledQuantity": 0,
            "price": 150.0,
            "duration": "DAY",
            "status": "WORKING",
            "enteredTime": "2026-11-02T09:30:00+0000",
            "orderLegCollection": [
                {
                    "legId": 1,
                    "instruction": "BUY",
                    "positionEffect": "OPENING",
                    "quantity": 100,
                    "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                }
            ],
            "childOrderStrategies": [
                {
                    "orderId": 102,
                    "orderStrategyType": "OCO",
                    "status": "AWAITING_PARENT_ORDER",
                    "childOrderStrategies": [
                        {
                            "orderId": 103,
                            "orderStrategyType": "SINGLE",
                            "orderType": "LIMIT",
                            "price": 155.0,
                            "quantity": 100,
                            "remainingQuantity": 100,
                            "status": "AWAITING_PARENT_ORDER",
                            "duration": "DAY",
                            "orderLegCollection": [
                                {
                                    "legId": 1,
                                    "instruction": "SELL",
                                    "quantity": 100,
                                    "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                                }
                            ],
                        },
                        {
                            "orderId": 104,
                            "orderStrategyType": "SINGLE",
                            "orderType": "STOP",
                            "stopPrice": 148.0,
                            "quantity": 100,
                            "remainingQuantity": 100,
                            "status": "AWAITING_PARENT_ORDER",
                            "duration": "DAY",
                            "orderLegCollection": [
                                {
                                    "legId": 1,
                                    "instruction": "SELL",
                                    "quantity": 100,
                                    "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    ]
    orders = backend._parse_orders(orders_data)
    assert len(orders) == 3

    entry = next(o for o in orders if o.order_id == 101)
    assert entry.role == "ENTRY"
    assert entry.symbol == "AAPL"
    assert entry.qty == 100.0
    assert entry.price == 150.0

    target = next(o for o in orders if o.order_id == 103)
    assert target.role == "TARGET"
    assert target.price == 155.0
    assert target.oco_id == 102

    stop = next(o for o in orders if o.order_id == 104)
    assert stop.role == "STOP"
    assert stop.stop == 148.0
    assert stop.oco_id == 102


def test_parse_orders_preserves_terminal_orders_and_discards_frozen_working():
    backend = SchwabBackend(auto_connect=False)
    # Scenario: order 201 has a frozen WORKING copy and an inner FILLED copy
    # Order 202 is pure CANCELED
    orders_data = [
        {
            "orderId": 201,
            "status": "WORKING",  # Frozen copy in sweep
            "quantity": 50,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{"legId": 1, "instruction": "BUY", "instrument": {"symbol": "MSFT"}}],
        },
        {
            "orderId": 200,
            "status": "WORKING",
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                {
                    "orderId": 201,
                    "status": "FILLED",  # Real terminal state
                    "quantity": 50,
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [{"legId": 1, "instruction": "BUY", "instrument": {"symbol": "MSFT"}}],
                }
            ],
        },
        {
            "orderId": 202,
            "status": "CANCELED",  # Terminal standalone order
            "quantity": 25,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{"legId": 1, "instruction": "SELL", "instrument": {"symbol": "TSLA"}}],
        },
    ]
    parsed = backend._parse_orders(orders_data)
    # Order 201 should be present with status FILLED (not working, not discarded)
    o201 = next(o for o in parsed if o.order_id == 201)
    assert o201.status == "FILLED"
    # Order 202 should be present with status CANCELED
    o202 = next(o for o in parsed if o.order_id == 202)
    assert o202.status == "CANCELED"


def test_parse_fills():
    backend = SchwabBackend(auto_connect=False)
    txns_data = [
        {
            "activityId": "999001",
            "orderId": 101,
            "type": "TRADE",
            "status": "VALID",
            "time": "2026-11-02T09:31:00-0500",
            "transferItems": [
                {
                    "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                    "amount": 100.0,
                    "price": 150.0,
                    "cost": -15000.0,
                    "positionEffect": "OPENING",
                },
                {
                    "instrument": {"assetType": "CURRENCY"},
                    "cost": -0.05,
                },
            ],
        }
    ]
    fills = backend._parse_fills(txns_data)
    assert len(fills) == 1
    f = fills[0]
    assert isinstance(f, Fill)
    assert f.id == "999001:0"
    assert f.order_id == 101
    assert f.symbol == "AAPL"
    assert f.qty == 100.0
    assert f.price == 150.0
    assert f.multiplier == 1.0
    assert f.fees == -0.05
    assert f.effect == "OPENING"


def test_parse_quotes():
    backend = SchwabBackend(auto_connect=False)
    quotes_data = {
        "AAPL": {
            "quote": {
                "bidPrice": 150.10,
                "askPrice": 150.15,
                "lastPrice": 150.12,
                "mark": 150.125,
                "totalVolume": 12500000.0,
                "netPercentChange": 1.25,
                "quoteTime": 1793617860000,
            }
        }
    }
    quotes = backend._parse_quotes(quotes_data)
    assert "AAPL" in quotes
    q = quotes["AAPL"]
    assert isinstance(q, Quote)
    assert q.symbol == "AAPL"
    assert q.bid == 150.10
    assert q.ask == 150.15
    assert q.last == 150.12
    assert q.mark == 150.125
    assert q.volume == 12500000.0
    assert q.change_pct == 1.25
    assert q.quote_time is not None


def test_parse_market_hours():
    backend = SchwabBackend(auto_connect=False)
    d = date(2026, 11, 2)
    hours_data = {
        "equity": {
            "EQ": {
                "date": "2026-11-02",
                "marketType": "EQUITY",
                "isOpen": True,
                "sessionHours": {
                    "regularMarket": [
                        {
                            "start": "2026-11-02T09:30:00-05:00",
                            "end": "2026-11-02T16:00:00-05:00",
                        }
                    ]
                },
            }
        }
    }
    mh = backend._parse_market_hours(hours_data, d)
    assert isinstance(mh, MarketHours)
    assert mh.date == d
    assert mh.is_open is True
    assert mh.open_time == datetime.fromisoformat("2026-11-02T09:30:00-05:00")
    assert mh.close_time == datetime.fromisoformat("2026-11-02T16:00:00-05:00")


# ---------------------------------------------------------------------------
# Rate Limit Cooldown Handling
# ---------------------------------------------------------------------------


def test_rate_limit_cooldown_tracking():
    backend = SchwabBackend(auto_connect=False)
    assert backend.is_rate_limited is False
    assert backend.cooldown_until == 0.0

    # Handle 429 with Retry-After = 45s
    backend.handle_429("45")
    assert backend.is_rate_limited is True
    assert backend.cooldown_until > 0.0

    # Default cooldown if Retry-After is missing
    backend2 = SchwabBackend(auto_connect=False)
    backend2.handle_429(None)
    assert backend2.is_rate_limited is True


# ---------------------------------------------------------------------------
# Broker Methods with Fake Client
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | list | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._json_data


class FakeSchwabClient:
    def __init__(self):
        self.calls = []

    def linked_accounts(self):
        self.calls.append("linked_accounts")
        return [{"accountNumber": "12345678", "hashValue": "HASH123", "displayName": "My Schwab"}]

    def account_details(self, hash_val: str, fields: str = "positions"):
        self.calls.append(("account_details", hash_val, fields))
        return {
            "securitiesAccount": {
                "positions": [],
                "currentBalances": {"liquidationValue": 50000.0, "buyingPower": 100000.0},
            }
        }

    def account_orders(self, hash_val: str, start, end, status: str | None = None):
        self.calls.append(("account_orders", hash_val, status))
        return []

    def order_details(self, hash_val: str, order_id: int):
        self.calls.append(("order_details", hash_val, order_id))
        return FakeResponse(200, {
            "orderId": order_id,
            "orderStrategyType": "SINGLE",
            "orderType": "STOP",
            "quantity": 100,
            "status": "WORKING",
            "stopPrice": 145.0,
            "orderLegCollection": [{"instruction": "SELL", "quantity": 100, "instrument": {"symbol": "AAPL"}}],
        })

    def transactions(self, hash_val: str, start, end, txn_type: str):
        self.calls.append(("transactions", hash_val, txn_type))
        return []

    def quotes(self, symbols: list[str]):
        self.calls.append(("quotes", symbols))
        return {s: {"quote": {"bidPrice": 100.0, "askPrice": 100.1, "lastPrice": 100.05, "mark": 100.05, "totalVolume": 1000}} for s in symbols}

    def market_hours(self, markets: list[str], date: str):
        self.calls.append(("market_hours", markets, date))
        return {"equity": {"EQ": {"date": date, "isOpen": True}}}

    def preview_order(self, hash_val: str, order: dict):
        self.calls.append(("preview_order", hash_val, order))
        return FakeResponse(200, {"commission": 0.0})

    def place_order(self, hash_val: str, order: dict):
        self.calls.append(("place_order", hash_val, order))
        return FakeResponse(201, {}, headers={"Location": "https://api.schwab.com/orders/888001"})

    def cancel_order(self, hash_val: str, order_id: int):
        self.calls.append(("cancel_order", hash_val, order_id))
        return FakeResponse(200, {})

    def replace_order(self, hash_val: str, order_id: int, order: dict):
        self.calls.append(("replace_order", hash_val, order_id, order))
        return FakeResponse(201, {}, headers={"Location": "https://api.schwab.com/orders/888002"})


def test_schwab_backend_broker_methods_with_client():
    fake_client = FakeSchwabClient()
    backend = SchwabBackend(auto_connect=False, client=fake_client)

    accounts = backend.get_accounts()
    assert len(accounts) == 1
    assert accounts[0].account_id == "12345678"
    assert accounts[0].hash_value == "HASH123"

    balances = backend.get_balances("HASH123")
    assert balances.liquidation_value == 50000.0

    quotes = backend.get_quotes(["AAPL"])
    assert "AAPL" in quotes
    assert quotes["AAPL"].mark == 100.05

    bracket = BracketOrder(
        symbol="AAPL",
        side="LONG",
        qty=100,
        entry_type="LIMIT",
        limit_price=Decimal("150.00"),
        stop=StopSpec(order_type="STOP", price=Decimal("145.00")),
        target=TargetSpec(price=Decimal("160.00")),
    )
    oid = backend.place_bracket("HASH123", bracket)
    assert oid == 888001

    cancelled = backend.cancel_order("HASH123", 888001)
    assert cancelled is True

    new_stop_id = backend.replace_stop("HASH123", 888001, 146.00)
    assert new_stop_id == 888002

    new_target_id = backend.replace_target("HASH123", 888001, 162.00)
    assert new_target_id == 888002

    close_id = backend.close_market("HASH123", "AAPL", "LONG", 100)
    assert close_id == 888001

    details = backend.get_order_details("HASH123", 888001)
    assert details is not None
    assert details.order_id == 888001


def test_make_backend_client_permissions(monkeypatch):
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: 100.0)
    monkeypatch.setenv("SCHWAB_APP_KEY", "mock_key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "mock_secret")

    class MockInnerClient:
        def __init__(self, *args, **kwargs):
            self._session = MagicMock()
            self.transactions = MagicMock()
            self.market_hours = MagicMock()
            self.linked_accounts = MagicMock()
            self.place_order = MagicMock()
            self.cancel_order = MagicMock()
            self.replace_order = MagicMock()
            self.streamer = MagicMock()

    import schwabdev

    monkeypatch.setattr(schwabdev, "Client", MockInnerClient)

    client = make_backend_client()
    assert isinstance(client, ReadOnlyClient)

    # Allowed read methods
    assert callable(client.transactions)
    assert callable(client.market_hours)
    assert callable(client.linked_accounts)

    # Allowed trading methods
    assert callable(client.place_order)
    assert callable(client.cancel_order)
    assert callable(client.replace_order)

    # Strictly forbidden methods (e.g. streamer)
    with pytest.raises(PermissionError) as exc_info:
        _ = client.streamer
    assert "streamer is not allowed on this client" in str(exc_info.value)


def test_schwab_backend_with_backend_allowed_client_allows_fills_and_market_hours():
    fake_client = FakeSchwabClient()
    wrapped_backend_client = ReadOnlyClient(fake_client, _BACKEND_ALLOWED)
    backend = SchwabBackend(auto_connect=False, client=wrapped_backend_client)

    # Calling get_fills() calls client.transactions() which is permitted under _BACKEND_ALLOWED
    fills = backend.get_fills("HASH123", datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert isinstance(fills, list)

    # Calling get_market_hours() calls client.market_hours() which is permitted under _BACKEND_ALLOWED
    mkt = backend.get_market_hours(date.today())
    assert isinstance(mkt, MarketHours)
    assert mkt.is_open is True

    # For comparison: a client limited strictly to _TRADING would fail with PermissionError
    trading_only_client = ReadOnlyClient(fake_client, schwab._TRADING)
    bad_backend = SchwabBackend(auto_connect=False, client=trading_only_client)
    with pytest.raises(PermissionError):
        bad_backend.get_fills("HASH123", datetime.now(timezone.utc), datetime.now(timezone.utc))
    with pytest.raises(PermissionError):
        bad_backend.get_market_hours(date.today())


def test_schwab_backend_get_order_details_multi_leg_matching():
    tree = {
        "orderId": 1001,
        "orderStrategyType": "TRIGGER",
        "orderType": "LIMIT",
        "price": 150.0,
        "quantity": 100,
        "status": "FILLED",
        "orderLegCollection": [
            {
                "legId": 1,
                "instruction": "BUY",
                "positionEffect": "OPENING",
                "quantity": 100,
                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
            }
        ],
        "childOrderStrategies": [
            {
                "orderId": 1002,
                "orderStrategyType": "OCO",
                "status": "WORKING",
                "childOrderStrategies": [
                    {
                        "orderId": 1003,
                        "orderStrategyType": "SINGLE",
                        "orderType": "LIMIT",
                        "price": 160.0,
                        "quantity": 100,
                        "status": "WORKING",
                        "orderLegCollection": [
                            {
                                "legId": 1,
                                "instruction": "SELL",
                                "quantity": 100,
                                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                            }
                        ],
                    },
                    {
                        "orderId": 1004,
                        "orderStrategyType": "SINGLE",
                        "orderType": "STOP",
                        "stopPrice": 145.0,
                        "quantity": 100,
                        "status": "WORKING",
                        "orderLegCollection": [
                            {
                                "legId": 1,
                                "instruction": "SELL",
                                "quantity": 100,
                                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                            }
                        ],
                    },
                ],
            }
        ],
    }

    class MultiLegClient(FakeSchwabClient):
        def order_details(self, hash_val: str, order_id: int):
            return FakeResponse(200, tree)

    client = MultiLegClient()
    backend = SchwabBackend(auto_connect=False, client=client)

    # 1. Fetching details for the STOP leg (1004) returns leg 1004, not the root 1001
    stop_order = backend.get_order_details("HASH123", 1004)
    assert stop_order is not None
    assert stop_order.order_id == 1004
    assert stop_order.order_type == "STOP"
    assert stop_order.stop == 145.0
    assert stop_order.role == "STOP"

    # 2. Fetching details for the TARGET leg (1003) returns leg 1003
    target_order = backend.get_order_details("HASH123", 1003)
    assert target_order is not None
    assert target_order.order_id == 1003
    assert target_order.order_type == "LIMIT"
    assert target_order.price == 160.0
    assert target_order.role == "TARGET"

    # 3. Fetching details for the ENTRY leg (1001) returns leg 1001
    entry_order = backend.get_order_details("HASH123", 1001)
    assert entry_order is not None
    assert entry_order.order_id == 1001
    assert entry_order.order_type == "LIMIT"
    assert entry_order.price == 150.0
    assert entry_order.role == "ENTRY"

    # 4. Fallback when order_id is not in the tree returns orders[0]
    fallback_order = backend.get_order_details("HASH123", 9999)
    assert fallback_order is not None
    assert fallback_order.order_id == 1001
