from types import SimpleNamespace

import ibkr_contracts


def test_option_parser_handles_equity_and_index_forms():
    equity = ibkr_contracts.parse_option_ticker("NVDA 07/17/26 C220", "Equity Option")
    assert (equity.root, equity.expiry, equity.right, equity.strike) == (
        "NVDA",
        "20260717",
        "C",
        220.0,
    )
    index = ibkr_contracts.parse_option_ticker(
        "SPXW US 06/18/26 C7500 Index",
        "Index Option",
    )
    assert index.underlying_sec_type == "IND"
    assert ibkr_contracts.option_underlying_candidates(index, "June Calls on SPX") == [
        "SPXW",
        "SPX",
    ]


def test_contract_schedule_refresh_detection_spans_global_date_boundary():
    now = ibkr_contracts.datetime(
        2026,
        7,
        17,
        23,
        30,
        tzinfo=ibkr_contracts.timezone.utc,
    )
    assert ibkr_contracts.schedule_covers_current_date(
        "20260718:20260718:0900-20260718:1500",
        now=now,
    )
    assert not ibkr_contracts.schedule_covers_current_date(
        "20260601:CLOSED",
        now=now,
    )


class FakeContract:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeOptionIB:
    def __init__(self):
        self.qualify_calls = 0

    def qualifyContracts(self, contract):
        self.qualify_calls += 1
        if self.qualify_calls == 1:
            return [
                SimpleNamespace(
                    symbol="NVDA",
                    secType="STK",
                    conId=4815747,
                )
            ]
        return [
            SimpleNamespace(
                symbol="NVDA",
                secType="OPT",
                conId=9001,
                exchange="SMART",
                primaryExchange="",
                currency="USD",
                tradingClass="NVDA",
                localSymbol="NVDA  260717C00220000",
                multiplier="100",
                lastTradeDateOrContractMonth="20260717",
                strike=220.0,
                right="C",
            )
        ]

    def reqSecDefOptParams(self, *_args):
        return [
            SimpleNamespace(
                expirations={"20260717"},
                strikes={220.0},
                tradingClass="NVDA",
                exchange="SMART",
                multiplier="100",
            )
        ]

    def reqContractDetails(self, _contract):
        return [
            SimpleNamespace(
                minTick=0.01,
                marketRuleIds="26",
                validExchanges="SMART",
                tradingHours="",
                liquidHours="",
                timeZoneId="US/Eastern",
            )
        ]

    def reqMarketRule(self, _rule_id):
        return [SimpleNamespace(lowEdge=0, increment=0.01)]


def test_option_qualification_selects_exact_contract(monkeypatch):
    monkeypatch.setattr(
        ibkr_contracts,
        "_underlying_contract",
        lambda symbol, sec_type: SimpleNamespace(symbol=symbol, secType=sec_type),
    )
    monkeypatch.setattr(
        "ib_async.Contract",
        FakeContract,
    )
    source = ibkr_contracts.ContractPlan(
        figi="OPTION1",
        symbol="NVDA",
        exchange="SMART",
        primary_exchange="",
        currency="USD",
        sec_type="OPT",
        country="United States",
        lot_size=1,
        name="July Calls on NVDA",
        source_exch_code="US",
        needs_contract_details=True,
        expiry="20260717",
        strike=220,
        right="C",
        multiplier=100,
        underlying_symbol="NVDA",
        underlying_sec_type="STK",
    )
    qualified, error = ibkr_contracts.qualify_option_contract_plan(FakeOptionIB(), source)
    assert error is None
    assert qualified.con_id == 9001
    assert qualified.local_symbol == "NVDA  260717C00220000"
    assert qualified.market_rules == [[0.0, 0.01]]
    assert qualified.multiplier == 100
    assert qualified.settlement_type == "PHYSICAL"
