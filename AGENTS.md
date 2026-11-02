# AGENTS.md — Agent & Architecture Reference for Edge Trader for Schwab

This document is the single source of truth for AI agents (and human developers) working on `edge-trader-for-schwab`. It documents system architecture, invariants, data models, control flows, and development practices so future sessions do not need to rescan the repository.

---

## 1. Project Mission & Core Invariants

**Edge Trader for Schwab** is a local-first, defined-risk day-trading companion and one-click order execution terminal. While built originally for Charles Schwab accounts, it features a pluggable, broker-agnostic backend architecture (`companion/backends/`) supporting multiple brokerages via `--broker <name>` with Charles Schwab as the reference backend. It operates alongside charting software (such as thinkorswim).

### Unbreakable Architectural Principles ("Safety Rails")
1. **Every Order is the User's Explicit Click**:
   - The app NEVER executes, places, replaces, or cancels orders autonomously.
   - No algorithmic trading, automated signals, background order loops, or retries.
   - **Sole Exception**: Opt-in **Auto B/E 1R** (`auto_breakeven`). Chosen per ticket by the user at entry; moves that specific ticket's fixed stop to break-even once when price touches +1R. It never loosens a stop and is never retried.
   - *Any PR or feature that introduces autonomous order execution without an explicit user click must be rejected.*
2. **Protection is Never Removed First**:
   - When a new order for a symbol is entered at Schwab, Schwab automatically cancels any existing working orders for that symbol.
   - Therefore, stops and targets are adjusted by sending a **rebuilt replacement bracket** (or replacing the live stop directly via Schwab `replace_order`), NEVER by cancelling protection and leaving the position unprotected.
3. **An Incomplete View Never Looks Complete**:
   - All P&L and lot calculations track confidence coverage (`MATCHED`, `ESTIMATED`, `UNRESOLVED`, `stale`).
   - Unknown values are NEVER displayed as zero. Stale data flags disable order buttons in the UI.
4. **When Unsure, Refuse**:
   - Any order action with an ambiguous or unconfirmed outcome transitions to `UNKNOWN` or `ACCEPTED_UNTRACKED`, blocking further order actions on that symbol until Schwab confirms or the user explicitly clicks `Resolve`.
5. **Strictly REST Polling (No Streamer)**:
   - NEVER open Schwab's WebSocket streamer (`streamer.start()`). Schwab permits only one streamer per user. Opening it would terminate thinkorswim or other concurrent tools.
6. **Strictly Local-First (`127.0.0.1`)**:
   - The FastAPI backend binds only to `127.0.0.1` (`HOSTS = {"127.0.0.1:8770", "localhost:8770"}`).
   - Host header and `X-Companion: 1` + origin checks protect against DNS rebinding and cross-site requests.
   - No telemetry, analytics, cloud backends, or third-party dependencies.
7. **Pure Calculation Separation**:
   - Sizing and order tree generation (`companion/bracket.py`), FIFO ledger reconstruction (`companion/pnl.py`), and state aggregation (`companion/view.py`) are pure functions with **zero I/O**, making them deterministic and completely testable in memory.
8. **Pluggable Broker Backend & Database Isolation**:
   - Only one broker backend is active per application runtime, selected via `--broker <name>` (default: `schwab`) or `EDGE_TRADER_BROKER`.
   - Each broker backend persists to its own isolated database (`data/db_<broker>.db`), ensuring credentials, transaction histories, and ticket tracking never collide. Demo mode uses `data/demo.db`.

---

## 2. Repository Layout & File Map

