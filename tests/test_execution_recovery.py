"""Cross-module regression cases: exits and recoverable, exactly-once accounting."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from conservative_trading import execution
from conservative_trading.api.service import Service, ServiceConflict
from conservative_trading.api.status import ProposalStatus, proposal_status
from conservative_trading.broker.base import BrokerUnavailable, Quote
from conservative_trading.portfolio import load_portfolio, snapshot_from_broker
from conservative_trading.proposals import (
    CANCELED,
    EXECUTED,
    PARTIALLY_FILLED,
    PROPOSED,
    SUBMITTED,
    UNKNOWN,
    ProposalStore,
)
from conservative_trading.scenarios import identity_lookup, regime
from conservative_trading.stage3_scenarios import _std
from conservative_trading.state_db import StateDB


@pytest.fixture(autouse=True)
def offline_market(monkeypatch):
    monkeypatch.setattr(execution, 'assess_regime', lambda *args: regime('NORMAL'))
    monkeypatch.setattr(execution, 'snapshot_from_broker',
                        lambda *args: snapshot_from_broker(*args, identity_lookup=identity_lookup))


def setup(tmp_path, fill_mode='full'):
    profiles, p, broker, ledger = _std(tmp_path, broker_kwargs={'cash': 3000, 'fill_mode': fill_mode})
    store = ProposalStore(tmp_path / 'proposals.json')
    store.save(p)
    return profiles, p, broker, ledger, store


def finish(broker, order, price=None):
    price = price or order.filled_avg_price or order.limit_price
    remaining = order.quantity - order.filled_quantity
    broker._apply_fill(order.symbol, order.side, remaining, price)
    avg = ((order.filled_avg_price or 0) * order.filled_quantity + remaining * price) / order.quantity
    updated = replace(order, status='filled', filled_quantity=order.quantity, filled_avg_price=avg)
    broker._orders[order.client_order_id] = updated
    return updated


@pytest.mark.parametrize('action,held,selling', [('SELL', 2, 2), ('REDUCE', 2, 1), ('SELL', .01, .01)])
def test_exit_reaches_broker_and_updates_ledger(tmp_path, action, held, selling):
    profiles, p, broker, ledger = _std(tmp_path,
        broker_kwargs={'cash': 3000 - held * 100, 'positions': {'VTI': (held, 100)}},
        ledger_kwargs={'core_cash': 2500 - held * 100, 'positions': [
            {'symbol': 'VTI', 'bucket': 'core', 'quantity': held, 'cost_basis_per_share': 100}]},
        proposal_kwargs={'action': action, 'notional': selling * 100, 'price': 100,
                         'fingerprint': f'VTI:{float(held)}'})
    broker.set_price('VTI', 100)
    p.sell_quantity = selling
    store = ProposalStore(tmp_path / 'proposals.json'); store.save(p)
    result = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert result.submitted, result.as_dict()
    assert result.order['side'] == 'sell'
    assert result.order['quantity'] == selling
    assert result.revalidation['hard_risk']['proposed_notional_usd'] > 0
    saved = load_portfolio(ledger)
    position = saved.position('VTI')
    assert (position.quantity if position else 0) == pytest.approx(held - selling)
    expected_cash = 2500 - held * 100 + selling * result.order['filled_avg_price']
    assert saved.buckets['core'].cash_usd == pytest.approx(round(expected_cash, 2))


def test_zero_sized_legacy_sell_requires_new_analysis(tmp_path):
    profiles, p, broker, ledger, store = setup(tmp_path)
    p.action, p.notional_usd, p.sell_quantity = 'SELL', 0, None
    store.save(p)
    result = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert not result.submitted
    assert not broker._orders


@pytest.mark.parametrize('mode,state', [('none', SUBMITTED), ('partial', PARTIALLY_FILLED)])
def test_pending_then_filled_recovery_is_incremental_and_idempotent(tmp_path, mode, state):
    profiles, p, broker, ledger, store = setup(tmp_path, mode)
    result = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert result.submitted
    assert store.get(p.proposal_id).status == state
    assert len(store.db.reservations(active_only=True)) == 1
    order = next(iter(broker._orders.values()))
    finish(broker, order, price=order.limit_price + 1)
    # A fresh service/process only needs the persisted proposal and the venue's order.
    recovered = execution.reconcile_proposal(profiles, store.get(p.proposal_id), broker,
                                             ProposalStore(store.path))
    assert recovered.outcome == 'RECONCILED', recovered.as_dict()
    filled = store.get(p.proposal_id)
    assert filled.status == EXECUTED
    assert proposal_status(filled, profiles) == ProposalStatus.EXECUTED
    after = load_portfolio(ledger).model_dump()
    fills = StateDB(tmp_path / 'fills.jsonl').fills()
    assert len(fills) == (2 if mode == 'partial' else 1)
    assert sum(fill['quantity'] for fill in fills) == pytest.approx(order.quantity)
    execution.reconcile_proposal(profiles, filled, broker, store)
    assert load_portfolio(ledger).model_dump() == after
    assert StateDB(tmp_path / 'fills.jsonl').fills() == fills
    assert store.db.reservations(active_only=True) == []
    assert len(broker._orders) == 1


def test_partial_then_canceled_preserves_fill_and_terminal_state(tmp_path):
    profiles, p, broker, ledger, store = setup(tmp_path, 'partial')
    execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    order = next(iter(broker._orders.values())); broker.cancel_order(order.order_id)
    before = load_portfolio(ledger).model_dump()
    execution.reconcile_proposal(profiles, store.get(p.proposal_id), broker, store)
    assert store.get(p.proposal_id).status == CANCELED
    assert store.get(p.proposal_id).filled_quantity == order.filled_quantity
    assert load_portfolio(ledger).model_dump() == before


def test_unknown_submit_recovers_without_second_submission(tmp_path, monkeypatch):
    profiles, p, broker, ledger, store = setup(tmp_path)
    submit = broker.submit_order
    def lost_response(request):
        submit(request)
        raise BrokerUnavailable('response lost after venue accepted')
    monkeypatch.setattr(broker, 'submit_order', lost_response)
    first = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert first.outcome == 'SUBMIT_UNCERTAIN'
    assert store.get(p.proposal_id).status == UNKNOWN
    recovered = execution.execute(profiles, store.get(p.proposal_id), broker, store)
    assert recovered.outcome == 'RECONCILED'
    assert len(broker._orders) == 1
    assert load_portfolio(ledger).position('VTI') is not None


def test_crash_between_ledger_and_audit_is_replayed_once(tmp_path, monkeypatch):
    profiles, p, broker, ledger, store = setup(tmp_path)
    append = execution._append_fill
    monkeypatch.setattr(execution, '_append_fill', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    first = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert first.outcome == 'RECONCILIATION_REQUIRED'
    saved = load_portfolio(ledger)
    assert saved.position('VTI') is None, 'ledger, proposal and fill audit roll back together'
    assert StateDB(tmp_path / 'fills.jsonl').fills() == []
    monkeypatch.setattr(execution, '_append_fill', append)
    execution.reconcile_proposal(profiles, store.get(p.proposal_id), broker, store)
    assert load_portfolio(ledger).position('VTI') is not None
    assert len(StateDB(tmp_path / 'fills.jsonl').fills()) == 1
    assert store.get(p.proposal_id).status == EXECUTED


def test_recovery_still_runs_when_trading_is_paused(tmp_path):
    profiles, p, broker, ledger, store = setup(tmp_path, 'none')
    execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    finish(broker, next(iter(broker._orders.values())))
    profiles.risk['execution_policy']['kill_switch']['trading_halted'] = True
    result = execution.execute(profiles, store.get(p.proposal_id), broker, store)
    assert result.outcome == 'RECONCILED'
    assert store.get(p.proposal_id).status == EXECUTED


def test_mismatched_order_is_not_applied(tmp_path, monkeypatch):
    profiles, p, broker, ledger, store = setup(tmp_path, 'none')
    execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    order = finish(broker, next(iter(broker._orders.values())))
    monkeypatch.setattr(broker, 'poll_order', lambda *args, **kwargs: replace(order, order_id='different'))
    before = load_portfolio(ledger).model_dump()
    result = execution.reconcile_proposal(profiles, store.get(p.proposal_id), broker, store)
    assert result.outcome == 'RECONCILIATION_REQUIRED'
    assert load_portfolio(ledger).model_dump() == before


def test_unresolved_order_blocks_another_proposal(tmp_path):
    profiles, p, broker, ledger, store = setup(tmp_path, 'none')
    execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    other = replace(p, proposal_id='other-proposal')
    store.save(other)
    result = execution.execute(profiles, other, broker, store, ledger_path=str(ledger))
    assert result.outcome == 'RECONCILIATION_REQUIRED'
    assert len(broker._orders) == 1


def service_for(profiles, store, broker, monkeypatch):
    service = Service()
    monkeypatch.setattr(service, 'profiles', lambda: profiles)
    monkeypatch.setattr(service, '_store', lambda _: store)
    monkeypatch.setattr(service, '_broker', lambda *_: broker)
    return service


def approve_arguments(p):
    return {'identity': 'verified-owner', 'version': p.version(), 'request_id': str(uuid4()), 'terms': {
        'ticker': p.ticker, 'bucket': p.bucket, 'action': p.action,
        'notional_usd': p.notional_usd, 'sell_quantity': p.sell_quantity}}


def test_approval_version_terms_identity_and_retries(tmp_path, monkeypatch):
    profiles, p, broker, _, store = setup(tmp_path)
    p.status = PROPOSED; p.approved_at = None; store.save(p)
    service = service_for(profiles, store, broker, monkeypatch)
    args = approve_arguments(p)
    for invalid in [{**args, 'version': 'a' * 64}, {**args, 'terms': {**args['terms'], 'notional_usd': 999}}]:
        with pytest.raises(ServiceConflict): service.approve(p.proposal_id, **invalid)
    assert len(broker._orders) == 0
    first = service.approve(p.proposal_id, **args)
    assert first['status'] == 'EXECUTED', first
    assert service.approve(p.proposal_id, **args) == first
    assert len(broker._orders) == 1
    saved = store.get(p.proposal_id)
    assert saved.approved_by == 'verified-owner'
    assert saved.history[-1]['event'] == 'order_updated'
    counts = store.db.status()['counts']
    assert counts['approvals'] == counts['orders'] == counts['fills'] == 1
    with pytest.raises(ServiceConflict):
        service.approve(p.proposal_id, **{**args, 'identity': 'another-user'})
    with pytest.raises(ServiceConflict):
        service.approve(p.proposal_id, **{**args, 'request_id': str(uuid4())})


def test_service_recovery_does_not_submit(tmp_path, monkeypatch):
    profiles, p, broker, ledger, store = setup(tmp_path, 'none')
    execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    finish(broker, next(iter(broker._orders.values())))
    service = service_for(profiles, store, broker, monkeypatch)
    monkeypatch.setattr(broker, 'submit_order', lambda *_: pytest.fail('recovery submitted a new order'))
    service.reconcile_orders()
    assert store.get(p.proposal_id).status == EXECUTED


def test_simulated_venue_recovers_after_broker_and_service_restart(tmp_path, monkeypatch):
    from conservative_trading.broker.simulated import SimulatedBroker

    profiles, p, _, ledger, store = setup(tmp_path)
    venue_state = tmp_path / 'venue-orders.json'
    broker = SimulatedBroker(cash=3000, prices={'VTI': p.reference_price}, order_state_path=venue_state)
    append = execution._append_fill
    monkeypatch.setattr(execution, '_append_fill', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    result = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert result.outcome == 'RECONCILIATION_REQUIRED'
    before_recovery = load_portfolio(ledger).model_dump()
    monkeypatch.setattr(execution, '_append_fill', append)
    restarted = SimulatedBroker(order_state_path=venue_state)
    service = service_for(profiles, ProposalStore(store.path), restarted, monkeypatch)
    monkeypatch.setattr(restarted, 'submit_order', lambda *_: pytest.fail('recovery must not submit'))
    service.reconcile_orders()
    assert load_portfolio(ledger).model_dump() != before_recovery
    assert store.get(p.proposal_id).status == EXECUTED
    assert len(StateDB(tmp_path / 'fills.jsonl').fills()) == 1


def test_concurrent_approval_retries_submit_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    profiles, p, broker, _, store = setup(tmp_path)
    p.status = PROPOSED; p.approved_at = None; store.save(p)
    service = service_for(profiles, store, broker, monkeypatch)
    args = approve_arguments(p)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.approve(p.proposal_id, **args), range(2)))
    assert results[0] == results[1]
    assert len(broker._orders) == 1


def test_preview_is_read_only_and_does_not_require_existing_approval(tmp_path, monkeypatch):
    profiles, p, broker, _, store = setup(tmp_path)
    p.status = PROPOSED; p.approved_at = None; store.save(p)
    service = service_for(profiles, store, broker, monkeypatch)
    before_proposals = [item.as_dict() for item in store.all()]
    before_portfolio = load_portfolio(tmp_path / 'portfolio.yaml').model_dump()
    result = service.revalidate_proposal(p.proposal_id)
    assert result['would_execute'], result
    assert not broker._orders
    assert [item.as_dict() for item in store.all()] == before_proposals
    assert load_portfolio(tmp_path / 'portfolio.yaml').model_dump() == before_portfolio


def test_disabled_micro_switch_does_not_disable_order_reads(tmp_path, monkeypatch):
    from conservative_trading.broker import robinhood

    profiles, p, broker, ledger, store = setup(tmp_path, 'none')
    execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    p = store.get(p.proposal_id); p.bucket = 'micro_live'; store.save(p)
    profiles.risk['micro_live_execution']['enabled'] = False
    service = service_for(profiles, store, broker, monkeypatch)
    opened = []
    monkeypatch.setattr(robinhood, 'RobinhoodBroker', lambda _: (opened.append(True) or broker))
    monkeypatch.setattr(broker, 'submit_order', lambda *_: pytest.fail('recovery must not submit'))
    service.reconcile_orders()
    assert opened == [True]
    assert store.get(p.proposal_id).status == SUBMITTED


@pytest.mark.parametrize('kind', ['missing', 'stale', 'future', 'wide'])
def test_execution_quote_requires_market_timestamp_and_tight_book(profiles, kind):
    now = datetime.now(UTC)
    as_of = {"missing": None, "stale": now - timedelta(minutes=3),
             "future": now + timedelta(seconds=10), "wide": now}[kind]
    bid, ask = (100.0, 102.0) if kind == "wide" else (99.98, 100.02)
    broker = SimpleNamespace(get_quote=lambda _symbol: Quote(
        "VTI", 100.0, as_of, "last_trade", bid=bid, ask=ask, received_at=now))
    price, detail = execution._venue_price(broker, "VTI", profiles)
    assert price is None
    assert ("timestamp" in detail) if kind != "wide" else ("spread" in detail)


def test_open_without_research_reference_price_is_not_repriced_and_submitted(tmp_path):
    profiles, p, broker, ledger, store = setup(tmp_path)
    p.reference_price = None
    store.save(p)
    result = execution.execute(profiles, p, broker, store, ledger_path=str(ledger))
    assert result.outcome == "REANALYZE" and not result.submitted
    assert not broker._orders
