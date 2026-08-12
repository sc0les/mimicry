"""IBKR contract planning for Mimicry equity and ETP positions."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "secrets.env")

IBKR_CONTRACT_CACHE_FILE = ROOT / "ibkr_contract_cache.json"

US_EXCH_CODES = {"US", "UA", "UN", "UQ", "UP", "UR", "UT"}


@dataclass(frozen=True)
class ExchangeSpec:
    ibkr_exchange: str
    currency: str
    country: str
    primary_exchange: str | None = None
    default_lot_size: int = 1
    needs_contract_details: bool = False
    price_multiplier: float = 1.0


@dataclass(frozen=True)
class ContractPlan:
    figi: str
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    sec_type: str
    country: str
    lot_size: int
    name: str
    source_exch_code: str
    needs_contract_details: bool
    con_id: int | None = None
    trading_class: str | None = None
    local_symbol: str | None = None
    qualified_exchange: str | None = None
    min_tick: float | None = None
    price_multiplier: float = 1.0


EXCHANGE_SPECS = {
    "US": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "UA": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "UN": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "UQ": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "UP": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "UR": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "UT": ExchangeSpec("SMART", "USD", "United States", primary_exchange=""),
    "AU": ExchangeSpec("ASX", "AUD", "Australia"),
    "BB": ExchangeSpec("ENEXT.BE", "EUR", "Belgium", needs_contract_details=True),
    "FH": ExchangeSpec("HEX", "EUR", "Finland"),
    "JP": ExchangeSpec("TSEJ", "JPY", "Japan", default_lot_size=100),
    "HK": ExchangeSpec("SEHK", "HKD", "Hong Kong", default_lot_size=1, needs_contract_details=True),
    "TT": ExchangeSpec("TWSE", "TWD", "Taiwan", default_lot_size=1000),
    "FP": ExchangeSpec("SBF", "EUR", "France"),
    "GR": ExchangeSpec("IBIS", "EUR", "Germany"),
    "DC": ExchangeSpec("CPH", "DKK", "Denmark"),
    "NA": ExchangeSpec("AEB", "EUR", "Netherlands"),
    "NO": ExchangeSpec("OSE", "NOK", "Norway"),
    "IM": ExchangeSpec("BVME", "EUR", "Italy"),
    "CN": ExchangeSpec("TSE", "CAD", "Canada"),
    "CN:XTSX": ExchangeSpec("VENTURE", "CAD", "Canada"),
    "LN": ExchangeSpec("LSE", "GBP", "United Kingdom", price_multiplier=0.01),
    "SP": ExchangeSpec("BM", "EUR", "Spain"),
    "SM": ExchangeSpec("BM", "EUR", "Spain"),
    "SS": ExchangeSpec("SFB", "SEK", "Sweden"),
    "SW": ExchangeSpec("EBS", "CHF", "Switzerland"),
}


def load_contract_cache() -> dict:
    if IBKR_CONTRACT_CACHE_FILE.exists():
        with open(IBKR_CONTRACT_CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_contract_cache(cache: dict) -> None:
    with open(IBKR_CONTRACT_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def ibkr_config() -> dict:
    return {
        "host": os.environ.get("IBKR_GATEWAY_HOST", "127.0.0.1"),
        "port": int(os.environ.get("IBKR_GATEWAY_PORT", "4002")),
        "client_id": int(os.environ.get("IBKR_CLIENT_ID", "11")),
        "timeout": float(os.environ.get("IBKR_TIMEOUT", "10")),
    }


def normalize_exch_code(exch_code: str | None) -> str:
    if not exch_code:
        return ""
    return str(exch_code).split()[0].strip()


def is_option_or_derivative(info: dict | None) -> bool:
    if not info:
        return True
    sec_type = info.get("securityType", "") or ""
    sec_type2 = info.get("securityType2", "") or ""
    ticker = info.get("ticker", "") or ""
    reject_types = {"Future", "Warrant", "Right", "Index"}
    if "Option" in sec_type or "Option" in sec_type2:
        return True
    if any(t in sec_type or t in sec_type2 for t in reject_types):
        return True
    return not ticker or " " in ticker


def is_foreign_equity(info: dict | None) -> bool:
    if is_option_or_derivative(info):
        return False
    exch = normalize_exch_code(info.get("exchCode"))
    return exch and exch not in US_EXCH_CODES


def exchange_spec_for_info(info: dict) -> tuple[str, ExchangeSpec | None]:
    exch = normalize_exch_code(info.get("exchCode"))
    mic = info.get("micPrimExch")
    spec_key = f"{exch}:{mic}" if f"{exch}:{mic}" in EXCHANGE_SPECS else exch
    return exch, EXCHANGE_SPECS.get(spec_key)


def ibkr_symbol_for_ticker(ticker: str, spec: ExchangeSpec | None) -> str:
    if spec and spec.ibkr_exchange in {"TSE", "VENTURE"}:
        return ticker.replace("/", ".")
    return ticker


def plan_contract(figi: str, info: dict, cache: dict | None = None) -> ContractPlan | None:
    cache = cache if cache is not None else load_contract_cache()
    con_id = info.get("ibkrConId")
    con_id = int(con_id) if con_id else None
    exch, spec = exchange_spec_for_info(info)
    ticker = info.get("ticker", "") or ""
    symbol = ibkr_symbol_for_ticker(ticker, spec)
    cached = cache.get(figi)
    if cached:
        plan = ContractPlan(**cached)
        updates = {}
        if con_id and not plan.con_id:
            updates["con_id"] = con_id
        if symbol and plan.symbol != symbol and not plan.con_id:
            updates["symbol"] = symbol
        if spec and plan.exchange != spec.ibkr_exchange and not plan.con_id:
            primary_exchange = spec.primary_exchange
            if primary_exchange is None:
                primary_exchange = spec.ibkr_exchange
            updates.update({
                "exchange": spec.ibkr_exchange,
                "primary_exchange": primary_exchange,
                "currency": spec.currency,
                "country": spec.country,
                "lot_size": spec.default_lot_size,
                "needs_contract_details": spec.needs_contract_details,
                "price_multiplier": spec.price_multiplier,
            })
        if spec and plan.price_multiplier != spec.price_multiplier:
            updates["price_multiplier"] = spec.price_multiplier
        if spec and spec.country == "United States" and plan.lot_size != spec.default_lot_size:
            updates["lot_size"] = spec.default_lot_size
        if updates:
            plan = ContractPlan(**{**cached, **updates})
            cache[figi] = asdict(plan)
        return plan

    if not spec or not ticker:
        return None

    primary_exchange = spec.primary_exchange
    if primary_exchange is None:
        primary_exchange = spec.ibkr_exchange
    plan = ContractPlan(
        figi=figi,
        symbol=symbol,
        exchange=spec.ibkr_exchange,
        primary_exchange=primary_exchange,
        currency=spec.currency,
        sec_type="STK",
        country=spec.country,
        lot_size=spec.default_lot_size,
        name=info.get("name", "") or "",
        source_exch_code=exch,
        needs_contract_details=spec.needs_contract_details,
        con_id=con_id,
        price_multiplier=spec.price_multiplier,
    )
    cache[figi] = asdict(plan)
    return plan


def to_ib_async_contract(plan: ContractPlan, exchange: str | None = None):
    try:
        from ib_async import Stock
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    contract = Stock(plan.symbol, exchange or plan.exchange, plan.currency)
    if plan.primary_exchange:
        contract.primaryExchange = plan.primary_exchange
    if plan.con_id:
        contract.conId = plan.con_id
    return contract


def fx_contract(pair: str):
    try:
        from ib_async import Forex
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    return Forex(pair)


def limit_order(action: str, quantity: int, limit_price: float, outside_rth: bool = False):
    try:
        from ib_async import LimitOrder
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    order = LimitOrder(action, quantity, limit_price, tif="DAY")
    order.outsideRth = outside_rth
    return order


def market_order(action: str, quantity: int, outside_rth: bool = False):
    try:
        from ib_async import MarketOrder
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    order = MarketOrder(action, quantity, tif="DAY")
    order.outsideRth = outside_rth
    return order


def connect_ibkr():
    try:
        from ib_async import IB
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    config = ibkr_config()
    ib = IB()
    ib.connect(config["host"], config["port"], clientId=config["client_id"], timeout=config["timeout"])
    return ib


def qualify_contract_plan(ib, plan: ContractPlan) -> tuple[ContractPlan | None, str | None]:
    contract = to_ib_async_contract(plan)
    qualified = ib.qualifyContracts(contract)
    if not qualified or qualified[0] is None:
        return None, "IBKR returned no qualified contract"

    q = qualified[0]
    details = ib.reqContractDetails(q)
    detail = details[0] if details else None
    min_tick = getattr(detail, "minTick", None) if detail else None
    lot_size_candidates = [plan.lot_size]
    if detail and plan.country != "United States":
        for attr in ("minSize", "sizeIncrement", "suggestedSizeIncrement"):
            value = getattr(detail, attr, None)
            if value:
                lot_size_candidates.append(int(float(value)))
    lot_size = max(lot_size_candidates)
    enriched = ContractPlan(
        figi=plan.figi,
        symbol=q.symbol or plan.symbol,
        exchange=q.exchange or plan.exchange,
        primary_exchange=q.primaryExchange or plan.primary_exchange,
        currency=q.currency or plan.currency,
        sec_type=q.secType or plan.sec_type,
        country=plan.country,
        lot_size=lot_size,
        name=plan.name,
        source_exch_code=plan.source_exch_code,
        needs_contract_details=False,
        con_id=q.conId or None,
        trading_class=q.tradingClass or None,
        local_symbol=q.localSymbol or None,
        qualified_exchange=q.exchange or None,
        min_tick=float(min_tick) if min_tick is not None else None,
        price_multiplier=plan.price_multiplier,
    )
    return enriched, None


def account_snapshot(ib) -> tuple[dict, list[dict]]:
    summary = {}
    for item in ib.accountSummary():
        summary[item.tag] = {
            "value": item.value,
            "currency": item.currency,
            "account": item.account,
        }

    positions = []
    for pos in ib.positions():
        contract = pos.contract
        positions.append({
            "account": pos.account,
            "symbol": contract.symbol,
            "local_symbol": contract.localSymbol,
            "exchange": contract.exchange,
            "currency": contract.currency,
            "sec_type": contract.secType,
            "con_id": contract.conId,
            "position": float(pos.position),
            "avg_cost": float(pos.avgCost),
        })
    return summary, positions


def ticker_price(ticker) -> float | None:
    for value in (ticker.marketPrice(), ticker.last, ticker.close, ticker.bid, ticker.ask):
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def snapshot_prices(ib, plans: list[ContractPlan], wait_seconds: float = 2.0) -> dict[int, float]:
    ib.reqMarketDataType(4)  # delayed-frozen when live data is not subscribed
    contracts = [to_ib_async_contract(plan) for plan in plans if plan.con_id]
    tickers = [ib.reqMktData(contract, "", False, False) for contract in contracts]
    ib.sleep(wait_seconds)

    prices = {}
    for contract, ticker in zip(contracts, tickers):
        price = ticker_price(ticker)
        if price is not None:
            prices[contract.conId] = price
        ib.cancelMktData(contract)
    return prices


def snapshot_shortability(ib, plans: list[ContractPlan], wait_seconds: float = 2.0) -> dict[int, dict]:
    """Return IBKR shortability data keyed by conId.

    IBKR generic tick 236 populates Ticker.shortable and, where available,
    Ticker.shortableShares. The shortable score is broker-defined; values at
    or above 2.5 are generally treated as shortable/locatable.
    """
    ib.reqMarketDataType(4)
    contracts = [to_ib_async_contract(plan) for plan in plans if plan.con_id]
    tickers = [ib.reqMktData(contract, "236", False, False) for contract in contracts]
    ib.sleep(wait_seconds)

    data = {}
    for contract, ticker in zip(contracts, tickers):
        shortable = getattr(ticker, "shortable", None)
        shortable_shares = getattr(ticker, "shortableShares", None)
        try:
            shortable = float(shortable)
        except (TypeError, ValueError):
            shortable = None
        try:
            shortable_shares = float(shortable_shares)
        except (TypeError, ValueError):
            shortable_shares = None
        if shortable is not None and not math.isfinite(shortable):
            shortable = None
        if shortable_shares is not None and not math.isfinite(shortable_shares):
            shortable_shares = None
        data[int(contract.conId)] = {
            "shortable": shortable,
            "shortable_shares": shortable_shares,
        }
        ib.cancelMktData(contract)

    time.sleep(0.1)
    return data


def snapshot_fx_rates(ib, currencies: set[str], wait_seconds: float = 2.0) -> dict[str, float]:
    ib.reqMarketDataType(4)  # delayed-frozen when live data is not subscribed
    rates = {"USD": 1.0}
    contracts = []
    for currency in sorted(currencies):
        if currency == "USD":
            continue
        try:
            direct = ib.qualifyContracts(fx_contract(f"{currency}USD"))
        except Exception:
            direct = []
        if direct and direct[0] is not None:
            contracts.append((currency, direct[0], False))
            continue
        try:
            inverse = ib.qualifyContracts(fx_contract(f"USD{currency}"))
        except Exception:
            inverse = []
        if inverse and inverse[0] is not None:
            contracts.append((currency, inverse[0], True))

    tickers = [ib.reqMktData(contract, "", False, False) for _, contract, _ in contracts]
    ib.sleep(wait_seconds)

    for (currency, contract, invert), ticker in zip(contracts, tickers):
        rate = ticker_price(ticker)
        if rate is not None:
            rates[currency] = (1.0 / rate) if invert else rate
        ib.cancelMktData(contract)

    # Give cancellations a beat to settle before disconnect.
    time.sleep(0.1)
    return rates