```
edge-trader-for-schwab/
├── companion/                      # Main application package
│   ├── __init__.py                 # Version string (__version__ = "1.1.0")
│   ├── __main__.py                 # CLI entry point (--broker, --demo, --private flags)
│   ├── app.py                      # FastAPI web server, routes, CORS/DNS rebinding security
│   ├── backends/                   # Pluggable broker abstraction subsystem
│   │   ├── __init__.py             # Exports domain models, BrokerBackend, get_backend
│   │   ├── base.py                 # Abstract BrokerBackend, BrokerCapabilities, normalized domain dataclasses
│   │   ├── registry.py             # BrokerBackend registry and factory (get_backend, register_backend)
│   │   └── schwab.py               # Schwab implementation: wire mapping, REST polling, bracket generation
│   ├── bracket.py                  # Pure domain logic: risk-based sizing & normalized BracketOrder generation
│   ├── demo.py                     # Synthetic demo dataset generator (--demo mode)
│   ├── floating.py                 # Pywebview floating windows (Entry, Manage)
│   ├── login.py                    # Multi-broker authentication CLI tool (--broker flag)
│   ├── orders.py                   # Order tree flattener, PendingOrder model, stop coverage calculation
│   ├── pnl.py                      # FIFO lot matching, trade book, realized/open P&L, multipliers
│   ├── poller.py                   # Background polling daemon (rate-limited request budget, BrokerBackend-driven)
│   ├── schwab.py                   # Schwabdev client factory, ReadOnlyClient proxies, token expiration
│   ├── store.py                    # SQLite persistence layer (data/db_<broker>.db or data/demo.db)
│   ├── trading.py                  # Trader service: bracket submission, arming, stop/target moves, partials
│   ├── update_check.py             # Non-blocking GitHub release notifier
│   ├── view.py                     # Pure view builder: constructs unified dashboard state JSON
│   └── static/
│       └── index.html              # Single-page frontend application (HTML5/CSS3/Vanilla JS)
├── tests/                          # Pytest test suite (175+ unit & integration tests)
│   ├── test_auto_be.py             # Auto B/E trigger rules and quote qualification tests
│   ├── test_backends.py            # BrokerBackend contracts, domain models, registry, and CLI parsing tests
│   ├── test_bracket.py             # Sizing, rounding, bounds, and order payload structure tests
│   ├── test_orders.py              # Order tree flattening, OCO semantics, stop coverage tests
│   ├── test_pnl.py                 # FIFO matching, multipliers (stocks/options/futures), P&L tests
│   ├── test_schwab_env.py          # Environment configuration & shared .env fallback tests
│   ├── test_trading.py             # Comprehensive Trader & mock broker test cases
│   └── test_update_check.py        # Version check parsing & cache tests
├── scripts/
│   ├── capture_screenshots.py      # Automated Playwright script to retake docs screenshots from demo mode
│   └── make_icon.py                # Generates companion.ico and companion-64.png
├── assets/                         # Desktop icons
├── docs/                           # Documentation screenshots & media
├── setup.bat / launch.bat          # Windows setup and windowless launch scripts
├── login.bat / stop.bat            # Windows login and process termination scripts
├── requirements.txt                # Python package dependencies
├── .env.example                    # Template for Schwab API credentials
└── README.md / CONTRIBUTING.md     # User & contributor documentation
```

---

## 3. Core Modules & Responsibilities

### `companion/backends/` Subsystem
The backend subsystem decouples application domain logic (sizing, polling, order management, P&L) from broker-specific API schemas and authentication protocols.

