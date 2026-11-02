"""Broker-agnostic backend abstraction layer and normalized domain models.

All domain models are immutable frozen dataclasses representing normalized concepts across
supported brokerages (Schwab, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


# ---------------------------------------------------------------------------
# Capabilities & Normalized Domain Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerCapabilities:
    """Feature flags and rate limits for a broker backend."""

    name: str
    supports_two_targets: bool = True
    supports_native_trailing_stop: bool = True
    rate_limit_per_min: int = 70
    has_market_hours_api: bool = True


@dataclass(frozen=True)
class AccountInfo:
    """Linked brokerage account identifier and presentation metadata."""

    account_id: str
    hash_value: str
    display_name: str


@dataclass(frozen=True)
class AccountBalances:
    """Account financial balances, equity, and buying power metrics."""

    liquidation_value: float | None = None
    equity: float | None = None
    buying_power: float | None = None
    day_trading_buying_power: float | None = None
    available_funds: float | None = None
    maintenance_requirement: float | None = None


@dataclass(frozen=True)
class Position:
    """Open brokerage position."""

    symbol: str
    asset_type: Literal["EQUITY", "OPTION", "FUTURE"]
    underlying: str
    side: Literal["LONG", "SHORT"]
    qty: float
    avg_price: float
    market_value: float
    open_pnl: float
    day_pnl: float
    held: Literal["INTRADAY", "CARRIED", "MIXED"]
    previous_session_qty: float = 0.0


@dataclass(frozen=True)
class Quote:
    """Real-time market quote data."""

    symbol: str
    bid: float
    ask: float
    last: float
    mark: float
    volume: float
    quote_time: datetime | None = None
    change_pct: float | None = None


@dataclass(frozen=True)
class MarketHours:
    """Market session open/close schedule for a calendar date."""

    date: date
    is_open: bool
    open_time: datetime | None = None
    close_time: datetime | None = None
    next_session: dict | None = None


@dataclass(frozen=True)
class PendingOrder:
    """Flattened leg of a working, queued, or recently updated order."""

    order_id: int
    leg_id: int
    legs: int
    group_id: int
    group_type: str
    parent_id: int | None
    oco_id: int | None
    status: str
    symbol: str
    underlying: str
    asset_type: str
    instruction: str
    effect: str | None
    order_type: str
    qty: float
    filled: float
    price: float | None
    stop: float | None
    duration: str
    entered: str
    tag: str | None
    role: Literal["ENTRY", "STOP", "TARGET", "EXIT"]
    trail_offset: float | None = None
    trail_link: Literal["VALUE", "PERCENT", "TICK"] | None = None
    trail_basis: Literal["MARK", "LAST", "BID", "ASK"] | None = None


@dataclass(frozen=True)
class Fill:
    """Execution fill details from a trade transaction."""

    id: str
    order_id: int | None
    time: datetime
    symbol: str
    asset_type: str
    underlying: str
    qty: float
    price: float
    multiplier: float
    fees: float
    effect: str | None


@dataclass(frozen=True)
class StopSpec:
    """Stop-loss parameter specification."""

    order_type: Literal["STOP", "TRAILING_STOP"]
    price: Decimal | None = None
    trail_offset: Decimal | None = None
    trail_link: Literal["VALUE", "PERCENT"] | None = None
    trail_basis: Literal["MARK", "LAST", "BID", "ASK"] | None = None


@dataclass(frozen=True)
class TargetSpec:
    """Profit target parameter specification."""

    price: Decimal


@dataclass(frozen=True)
class BracketOrder:
    """Unified representation of a bracket order payload."""

    symbol: str
    side: Literal["LONG", "SHORT"]
    qty: int
    entry_type: Literal["LIMIT", "MARKET"]
    limit_price: Decimal | None
    stop: StopSpec
    target: TargetSpec
    qty1: int | None = None
    target1: TargetSpec | None = None
    duration: str = "DAY"


# ---------------------------------------------------------------------------
# BrokerBackend Interface
# ---------------------------------------------------------------------------


class BrokerBackend(ABC):
    """Abstract base class defining the contract for brokerage backends."""

    capabilities: BrokerCapabilities

    # -----------------------------------------------------------------------
    # Lifecycle & Authentication
    # -----------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish connection and validate credentials."""
        ...

    @abstractmethod
    def auth_status(self) -> str:
        """Return auth health string: 'live', 'expired', or 'needs_login'."""
        ...

    @abstractmethod
    def auth_hours_left(self) -> float | None:
        """Hours before session or refresh token expires (or None)."""
        ...

    @abstractmethod
    def login_cli(self, reauth: bool = False) -> None:
        """Interactive terminal login flow for credential initial setup / refresh."""
        ...

    # -----------------------------------------------------------------------
    # Market Data
    # -----------------------------------------------------------------------

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch real-time bid, ask, last, mark quotes for symbols."""
        ...

    @abstractmethod
    def get_market_hours(self, d: date) -> MarketHours:
        """Fetch trading session hours for the given date."""
        ...

    # -----------------------------------------------------------------------
    # Account & State
    # -----------------------------------------------------------------------

    @abstractmethod
    def get_accounts(self) -> list[AccountInfo]:
        """Return linked brokerage accounts."""
        ...

    @abstractmethod
    def get_positions(self, account_id: str) -> list[Position]:
        """Return current open positions."""
        ...

    @abstractmethod
    def get_balances(self, account_id: str) -> AccountBalances:
        """Return account equity, buying power, and liquidation values."""
        ...

    # -----------------------------------------------------------------------
    # Orders & Fills History
    # -----------------------------------------------------------------------

    @abstractmethod
    def get_orders(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        status: str | None = None,
    ) -> list[PendingOrder]:
        """Return working or historical orders flattened into PendingOrder legs."""
        ...

    @abstractmethod
    def get_order_details(self, account_id: str, order_id: int) -> PendingOrder | None:
        """Return latest state of a single order."""
        ...

    @abstractmethod
    def get_fills(self, account_id: str, start: datetime, end: datetime) -> list[Fill]:
        """Return executed transaction fills with multipliers and fees."""
        ...

    # -----------------------------------------------------------------------
    # Order Actions (Explicit user clicks)
    # -----------------------------------------------------------------------

    @abstractmethod
    def preview_bracket(self, account_id: str, bracket: BracketOrder) -> dict:
        """Preview bracket margin/commissions without submitting."""
        ...

    @abstractmethod
    def place_bracket(self, account_id: str, bracket: BracketOrder) -> int:
        """Submit a new entry + stop + target bracket. Returns broker order ID."""
        ...

    @abstractmethod
    def cancel_order(self, account_id: str, order_id: int) -> bool:
        """Cancel a working entry or exit order."""
        ...

    @abstractmethod
    def replace_stop(self, account_id: str, order_id: int, new_stop: float) -> int:
        """Replace the price of a live stop order."""
        ...

    @abstractmethod
    def replace_with_trail(self, account_id: str, order_id: int, trail_spec: dict) -> int:
        """Replace a fixed stop order with a trailing stop."""
        ...

    @abstractmethod
    def replace_target(self, account_id: str, order_id: int, new_target: float) -> int:
        """Replace the limit price of a live target order."""
        ...

    @abstractmethod
    def close_market(self, account_id: str, symbol: str, side: str, qty: float) -> int:
        """Submit an immediate market order to close an open position."""
        ...
