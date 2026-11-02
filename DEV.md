# DEV.md — Developer & Operations Guide

Welcome to the development guide for **Edge Trader for Schwab**. This document covers environment setup, running modes, test execution, database structure, and troubleshooting.

---

## 1. Quick Start & Prerequisites

### System Requirements
- **Python**: 3.11 or newer (Python 3.11, 3.12 tested).
- **OS**: Windows 10/11, macOS, or Linux.
- **Broker Requirements** (for live trading only):
  - Charles Schwab brokerage account with **thinkorswim enabled**.
  - Registered Schwab Developer App at [developer.schwab.com](https://developer.schwab.com) with:
    - Status: **Ready For Use** (not "Approved - Pending").
    - Products enabled: **Accounts and Trading Production** AND **Market Data Production**.
    - Callback URL set to `https://127.0.0.1`.
    - **Order Limit > 0** (Check under *Modify App*; if 0, all orders fail with HTTP 429).

---

## 2. Environment Setup

### macOS / Linux

```bash
# 1. Clone repository and navigate inside
cd /Volumes/Personal/code/edge-trader-for-schwab

# 2. Create virtual environment (using python3 or uv)
python3 -m venv .venv
# or: uv venv .venv --python 3.12

# 3. Install dependencies
.venv/bin/python -m pip install -r requirements.txt
# or: uv pip install --python .venv/bin/python -r requirements.txt

# 4. Configure .env
cp .env.example .env
# Open .env in your editor and enter SCHWAB_APP_KEY and SCHWAB_APP_SECRET
```

### Windows

You can use the bundled batch scripts:
1. Double-click `setup.bat` (creates `.venv`, installs dependencies, and creates `.env`).
2. Edit `.env` to insert your Schwab app key and secret.
3. Run `login.bat` to authorize Schwab access.
4. Run `launch.bat` to start the app.

---

## 3. Configuration Variables (`.env`)

| Variable | Description | Default |
|---|---|---|
| `EDGE_TRADER_BROKER` | Name of active broker backend (`schwab`, etc.) | `schwab` |
| `SCHWAB_APP_KEY` | Your Schwab Developer App Key (Required for live Schwab mode) | *None* |
| `SCHWAB_APP_SECRET` | Your Schwab Developer App Secret (Required for live Schwab mode) | *None* |
| `SCHWAB_CALLBACK_URL` | Registered Callback URL on your Schwab App | `https://127.0.0.1` |
| `SCHWAB_TOKENS_DB` | Path where Schwabdev stores token SQLite database | `~/.schwabdev/tokens.db` |
| `SCHWAB_ENV_FILE` | Path to another app's `.env` to share credentials | *None* |
| `COMPANION_PORT` | Local HTTP port for the FastAPI server | `8770` |
| `EDGE_TRADER_NO_UPDATE_CHECK` | Set to `1` to disable daily GitHub release checks | `0` |

---

## 4. Authentication Workflow (`companion.login`)

To sign in and initialize broker authentication:

```bash
# Default Schwab sign-in:
.venv/bin/python -m companion.login

# Force a brand new sign-in prompt even if tokens are currently valid:
.venv/bin/python -m companion.login --reauth

# Sign in with a specific broker backend:
.venv/bin/python -m companion.login --broker schwab
```

### How Schwab Authentication Works:
1. `companion.login` opens Schwab's OAuth sign-in page in your browser.
2. Sign in with your Schwab brokerage credentials and approve the application permissions.
3. The browser will redirect to `https://127.0.0.1/...`. This page will intentionally display a connection/SSL error — **this is normal**.
4. Copy the complete URL from your browser's address bar (must include `https://`) and paste it into the terminal within ~30 seconds.
5. Tokens are persisted to `~/.schwabdev/tokens.db`.
6. Schwab refresh tokens are valid for **7 days**. Run `login.bat` / `python -m companion.login` once a week.

---

## 5. Running the Application

### 1. Live Trading Mode (Production)
```bash
# Default Schwab backend:
.venv/bin/python -m companion

# Specify broker backend via CLI flag:
.venv/bin/python -m companion --broker schwab

# Or via environment variable:
EDGE_TRADER_BROKER=schwab .venv/bin/python -m companion
```
- Listens at `http://127.0.0.1:8770`.
- Background poller connects to the selected broker backend and streams quotes/positions to local cache.
- Each broker persists to its own isolated database at `data/db_<broker>.db` (e.g. `data/db_schwab.db`).
- Trading is **disarmed by default** on startup. You must toggle the ARM switch in the UI before placing orders.

### 2. Demo Mode (Mock / No Broker Connection)
```bash
.venv/bin/python -m companion --demo
```
- **Zero connection to any broker**: Uses purely synthetic mock data defined in `companion/demo.py`.
- **Safe for UI development & testing**: Every order action is blocked.
- Uses an isolated database at `data/demo.db` so live broker data is completely untouched.
- Perfect for UI adjustments, styling, and testing layout responsiveness.

### 3. Screen Privacy Mode (Recording Mode)
```bash
.venv/bin/python -m companion --private
```
- Hides carried overnight positions, prior session orders, overnight P&L, and account liquidation balance.
- Shows only intraday trades opened during the current session.
- Useful for video recording or livestreaming while keeping total account size private.

### 4. Floating Windows (pywebview)
Small, always-on-top companion windows designed to sit directly alongside your charting platform:
```bash
# Floating Entry Ticket
.venv/bin/python -m companion.floating entry

# Floating Manage Position Workbench
.venv/bin/python -m companion.floating manage
```
- Geometry and pinned status are automatically remembered in `data/floating.json`.

---

## 6. Testing & Quality Assurance

### Running the Test Suite
Always invoke pytest via `python -m pytest` so `sys.path` correctly includes the root repository directory:

```bash
# Run all tests quietly
.venv/bin/python -m pytest -q

# Run all tests with verbose output
.venv/bin/python -m pytest -v

# Run a specific test file
.venv/bin/python -m pytest tests/test_trading.py -v
```

### Test Suite Map:
- **`tests/test_backends.py`**: Validates the `BrokerBackend` abstraction contract, normalized immutable domain models (`Quote`, `Position`, `PendingOrder`, `Fill`, `MarketHours`), registry lookup (`get_backend`, `register_backend`), database path resolution (`resolve_db_path`), and `--broker` CLI parsing.
- **`tests/test_trading.py`**: Validates the `Trader` engine against a simulated mock broker (`MockBackend(BrokerBackend)`). Tests submission, two-target brackets, reconciliation, stale quote rejection, order duplicate guards, session arming timeouts, stop replacement, trailing stop creation, and partial fills.
- **`tests/test_bracket.py`**: Verifies pure domain mathematical sizing, tick step rounding, long/short limits, two-target share splits, and normalized `BracketOrder` construction.
- **`tests/test_pnl.py`**: Tests FIFO lot building, options (100x multiplier), futures contract multipliers, partial closes, short closes, and opening inventory confidence tagging.
- **`tests/test_orders.py`**: Tests pending order tree traversal, OCO alternative semantics, and multi-leg order parsing.
- **`tests/test_auto_be.py`**: Validates the poller's Auto B/E 1R trigger logic, timestamp filters, and single-fire idempotency.
- **`tests/test_schwab_env.py`**: Verifies `.env` key loading and priority fallbacks.
- **`tests/test_update_check.py`**: Verifies GitHub release tag comparison and caching.

---

## 7. Database & File Persistence

All application data is stored in the local `data/` directory (ignored by git):

- **`data/db_<broker>.db`** (e.g. `data/db_schwab.db`): Isolated SQLite database per broker storing:
  - `transactions`: Fill activity records from the broker.
  - `orders`: Raw broker order structures and status tracking.
  - `position_snapshots`: Historical position states.
  - `quotes`: Batched quote samples.
  - `requests`: HTTP latency, status codes, and error logging for broker API calls.
  - `tickets`: Locally generated orders and reconciliation records.
  - `auto_be`: State machine for Auto B/E 1R tickets.
  - `actions`: Audit log of user actions (moves, cancels, partials).
- **`data/demo.db`**: Isolated SQLite database used when running with `--demo`.
- **`data/companion.log`**: Standard output and error log from the FastAPI process.
- **`data/floating.json`**: Window coordinates and sizes for floating pywebview windows.

### Inspecting SQLite Database:
```bash
sqlite3 data/db_schwab.db ".tables"
sqlite3 data/db_schwab.db "SELECT * FROM tickets ORDER BY created DESC LIMIT 5;"
sqlite3 data/db_schwab.db "SELECT endpoint, http, error, started FROM requests ORDER BY started DESC LIMIT 10;"
```

---

## 8. Frontend Architecture (`companion/static/index.html`)

- The entire frontend is housed in `companion/static/index.html`.
- Implemented with clean Vanilla JavaScript, responsive CSS, and native web APIs (no heavy node/npm build step required).
- Polls `/api/state` once per second (`setInterval(poll, 1000)`).
- When submitting actions (POST requests), always passes:
  - `headers: { 'Content-Type': 'application/json', 'X-Companion': '1' }`
- Keyboard shortcuts:
  - `Ctrl+Shift+A` (or clicking arm chip): Toggles ARM status.
  - Configurable in preferences (`/api/ui/prefs`).

### Retaking Documentation Screenshots
When UI updates are made, update documentation screenshots using Playwright:
```bash
# In one terminal, start demo server:
.venv/bin/python -m companion --demo

# In another terminal:
.venv/bin/python scripts/capture_screenshots.py
```
This updates `docs/screenshot-dashboard.png`, `docs/screenshot-workbench.png`, etc.

---

## 9. Troubleshooting & Common Pitfalls

### 1. `ModuleNotFoundError: No module named 'companion'`
- **Cause**: Running `pytest` instead of `python -m pytest`.
- **Solution**: Always use `.venv/bin/python -m pytest` or set `export PYTHONPATH=.`.

### 2. Schwab HTTP 429 ("Too Many Requests")
- **Cause 1**: Exceeding the request rate limit. Edge Trader throttles to 70 req/min, but other tools (or thinkorswim API plugins) might be sharing the quota.
- **Cause 2**: **Order Limit is set to 0** on your Schwab Developer App. Open [developer.schwab.com](https://developer.schwab.com), edit your app ("Modify App"), and verify the Order Limit field is > 0.

### 3. `AuthExpired: Schwab login expired or expiring`
- **Cause**: The 7-day OAuth refresh token has expired or is within 2 hours of expiry.
- **Solution**: Run `python -m companion.login` (or `login.bat`) to refresh tokens in the browser.

### 4. Port Conflict (`COMPANION_PORT`)
- If port 8770 is already in use, set `COMPANION_PORT=8775` in your `.env` file.