#### `companion/backends/base.py`
- Defines the `BrokerBackend` abstract base class, `BrokerCapabilities`, and all normalized domain models.
- **Normalized Domain Models** (immutable frozen dataclasses):
  - `AccountInfo`: Linked brokerage account identifier and presentation metadata (`account_id`, `hash_value`, `display_name`).
  - `AccountBalances`: Account financial metrics (`liquidation_value`, `equity`, `buying_power`, `day_trading_buying_power`, `available_funds`, `maintenance_requirement`).
  - `Position`: Open position state (`symbol`, `asset_type` [`EQUITY`, `OPTION`, `FUTURE`], `underlying`, `side` [`LONG`, `SHORT`], `qty`, `avg_price`, `market_value`, `open_pnl`, `day_pnl`, `held` [`INTRADAY`, `CARRIED`, `MIXED`], `previous_session_qty`).
  - `Quote`: Real-time market quote (`symbol`, `bid`, `ask`, `last`, `mark`, `volume`, `quote_time`, `change_pct`).
  - `MarketHours`: Regular trading session window (`date`, `is_open`, `open_time`, `close_time`, `next_session`).
  - `PendingOrder`: Flattened single-leg order state (`order_id`, `leg_id`, `legs`, `group_id`, `group_type`, `parent_id`, `oco_id`, `status`, `symbol`, `underlying`, `asset_type`, `instruction`, `effect`, `order_type`, `qty`, `filled`, `price`, `stop`, `duration`, `entered`, `tag`, `role` [`ENTRY`, `STOP`, `TARGET`, `EXIT`], trailing stop parameters).
  - `Fill`: Trade execution fill details (`id`, `order_id`, `time`, `symbol`, `asset_type`, `underlying`, `qty`, `price`, `multiplier`, `fees`, `effect`).
  - `StopSpec`: Stop-loss order specification (`order_type` [`STOP`, `TRAILING_STOP`], `price`, `trail_offset`, `trail_link`, `trail_basis`).
  - `TargetSpec`: Profit target specification (`price`).
  - `BracketOrder`: Complete unified bracket payload (`symbol`, `side`, `qty`, `entry_type`, `limit_price`, `stop`, `target`, `qty1`, `target1`, `duration`).
- **`BrokerCapabilities`**:
  - `name`: Unique broker identifier string (e.g. `"schwab"`).
  - `supports_two_targets`: Whether backend natively handles dual-target brackets (OT2OCO).
  - `supports_native_trailing_stop`: Whether broker supports native trailing stop orders.
  - `rate_limit_per_min`: Throttling ceiling (default 70 req/min).
  - `has_market_hours_api`: Whether broker provides a market hours session schedule endpoint.
- **`BrokerBackend` Abstract Interface**:
  - *Lifecycle & Authentication*:
    - `connect() -> None`: Establishes broker connection and validates credentials.
    - `auth_status() -> str`: Returns auth health (`"live"`, `"expired"`, or `"needs_login"`).
    - `auth_hours_left() -> float | None`: Hours before session or refresh token expires (or None).
    - `login_cli(reauth: bool = False) -> None`: Interactive terminal login flow for credential setup/refresh.
  - *Market Data*:
    - `get_quotes(symbols: list[str]) -> dict[str, Quote]`: Fetches real-time quotes for symbols.
    - `get_market_hours(d: date) -> MarketHours`: Fetches trading session hours for the given date.
  - *Account & State*:
    - `get_accounts() -> list[AccountInfo]`: Returns linked brokerage accounts.
    - `get_positions(account_id: str) -> list[Position]`: Returns open positions.
    - `get_balances(account_id: str) -> AccountBalances`: Returns account equity, buying power, and liquidation values.
  - *Orders & Fills History*:
    - `get_orders(account_id: str, start: datetime, end: datetime, status: str | None = None) -> list[PendingOrder]`: Returns working or historical orders flattened into `PendingOrder` legs.
    - `get_order_details(account_id: str, order_id: int) -> PendingOrder | None`: Returns latest state of a single order.
    - `get_fills(account_id: str, start: datetime, end: datetime) -> list[Fill]`: Returns executed transaction fills with multipliers and fees.
  - *Order Actions (Explicit user clicks)*:
    - `preview_bracket(account_id: str, bracket: BracketOrder) -> dict`: Previews bracket margin/commissions without submitting.
    - `place_bracket(account_id: str, bracket: BracketOrder) -> int`: Submits entry + stop + target bracket. Returns broker order ID.
    - `cancel_order(account_id: str, order_id: int) -> bool`: Cancels a working entry or exit order.
    - `replace_stop(account_id: str, order_id: int, new_stop: float) -> int`: Replaces price of a live stop order.
    - `replace_with_trail(account_id: str, order_id: int, trail_spec: dict) -> int`: Replaces fixed stop with trailing stop.
    - `replace_target(account_id: str, order_id: int, new_target: float) -> int`: Replaces limit price of a live target order.
    - `close_market(account_id: str, symbol: str, side: str, qty: float) -> int`: Submits immediate market order to close position.

