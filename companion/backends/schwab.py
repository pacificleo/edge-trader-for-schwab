"""Charles Schwab Trader API backend implementation of BrokerBackend."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import time
from typing import Any, Literal

from .base import (
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
)

TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"})
STOP_TYPES = frozenset({"STOP", "STOP_LIMIT", "TRAILING_STOP", "TRAILING_STOP_LIMIT"})
OPENING_INSTRUCTIONS = frozenset({"SELL_SHORT", "BUY_TO_OPEN", "SELL_TO_OPEN"})
EPS = 1e-9


def _to_dec(val: Decimal | float | int | str) -> Decimal:
    return val if isinstance(val, Decimal) else Decimal(str(val))


def fmt(price: Decimal) -> str:
    """Format price to standard Charles Schwab tick string."""
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


def parse_retry_after(value: str | float | None) -> float:
    """Parse HTTP Retry-After header as seconds float, defaulting to 30.0s."""
    if value is None:
        return 30.0
    if isinstance(value, (int, float)):
        return max(1.0, float(value))
    try:
        return max(1.0, float(value))
    except ValueError:
        try:
            return max(1.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 30.0


def _parse_time_field(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e11:  # epoch in milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            try:
                return datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                return None
    return None


def _order_role(order_type: str, effect: str | None, instruction: str) -> Literal["ENTRY", "STOP", "TARGET", "EXIT"]:
    if effect == "OPENING" or instruction in OPENING_INSTRUCTIONS:
        return "ENTRY"
    if order_type in STOP_TYPES:
        return "STOP"
    if order_type == "LIMIT":
        return "TARGET"
    return "EXIT"


def _walk_orders(
    o: dict,
    group_id: int,
    group_type: str,
    parent_id: int | None,
    out: list[PendingOrder],
    oco_id: int | None = None,
) -> None:
    status = o.get("status", "?")
    legs = o.get("orderLegCollection") or []
    if legs:
        order_qty = float(o.get("quantity") or 0.0)
        remaining = float(o.get("remainingQuantity")) if o.get("remainingQuantity") is not None else order_qty
        order_type = o.get("orderType", "?")
        for i, leg in enumerate(legs):
            ins = leg.get("instrument") or {}
            leg_qty = float(leg.get("quantity")) if leg.get("quantity") is not None else order_qty
            ratio = (leg_qty / order_qty) if order_qty else 1.0
            effect = leg.get("positionEffect")
            instruction = leg.get("instruction", "?")
            out.append(
                PendingOrder(
                    order_id=o["orderId"],
                    leg_id=leg.get("legId", i + 1),
                    legs=len(legs),
                    group_id=group_id,
                    group_type=group_type,
                    parent_id=parent_id,
                    oco_id=oco_id,
                    status=status,
                    symbol=ins.get("symbol", "?"),
                    underlying=ins.get("underlyingSymbol") or ins.get("symbol", "?"),
                    asset_type=ins.get("assetType", "?"),
                    instruction=instruction,
                    effect=effect,
                    order_type=order_type,
                    qty=remaining * ratio,
                    filled=float(o.get("filledQuantity") or 0.0),
                    price=float(o.get("price")) if o.get("price") is not None else None,
                    stop=float(o.get("stopPrice")) if o.get("stopPrice") is not None else None,
                    duration=o.get("duration", "?"),
                    entered=o.get("enteredTime", ""),
                    tag=o.get("tag"),
                    role=_order_role(order_type, effect, instruction),
                    trail_offset=float(o.get("stopPriceOffset")) if o.get("stopPriceOffset") is not None else None,
                    trail_link=o.get("stopPriceLinkType"),
                    trail_basis=o.get("stopPriceLinkBasis"),
                )
            )
    child_oco = o.get("orderId") if o.get("orderStrategyType") == "OCO" else oco_id
    for child in o.get("childOrderStrategies") or []:
        _walk_orders(child, group_id, group_type, o.get("orderId"), out, child_oco)


def _collect_terminal_ids(orders: list[dict]) -> set[int]:
    seen: dict[int, set[str]] = {}

    def _walk(o: dict) -> None:
        oid = o.get("orderId")
        if oid is not None:
            seen.setdefault(oid, set()).add(o.get("status", "?"))
        for child in o.get("childOrderStrategies") or []:
            _walk(child)

    for o in orders:
        _walk(o)
    return {oid for oid, statuses in seen.items() if statuses & TERMINAL_STATUSES}


class SchwabBackend(BrokerBackend):
    """Charles Schwab brokerage implementation."""

    capabilities = BrokerCapabilities(
        name="schwab",
        supports_two_targets=True,
        supports_native_trailing_stop=True,
        rate_limit_per_min=70,
        has_market_hours_api=True,
    )

    def __init__(self, auto_connect: bool = False, client: Any = None):
        self.client = client
        self._cooldown_until: float = 0.0
        if auto_connect and self.client is None:
            self.connect()

    def __getattr__(self, name: str) -> Any:
        if self.client is not None and hasattr(self.client, name):
            return getattr(self.client, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # -----------------------------------------------------------------------
    # Cooldown & Rate Limiting
    # -----------------------------------------------------------------------

    @property
    def cooldown_until(self) -> float:
        return self._cooldown_until

    @property
    def is_rate_limited(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def handle_429(self, retry_after: str | float | None = None) -> float:
        cd = parse_retry_after(retry_after)
        self._cooldown_until = time.monotonic() + cd
        return cd

    def _handle_response(self, r: Any) -> Any:
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            if r.status_code not in (200, 201):
                text = getattr(r, "text", "")
                raise RuntimeError(f"Schwab API error HTTP {r.status_code}: {text[:300]}")
            if hasattr(r, "json"):
                try:
                    return r.json()
                except Exception:
                    return {}
        return r

    # -----------------------------------------------------------------------
    # Order Translation
    # -----------------------------------------------------------------------

    def _build_schwab_order(self, bracket: BracketOrder) -> dict:
        entry_instr, exit_instr = (
            ("BUY", "SELL") if bracket.side == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
        )
        instrument = {"symbol": bracket.symbol, "assetType": "EQUITY"}

        def leg(instruction: str, qty: int) -> list[dict]:
            return [{"instruction": instruction, "quantity": qty, "instrument": instrument}]

        def stop_child(stop: StopSpec, legs: list[dict], duration: str = "DAY") -> dict:
            if stop.order_type == "TRAILING_STOP":
                return {
                    "orderStrategyType": "SINGLE",
                    "orderType": "TRAILING_STOP",
                    "session": "NORMAL",
                    "duration": duration,
                    "stopPriceLinkBasis": stop.trail_basis,
                    "stopPriceLinkType": stop.trail_link,
                    "stopPriceOffset": float(stop.trail_offset) if stop.trail_offset is not None else 0.0,
                    "orderLegCollection": legs,
                }
            if stop.price is None:
                raise ValueError("STOP order requires price")
            return {
                "orderStrategyType": "SINGLE",
                "orderType": "STOP",
                "session": "NORMAL",
                "duration": duration,
                "stopPrice": fmt(_to_dec(stop.price)),
                "orderLegCollection": legs,
            }

        def oco(qty: int, target: TargetSpec, stop: StopSpec) -> dict:
            return {
                "orderStrategyType": "OCO",
                "childOrderStrategies": [
                    {
                        "orderStrategyType": "SINGLE",
                        "orderType": "LIMIT",
                        "session": "NORMAL",
                        "duration": bracket.duration,
                        "price": fmt(_to_dec(target.price)),
                        "orderLegCollection": leg(exit_instr, qty),
                    },
                    stop_child(stop, leg(exit_instr, qty), bracket.duration),
                ],
            }

        if bracket.qty1 is not None and bracket.target1 is not None and bracket.qty1 > 0:
            exits = [
                oco(bracket.qty1, bracket.target1, bracket.stop),
                oco(bracket.qty - bracket.qty1, bracket.target, bracket.stop),
            ]
        else:
            exits = [oco(bracket.qty, bracket.target, bracket.stop)]

        order: dict[str, Any] = {
            "orderStrategyType": "TRIGGER",
            "orderType": bracket.entry_type,
            "session": "NORMAL",
            "duration": "DAY",
            "orderLegCollection": leg(entry_instr, bracket.qty),
            "childOrderStrategies": exits,
        }
        if bracket.entry_type == "LIMIT":
            if bracket.limit_price is None:
                raise ValueError("LIMIT entry requires limit_price")
            order["price"] = fmt(_to_dec(bracket.limit_price))

        return order

    # -----------------------------------------------------------------------
    # Inbound Payload Parsing
    # -----------------------------------------------------------------------

    def _parse_positions(self, account_data: dict) -> list[Position]:
        sec_acct = account_data.get("securitiesAccount", account_data) if isinstance(account_data, dict) else {}
        positions_raw = sec_acct.get("positions", []) if isinstance(sec_acct, dict) else []
        out: list[Position] = []

        for p in positions_raw:
            ins = p.get("instrument") or {}
            sym = ins.get("symbol", "?")
            raw_asset = (ins.get("assetType") or "EQUITY").upper()
            asset_type: Literal["EQUITY", "OPTION", "FUTURE"] = (
                raw_asset if raw_asset in ("EQUITY", "OPTION", "FUTURE") else "EQUITY"
            )
            underlying = ins.get("underlyingSymbol") or sym

            long_qty = float(p.get("longQuantity") or 0.0)
            short_qty = float(p.get("shortQuantity") or 0.0)
            net_qty = long_qty - short_qty
            if abs(net_qty) < EPS:
                continue

            side: Literal["LONG", "SHORT"] = "LONG" if net_qty > 0 else "SHORT"
            size = abs(net_qty)
            avg_price = float(p.get("averagePrice") or 0.0)
            market_value = float(p.get("marketValue") or 0.0)

            open_pnl_val = p.get("longOpenProfitLoss") if side == "LONG" else p.get("shortOpenProfitLoss")
            if open_pnl_val is None:
                open_pnl_val = p.get("openProfitLoss", 0.0)
            open_pnl = float(open_pnl_val or 0.0)

            day_pnl = float(p.get("currentDayProfitLoss") or 0.0)

            prev_long = float(p.get("previousSessionLongQuantity") or 0.0)
            prev_short = float(p.get("previousSessionShortQuantity") or 0.0)
            prev = prev_long if side == "LONG" else prev_short
            cost_today = float(p.get("currentDayCost") or 0.0)

            if prev < EPS:
                held: Literal["INTRADAY", "CARRIED", "MIXED"] = "INTRADAY"
            elif abs(prev - size) < EPS and cost_today == 0.0:
                held = "CARRIED"
            else:
                held = "MIXED"

            out.append(
                Position(
                    symbol=sym,
                    asset_type=asset_type,
                    underlying=underlying,
                    side=side,
                    qty=size,
                    avg_price=avg_price,
                    market_value=market_value,
                    open_pnl=open_pnl,
                    day_pnl=day_pnl,
                    held=held,
                    previous_session_qty=prev,
                )
            )
        return out

    def _parse_balances(self, account_data: dict) -> AccountBalances:
        sec_acct = account_data.get("securitiesAccount", account_data) if isinstance(account_data, dict) else {}
        cb = sec_acct.get("currentBalances", {}) if isinstance(sec_acct, dict) else {}

        def _f(v: Any) -> float | None:
            return float(v) if v is not None else None

        return AccountBalances(
            liquidation_value=_f(cb.get("liquidationValue")),
            equity=_f(cb.get("equity")),
            buying_power=_f(cb.get("buyingPower")),
            day_trading_buying_power=_f(cb.get("dayTradingBuyingPower")),
            available_funds=_f(cb.get("availableFunds")),
            maintenance_requirement=_f(cb.get("maintenanceRequirement")),
        )

    def _parse_orders(self, orders_data: list[dict]) -> list[PendingOrder]:
        if not orders_data:
            return []
        done = _collect_terminal_ids(orders_data)
        out: list[PendingOrder] = []
        for o in orders_data:
            _walk_orders(o, o.get("orderId", 0), o.get("orderStrategyType", "SINGLE"), None, out)
        deduped: dict[tuple[int, int], PendingOrder] = {}
        for p in out:
            if p.order_id in done and p.status not in TERMINAL_STATUSES:
                continue
            deduped[(p.order_id, p.leg_id)] = p
        return sorted(
            deduped.values(),
            key=lambda p: (p.underlying, p.group_id, p.role, p.order_id, p.leg_id),
        )

    def _parse_fills(self, txns_data: list[dict]) -> list[Fill]:
        if not txns_data:
            return []
        seen: set[str] = set()
        out: list[Fill] = []

        for t in txns_data:
            if t.get("type") != "TRADE" or t.get("status") not in (None, "VALID"):
                continue
            items = t.get("transferItems") or []
            fees = sum(
                float(i.get("cost") or 0.0)
                for i in items
                if (i.get("instrument") or {}).get("assetType") == "CURRENCY"
            )
            for idx, it in enumerate(items):
                ins = it.get("instrument") or {}
                if ins.get("assetType") in (None, "CURRENCY"):
                    continue
                qty = float(it.get("amount") or 0.0)
                price = float(it.get("price") or 0.0)
                if abs(qty) < EPS or price <= 0:
                    continue
                cost = float(it.get("cost") or 0.0)
                mult = round(abs(cost / (qty * price)), 4) if cost else 1.0
                symbol = ins.get("quoteSymbol") or ins.get("symbol", "?")
                if ins.get("assetType") == "FUTURE":
                    symbol = symbol.split(":")[0]

                fill_id = f"{t.get('activityId')}:{idx}"
                if fill_id in seen:
                    continue
                seen.add(fill_id)

                fill_time = _parse_time_field(t.get("time")) or datetime.now(timezone.utc)
                has_earlier_fill_for_txn = any(f.id.startswith(f"{t.get('activityId')}:") for f in out)
                fill_fees = min(fees, 0.0) if not has_earlier_fill_for_txn else 0.0

                out.append(
                    Fill(
                        id=fill_id,
                        order_id=t.get("orderId"),
                        time=fill_time,
                        symbol=symbol,
                        asset_type=ins.get("assetType", "?"),
                        underlying=ins.get("underlyingSymbol") or ins.get("rootSymbol") or symbol,
                        qty=qty,
                        price=price,
                        multiplier=mult or 1.0,
                        fees=fill_fees,
                        effect=it.get("positionEffect"),
                    )
                )

        def _seq(fill_id: str) -> tuple[int, int]:
            parts = fill_id.split(":")
            try:
                return (int(parts[0]), int(parts[1]))
            except (ValueError, IndexError):
                return (0, 0)

        return sorted(out, key=lambda f: (f.time, _seq(f.id)))

    def _parse_quotes(self, quotes_data: dict) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        if not isinstance(quotes_data, dict):
            return out

        for sym, val in quotes_data.items():
            if not isinstance(val, dict):
                continue
            qq = val.get("quote", val)
            if not isinstance(qq, dict):
                continue
            bid = float(qq.get("bidPrice") or 0.0)
            ask = float(qq.get("askPrice") or 0.0)
            last = float(qq.get("lastPrice") or 0.0)
            mark = float(qq.get("mark") or last or ((bid + ask) / 2 if (bid and ask) else 0.0))
            volume = float(qq.get("totalVolume") or 0.0)
            change_pct = float(qq.get("netPercentChange")) if qq.get("netPercentChange") is not None else None
            qt = _parse_time_field(qq.get("quoteTime"))
            out[sym] = Quote(
                symbol=sym,
                bid=bid,
                ask=ask,
                last=last,
                mark=mark,
                volume=volume,
                quote_time=qt,
                change_pct=change_pct,
            )
        return out

    def _parse_market_hours(self, hours_data: dict, d: date) -> MarketHours:
        if not isinstance(hours_data, dict):
            return MarketHours(date=d, is_open=False)
        eq = hours_data.get("equity") or hours_data
        if isinstance(eq, dict) and "isOpen" not in eq:
            eq = next(iter(eq.values()), {}) if eq else {}
        is_open = bool(eq.get("isOpen", False))
        open_time = None
        close_time = None
        if is_open:
            reg = (eq.get("sessionHours") or {}).get("regularMarket") or []
            if reg:
                start_str = reg[0].get("start")
                end_str = reg[-1].get("end")
                if start_str:
                    open_time = _parse_time_field(start_str)
                if end_str:
                    close_time = _parse_time_field(end_str)
        return MarketHours(date=d, is_open=is_open, open_time=open_time, close_time=close_time)

    # -----------------------------------------------------------------------
    # BrokerBackend Abstract Method Implementations
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        from .. import schwab
        self.client = schwab.make_backend_client()

    def auth_status(self) -> str:
        from .. import schwab
        left = self.auth_hours_left()
        if left is None:
            return "needs_login"
        if left < schwab.SAFETY_HOURS:
            return "expired"
        return "live"

    def auth_hours_left(self) -> float | None:
        from .. import schwab
        return schwab.auth_hours_left()

    def login_cli(self, reauth: bool = False) -> None:
        from ..login import login_schwab
        login_schwab(reauth=reauth)

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols or self.client is None:
            return {}
        resp = self.client.quotes(symbols)
        data = self._handle_response(resp)
        return self._parse_quotes(data) if isinstance(data, dict) else {}

    def get_market_hours(self, d: date) -> MarketHours:
        if self.client is None:
            return MarketHours(date=d, is_open=False)
        resp = self.client.market_hours(["equity"], date=d.isoformat())
        data = self._handle_response(resp)
        return self._parse_market_hours(data, d) if isinstance(data, dict) else MarketHours(date=d, is_open=False)

    def get_accounts(self) -> list[AccountInfo]:
        if self.client is None:
            return []
        resp = self.client.linked_accounts()
        data = self._handle_response(resp)
        if not isinstance(data, list):
            return []
        out: list[AccountInfo] = []
        for a in data:
            acct_id = str(a.get("accountNumber", ""))
            hash_val = str(a.get("hashValue", ""))
            name = a.get("displayName") or f"Account ...{acct_id[-4:] if acct_id else ''}"
            out.append(AccountInfo(account_id=acct_id, hash_value=hash_val, display_name=name))
        return out

    def get_positions(self, account_id: str) -> list[Position]:
        if self.client is None:
            return []
        resp = self.client.account_details(account_id, fields="positions")
        data = self._handle_response(resp)
        return self._parse_positions(data) if isinstance(data, dict) else []

    def get_balances(self, account_id: str) -> AccountBalances:
        if self.client is None:
            return AccountBalances()
        resp = self.client.account_details(account_id, fields="positions")
        data = self._handle_response(resp)
        return self._parse_balances(data) if isinstance(data, dict) else AccountBalances()

    def get_orders(
        self, account_id: str, start: datetime, end: datetime, status: str | None = None
    ) -> list[PendingOrder]:
        if self.client is None:
            return []
        if status:
            resp = self.client.account_orders(account_id, start, end, status=status)
        else:
            resp = self.client.account_orders(account_id, start, end)
        data = self._handle_response(resp)
        return self._parse_orders(data) if isinstance(data, list) else []

    def get_order_details(self, account_id: str, order_id: int) -> PendingOrder | None:
        if self.client is None:
            return None
        resp = self.client.order_details(account_id, order_id)
        data = self._handle_response(resp)
        if isinstance(data, dict):
            orders = self._parse_orders([data])
            return next((o for o in orders if o.order_id == order_id), orders[0] if orders else None)
        return None

    def get_fills(self, account_id: str, start: datetime, end: datetime) -> list[Fill]:
        if self.client is None:
            return []
        resp = self.client.transactions(account_id, start, end, "TRADE")
        data = self._handle_response(resp)
        return self._parse_fills(data) if isinstance(data, list) else []

    def preview_bracket(self, account_id: str, bracket: BracketOrder) -> dict:
        if self.client is None:
            raise RuntimeError("Client not connected")
        payload = self._build_schwab_order(bracket)
        resp = self.client.preview_order(account_id, payload)
        return self._handle_response(resp)

    def place_bracket(self, account_id: str, bracket: BracketOrder) -> int:
        if self.client is None:
            raise RuntimeError("Client not connected")
        payload = self._build_schwab_order(bracket)
        r = self.client.place_order(account_id, payload)
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Schwab rejected order: HTTP {r.status_code} {getattr(r, 'text', '')[:300]}")
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1] if hasattr(r, "headers") else ""
            if tail.isdigit():
                return int(tail)
        return 0

    def cancel_order(self, account_id: str, order_id: int) -> bool:
        if self.client is None:
            raise RuntimeError("Client not connected")
        r = self.client.cancel_order(account_id, order_id)
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            return r.status_code in (200, 201)
        return True

    def replace_stop(self, account_id: str, order_id: int, new_stop: float) -> int:
        if self.client is None:
            raise RuntimeError("Client not connected")
        cur_resp = self.client.order_details(account_id, order_id)
        cur = (
            cur_resp.json()
            if hasattr(cur_resp, "json")
            else (cur_resp if isinstance(cur_resp, dict) else {})
        )
        legs = cur.get("orderLegCollection") or []
        body = {
            "orderStrategyType": "SINGLE",
            "orderType": cur.get("orderType", "STOP"),
            "session": cur.get("session", "NORMAL"),
            "duration": cur.get("duration", "DAY"),
            "stopPrice": fmt(_to_dec(new_stop)),
            "orderLegCollection": [
                {
                    "instruction": l.get("instruction"),
                    "quantity": int(l.get("quantity"))
                    if float(l.get("quantity") or 0).is_integer()
                    else l.get("quantity"),
                    "instrument": {
                        "symbol": (l.get("instrument") or {}).get("symbol"),
                        "assetType": (l.get("instrument") or {}).get("assetType", "EQUITY"),
                    },
                }
                for l in legs
            ],
        }
        if cur.get("orderType") == "STOP_LIMIT" and cur.get("price") is not None:
            body["price"] = cur.get("price")

        r = self.client.replace_order(account_id, order_id, body)
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Schwab rejected replace: HTTP {r.status_code} {getattr(r, 'text', '')[:300]}")
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1] if hasattr(r, "headers") else ""
            if tail.isdigit():
                return int(tail)
        return order_id

    def replace_with_trail(self, account_id: str, order_id: int, trail_spec: dict) -> int:
        if self.client is None:
            raise RuntimeError("Client not connected")
        cur_resp = self.client.order_details(account_id, order_id)
        cur = (
            cur_resp.json()
            if hasattr(cur_resp, "json")
            else (cur_resp if isinstance(cur_resp, dict) else {})
        )
        legs = cur.get("orderLegCollection") or []
        body = {
            "orderStrategyType": "SINGLE",
            "orderType": "TRAILING_STOP",
            "session": cur.get("session", "NORMAL"),
            "duration": cur.get("duration", "DAY"),
            "stopPriceLinkBasis": trail_spec.get("basis", "MARK"),
            "stopPriceLinkType": trail_spec.get("link", "VALUE"),
            "stopPriceOffset": float(trail_spec.get("offset") or 0.0),
            "orderLegCollection": [
                {
                    "instruction": l.get("instruction"),
                    "quantity": int(l.get("quantity"))
                    if float(l.get("quantity") or 0).is_integer()
                    else l.get("quantity"),
                    "instrument": {
                        "symbol": (l.get("instrument") or {}).get("symbol"),
                        "assetType": (l.get("instrument") or {}).get("assetType", "EQUITY"),
                    },
                }
                for l in legs
            ],
        }

        r = self.client.replace_order(account_id, order_id, body)
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Schwab rejected replace with trail: HTTP {r.status_code} {getattr(r, 'text', '')[:300]}")
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1] if hasattr(r, "headers") else ""
            if tail.isdigit():
                return int(tail)
        return order_id

    def replace_target(self, account_id: str, order_id: int, new_target: float) -> int:
        if self.client is None:
            raise RuntimeError("Client not connected")
        cur_resp = self.client.order_details(account_id, order_id)
        cur = (
            cur_resp.json()
            if hasattr(cur_resp, "json")
            else (cur_resp if isinstance(cur_resp, dict) else {})
        )
        legs = cur.get("orderLegCollection") or []
        body = {
            "orderStrategyType": "SINGLE",
            "orderType": "LIMIT",
            "session": cur.get("session", "NORMAL"),
            "duration": cur.get("duration", "DAY"),
            "price": fmt(_to_dec(new_target)),
            "orderLegCollection": [
                {
                    "instruction": l.get("instruction"),
                    "quantity": int(l.get("quantity"))
                    if float(l.get("quantity") or 0).is_integer()
                    else l.get("quantity"),
                    "instrument": {
                        "symbol": (l.get("instrument") or {}).get("symbol"),
                        "assetType": (l.get("instrument") or {}).get("assetType", "EQUITY"),
                    },
                }
                for l in legs
            ],
        }

        r = self.client.replace_order(account_id, order_id, body)
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Schwab rejected replace target: HTTP {r.status_code} {getattr(r, 'text', '')[:300]}")
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1] if hasattr(r, "headers") else ""
            if tail.isdigit():
                return int(tail)
        return order_id

    def close_market(self, account_id: str, symbol: str, side: str, qty: float) -> int:
        if self.client is None:
            raise RuntimeError("Client not connected")
        instr = "SELL" if side.upper() == "LONG" else "BUY_TO_COVER"
        size = abs(qty)
        order = {
            "orderStrategyType": "SINGLE",
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderLegCollection": [
                {
                    "instruction": instr,
                    "quantity": int(size) if size.is_integer() else size,
                    "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                }
            ],
        }
        r = self.client.place_order(account_id, order)
        if hasattr(r, "status_code"):
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                self.handle_429(ra)
                raise RuntimeError(f"Rate limited by Schwab (HTTP 429). Retry after {ra or 30}s")
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Schwab rejected close: HTTP {r.status_code} {getattr(r, 'text', '')[:300]}")
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1] if hasattr(r, "headers") else ""
            if tail.isdigit():
                return int(tail)
        return 0
