"""IBKR contract planning for Mimicry equity and ETP positions."""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "secrets.env")

IBKR_CONTRACT_CACHE_FILE = ROOT / "ibkr_contract_cache.json"

US_EXCH_CODES = {"US", "UA", "UN", "UQ", "UP", "UR", "UT"}
OPTION_TICKER_RE = re.compile(
    r"^\s*(?P<root>[A-Z0-9./-]+)(?:\s+US)?\s+"
    r"(?P<expiry>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<right>[CP])(?P<strike>\d+(?:\.\d+)?)"
    r"(?:\s+(?:Equity|Index))?\s*$",
    re.IGNORECASE,
)


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
    expiry: str | None = None
    strike: float | None = None
    right: str | None = None
    multiplier: float = 1.0
    underlying_symbol: str | None = None
    underlying_sec_type: str | None = None
    market_rules: list[list[float]] | None = None
    trading_hours: str | None = None
    liquid_hours: str | None = None
    time_zone_id: str | None = None
    settlement_type: str | None = None


@dataclass(frozen=True)
class Quote:
    con_id: int
    bid: float | None
    ask: float | None
    last: float | None
    close: float | None
    timestamp: str | None
    age_seconds: float | None
    market_data_type: int | None
    frozen: bool


@dataclass(frozen=True)
class ParsedOption:
    root: str
    expiry: str
    right: str
    strike: float
    underlying_sec_type: str


@dataclass(frozen=True)
class FxQuote:
    currency: str
    usd_rate: float | None
    quote: Quote
    inverted: bool


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
        "account_id": os.environ.get("IBKR_ACCOUNT_ID", "").strip(),
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


def is_option(info: dict | None) -> bool:
    if not info:
        return False
    sec_type = str(info.get("securityType") or "")
    sec_type2 = str(info.get("securityType2") or "")
    return bool(info.get("isOption")) or "Option" in sec_type or "Option" in sec_type2


def parse_option_ticker(ticker: str, security_type: str = "") -> ParsedOption:
    match = OPTION_TICKER_RE.match(str(ticker or "").upper())
    if not match:
        raise ValueError(f"unrecognized OpenFIGI option ticker: {ticker!r}")
    expiry = datetime.strptime(match.group("expiry"), "%m/%d/%y").strftime("%Y%m%d")
    underlying_sec_type = "IND" if "Index" in security_type else "STK"
    return ParsedOption(
        root=match.group("root").upper(),
        expiry=expiry,
        right=match.group("right").upper(),
        strike=float(match.group("strike")),
        underlying_sec_type=underlying_sec_type,
    )