#### `companion/backends/registry.py`
- Central backend registry and factory.
- `register_backend(name: str, cls: type[BrokerBackend])`: Registers a backend implementation by name (case-insensitive).
- `get_backend(name: str, **kwargs: Any) -> BrokerBackend`: Instantiates and returns the registered backend.

#### `companion/backends/schwab.py`
- Concrete Charles Schwab implementation of `BrokerBackend`.
- Encapsulates `schwabdev.Client`, `ReadOnlyClient` proxy, rate limiting, and 429 retry-after cooldowns.
- Converts raw Schwab JSON responses into normalized domain objects (`Quote`, `Position`, `PendingOrder`, `Fill`, `MarketHours`).
- Translates normalized `BracketOrder` into Schwab-native order trees (`TRIGGER` with child `OCO`s).

### `companion/schwab.py`
- Factory for `schwabdev.Client` instances.
- **Client Security & Allowlists**: Wraps the underlying client in `ReadOnlyClient` which intercepts `__getattr__` and allows ONLY explicit method sets:
  - `_ALLOWED` (for poller): `linked_accounts`, `account_details`, `account_orders`, `transactions`, `quote`, `quotes`, `price_history`, `market_hours`, etc.
  - `_TRADING` (for trader): `linked_accounts`, `account_details`, `account_orders`, `quote`, `quotes`, `order_details`, `preview_order`, `place_order`, `cancel_order`, `replace_order`.
- **Zero Retries**: Overrides HTTP adapter `max_retries=Retry(total=0)`. Schwabdev's automatic retry logic is disabled to guarantee no hidden order duplicates or rate-budget overflows.
- **Auth Expiration Guard**: `auth_hours_left()` inspects `tokens.db`. If refresh token age is within `SAFETY_HOURS` (2 hours), raises `AuthExpired` to prevent server hang on browser login prompt.

### `companion/store.py`
- SQLite database abstraction (`data/db_<broker>.db` or `data/demo.db`).
- **Dynamic Database Isolation**: Database path is resolved via `resolve_db_path(broker, demo)`:
  - Live broker: `data/db_<broker>.db` (e.g. `data/db_schwab.db`).
  - Demo mode: `data/demo.db`.
- Key Tables:
  - `transactions` / `transaction_versions`: Broker transactions and immutable audit trail.
  - `orders` / `order_versions`: All observed broker orders and state changes.
  - `position_snapshots`: Captured only when account positions hash changes.
  - `quotes`: Batched quote history (symbol, bid, ask, last, mark, volume, quote_time).
  - `requests`: HTTP endpoint metrics (timing, HTTP code, errors).
  - `prev_session`: Previous day's closing inventory per symbol to anchor FIFO basis.
  - `tickets`: Locally generated tickets before dispatch (`SENDING`, `ACCEPTED`, `UNKNOWN`, `REJECTED`).
  - `auto_be`: Auto Break-Even tracker (`PENDING`, `SENDING`, `DONE`, `FAILED`, `SKIPPED`).
  - `actions`: Audit log of user actions (order moves, partials, closes, cancels).
  - `settings`: Application settings (e.g. `trading_armed`, `trading_armed_at`, `ui_prefs`).

### `companion/bracket.py`
- Pure math & normalized domain generation (no external calls, zero broker-specific JSON).
- **Position Sizing**:
  - Formulas: `qty = floor(risk_dollars / per_share_risk)`.
  - Caps: `MAX_SHARES = 10_000`, `MAX_NOTIONAL = $250,000`, `MIN_STOP_TICKS = 2`.
  - Tick rounding: `$0.01` if price >= $1.00, `$0.0001` if sub-dollar.
  - Price modes: `mid`, `bid`, `ask`, `market`, `cross` (0.05% through market).
