# Specification: Pluggable Broker Backend Architecture & SchwabBackend

- **Date**: 2026-11-01
- **Status**: Approved
- **Author**: Antigravity & Pair Programming Partner

---

## 1. Overview & Goals

Edge Trader for Schwab currently couples its polling (`companion/poller.py`), order execution (`companion/trading.py`), and order generation (`companion/bracket.py`) directly to Charles Schwab Trader API semantics, Schwab JSON payloads, and the `schwabdev` client.

This design introduces a pluggable, normalized backend abstraction layer:
1. Defines a broker-agnostic `BrokerBackend` interface with normalized domain models for accounts, balances, positions, orders, fills, and market data.
2. Implements `SchwabBackend`, encapsulating all `schwabdev` communication, Schwab JSON schema parsing, and OAuth authentication.
3. Supports a `--broker <name>` CLI parameter (defaulting to `schwab`) with an isolated SQLite database per broker (`data/db_<broker>.db`).
4. Decouples `Poller`, `Trader`, and `bracket.py` so they interact purely with `BrokerBackend` without any broker-specific imports or data structures.

---

## 2. Architectural Principles & Invariants

Any implementation of `BrokerBackend` MUST adhere to the application's inviolable safety principles:
1. **Every Order is the User's Explicit Click**: The backend must never place, replace, cancel, or retry orders autonomously (with the sole exception of the user opt-in Auto B/E 1R stop move).
2. **Protection is Never Removed First**: When replacing stops or targets, the backend must use atomic replace operations or send rebuilt replacement brackets; protection must never be cancelled prior to new order confirmation.
3. **Incomplete Views Never Look Complete**: Missing or unconfirmed data must carry confidence flags (`MATCHED`, `ESTIMATED`, `UNRESOLVED`); unknown balances or P&L must never default to zero.
4. **Single Active Broker Per Process**: Only one backend is active per application runtime, selected via `--broker <name>`.
5. **Database Isolation**: Each backend writes to its own SQLite database: `data/db_<broker>.db` (or `data/demo.db` in `--demo` mode).

---

## 3. Package & Module Layout

```
companion/
├── backends/                       # NEW: Pluggable backend subsystem
│   ├── __init__.py                 # Exports BrokerBackend, BrokerCapabilities, models, get_backend
│   ├── base.py                     # BrokerBackend ABC, BrokerCapabilities, and normalized domain models
│   ├── registry.py                 # Factory: get_backend(name: str) -> BrokerBackend
│   └── schwab.py                   # SchwabBackend implementation (moved from companion/schwab.py)
├── bracket.py                      # Pure position sizing; outputs BracketOrder dataclass (no Schwab JSON)
├── poller.py                       # Poller daemon; accepts backend: BrokerBackend (no Schwab imports)
├── trading.py                      # Trader service; accepts backend: BrokerBackend (no Schwab imports)
├── store.py                        # SQLite store initialized with data/db_<broker>.db
├── app.py                          # FastAPI web server and dependency wiring
└── __main__.py                     # CLI entry point parsing --broker <name>
```

---

## 4. Normalized Domain Models (`companion/backends/base.py`)

All domain models are immutable Python dataclasses:

```python
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    hash_value: str
    display_name: str

@dataclass(frozen=True)
class AccountBalances:
    liquidation_value: float | None = None
    equity: float | None = None
    buying_power: float | None = None
    day_trading_buying_power: float | None = None
    available_funds: float | None = None
    maintenance_requirement: float | None = None

@dataclass(frozen=True)
class Position:
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
    date: date
    is_open: bool
    open_time: datetime | None = None
    close_time: datetime | None = None
    next_session: dict | None = None

@dataclass(frozen=True)
class PendingOrder:
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
    order_type: Literal["STOP", "TRAILING_STOP"]
    price: Decimal | None = None
    trail_offset: Decimal | None = None
    trail_link: Literal["VALUE", "PERCENT"] | None = None
    trail_basis: Literal["MARK", "LAST", "BID", "ASK"] | None = None

@dataclass(frozen=True)
class TargetSpec:
    price: Decimal

@dataclass(frozen=True)
class BracketOrder:
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

@dataclass(frozen=True)
class BrokerCapabilities:
    name: str
    supports_two_targets: bool = True
    supports_native_trailing_stop: bool = True
    rate_limit_per_min: int = 70
    has_market_hours_api: bool = True
```

