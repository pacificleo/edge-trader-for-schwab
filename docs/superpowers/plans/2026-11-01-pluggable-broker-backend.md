# Pluggable Broker Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Edge Trader to introduce a pluggable, normalized `BrokerBackend` interface, move all Charles Schwab API and payload code into `SchwabBackend`, decouple `Poller`, `Trader`, and `bracket.py`, and support `--broker <name>` with isolated databases (`data/db_<broker>.db`).

**Architecture:** Create `companion/backends/` containing the `BrokerBackend` ABC, `BrokerCapabilities`, and normalized domain models (`AccountInfo`, `AccountBalances`, `Position`, `PendingOrder`, `Fill`, `Quote`, `MarketHours`, `BracketOrder`). Implement `SchwabBackend` which handles all `schwabdev` communication, Schwab JSON wire-format translation, and rate-limiting. Decouple `bracket.py` to output `BracketOrder`, and inject `backend: BrokerBackend` into `Poller` and `Trader`. Wire `--broker` flag in CLI and `app.py`.

**Tech Stack:** Python 3.11+, dataclasses, FastAPI, pytest.

**Spec:** [docs/superpowers/specs/2026-11-01-pluggable-broker-backend-design.md](file:///Volumes/Personal/code/edge-trader-for-schwab/docs/superpowers/specs/2026-11-01-pluggable-broker-backend-design.md)

## Global Constraints

- Every order execution is triggered solely by an explicit user click (no autonomous loops or retries).
- Protection is never removed before a replacement is confirmed.
- Unknown/incomplete numbers are flagged, never zeroed.
- Only one broker backend is active per run, selected via `--broker <name>` (default: `schwab`).
- Each broker backend writes to an isolated database: `data/db_<broker>.db`.
- All existing 123+ tests must pass upon completion.

---

### Task 1: Define Backend Contract & Domain Models

**Files:**
- Create: `companion/backends/__init__.py`
- Create: `companion/backends/base.py`
- Create: `companion/backends/registry.py`
- Test: `tests/test_backends.py`

**Interfaces:**
- Consumes: Standard library dataclasses, typing, datetime, decimal.
- Produces:
  - `BrokerBackend`: ABC with abstract methods `connect`, `auth_status`, `auth_hours_left`, `login_cli`, `get_quotes`, `get_market_hours`, `get_accounts`, `get_positions`, `get_balances`, `get_orders`, `get_order_details`, `get_fills`, `preview_bracket`, `place_bracket`, `cancel_order`, `replace_stop`, `replace_with_trail`, `replace_target`, `close_market`.
  - `BrokerCapabilities`: Dataclass (`name`, `supports_two_targets`, `supports_native_trailing_stop`, `rate_limit_per_min`, `has_market_hours_api`).
  - Domain models: `AccountInfo`, `AccountBalances`, `Position`, `Quote`, `MarketHours`, `PendingOrder`, `Fill`, `StopSpec`, `TargetSpec`, `BracketOrder`.
  - Registry: `register_backend(name: str, cls: type[BrokerBackend])`, `get_backend(name: str) -> BrokerBackend`.

- [ ] **Step 1: Write the failing test**
Create `tests/test_backends.py` to verify backend registration, capability inspection, and domain model instantiations.

```python
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from companion.backends.base import (
    BrokerBackend, BrokerCapabilities, BracketOrder, StopSpec, TargetSpec,
    AccountInfo, AccountBalances, Position, Quote, PendingOrder, Fill
)
from companion.backends.registry import register_backend, get_backend

class DummyBackend(BrokerBackend):
    capabilities = BrokerCapabilities(name="dummy", rate_limit_per_min=50)
    def connect(self): pass
    def auth_status(self): return "live"
    def auth_hours_left(self): return 100.0
    def login_cli(self, reauth=False): pass
    def get_quotes(self, symbols): return {}
    def get_market_hours(self, d): pass
    def get_accounts(self): return [AccountInfo("123", "hash123", "Test Acct")]
    def get_positions(self, account_id): return []
    def get_balances(self, account_id): return AccountBalances(equity=10000.0)
    def get_orders(self, account_id, start, end, status=None): return []
    def get_order_details(self, account_id, order_id): return None
    def get_fills(self, account_id, start, end): return []
    def preview_bracket(self, account_id, bracket): return {"ok": True}
    def place_bracket(self, account_id, bracket): return 999
    def cancel_order(self, account_id, order_id): return True
    def replace_stop(self, account_id, order_id, new_stop): return 1001
    def replace_with_trail(self, account_id, order_id, trail_spec): return 1002
    def replace_target(self, account_id, order_id, new_target): return 1003
    def close_market(self, account_id, symbol, side, qty): return 1004

def test_registry_and_dummy_backend():
    register_backend("dummy", DummyBackend)
    b = get_backend("dummy")
    assert b.capabilities.name == "dummy"
    assert b.capabilities.rate_limit_per_min == 50
    assert b.auth_status() == "live"
    assert b.place_bracket("123", None) == 999

def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown broker backend"):
        get_backend("nonexistent")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv/bin/python -m pytest tests/test_backends.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'companion.backends'`

- [ ] **Step 3: Implement `companion/backends/base.py`, `registry.py`, and `__init__.py`**
Implement the dataclasses, `BrokerBackend` ABC, and registry functions according to the spec.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv/bin/python -m pytest tests/test_backends.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add companion/backends/ tests/test_backends.py
git commit -m "feat(backend): add BrokerBackend interface and registry"
```

---

### Task 2: Refactor `bracket.py` to Output Normalized `BracketOrder`

**Files:**
- Modify: `companion/bracket.py`
- Modify: `tests/test_bracket.py`

**Interfaces:**
- Consumes: `BracketOrder`, `StopSpec`, `TargetSpec` from `companion.backends.base`.
- Produces: `to_bracket_order(p: Plan, duration: str = "DAY") -> BracketOrder`.
  (Also keep `bracket_order(p)` as a helper returning `to_bracket_order(p)` or adapter for backwards compatibility during transition).

- [ ] **Step 1: Write the failing test**
Update `tests/test_bracket.py` to add tests for `to_bracket_order()`.

```python
from companion.bracket import plan, to_bracket_order
from companion.backends.base import BracketOrder, StopSpec, TargetSpec

def test_to_bracket_order_single_target():
    p = plan("AVGO", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    bo = to_bracket_order(p)
    assert isinstance(bo, BracketOrder)
    assert bo.symbol == "AVGO"
    assert bo.side == "LONG"
    assert bo.qty == 170
    assert bo.entry_type == "LIMIT"
    assert bo.limit_price == Decimal("338.94")
    assert bo.stop == StopSpec(order_type="STOP", price=Decimal("336.00"))
    assert bo.target == TargetSpec(price=Decimal("344.82"))
    assert bo.target1 is None

def test_to_bracket_order_two_targets():
    p = plan("BRVO", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77", split=0.5, t1_r=1)
    bo = to_bracket_order(p)
    assert bo.qty1 == 85
    assert bo.target1 == TargetSpec(price=Decimal("341.88"))
    assert bo.target == TargetSpec(price=Decimal("344.82"))
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv/bin/python -m pytest tests/test_bracket.py -k test_to_bracket_order -v`
Expected: FAIL with `ImportError: cannot import name 'to_bracket_order'`

- [ ] **Step 3: Implement `to_bracket_order` in `companion/bracket.py`**
Implement `to_bracket_order(p: Plan, exit_duration: str = "DAY") -> BracketOrder`.

- [ ] **Step 4: Run tests to verify all bracket tests pass**
Run: `.venv/bin/python -m pytest tests/test_bracket.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add companion/bracket.py tests/test_bracket.py
git commit -m "feat(bracket): add to_bracket_order emitting normalized BracketOrder"
```

---

### Task 3: Implement `SchwabBackend`

**Files:**
- Create: `companion/backends/schwab.py`
- Modify: `companion/backends/registry.py` (register `schwab` backend by default)
- Test: `tests/test_schwab_backend.py`

**Interfaces:**
- Consumes: `BrokerBackend`, `BrokerCapabilities`, domain models from `companion.backends.base`. Existing logic from `companion/schwab.py`, `orders.py:pending_orders`, `pnl.py:fills_from_transactions`.
- Produces: `SchwabBackend(BrokerBackend)`. Implements all methods of `BrokerBackend`. Translates `BracketOrder` to Schwab `TRIGGER` / `OCO` JSON trees. Handles HTTP 429 rate limits.

- [ ] **Step 1: Write the failing test**
Create `tests/test_schwab_backend.py` to test payload translation and bracket JSON construction.

```python
from decimal import Decimal
import pytest
from companion.backends.base import BracketOrder, StopSpec, TargetSpec
from companion.backends.schwab import SchwabBackend

def test_schwab_bracket_order_json_single_target():
    backend = SchwabBackend(auto_connect=False)
    bo = BracketOrder(
        symbol="AAPL", side="LONG", qty=100, entry_type="LIMIT",
        limit_price=Decimal("220.00"),
        stop=StopSpec(order_type="STOP", price=Decimal("215.00")),
        target=TargetSpec(price=Decimal("230.00")),
    )
    payload = backend._build_schwab_order(bo)
    assert payload["orderStrategyType"] == "TRIGGER"
    assert payload["orderType"] == "LIMIT"
    assert payload["price"] == "220.00"
    assert len(payload["childOrderStrategies"]) == 1
    oco = payload["childOrderStrategies"][0]
    assert oco["orderStrategyType"] == "OCO"
    target, stop = oco["childOrderStrategies"]
    assert target["orderType"] == "LIMIT" and target["price"] == "230.00"
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "215.00"

def test_schwab_bracket_order_json_two_targets():
    backend = SchwabBackend(auto_connect=False)
    bo = BracketOrder(
        symbol="AAPL", side="LONG", qty=100, entry_type="LIMIT",
        limit_price=Decimal("220.00"),
        stop=StopSpec(order_type="STOP", price=Decimal("215.00")),
        target=TargetSpec(price=Decimal("230.00")),
        qty1=40, target1=TargetSpec(price=Decimal("225.00"))
    )
    payload = backend._build_schwab_order(bo)
    assert len(payload["childOrderStrategies"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv/bin/python -m pytest tests/test_schwab_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'companion.backends.schwab'`

- [ ] **Step 3: Implement `companion/backends/schwab.py`**
Implement `SchwabBackend`, migrating `companion/schwab.py` code into it, adding payload translation methods, and registering `"schwab"` in `companion/backends/registry.py`.

- [ ] **Step 4: Run tests to verify it passes**
Run: `.venv/bin/python -m pytest tests/test_schwab_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add companion/backends/schwab.py companion/backends/registry.py tests/test_schwab_backend.py
git commit -m "feat(backend): implement SchwabBackend with order and payload translation"
```

---

### Task 4: Decouple `Poller` to Consume `BrokerBackend`

**Files:**
- Modify: `companion/poller.py`
- Modify: `tests/test_auto_be.py`

**Interfaces:**
- Consumes: `backend: BrokerBackend`.
- Produces: `Poller(backend: BrokerBackend, store: Store)` with all raw `schwabdev` calls replaced by `backend.get_positions()`, `backend.get_orders()`, `backend.get_fills()`, `backend.get_quotes()`, `backend.get_market_hours()`.

- [ ] **Step 1: Write the failing test**
Update `tests/test_auto_be.py` or create a unit test verifying `Poller` operates with a mock `BrokerBackend`.

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv/bin/python -m pytest tests/test_auto_be.py -v`
Expected: Check failure or incompatibility with new `Poller(backend, store)` signature.

- [ ] **Step 3: Refactor `companion/poller.py`**
Inject `backend: BrokerBackend` into `Poller`. Replace direct client calls with `backend` method calls.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv/bin/python -m pytest tests/test_auto_be.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add companion/poller.py tests/test_auto_be.py
git commit -m "refactor(poller): decouple poller from schwab client using BrokerBackend"
```

---

### Task 5: Decouple `Trader` to Consume `BrokerBackend`

**Files:**
- Modify: `companion/trading.py`
- Modify: `tests/test_trading.py`

**Interfaces:**
- Consumes: `backend: BrokerBackend`.
- Produces: `Trader(backend: BrokerBackend, store: Store)`. Replaces `FakeClient` with `MockBackend(BrokerBackend)` in tests.

- [ ] **Step 1: Write the failing test**
Update `tests/test_trading.py` to use a `MockBackend(BrokerBackend)` fixture.

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv/bin/python -m pytest tests/test_trading.py -k test_places_exactly_the_displayed_bracket -v`
Expected: FAIL due to signature or missing backend integration.

- [ ] **Step 3: Refactor `companion/trading.py`**
Inject `backend: BrokerBackend` into `Trader`. Replace `_ensure()`, client order placement/cancellation/replacement with `self.backend` calls.

- [ ] **Step 4: Run tests to verify all trading tests pass**
Run: `.venv/bin/python -m pytest tests/test_trading.py -v`
Expected: PASS (all tests pass against `MockBackend`).

- [ ] **Step 5: Commit**
```bash
git add companion/trading.py tests/test_trading.py
git commit -m "refactor(trading): decouple Trader from schwab client using BrokerBackend"
```

---

### Task 6: Wire CLI `--broker`, Dynamic Database Path, and FastAPI Integration

**Files:**
- Modify: `companion/__main__.py`
- Modify: `companion/app.py`
- Modify: `companion/login.py`
- Modify: `tests/test_schwab_env.py`

**Interfaces:**
- Consumes: `--broker <name>` from CLI or `EDGE_TRADER_BROKER` env var.
- Produces: Backend instantiation via `get_backend(broker_name)`, database path `data/db_<broker>.db` (or `data/demo.db` if `--demo`), and wiring into `poller` and `trader`.

- [ ] **Step 1: Write the failing test**
Add a test in `tests/test_backends.py` for database path resolution and CLI flag parsing.

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv/bin/python -m pytest tests/test_backends.py -k test_cli_broker_selection -v`
Expected: FAIL

- [ ] **Step 3: Implement `--broker` wiring in `__main__.py`, `app.py`, and `login.py`**
Parse `--broker` (defaulting to `"schwab"`), configure database name, instantiate backend, wire into FastAPI app.

- [ ] **Step 4: Run all tests to verify full integration passes**
Run: `.venv/bin/python -m pytest -v`
Expected: PASS (all tests pass cleanly).

- [ ] **Step 5: Commit**
```bash
git add companion/__main__.py companion/app.py companion/login.py tests/test_backends.py
git commit -m "feat(cli): wire --broker flag and isolated database path selection"
```

---

### Task 7: Full System Verification, Regression Tests & Documentation Updates

**Files:**
- Modify: `AGENTS.md`
- Modify: `DEV.md`

**Interfaces:**
- Consumes: All completed tasks and passing tests.
- Produces: Fully updated documentation detailing `--broker`, the `BrokerBackend` interface, and how developers can plug in new brokers.

- [ ] **Step 1: Run the full test suite**
Run: `.venv/bin/python -m pytest -v`
Expected: 125+ tests pass with zero errors.

- [ ] **Step 2: Update `AGENTS.md` and `DEV.md`**
Document the `companion/backends/` package, `BrokerBackend` interface, and the `--broker` CLI argument.

- [ ] **Step 3: Commit documentation updates**
```bash
git add AGENTS.md DEV.md
git commit -m "docs: document pluggable broker backend interface and --broker CLI option"
```