- **Bracket Order Conversion**:
  - `to_bracket_order(p: Plan, exit_duration: str = "DAY") -> BracketOrder`: Converts a validated `Plan` to a normalized `BracketOrder` containing `StopSpec` and `TargetSpec`.
- **Target Configurations**:
  - Single target: 1 target and 1 stop.
  - Two targets: Splits position into Target 1 (`qty1`) and Runner (`qty - qty1`), each paired with its own stop.
- **Stop Types**:
  - `fixed`: Standard fixed price stop market.
  - `autobe`: Fixed stop + poller monitors price until mark reaches +1R.
  - `trail`: Trailing stop with `offset`, `link` (`VALUE` / `PERCENT`), and `basis` (`MARK` / `LAST` / `BID` / `ASK`).

### `companion/trading.py`
- Order action executor (`Trader` class). Decoupled from broker specifics; consumes `backend: BrokerBackend` via dependency injection.
- **Session Arming**:
  - Arming is required to submit entries (`Trader.armed`).
  - Auto-disarms on every app startup and at 16:00 ET market close.
- **Pre-flight & Double-Click Guard**:
  - Ticket ID generated client-side; ticket row inserted as `SENDING` in SQLite *before* network request. Duplicate clicks return existing ticket status without placing a second order.
  - Lock-in check: If price moved > 0.1R against the user between display and click, order is refused.
- **Action Suite**:
  - `submit()`: Submits initial bracket order via `backend.place_bracket()`.
  - `cancel_entry()` / `cancel_all_entries()`: Cancels unfilled entry orders safely via `backend.cancel_order()`.
  - `cancel_symbol_orders()`: Cancels all orders for a symbol.
  - `close_position()`: Sends market order to close position via `backend.close_market()`.
  - `move_stop()`: Replaces live stop directly via `backend.replace_stop()` or rebuilds bracket.
  - `set_trail()`: Replaces fixed stop with trailing stop via `backend.replace_with_trail()`.
  - `set_target()`: Modifies target price or target R multiple via `backend.replace_target()`.
  - `take_partial()`: Sells portion of position at mid/bid/ask/market, waits for fill confirmation, then rebuilds stop/target on remaining shares.
  - `auto_breakeven()`: Executed once in worker thread when mark touches +1R.
  - `reconcile()`: Correlates local `tickets` with broker orders by matching timestamps and order structures.

### `companion/poller.py`
- Background daemon thread `schwab-poller` (or `<broker>-poller`). Consumes `backend: BrokerBackend` via dependency injection.
- **Throttled Request Schedule**:
  - Account + positions: every 5s (`backend.get_positions()`, `backend.get_balances()`).
  - Orders today: every 5s (`backend.get_orders()`).
  - Orders 59-day sweep (WORKING, PENDING_ACTIVATION): every 30s.
  - Transactions today: every 10s (`backend.get_fills()`).
  - Batched quotes for active symbols: every 3s (`backend.get_quotes()`).
  - Transactions 3-day sweep (corrections): every 10m.
  - Market hours: every 30m (`backend.get_market_hours()`).
- **Rate-Limiter**:
  - Dynamically configured from `backend.capabilities.rate_limit_per_min` (e.g. 70 req/min).
  - Sliding 60-second window tracking request timestamps.
  - On HTTP 429 response: Parses `Retry-After` header (or defaults to 30s) and pauses ALL poller and trader traffic across the entire application.
- **Auto B/E Watcher**:
  - `_auto_be()` checks real-time quotes against +1R threshold. Quote must be newer than fill timestamp and < 15 seconds old.
  - Atomically claims ticket state `set_auto_be(..., "SENDING", only_from="PENDING")` and spawns daemon worker thread to execute `trader.auto_breakeven()`.

