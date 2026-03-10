"""The assistant bound to an account scope by the server. Offline.

A personal task names one observed account or, with none, all of them as the snapshot lists
them; a conversation keeps the scope it was opened under; the Agentic conversation explains
what is recorded and never sees a personal account; and what reaches a model is decided by
these bindings, never by anything the model says.
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest

from agentic_trading.advisor import SNAPSHOT, now, run_task
from agentic_trading.research_jobs import ResearchJobManager
from agentic_trading.service import Service, ServiceConflict, ServiceError
from tests.conftest import ROOT, copy_example_ledger

A, B = "a" * 20, "b" * 20


def snapshot(fetched_at=None):
    return {"fetched_at": fetched_at or now(), "complete": True, "errors": [], "source": "test",
            "accounts": [{"id": A, "label": "••••1111", "complete": True, "cash_usd": 900, "equity_usd": 1000,
                          "positions": [{"symbol": "VTI", "quantity": 1, "average_cost": 90,
                                         "market_value_usd": 100, "price": 100,
                                         "quote_at": "2026-09-16T19:59:59Z", "weight_pct": 10.0}]},
                         {"id": B, "label": "••••2222", "complete": True, "cash_usd": 50, "equity_usd": 500,
                          "positions": []}],
            "candidates": [{"symbol": "VTI", "sources": ["watchlists:My list"]}]}


@pytest.fixture
def service(tmp_path, monkeypatch, offline_market_reads):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    service = Service(cfg)
    service.state_db().write_meta(SNAPSHOT, snapshot())
    return service


def finish(service, job, **result):
    manager = service._research_manager(service.profiles())
    manager.store.update(job["run_key"], {"state": "COMPLETE", "finished_at": now()})
    if result:
        path = service.profiles().project_dir / "runs" / job["run_key"] / "decision_record.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"run_id": job["run_key"], "ticker": job["ticker"], "bucket": "advisory",
                                    "advisory": True, "task_kind": job["task"]["kind"], **result}))


def record(service, run_id, **fields):
    doc = {"run_id": run_id, "ticker": "VTI", "bucket": "core", "trade_date": "2026-09-16",
           "created_at": "2026-09-16T12:00:00+00:00", "instrument_type": "etf",
           "policy": {"final_action": "NO_TRADE", "confidence": 55, "proposal": {}},
           "final_recommendation": {"action": "NO_TRADE"}, "hard_risk": {"verdict": "NO_ACTION"}}
    doc.update(fields)
    path = service.profiles().project_dir / "runs" / run_id / "decision_record.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return doc


# ------------------------------------------------------------------ starting a task
def test_the_agentic_assistant_explains_and_admits_nothing_else(service):
    with pytest.raises(ServiceError, match="only explains what is recorded"):
        service.start_advisor(kind="analysis", request_id=str(uuid.uuid4()), ticker="VTI", scope="agentic")
    with pytest.raises(ServiceError, match="only explains"):
        service.start_advisor(kind="portfolio_review", request_id=str(uuid.uuid4()), scope="agentic")
    with pytest.raises(ServiceError, match="names no personal account"):
        service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="why?", scope="agentic",
                              account_id=A)
    job = service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="Why REDUCE VTI?",
                                scope="agentic", conversation_id="agentic-1")
    assert job["task"]["scope"] == "agentic" and job["task"]["account_scope_kind"] == "single"
    assert job["task"]["account_id"] is None and job["task"]["account_ids"] == []


def test_a_personal_task_is_bound_to_one_observed_account_or_to_all_of_them(service):
    with pytest.raises(ServiceError, match="unknown personal account"):
        service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="hi", account_id="c" * 20)
    one = service.start_advisor(kind="analysis", request_id=str(uuid.uuid4()), ticker="VTI", account_id=A)
    assert one["task"]["account_scope_kind"] == "single" and one["task"]["account_ids"] == []
    every = service.start_advisor(kind="portfolio_review", request_id=str(uuid.uuid4()))
    assert every["task"]["kind"] == "portfolio_review" and every["ticker"] == "ASSISTANT"
    assert every["task"]["account_scope_kind"] == "all_personal" and every["task"]["account_ids"] == [A, B]
    # A replay under the same id returns the same task even if the snapshot has since changed.
    service.state_db().write_meta(SNAPSHOT, {**snapshot(), "accounts": snapshot()["accounts"][:1]})
    again = service.start_advisor(kind="portfolio_review", request_id=every["run_key"].removeprefix("advisor_")
                                  and str(uuid.UUID(every["run_key"].removeprefix("advisor_"))))
    assert again["run_key"] == every["run_key"] and again["deduplicated"] is True


def test_a_conversation_keeps_the_scope_it_was_opened_under(service):
    first = service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="hi",
                                  conversation_id="c1", account_id=A)
    finish(service, first)
    with pytest.raises(ServiceConflict, match="bound to another account scope"):
        service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="and?",
                              conversation_id="c1", scope="agentic")
    with pytest.raises(ServiceConflict, match="bound to another account scope"):
        service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="and?",
                              conversation_id="c1", account_id=B)
    with pytest.raises(ServiceConflict, match="bound to another account scope"):
        service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="and?", conversation_id="c1")
    second = service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="and?",
                                   conversation_id="c1", account_id=A)
    assert second["task"]["conversation_id"] == "c1"
    conversation = service.advisor_conversation("c1")
    assert (conversation.scope, conversation.account_scope_kind, conversation.account_id) == ("personal", "single", A)


def test_a_conversation_from_before_bindings_is_personal_and_takes_the_kind_of_its_next_message(service):
    manager = service._research_manager(service.profiles())
    legacy, _ = manager.create(run_key="advisor_legacy", ticker="ASSISTANT", bucket="advisory",
                               trade_date="2026-09-16", instrument_type="auto", trigger="manual",
                               task={"kind": "chat", "language": "zh", "account_id": None,
                                     "conversation_id": "old", "message": "earlier"})
    finish(service, legacy)
    assert service.advisor_conversation("old").account_scope_kind == "unknown"
    with pytest.raises(ServiceConflict, match="bound to another account scope"):
        service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="x", conversation_id="old",
                              scope="agentic")
    service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="x", conversation_id="old",
                          account_id=B)
    bound = service.advisor_conversation("old")
    assert (bound.account_scope_kind, bound.account_id) == ("single", B)


def test_a_replayed_assistant_request_aimed_at_another_account_conflicts(service):
    """A replay is matched on the account it named, not only on the question it asked.

    The idempotency key is the console's, and a client that derives one badly -- a retry that
    reuses the last key, a key made from the symbol -- makes two accounts collide on it. The
    danger is not a second run; it is being handed the first account's answer and reading it
    as this account's. The same holds for the all-accounts review, which is a third scope and
    not a superset a single account's request may be served from.
    """
    request_id = str(uuid.uuid4())
    first = service.start_advisor(kind="analysis", request_id=request_id, ticker="VTI", account_id=A)
    again = service.start_advisor(kind="analysis", request_id=request_id, ticker="VTI", account_id=A)
    assert again["run_key"] == first["run_key"] and again["deduplicated"] is True
    for other in ({"account_id": B}, {}):
        with pytest.raises(ServiceConflict, match="different request"):
            service.start_advisor(kind="analysis", request_id=request_id, ticker="VTI", **other)
    with pytest.raises(ServiceConflict, match="different request"):
        service.start_advisor(kind="chat", request_id=request_id, message="why?", scope="agentic",
                              conversation_id="ag")
    manager = service._research_manager(service.profiles())
    assert [j["run_key"] for j in manager.store.list(limit=10)] == [first["run_key"]]


# ------------------------------------------------------------------ what a scope sees
@pytest.fixture
def records(service):
    """Records for both accounts, the all-accounts view, a legacy personal one and the Agentic book."""
    manager = service._research_manager(service.profiles())
    for run_id, fields in (
            ("adv_a", {"account_id": A, "account_scope_kind": "single", "account_ids": []}),
            ("adv_all", {"account_id": None, "account_scope_kind": "all_personal", "account_ids": [A, B]}),
            ("adv_legacy", {"account_id": None})):
        record(service, run_id, bucket="advisory", advisory=True, task_kind="analysis", **fields)
        job, _ = manager.create(run_key=run_id, ticker="VTI", bucket="advisory", trade_date="2026-09-16",
                                instrument_type="auto", trigger="manual",
                                task={"kind": "analysis", "language": "zh", "scope": "personal",
                                      "account_id": fields["account_id"],
                                      "account_scope_kind": fields.get("account_scope_kind"),
                                      "account_ids": fields.get("account_ids") or []})
        finish(service, job)
    record(service, "micro_vti", bucket="micro_live")
    record(service, "core_vti", bucket="core")


def test_each_scope_sees_its_own_records_and_nothing_of_another(service, records):
    everyone = service.advisor_state()
    assert everyone.scope == "personal" and everyone.account_scope_kind == "all_personal"
    assert everyone.account_ids == [A, B] and everyone.account_label == "all personal accounts"
    assert {d.run_id for d in everyone.decisions} == {"adv_a", "adv_all", "adv_legacy"}
    assert {d.account_scope_kind for d in everyone.decisions} == {"single", "all_personal", "unknown"}

    only_a = service.advisor_state(account_id=A)
    assert only_a.account_label == "••••1111" and [a.id for a in only_a.snapshot.accounts] == [A]
    assert {d.run_id for d in only_a.decisions} == {"adv_a", "adv_all"}          # never the legacy one
    assert {j.run_key for j in only_a.jobs} == {"adv_a", "adv_all"}              # its own, and what covered it
    only_b = service.advisor_state(account_id=B)
    assert {d.run_id for d in only_b.decisions} == {"adv_all"}

    agentic = service.advisor_state(scope="agentic")
    assert agentic.snapshot.accounts == [] and agentic.snapshot.fetched_at is None
    assert [d.run_id for d in agentic.decisions] == ["micro_vti"] and agentic.decisions[0].scope == "agentic"
    assert agentic.account_id == "micro_live" and agentic.latest_review is None
    assert agentic.coverage_info["scope"] == "agentic" and "cannot act" in agentic.coverage

    with pytest.raises(ServiceError, match="unknown assistant scope"):
        service.advisor_state(scope="demo")
    with pytest.raises(ServiceError, match="one observed personal account"):
        service.advisor_state(scope="agentic", account_id=A)


def test_a_review_is_shown_with_its_own_time_and_goes_stale_when_the_snapshot_moves_on(service):
    job = service.start_advisor(kind="portfolio_review", request_id=str(uuid.uuid4()))
    review = {"scope": "personal", "account_scope_kind": "all_personal", "account_id": None,
              "account_ids": [A, B], "account_label": "all personal accounts",
              "snapshot_at": snapshot()["fetched_at"], "analysed_at": now(),
              "priorities": [{"symbol": "VTI", "recommendation": "hold", "rationale": "unchanged",
                              "citations": [], "missing": []}], "limitations": []}
    finish(service, job, assistant_result={"answer": "1 priority; 0 limitation(s)", "review": review})
    latest = service.advisor_state().latest_review
    assert latest["run_key"] == job["run_key"] and latest["stale"] is False
    assert latest["priorities"][0]["recommendation"] == "hold"
    # Refreshing the portfolio does not make the old review new: it is marked, and kept.
    service.state_db().write_meta(SNAPSHOT, snapshot(fetched_at="2030-01-01T00:00:00+00:00"))
    assert service.advisor_state().latest_review["stale"] is True
    assert service.advisor_state(account_id=A).latest_review["run_key"] == job["run_key"]  # it covered A
    assert service.advisor_state(scope="agentic").latest_review is None


# ------------------------------------------------------------------ what a model is told
def test_the_agentic_conversation_is_told_the_record_and_the_authority_and_nothing_personal(
        service, records, monkeypatch):
    captured = {}

    def reply(profiles, context, message, history, language):
        captured.update(context)
        return {"answer": "It was a reduction under the reduce rule.", "research_candidates": []}

    monkeypatch.setattr("agentic_trading.advisor.answer", reply)
    monkeypatch.setattr("agentic_trading.advisor.fetch_snapshot",
                        lambda *_: pytest.fail("the Agentic conversation never reads personal accounts"))
    job = service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="Why?", scope="agentic",
                                conversation_id="ag")
    assert run_task(service.profiles(), job) == 0
    assert captured["scope"] == "agentic" and "snapshot" not in captured
    assert {d["run_id"] for d in captured["research"]} == {"micro_vti"}
    assert set(captured["authority"]["actions"]) == {"BUY", "ADD", "REDUCE", "SELL"}
    assert "version" in captured["authority"] and "proposals" in captured
    written = json.loads((service.profiles().project_dir / "runs" / job["run_key"] / "decision_record.json").read_text())
    assert written["scope"] == "agentic" and written["route"]["name"] == "assistant"


def test_a_single_account_conversation_is_told_that_account_alone(service, records, monkeypatch):
    captured = {}

    def reply(profiles, context, message, history, language):
        captured.update(context)
        return {"answer": "ok", "research_candidates": []}

    monkeypatch.setattr("agentic_trading.advisor.answer", reply)
    job = service.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="My VTI?",
                                conversation_id="mine", account_id=A)
    assert run_task(service.profiles(), job) == 0
    assert captured["scope"] == "personal" and captured["account"]["account_id"] == A
    assert [a["id"] for a in captured["snapshot"]["accounts"]] == [A]
    assert {d["run_id"] for d in captured["research"]} == {"adv_a", "adv_all"}   # never legacy, never Agentic
    assert "risk" not in captured and "authority" not in captured


def test_a_portfolio_review_reuses_what_is_known_and_records_what_it_rests_on(service, monkeypatch):
    seen = {}

    def review(profiles, context, language="zh", llm=None):
        seen.update(context)
        return {"priorities": [{"symbol": "VTI", "recommendation": "needs_full_research",
                                "rationale": "23% in one name", "citations": [], "missing": ["cost basis"]}],
                "limitations": ["options are not read"]}

    monkeypatch.setattr("agentic_trading.advisor.review_portfolio", review)
    job = service.start_advisor(kind="portfolio_review", request_id=str(uuid.uuid4()))
    assert run_task(service.profiles(), job) == 0
    assert seen["scope"] == "personal" and seen["account"]["account_scope_kind"] == "all_personal"
    written = json.loads((service.profiles().project_dir / "runs" / job["run_key"] / "decision_record.json").read_text())
    out = written["assistant_result"]["review"]
    assert out["account_scope_kind"] == "all_personal" and out["account_ids"] == [A, B]
    assert out["snapshot_at"] == snapshot()["fetched_at"][:10] + out["snapshot_at"][10:]  # the snapshot's own time
    assert out["analysed_at"] and out["route"]["name"] == "assistant"
    assert out["priorities"][0]["recommendation"] == "needs_full_research"
    assert written["task_kind"] == "portfolio_review" and written["route"]["name"] == "assistant"
    # The worker, not run_task, records the terminal state; until then the review is not shown.
    assert service.advisor_state().latest_review is None
    service._research_manager(service.profiles()).store.update(job["run_key"], {"state": "COMPLETE"})
    assert service.advisor_state().latest_review["run_key"] == job["run_key"]
    # It is a research record too, findable by type, and it never became a proposal.
    assert [r["run_id"] for r in service.research_history(task_type="portfolio_review")] == [job["run_key"]]
    assert not service._store(service.profiles()).all()


# ------------------------------------------------------------------ the switcher
def test_the_account_scopes_come_from_readings_the_console_already_holds(service):
    scopes = service.account_scopes()
    by_label = {a["account_label"]: a for a in scopes["accounts"]}
    assert by_label["Agentic"]["scope"] == "agentic" and by_label["Agentic"]["account_id"] == "micro_live"
    assert by_label["••••1111"]["account_scope_kind"] == "single" and by_label["••••1111"]["complete"] is True
    assert by_label["all personal accounts"]["account_ids"] == [A, B]
    assert "Demo" not in by_label and scopes["errors"] == {}
    assert scopes["personal_snapshot"]["stale"] is False