def option_underlying_candidates(parsed: ParsedOption, name: str = "") -> list[str]:
    candidates = [parsed.root]
    match = re.search(r"\bon\s+([A-Z0-9./-]+)(?:\s+US)?\b", str(name or ""), re.IGNORECASE)
    if match:
        candidates.append(match.group(1).upper())
    if parsed.underlying_sec_type == "IND" and parsed.root.endswith("W"):
        candidates.append(parsed.root[:-1])
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def schedule_covers_current_date(
    hours: str | None,
    now: datetime | None = None,
) -> bool:
    if not hours:
        return False
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    date_keys = {
        (now_utc + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in (-1, 0, 1)
    }
    return any(f"{date_key}:" in hours for date_key in date_keys)


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
    if is_option(info):
        try:
            parsed = parse_option_ticker(
                ticker,
                f"{info.get('securityType', '')} {info.get('securityType2', '')}",
            )
        except ValueError:
            return None
        cached = cache.get(figi)
        if cached:
            return ContractPlan(**cached)
        plan = ContractPlan(
            figi=figi,
            symbol=parsed.root,
            exchange="SMART",
            primary_exchange="",
            currency="USD",
            sec_type="OPT",
            country="United States",
            lot_size=1,
            name=info.get("name", "") or "",
            source_exch_code=exch or "US",
            needs_contract_details=True,
            expiry=parsed.expiry,
            strike=parsed.strike,
            right=parsed.right,
            multiplier=100.0,
            underlying_symbol=parsed.root,
            underlying_sec_type=parsed.underlying_sec_type,
            settlement_type=(
                "CASH" if parsed.underlying_sec_type == "IND" else "PHYSICAL"
            ),
        )
        cache[figi] = asdict(plan)
        return plan

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
        from ib_async import Contract, Stock
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    if plan.sec_type == "OPT":
        contract = Contract(
            secType="OPT",
            symbol=plan.symbol,
            lastTradeDateOrContractMonth=plan.expiry or "",
            strike=float(plan.strike or 0),
            right=plan.right or "",
            multiplier=str(int(plan.multiplier)) if float(plan.multiplier).is_integer() else str(plan.multiplier),
            exchange=exchange or plan.exchange,
            currency=plan.currency,
            tradingClass=plan.trading_class or "",
        )
        if plan.local_symbol:
            contract.localSymbol = plan.local_symbol
        if plan.con_id:
            contract.conId = plan.con_id
        return contract

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


def limit_order(
    action: str,
    quantity: int | float,
    limit_price: float,
    outside_rth: bool = False,
    account_id: str | None = None,
    order_ref: str | None = None,
):
    try:
        from ib_async import LimitOrder
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    order = LimitOrder(action, quantity, limit_price, tif="DAY")
    order.outsideRth = outside_rth
    if account_id:
        order.account = account_id
    if order_ref:
        order.orderRef = order_ref
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


def managed_account_ids(ib) -> list[str]:
    return sorted({str(account).strip() for account in (ib.managedAccounts() or []) if str(account).strip()})


def require_account_id(ib, expected_account_id: str | None = None) -> str:
    expected = (expected_account_id or ibkr_config().get("account_id") or "").strip()
    if not expected:
        raise RuntimeError("IBKR_ACCOUNT_ID is required for every execution run")
    accounts = managed_account_ids(ib)
    matches = [account for account in accounts if account == expected]
    if len(matches) != 1:
        available = ", ".join(accounts) or "(none)"
        raise RuntimeError(
            f"IBKR_ACCOUNT_ID {expected!r} did not exactly match one managed account; available: {available}"
        )
    return matches[0]


def _float_or_none(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def contract_market_rules(ib, contract, detail=None) -> list[list[float]]:
    if detail is None:
        details = ib.reqContractDetails(contract)
        detail = details[0] if details else None
    if detail is None:
        return []
    rule_ids = [item.strip() for item in str(getattr(detail, "marketRuleIds", "") or "").split(",")]
    exchanges = [item.strip() for item in str(getattr(detail, "validExchanges", "") or "").split(",")]
    requested_exchange = str(getattr(contract, "exchange", "") or "")
    ordered_ids = []
    for exchange, rule_id in zip(exchanges, rule_ids):
        if exchange == requested_exchange and rule_id:
            ordered_ids.append(rule_id)
    ordered_ids.extend(rule_id for rule_id in rule_ids if rule_id and rule_id not in ordered_ids)
    for rule_id in ordered_ids:
        try:
            increments = ib.reqMarketRule(int(rule_id))
        except Exception:
            continue
        rules = []
        for increment in increments or []:
            low_edge = _float_or_none(getattr(increment, "lowEdge", None))
            step = _float_or_none(getattr(increment, "increment", None))
            if low_edge is not None and step is not None and step > 0:
                rules.append([low_edge, step])
        if rules:
            return sorted(rules, key=lambda item: item[0])
    return []


def qualify_contract_plan(ib, plan: ContractPlan) -> tuple[ContractPlan | None, str | None]:
    if plan.sec_type == "OPT":
        return qualify_option_contract_plan(ib, plan)

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
        multiplier=float(getattr(q, "multiplier", None) or plan.multiplier or 1),
        market_rules=contract_market_rules(ib, q, detail),
        trading_hours=getattr(detail, "tradingHours", None) if detail else None,
        liquid_hours=getattr(detail, "liquidHours", None) if detail else None,
        time_zone_id=getattr(detail, "timeZoneId", None) if detail else None,
    )
    return enriched, None


def _underlying_contract(symbol: str, sec_type: str):
    try:
        from ib_async import Index, Stock
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc
    if sec_type == "IND":
        return Index(symbol, "CBOE", "USD")
    return Stock(symbol, "SMART", "USD")


def qualify_option_contract_plan(ib, plan: ContractPlan) -> tuple[ContractPlan | None, str | None]:
    parsed = ParsedOption(
        root=plan.underlying_symbol or plan.symbol,
        expiry=plan.expiry or "",
        right=plan.right or "",
        strike=float(plan.strike or 0),
        underlying_sec_type=plan.underlying_sec_type or "STK",
    )
    underlying = None
    errors = []
    for symbol in option_underlying_candidates(parsed, plan.name):
        candidate = _underlying_contract(symbol, parsed.underlying_sec_type)
        try:
            qualified = ib.qualifyContracts(candidate)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
            continue
        if qualified and qualified[0] is not None:
            underlying = qualified[0]
            break
    if underlying is None:
        return None, "underlying qualification failed" + (f": {'; '.join(errors)}" if errors else "")

    try:
        chains = ib.reqSecDefOptParams(
            underlying.symbol,
            "",
            underlying.secType,
            underlying.conId,
        )
    except Exception as exc:
        return None, f"option-chain request failed: {exc}"

    matching_chains = []
    for chain in chains or []:
        expirations = set(getattr(chain, "expirations", []) or [])
        strikes = [float(value) for value in (getattr(chain, "strikes", []) or [])]
        if parsed.expiry not in expirations:
            continue
        if not any(math.isclose(parsed.strike, strike, abs_tol=1e-8) for strike in strikes):
            continue
        matching_chains.append(chain)
    if not matching_chains:
        return None, (
            f"no option chain matched expiry={parsed.expiry} strike={parsed.strike:g} "
            f"for underlying {underlying.symbol}"
        )

    matching_chains.sort(
        key=lambda chain: (
            0 if str(getattr(chain, "tradingClass", "") or "") == plan.symbol else 1,
            0 if str(getattr(chain, "exchange", "") or "") == "SMART" else 1,
        )
    )
    try:
        from ib_async import Contract
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc

    for chain in matching_chains:
        multiplier = str(getattr(chain, "multiplier", "") or int(plan.multiplier or 100))
        candidate = Contract(
            secType="OPT",
            symbol=underlying.symbol,
            lastTradeDateOrContractMonth=parsed.expiry,
            strike=parsed.strike,
            right=parsed.right,
            multiplier=multiplier,
            exchange="SMART",
            currency="USD",
            tradingClass=str(getattr(chain, "tradingClass", "") or ""),
        )
        try:
            qualified = ib.qualifyContracts(candidate)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not qualified or qualified[0] is None:
            continue
        q = qualified[0]
        if (
            str(q.lastTradeDateOrContractMonth or "")[:8] != parsed.expiry
            or str(q.right or "").upper()[:1] != parsed.right
            or not math.isclose(float(q.strike or 0), parsed.strike, abs_tol=1e-8)
        ):
            continue
        details = ib.reqContractDetails(q)
        detail = details[0] if details else None
        min_tick = _float_or_none(getattr(detail, "minTick", None)) if detail else None
        return ContractPlan(
            figi=plan.figi,
            symbol=q.symbol or underlying.symbol,
            exchange=q.exchange or "SMART",
            primary_exchange=q.primaryExchange or "",
            currency=q.currency or "USD",
            sec_type="OPT",
            country=plan.country,
            lot_size=1,
            name=plan.name,
            source_exch_code=plan.source_exch_code,
            needs_contract_details=False,
            con_id=int(q.conId) if q.conId else None,
            trading_class=q.tradingClass or None,
            local_symbol=q.localSymbol or None,
            qualified_exchange=q.exchange or None,
            min_tick=min_tick,
            price_multiplier=1.0,
            expiry=parsed.expiry,
            strike=parsed.strike,
            right=parsed.right,
            multiplier=float(q.multiplier or multiplier or 100),
            underlying_symbol=underlying.symbol,
            underlying_sec_type=underlying.secType,
            market_rules=contract_market_rules(ib, q, detail),
            trading_hours=getattr(detail, "tradingHours", None) if detail else None,
            liquid_hours=getattr(detail, "liquidHours", None) if detail else None,
            time_zone_id=getattr(detail, "timeZoneId", None) if detail else None,
            settlement_type=(
                str(getattr(detail, "settlementType", "") or "")
                or ("CASH" if underlying.secType == "IND" else "PHYSICAL")
            ),
        ), None
    return None, "exact option contract qualification failed" + (f": {'; '.join(errors[-3:])}" if errors else "")


def qualify_position_plan(ib, position: dict) -> tuple[ContractPlan | None, str | None]:
    try:
        from ib_async import Contract
    except ImportError as exc:
        raise RuntimeError("ib-async is not installed. Install it with: pip install ib-async") from exc
    con_id = int(position.get("con_id") or 0)
    if not con_id:
        return None, "position has no conId"
    contract = Contract(conId=con_id, exchange="SMART")
    qualified = ib.qualifyContracts(contract)
    if not qualified or qualified[0] is None:
        return None, f"IBKR could not qualify held conId {con_id}"
    q = qualified[0]
    details = ib.reqContractDetails(q)
    detail = details[0] if details else None
    min_tick = _float_or_none(getattr(detail, "minTick", None)) if detail else None
    sec_type = str(q.secType or position.get("sec_type") or "")
    currency = str(q.currency or position.get("currency") or "")
    return ContractPlan(
        figi=f"POSITION:{con_id}",
        symbol=q.symbol or position.get("symbol") or "",
        exchange=q.exchange or "SMART",
        primary_exchange=q.primaryExchange or "",
        currency=currency,
        sec_type=sec_type,
        country="United States" if currency == "USD" else "Foreign",
        lot_size=1,
        name=str(getattr(detail, "longName", "") or position.get("symbol") or ""),
        source_exch_code="",
        needs_contract_details=False,
        con_id=con_id,
        trading_class=q.tradingClass or None,
        local_symbol=q.localSymbol or position.get("local_symbol") or None,
        qualified_exchange=q.exchange or None,
        min_tick=min_tick,
        price_multiplier=1.0,
        expiry=q.lastTradeDateOrContractMonth or position.get("expiry") or None,
        strike=_float_or_none(q.strike) if sec_type == "OPT" else None,
        right=q.right or position.get("right") or None,
        multiplier=_float_or_none(q.multiplier) or _float_or_none(position.get("multiplier")) or 1.0,
        underlying_symbol=q.symbol or None,
        underlying_sec_type=None,
        market_rules=contract_market_rules(ib, q, detail),
        trading_hours=getattr(detail, "tradingHours", None) if detail else None,
        liquid_hours=getattr(detail, "liquidHours", None) if detail else None,
        time_zone_id=getattr(detail, "timeZoneId", None) if detail else None,
        settlement_type=(
            str(getattr(detail, "settlementType", "") or "")
            or (
                "UNKNOWN"
                if sec_type == "OPT"
                else None
            )
        ),
    ), None


def account_snapshot(ib, account_id: str | None = None) -> tuple[dict, list[dict]]:
    if account_id is None:
        configured = ibkr_config().get("account_id")
        accounts = managed_account_ids(ib)
        if configured:
            account_id = require_account_id(ib, configured)
        elif len(accounts) == 1:
            account_id = accounts[0]
        else:
            available = ", ".join(accounts) or "(none)"
            raise RuntimeError(
                "IBKR_ACCOUNT_ID is required when more than one account is managed; "
                f"available: {available}"
            )
    summary = {}
    for item in ib.accountSummary():
        if str(item.account) != account_id:
            continue
        summary[item.tag] = {
            "value": item.value,
            "currency": item.currency,
            "account": item.account,
        }

    positions = []
    try:
        raw_positions = ib.positions(account_id)
    except TypeError:
        raw_positions = ib.positions()
    for pos in raw_positions:
        if str(pos.account) != account_id:
            continue
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
            "multiplier": _float_or_none(getattr(contract, "multiplier", None)) or 1.0,
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", None) or None,
            "strike": _float_or_none(getattr(contract, "strike", None)),
            "right": getattr(contract, "right", None) or None,
            "trading_class": getattr(contract, "tradingClass", None) or None,
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


def _quote_time(ticker) -> datetime | None:
    value = getattr(ticker, "time", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def quote_from_ticker(contract, ticker, now: datetime | None = None) -> Quote:
    now = now or datetime.now(timezone.utc)
    quote_time = _quote_time(ticker)
    age_seconds = max(0.0, (now - quote_time).total_seconds()) if quote_time else None
    data_type = getattr(ticker, "marketDataType", None)
    try:
        data_type = int(data_type)
    except (TypeError, ValueError):
        data_type = None
    return Quote(
        con_id=int(getattr(contract, "conId", 0) or 0),
        bid=_float_or_none(getattr(ticker, "bid", None)),
        ask=_float_or_none(getattr(ticker, "ask", None)),
        last=_float_or_none(getattr(ticker, "last", None)),
        close=_float_or_none(getattr(ticker, "close", None)),
        timestamp=quote_time.isoformat() if quote_time else None,
        age_seconds=age_seconds,
        market_data_type=data_type,
        frozen=data_type in {2, 4},
    )


def snapshot_quotes(ib, plans: list[ContractPlan], wait_seconds: float = 2.0) -> dict[int, Quote]:
    ib.reqMarketDataType(3)
    contracts = [to_ib_async_contract(plan) for plan in plans if plan.con_id]
    tickers = [ib.reqMktData(contract, "", False, False) for contract in contracts]
    ib.sleep(wait_seconds)
    now = datetime.now(timezone.utc)
    quotes = {}
    for contract, ticker in zip(contracts, tickers):
        quote = quote_from_ticker(contract, ticker, now=now)
        quotes[quote.con_id] = quote
        ib.cancelMktData(contract)
    return quotes


def snapshot_prices(ib, plans: list[ContractPlan], wait_seconds: float = 2.0) -> dict[int, float]:
    quotes = snapshot_quotes(ib, plans, wait_seconds=wait_seconds)
    prices = {}
    for con_id, quote in quotes.items():
        for value in (quote.last, quote.close, quote.bid, quote.ask):
            if value is not None and value > 0:
                prices[con_id] = value
                break
    return prices


def snapshot_shortability(ib, plans: list[ContractPlan], wait_seconds: float = 2.0) -> dict[int, dict]:
    """Return IBKR shortability data keyed by conId.

    IBKR generic tick 236 populates Ticker.shortable and, where available,
    Ticker.shortableShares. A score of 3 means shortable with shares.
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
    quotes = snapshot_fx_quotes(ib, currencies, wait_seconds=wait_seconds)
    return {
        currency: fx_quote.usd_rate
        for currency, fx_quote in quotes.items()
        if fx_quote.usd_rate is not None
    }


def snapshot_fx_quotes(
    ib,
    currencies: set[str],
    wait_seconds: float = 2.0,
) -> dict[str, FxQuote]:
    ib.reqMarketDataType(3)
    now = datetime.now(timezone.utc)
    usd_quote = Quote(
        con_id=0,
        bid=1.0,
        ask=1.0,
        last=1.0,
        close=1.0,
        timestamp=now.isoformat(),
        age_seconds=0.0,
        market_data_type=1,
        frozen=False,
    )
    quotes = {"USD": FxQuote("USD", 1.0, usd_quote, False)}
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
        quote = quote_from_ticker(contract, ticker)
        prices = [finite for finite in (quote.bid, quote.ask) if finite is not None and finite > 0]
        raw_rate = sum(prices) / len(prices) if prices else ticker_price(ticker)
        usd_rate = (1.0 / raw_rate) if invert and raw_rate else raw_rate
        quotes[currency] = FxQuote(currency, usd_rate, quote, invert)
        ib.cancelMktData(contract)

    # Give cancellations a beat to settle before disconnect.
    time.sleep(0.1)
    return quotes