### `companion/orders.py`
- Flattens complex, recursive Schwab order structures (`SINGLE`, `OCO`, `TRIGGER`).
- Extracts `PendingOrder` dataclass instances.
- Resolves conflicting Schwab states (e.g., when thinkorswim leaves a stand-alone order stuck at `WORKING` while its nested OCO version is `FILLED`).
- `stop_coverage()`: Determines total shares protected by live stops, respecting OCO mutual-cancellation rules.

### `companion/pnl.py`
- Pure FIFO ledger book reconstruction (`build_book`).
- Parses `TRADE` transaction transferItems and derives exact multipliers automatically without hardcoded symbol maps: `multiplier = round(abs(cost / (qty * price)), 4)`.
- Reconstructs round-trip `Trade` objects and calculates realized P&L, fees, hold time, and entry/exit averages.
- Handles overnight/carried positions via `opening_inventory`.
- Flags status: `MATCHED` (full fill history), `ESTIMATED` (Schwab average cost basis), `UNRESOLVED` (missing history; P&L remains `None`, never zero).

### `companion/view.py`
- Compiles the global state dictionary returned by `/api/state`.
- Categorizes positions: `INTRADAY` (opened today), `CARRIED` (carried from prior session), `MIXED` (carried and traded today).
- Computes protection levels: `FULL`, `PARTIAL`, `NONE`, `UNKNOWN`.
- Formats OCC option symbols into readable notation.
- `private_view()`: Strips out carried positions, balances, and carried P&L when running with `--private`.

### `companion/app.py`
- FastAPI web service listening on `127.0.0.1:8770`.
- Dependency injection: dynamically initializes `store = Store(resolve_db_path(broker, demo))` and `backend = get_backend(broker)`.
- Injects `broker` identifier into `/api/state` metadata for frontend visibility.
- Endpoints:
  - `GET /`: Serves `static/index.html`.
  - `GET /api/state`: Returns latest cached dashboard state snapshot.
  - `POST /api/ticket/plan`: Sizing & preview calculation.
  - `POST /api/ticket/lookup`: Symbol metadata & quote check.
  - `POST /api/ticket/submit`: Arm-checked order submission.
  - `POST /api/ticket/cancel`: Unfilled entry cancellation.
  - `POST /api/ticket/cancel_entries`: Cancel all unfilled entries.
  - `POST /api/ticket/arm`: Toggles trading arm switch.
  - `POST /api/ticket/resolve`: Manually resolves stuck ticket.
  - `POST /api/position/close`: Closes position at market.
  - `POST /api/position/move_stop`: Moves stop to price or break-even.
  - `POST /api/position/partial`: Takes partial and rebuilds bracket.
  - `POST /api/position/set_target`: Updates target price / R.
  - `POST /api/position/trail`: Sets trailing stop.
  - `POST /api/position/cancel_orders`: Cancels all working orders for symbol.
  - `POST /api/floating/open`: Spawns floating pywebview window.
  - `GET /api/ui/prefs` & `POST /api/ui/prefs`: Manages UI settings (hotkeys, risk buttons).
- Security middleware: Enforces local-only Host headers and `_guard` checks (`X-Companion: 1`, origin check).

### `companion/__main__.py`
- CLI entry point parsing command-line flags:
  - `--broker <name>`: Selects active broker backend (defaults to `"schwab"` or `EDGE_TRADER_BROKER`).
  - `--demo`: Activates synthetic data demo mode (persists to `data/demo.db`).
  - `--private`: Activates screen privacy mode, hiding carried positions and total account balances.

### `companion/login.py`
- Multi-broker authentication CLI dispatcher.
- Accepts `--broker <name>` (default: `schwab`) and `--reauth`.
- Instantiates the chosen backend via `get_backend(args.broker)` and delegates terminal authentication to `backend.login_cli(reauth=args.reauth)`.

