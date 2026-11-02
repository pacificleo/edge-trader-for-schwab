"""Unit tests for BrokerBackend ABC, normalized domain models, and backend registry."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal
import pytest

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
    StopSpec,
    TargetSpec,
    get_backend,
    register_backend,
)


# ---------------------------------------------------------------------------
# Domain Models Tests
# ---------------------------------------------------------------------------


def test_account_info_immutable():
    info = AccountInfo(account_id="12345", hash_value="hash_abc", display_name="Individual ...12345")
    assert info.account_id == "12345"
    assert info.hash_value == "hash_abc"
    assert info.display_name == "Individual ...12345"
    with pytest.raises(FrozenInstanceError):
        info.account_id = "new_id"  # type: ignore


def test_account_balances_defaults_and_immutable():
    balances = AccountBalances()
    assert balances.liquidation_value is None
    assert balances.equity is None
    assert balances.buying_power is None
    assert balances.day_trading_buying_power is None
    assert balances.available_funds is None
    assert balances.maintenance_requirement is None

    b2 = AccountBalances(liquidation_value=25000.50, equity=25000.50, buying_power=50000.0)
    assert b2.liquidation_value == 25000.50
    with pytest.raises(FrozenInstanceError):
        b2.liquidation_value = 0.0  # type: ignore


def test_position_fields_and_immutable():
    pos = Position(
        symbol="AAPL",
        asset_type="EQUITY",
        underlying="AAPL",
        side="LONG",
        qty=100.0,
        avg_price=150.0,
        market_value=15500.0,
        open_pnl=500.0,
        day_pnl=250.0,
        held="INTRADAY",
    )
    assert pos.symbol == "AAPL"
    assert pos.asset_type == "EQUITY"
    assert pos.previous_session_qty == 0.0
    with pytest.raises(FrozenInstanceError):
        pos.qty = 200.0  # type: ignore


def test_quote_fields_and_defaults():
    q = Quote(
        symbol="SPY",
        bid=500.10,
        ask=500.15,
        last=500.12,
        mark=500.125,
        volume=10_000_000.0,
    )
    assert q.symbol == "SPY"
    assert q.quote_time is None
    assert q.change_pct is None
    with pytest.raises(FrozenInstanceError):
        q.mark = 501.0  # type: ignore


def test_market_hours_fields_and_defaults():
    mh = MarketHours(date=date(2026, 11, 2), is_open=True)
    assert mh.is_open is True
    assert mh.open_time is None
    assert mh.close_time is None
    assert mh.next_session is None
    with pytest.raises(FrozenInstanceError):
        mh.is_open = False  # type: ignore


def test_pending_order_fields_and_immutable():
    order = PendingOrder(
        order_id=101,
        leg_id=1,
        legs=1,
        group_id=101,
        group_type="SINGLE",
        parent_id=None,
        oco_id=None,
        status="WORKING",
        symbol="QQQ",
        underlying="QQQ",
        asset_type="EQUITY",
        instruction="BUY",
        effect="OPENING",
        order_type="LIMIT",
        qty=50.0,
        filled=0.0,
        price=440.0,
        stop=None,
        duration="DAY",
        entered="2026-11-02T09:30:00Z",
        tag=None,
        role="ENTRY",
    )
    assert order.order_id == 101
    assert order.role == "ENTRY"
    assert order.trail_offset is None
    assert order.trail_link is None
    assert order.trail_basis is None
    with pytest.raises(FrozenInstanceError):
        order.status = "FILLED"  # type: ignore


def test_fill_fields_and_immutable():
    now = datetime(2026, 11, 2, 9, 31, 0)
    fill = Fill(
        id="fill-1",
        order_id=101,
        time=now,
        symbol="QQQ",
        asset_type="EQUITY",
        underlying="QQQ",
        qty=50.0,
        price=440.0,
        multiplier=1.0,
        fees=0.02,
        effect="OPENING",
    )
    assert fill.id == "fill-1"
    assert fill.multiplier == 1.0
    with pytest.raises(FrozenInstanceError):
        fill.fees = 1.0  # type: ignore


def test_stop_spec_and_target_spec():
    stop = StopSpec(order_type="STOP", price=Decimal("435.00"))
    assert stop.order_type == "STOP"
    assert stop.price == Decimal("435.00")
    assert stop.trail_offset is None

    trail = StopSpec(
        order_type="TRAILING_STOP",
        trail_offset=Decimal("2.50"),
        trail_link="VALUE",
        trail_basis="MARK",
    )
    assert trail.trail_basis == "MARK"

    target = TargetSpec(price=Decimal("450.00"))
    assert target.price == Decimal("450.00")


def test_bracket_order():
    stop = StopSpec(order_type="STOP", price=Decimal("435.00"))
    target = TargetSpec(price=Decimal("450.00"))
    bracket = BracketOrder(
        symbol="QQQ",
        side="LONG",
        qty=100,
        entry_type="LIMIT",
        limit_price=Decimal("440.00"),
        stop=stop,
        target=target,
    )
    assert bracket.symbol == "QQQ"
    assert bracket.side == "LONG"
    assert bracket.qty == 100
    assert bracket.qty1 is None
    assert bracket.target1 is None
    assert bracket.duration == "DAY"
    with pytest.raises(FrozenInstanceError):
        bracket.qty = 200  # type: ignore


def test_broker_capabilities_defaults():
    caps = BrokerCapabilities(name="test")
    assert caps.name == "test"
    assert caps.supports_two_targets is True
    assert caps.supports_native_trailing_stop is True
    assert caps.rate_limit_per_min == 70
    assert caps.has_market_hours_api is True


# ---------------------------------------------------------------------------
# BrokerBackend ABC Tests
# ---------------------------------------------------------------------------


def test_broker_backend_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BrokerBackend()  # type: ignore


def test_broker_backend_subclass_must_implement_all_abstract_methods():
    class IncompleteBackend(BrokerBackend):
        def connect(self) -> None:
            pass

    with pytest.raises(TypeError):
        IncompleteBackend()  # type: ignore


class DummyValidBackend(BrokerBackend):
    def __init__(self, custom_param: str = "default"):
        self.custom_param = custom_param
        self.capabilities = BrokerCapabilities(name="dummy")

    def connect(self) -> None:
        pass

    def auth_status(self) -> str:
        return "live"

    def auth_hours_left(self) -> float | None:
        return 24.0

    def login_cli(self, reauth: bool = False) -> None:
        pass

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {}

    def get_market_hours(self, d: date) -> MarketHours:
        return MarketHours(date=d, is_open=True)

    def get_accounts(self) -> list[AccountInfo]:
        return []

    def get_positions(self, account_id: str) -> list[Position]:
        return []

    def get_balances(self, account_id: str) -> AccountBalances:
        return AccountBalances()

    def get_orders(
        self, account_id: str, start: datetime, end: datetime, status: str | None = None
    ) -> list[PendingOrder]:
        return []

    def get_order_details(self, account_id: str, order_id: int) -> PendingOrder | None:
        return None

    def get_fills(self, account_id: str, start: datetime, end: datetime) -> list[Fill]:
        return []

    def preview_bracket(self, account_id: str, bracket: BracketOrder) -> dict:
        return {"status": "ok"}

    def place_bracket(self, account_id: str, bracket: BracketOrder) -> int:
        return 12345

    def cancel_order(self, account_id: str, order_id: int) -> bool:
        return True

    def replace_stop(self, account_id: str, order_id: int, new_stop: float) -> int:
        return 23456

    def replace_with_trail(self, account_id: str, order_id: int, trail_spec: dict) -> int:
        return 34567

    def replace_target(self, account_id: str, order_id: int, new_target: float) -> int:
        return 45678

    def close_market(self, account_id: str, symbol: str, side: str, qty: float) -> int:
        return 56789


def test_complete_backend_instantiates():
    backend = DummyValidBackend(custom_param="hello")
    assert backend.custom_param == "hello"
    assert backend.capabilities.name == "dummy"
    assert backend.auth_status() == "live"


# ---------------------------------------------------------------------------
# Registry Tests
# ---------------------------------------------------------------------------


def test_registry_registration_and_retrieval():
    register_backend("dummy", DummyValidBackend)
    instance = get_backend("dummy", custom_param="custom_val")
    assert isinstance(instance, DummyValidBackend)
    assert instance.custom_param == "custom_val"

    # Case-insensitive
    instance2 = get_backend("DUMMY")
    assert isinstance(instance2, DummyValidBackend)
    assert instance2.custom_param == "default"


def test_registry_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown broker backend: 'nonexistent'"):
        get_backend("nonexistent")


def test_registry_rejects_non_subclass():
    class NotABackend:
        pass

    with pytest.raises(TypeError, match="must be a subclass of BrokerBackend"):
        register_backend("not_a_backend", NotABackend)  # type: ignore