---

## 5. The `BrokerBackend` Interface

```python
from abc import ABC, abstractmethod

class BrokerBackend(ABC):
    capabilities: BrokerCapabilities

    # Lifecycle & Authentication
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

    # Market Data
    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch real-time bid, ask, last, mark quotes for symbols."""
        ...

    @abstractmethod
    def get_market_hours(self, d: date) -> MarketHours:
        """Fetch trading session hours for the given date."""
        ...

    # Account & State
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

    # Orders & Fills History
    @abstractmethod
    def get_orders(self, account_id: str, start: datetime, end: datetime, status: str | None = None) -> list[PendingOrder]:
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

    # Order Actions (Explicit user clicks)
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
```

---

## 6. Component Decoupling Details

### 6.1 `companion/bracket.py`
- Sizing calculations (`plan()`) remain unchanged.
- Replace `bracket_order(p: Plan)` (which created Schwab JSON) with `to_bracket_order(p: Plan) -> BracketOrder`.
- `bracket.py` contains zero references to Schwab API JSON dictionary keys.

### 6.2 `companion/backends/schwab.py` (`SchwabBackend`)
- Contains all logic previously in `companion/schwab.py`.
- Implements `BrokerBackend`.
- Holds internal method `_to_schwab_order(bracket: BracketOrder) -> dict` translating generic `BracketOrder` into Schwab's native `TRIGGER`/`OCO` tree.
- Holds internal parsers translating Schwab responses into `list[Position]`, `AccountBalances`, `list[PendingOrder]`, `list[Fill]`, and `dict[str, Quote]`.

### 6.3 `companion/poller.py`
- Constructor signature: `Poller(backend: BrokerBackend, store: Store)`.
- Replaces raw `self.client` calls with `self.backend.get_positions()`, `self.backend.get_orders()`, `self.backend.get_fills()`, `self.backend.get_quotes()`, `self.backend.get_market_hours()`.
- Rate limiting ceiling set to `self.backend.capabilities.rate_limit_per_min`.

### 6.4 `companion/trading.py`
- Constructor signature: `Trader(backend: BrokerBackend, store: Store)`.
- Replaces direct Schwab client calls with backend methods (`place_bracket`, `replace_stop`, `replace_with_trail`, `replace_target`, `close_market`, `cancel_order`).

### 6.5 CLI & Database Configuration (`companion/__main__.py` & `app.py`)
- CLI accepts `--broker <name>` (defaulting to `"schwab"`).
- Sets `os.environ["EDGE_TRADER_BROKER"] = broker_name`.
- Initializes `Store(Path(f"data/db_{broker_name}.db"))` (or `data/demo.db` if `--demo`).
- Calls `get_backend(broker_name)` to instantiate the active backend and injects it into `Poller` and `Trader`.

---

## 7. Testing Strategy

1. **`tests/test_backends.py`**:
   - Tests `get_backend("schwab")` factory.
   - Tests rejection of unknown backend names.
   - Tests domain dataclass instantiations and validation.
2. **`tests/test_schwab_backend.py`**:
   - Tests `SchwabBackend` translating incoming Schwab payloads to normalized models.
   - Tests `SchwabBackend` translating `BracketOrder` to Schwab `TRIGGER` / `OCO` JSON trees (both single and two-target OT2OCO).
   - Tests rate-limit cooldown handling on HTTP 429.
3. **`tests/test_trading.py`**:
   - Replaces `FakeClient` with `MockBackend(BrokerBackend)`.
   - Tests submit, cancel, stop move, trail move, target replacement, and close through the backend interface.
4. **`tests/test_bracket.py`**:
   - Updated to verify `to_bracket_order()` emits `BracketOrder` dataclass.
5. **Full Regression Verification**:
   - All 123+ pytest tests pass.