### `companion/static/index.html`
- Monolithic responsive single-page frontend.
- Contains all HTML markup, CSS styling, and Vanilla JS UI logic.
- Polls `/api/state` every 1 second.
- Sections:
  - Header: System health light (`LIVE`, `STALE`, `DISCONNECTED`), broker chip, market clock countdown, voice alert toggle, arm toggle.
  - Left Panel: Positions table (day trades vs carried), protection chips, live P&L, live R.
  - Middle Panel: Selected trade workbench with dynamic R-ruler, break-even button, stop/target adjuster, trailing stop configuration, partial take buttons, close button, and per-symbol action log.
  - Right Panel: New trade bracket ticket (risk buttons, entry style, target/split controls, Auto B/E toggle, planned order summary).
  - Bottom Panel: Working orders list, completed trades with realized P&L breakdown.

---

## 4. Typical Development Workflows

### Running Tests
Always use `python -m pytest` or set `PYTHONPATH=.` so the `companion` package resolves cleanly:
```bash
.venv/bin/python -m pytest -v
```

### Running with a Specific Broker Backend
```bash
# Charles Schwab (default)
.venv/bin/python -m companion --broker schwab

# Custom or alternative broker backend
.venv/bin/python -m companion --broker alpaca
```
Or set the environment variable:
```bash
EDGE_TRADER_BROKER=schwab .venv/bin/python -m companion
```

### Developing in Demo Mode (No Broker Credentials Required)
For UI and front-end development, run:
```bash
.venv/bin/python -m companion --demo
```
- Operates entirely on invented data (`companion/demo.py`).
- Refuses every order action safely.
- Persists to a separate throwaway database (`data/demo.db`).

### Running Screen Privacy Mode
```bash
.venv/bin/python -m companion --private
```
Hides carried positions, overnight P&L, and account liquidation balance.

### Adding New Trading or Order Logic
1. **Never make raw broker API calls in domain logic**: All API interactions go through `Trader._logged()` or `backend` methods.
2. **Always test unhappy paths**: Network timeout, HTTP 5xx, partial fills, price slip, 429 response.
3. **Use the `MockBackend` fixture**: See `tests/test_trading.py` and `tests/test_auto_be.py` for how to simulate broker behavior and test order trees in isolation.

---

## 5. Adding a New Broker Backend

To add support for a new brokerage (e.g. Alpaca, Interactive Brokers, Tradier), follow these step-by-step instructions:

### Step 1: Create the Backend Module
Create `companion/backends/<name>.py` and subclass `BrokerBackend`:
```python
from companion.backends.base import BrokerBackend, BrokerCapabilities, BracketOrder, Position, Quote, PendingOrder, Fill, AccountInfo, AccountBalances, MarketHours

class MyBrokerBackend(BrokerBackend):
    capabilities = BrokerCapabilities(
        name="mybroker",
        supports_two_targets=True,            # Set False if broker cannot split brackets
        supports_native_trailing_stop=True,   # Set False if trailing stops are unsupported
        rate_limit_per_min=120,               # Match broker request quota
        has_market_hours_api=False,           # Set False if broker lacks market session API
    )
```

### Step 2: Implement Lifecycle & Authentication
- `connect(self) -> None`: Initialize SDK client or HTTP session with API keys/tokens.
- `auth_status(self) -> str`: Return `"live"`, `"expired"`, or `"needs_login"`.
- `auth_hours_left(self) -> float | None`: Return remaining token validity hours (or `None` for static API keys).
- `login_cli(self, reauth: bool = False) -> None`: Interactive terminal login / credentials check invoked via `python -m companion.login --broker mybroker`.

### Step 3: Implement Market Data & Account State
- `get_quotes(self, symbols: list[str]) -> dict[str, Quote]`: Query broker quote endpoint and return normalized `Quote(symbol, bid, ask, last, mark, volume, quote_time)`.
- `get_market_hours(self, d: date) -> MarketHours`: If `has_market_hours_api=False`, calculate standard NYSE/NASDAQ hours (9:30 - 16:00 ET).
- `get_accounts(self) -> list[AccountInfo]`: Return list of account metadata (`account_id`, `hash_value`, `display_name`).
- `get_positions(self, account_id: str) -> list[Position]`: Map broker positions to normalized `Position` instances (`side`, `qty`, `avg_price`, `held` [`INTRADAY`, `CARRIED`, `MIXED`]).
- `get_balances(self, account_id: str) -> AccountBalances`: Map equity, buying power, and liquidation values.

### Step 4: Implement Orders & Fills History
- `get_orders(self, account_id: str, start: datetime, end: datetime, status: str | None = None) -> list[PendingOrder]`: Flatten broker orders into normalized `PendingOrder` legs. Assign each leg a `role`:
  - `ENTRY`: Unfilled entry order.
  - `STOP`: Working stop-loss order (fixed or trailing).
  - `TARGET`: Working profit-taking limit order.
  - `EXIT`: Other closing orders.
- `get_order_details(self, account_id: str, order_id: int) -> PendingOrder | None`: Return fresh single-order state.
- `get_fills(self, account_id: str, start: datetime, end: datetime) -> list[Fill]`: Parse execution transactions into normalized `Fill` objects with multipliers (e.g. 100 for equity options) and fees.

### Step 5: Implement Order Actions (Explicit User Clicks)
- `preview_bracket(self, account_id: str, bracket: BracketOrder) -> dict`: Dry-run margin and commission estimation.
- `place_bracket(self, account_id: str, bracket: BracketOrder) -> int`:
  - Translate domain `BracketOrder` into the broker's native wire schema (e.g., OTO/OCO tree or parent-contingent bracket).
  - If `bracket.qty1` and `bracket.target1` are provided and `capabilities.supports_two_targets` is True, emit a dual-target bracket (Target 1 on `qty1` + stop, Runner on `qty - qty1` + stop).
  - Return the primary broker order ID.
- `cancel_order(self, account_id: str, order_id: int) -> bool`: Cancel a working order leg.
- `replace_stop(self, account_id: str, order_id: int, new_stop: float) -> int`: Replace stop price using broker replace API.
- `replace_with_trail(self, account_id: str, order_id: int, trail_spec: dict) -> int`: Replace fixed stop with trailing stop.
- `replace_target(self, account_id: str, order_id: int, new_target: float) -> int`: Replace target limit price.
- `close_market(self, account_id: str, symbol: str, side: str, qty: float) -> int`: Submit immediate market order to exit.

> [!CRITICAL]
> **Protection Invariant**: Protection must NEVER be cancelled before a replacement is confirmed! Use atomic broker replacement endpoints or submit replacement brackets before cancelling existing stops.

### Step 6: Register in Backend Registry
In `companion/backends/registry.py`:
```python
from .mybroker import MyBrokerBackend
register_backend("mybroker", MyBrokerBackend)
```
And expose in `companion/backends/__init__.py`.

### Step 7: Write Tests & Verify Invariants
Add test coverage in `tests/test_backends.py` or `tests/test_mybroker_backend.py`:
- Test wire response mapping to normalized `Quote`, `Position`, `PendingOrder`, `Fill`.
- Test `place_bracket` payload generation for single-target, dual-target, and trailing stop brackets.
- Verify all 175+ existing tests pass cleanly: `.venv/bin/python -m pytest -v`.

---

## 6. Security & Safety Checklist for PRs

Before committing or submitting changes:
- [ ] No autonomous or unsolicited orders introduced.
- [ ] No WebSocket streamer introduced (`REST` polling only).
- [ ] No automated retries added to broker POST/PUT endpoints.
- [ ] Incomplete or missing data is never shown as complete or zero.
- [ ] Server remains strictly bound to `127.0.0.1`.
- [ ] Only one broker active per runtime via `--broker <name>`.
- [ ] SQLite database isolated per broker (`data/db_<broker>.db`).
- [ ] All 175+ pytest tests pass (`.venv/bin/python -m pytest -q`).
- [ ] No real account numbers, tokens, or personal data in tests or fixtures.
