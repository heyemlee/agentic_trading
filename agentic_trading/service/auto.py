"""Automatic trading as the API, the CLI and the scheduler all reach it: one service, one answer.

What is here, in the order it came to be:

* the plan for an envelope a person names -- what it could invest, why not more, and how each
  setting it needs would land on the real account;
* the simulated account: started from a previewed plan, run, paused, funded, adjusted, and
  measured against doing something simpler;
* the real account: every condition for starting a plan there, the start itself, and the
  replacement of its strategy by one that has been proved.

None of it places an order. A plan in force means a pass reads research and writes proposals;
approving one is still ``approvals``' and nobody else's. And none of it changes a setting by
itself: what a plan needs changed goes through ``configuration`` exactly as a change typed into
Settings would, with the same password and the same day of waiting. Everything about the real
account is off until a person confirms a plan for it, behind the conditions in ``auto.gates``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from ..auto import advice, modes, plan
from ..config_changes import CREATABLE
from ..llm.claude_code import ClaudeCodeError
from ..micro_live import BUCKET as MICRO_BUCKET
from ..profile import Profiles
from ..proposals import utcnow
from .base import _last_session
from .errors import ServiceConflict, ServiceError

#: Named for the package rather than this module: the split is internal, so a log
#: record must still read as it did, and getLogger returns the one same object.
logger = logging.getLogger("agentic_trading.service")

#: The settings a plan may name that the configuration does not yet declare. Only these are
#: created; any other path that does not exist is refused, as a misspelt key always is. The
#: list is ``config_changes.CREATABLE``, kept beside the rules that classify each of them.
PLAN_CREATES = CREATABLE

#: How far behind the last session the book's last agreement with the venue may be dated and
#: still count. A weekend and a holiday; past that the check is not about the account as it is.
BOOK_CHECK_MAX_AGE_DAYS = 4


def _effect_on(path: str, classified: list[dict[str, Any]]) -> str:
    """What the console's rules made of one setting the plan would change.

    The classification names the leaf that differs, which is not always the path that was set:
    a setting created together with the block that holds it is reported as the block, whole. So
    the answer is looked for at the path, above it and below it, and the most cautious one found
    is the one given -- a new authority must not be shown as "applies at once" because it was
    filed under its parent. A path nothing was reported for already had that value.
    """
    from .. import config_changes as cc

    found = {item["effect"] for item in classified
             if item["path"] == path or path.startswith(item["path"] + ".")
             or item["path"].startswith(path + ".")}
    for effect in (cc.UNKNOWN, cc.LOOSENING, cc.TIGHTENING):
        if effect in found:
            return effect
    return cc.NEUTRAL


class AutoTradingMixin:
    """The plan, the modes, the simulated account, and the real account under a plan."""

    def _auto_snapshot(self, profiles: Profiles) -> tuple[Any, dict[str, Any]]:
        """The Agentic account as its ledger has it, priced for the last session, and what an
        order still working has already committed. No venue is read.

        Cash an unresolved order has reserved is taken out before anything is sized, so a plan
        never counts the same dollars twice. That understates the account by the same amount,
        which is the direction to err in: every limit is a share of it.
        """
        from ..portfolio import PortfolioError, build_snapshot, load_portfolio
        from ..schedule import ledger_for

        try:
            state = load_portfolio(ledger_for(profiles, MICRO_BUCKET),
                                   required_buckets=(MICRO_BUCKET,))
            working = [row for row in self.state_db(profiles).reservations(active_only=True)
                       if str(row.get("account_key")) == MICRO_BUCKET]
            committed = round(sum(float(row.get("cash_usd") or 0.0) for row in working), 2)
            if committed:
                state.buckets[MICRO_BUCKET].cash_usd = max(
                    0.0, round(state.buckets[MICRO_BUCKET].cash_usd - committed, 2))
            # The newest session with prices, or the day the book was last confirmed if that is
            # later -- which it is every morning before the close, and all of a Monday: a book
            # confirmed today is not "ahead" of anything, it is waiting for today's bar. A date
            # past today is still refused, by build_snapshot, as the mistake it would be.
            from zoneinfo import ZoneInfo

            priced_for = _last_session()
            today = utcnow().astimezone(ZoneInfo("America/New_York")).date().isoformat()
            if priced_for < str(state.as_of) <= today:
                priced_for = str(state.as_of)
            snap = build_snapshot(state, profiles, priced_for)
        except (PortfolioError, OSError, ValueError) as exc:
            raise ServiceError(f"the Agentic ledger could not be read and priced, so there is "
                               f"nothing to plan against: {exc}") from exc
        return snap, {"open_orders": len(working), "committed_usd": committed}

    def auto_plan(self, *, mode: str = modes.STEADY, capital_usd: float | None = None,
                  loss_pause_pct: float | None = None, ask_model: bool = False,
                  template: str | None = None) -> dict[str, Any]:
        """The plan for an envelope, with what the account could order today and under it.

        ``capital_usd`` defaults to what the account holds now: the plan is validated at the
        size the account already is, and asks nobody to add money first. ``ask_model`` asks the
        model for the strategy parameters, which spends the subscription research runs on; left
        off, the deterministic template is used and the plan says so. Nothing is written.

        ``template`` is which of the mode's templates the person chose; absent, the mode's first.
        The baseline template has nothing for a model to choose -- its numbers are the owner's
        weights and the template's own rules -- so no model is asked for one.
        """
        profiles = self.profiles()
        snap, working = self._auto_snapshot(profiles)
        bucket = snap.buckets[MICRO_BUCKET]
        equity = bucket.allocation_usd or bucket.equity
        if equity <= 0:
            raise ServiceError("the Agentic account holds nothing, so there is nothing to plan with")
        envelope = plan.Envelope(
            mode=mode,
            capital_usd=float(capital_usd) if capital_usd is not None else round(equity, 2),
            loss_pause_pct=(float(loss_pause_pct) if loss_pause_pct is not None
                            else plan.DEFAULT_LOSS_PAUSE_PCT))
        try:
            suggestion, by, error = None, "template", None
            if ask_model and template != plan.BASELINE:
                suggestion, by, error = self._auto_suggestion(profiles, snap, envelope)
            document = plan.build(profiles, snap, envelope, suggestion=suggestion,
                                  suggested_by=by, suggestion_error=error, template=template)
        except plan.PlanError as exc:
            raise ServiceError(str(exc)) from exc
        document["deployment"] = self._auto_deployment_preview(document["strategy"])
        # An order still working holds its cash, and the account places one order at a time.
        document["account"].update(working)
        if working["open_orders"]:
            document["summary"] = {
                "zh": document["summary"]["zh"] + (
                    f" 账户里还有 {working['open_orders']} 笔订单未了结，占用现金 "
                    f"${working['committed_usd']:,.2f}；它成交或取消之前不会下新单。"),
                "en": document["summary"]["en"] + (
                    f" {working['open_orders']} order(s) in the account are still unresolved, "
                    f"holding ${working['committed_usd']:,.2f}; no new order is placed until "
                    "they settle."),
            }
        document["landing"] = self._auto_landing(profiles, document["changes"])
        document["modes"] = self.auto_status()["modes"]
        document["checked_at"] = utcnow().isoformat()
        return document

    def _auto_suggestion(self, profiles: Profiles, snap, envelope: plan.Envelope):
        """(the model's strategy, who suggested, why not) -- the template stands in on failure."""
        try:
            plan.check_envelope(profiles, envelope)
            bounds = plan.bounds_for(profiles, envelope)
            template = plan.build(profiles, snap, envelope)
            facts = {"account": template["account"],
                     "today": {k: template["today"][k] for k in ("any_order", "deployable_usd", "minimum_order")},
                     "under_plan": {k: template["under_plan"][k]
                                    for k in ("any_order", "deployable_usd", "positions", "path", "pace")},
                     "template": template["strategy"]}
            return advice.suggest(profiles, envelope, bounds, facts), "model", None
        except ClaudeCodeError as exc:
            reason = ("the Claude login on this Mac has expired" if exc.login_expired else
                      "the Claude subscription limit was reached" if exc.rate_limited else
                      f"the model call failed: {exc}")
            return None, "template", reason[:300]
        except Exception as exc:  # noqa: BLE001 - a suggestion is optional; the template is not
            logger.warning("the plan's model suggestion failed: %s", exc)
            return None, "template", f"the model's answer could not be used: {type(exc).__name__}"[:300]

    def _auto_landing(self, profiles: Profiles, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """How the plan's changes would land on the real account, by the console's own rules.

        Each is put to the same classification every console change gets, so the plan cannot
        describe a loosening as anything else. Nothing is written here: this says "this would
        need your password and a day's wait", and submitting it is a separate, deliberate act.
        """
        import copy
        import json as _json

        from .. import config_changes as cc
        from .base import _directory_stamp

        # The answer depends on two things only: the configuration as it is on disk, and the
        # changes. Working it out rewrites and reloads the whole risk profile, a quarter of a
        # second measured, and the screen that shows it polls -- so it is kept until either moves.
        key = (_directory_stamp(profiles.config_dir),
               _json.dumps(changes, sort_keys=True, default=str, ensure_ascii=False))
        kept = getattr(self, "_auto_landing_kept", None)
        if kept is not None and kept[0] == key:
            return copy.deepcopy(kept[1])

        by_file: dict[str, dict[str, Any]] = {}
        for change in changes:
            by_file.setdefault(change["file"], {})[change["path"]] = change["to"]
        rows: list[dict[str, Any]] = []
        overall = cc.IMMEDIATE
        errors: list[str] = []
        for file, updates in by_file.items():
            try:
                current = self._config_text(profiles, file)
                new_text = cc.set_values(current, updates, create=PLAN_CREATES & set(updates))
                cc.validate(profiles.config_dir, file, new_text)
                verdict = cc.classify(file, current, new_text)
            except (cc.ConfigChangeError, ServiceError) as exc:
                errors.append(f"{file}: {exc}"[:300])
                continue
            if verdict["classification"] == cc.COOLING:
                overall = cc.COOLING
            for path in updates:
                effect = _effect_on(path, verdict["changes"])
                rows.append({"file": file, "path": path, "effect": effect,
                             "waits": effect in (cc.LOOSENING, cc.UNKNOWN)})
        waiting = [row["path"] for row in rows if row["waits"]]
        answer = {
            "classification": overall,
            "changes": rows,
            "needs_password": bool(waiting),
            "cooling_hours": cc.COOLING_HOURS if waiting else 0,
            "errors": errors,
            "summary": {
                "zh": (f"其中 {len(waiting)} 项会放宽现有限制：在实盘账户上需要控制台密码确认，并等待 "
                       f"{cc.COOLING_HOURS} 小时后生效，期间可以撤回。其余立即生效。" if waiting else
                       "这些调整都不放宽现有限制，确认后立即生效。") if rows else "不需要调整任何设置。",
                "en": (f"{len(waiting)} of these loosen a limit: on the real account they need the "
                       f"console password and take effect after {cc.COOLING_HOURS} hours, and can be "
                       "withdrawn until then. The rest apply at once." if waiting else
                       "None of these loosens a limit; they apply once confirmed.")
                if rows else "No setting needs to change.",
            },
        }
        self._auto_landing_kept = (key, answer)
        return copy.deepcopy(answer)

    def auto_status(self) -> dict[str, Any]:
        """Where automatic trading stands: every mode, open or not, and why not.

        What a mode has on its record is read from the simulated account, where the record is
        made: the sessions its strategy version was actually run through, and whether that
        version has carried a position from purchase to sale. Nothing here is asserted.
        """
        from ..auto import paper, store

        profiles = self.profiles()
        simulation = self.auto_simulation()
        days: dict[str, int] = {}
        verified: dict[str, bool] = {}
        version = simulation.get("version")
        if version:
            days[version["mode"]] = int(version["validation"].get("simulated_days") or 0)
            verified[version["mode"]] = bool(version["validation"].get("exits_verified"))
        # What a person has started on the real account, now and before. In the simulated
        # account's own service there is no real account to speak of.
        real: dict[str, Any] = {"plan": None, "open": False, "request": None}
        opened: tuple[str, ...] = ()
        if not paper.is_paper(profiles):
            db = self.state_db(profiles)
            active = store.active_plan(db)
            opened = store.opened_modes(db)
            waiting = store.start_request(db)
            real = {"plan": None if active is None else {
                        key: active.get(key) for key in ("plan_id", "envelope", "opening_version",
                                                         "managing", "confirmed_at", "confirmed_by")},
                    "open": active is not None,
                    "mode": (active or {}).get("envelope", {}).get("mode"),
                    "state": _public_state(store.state(db)),
                    "opened_modes": list(opened),
                    "request": None if waiting is None else _public_request(waiting)}
        rows = modes.status(profiles, simulated_days=days, exits_verified=verified, opened=opened)
        for row in rows:
            # Open means a plan of this mode is in force there now -- not that one once was.
            row["open_for_real_money"] = bool(real["open"]) and real.get("mode") == row["key"]
        return {"checked_at": utcnow().isoformat(),
                "account": {"scope": "agentic", "account_id": MICRO_BUCKET},
                "modes": rows,
                # What is not a mode and does not open because a mode did: single stocks.
                "extensions": [modes.stock_extension(profiles)],
                "simulation": simulation,
                "real_account": real}

    # ---------------------------------------------------------------- the simulated account
    def _auto_paper(self, *, required: bool = True):
        """The simulated account's own service -- the same code over its own directory -- or None."""
        from ..auto import paper

        live = self.profiles()
        if paper.is_paper(live):
            raise ServiceError("this is already the simulated account; it is run from the real "
                               "account's console and has none of its own")
        try:
            prepared = paper.profiles_of(live)
        except paper.PaperError as exc:
            raise ServiceError(str(exc)) from exc
        if prepared is None:
            if required:
                raise ServiceError("the simulated account has not been started; preview a plan "
                                   "and start the simulation first")
            return None
        kept = getattr(self, "_auto_paper_service", None)
        if kept is None or kept.config_dir != prepared.config_dir:
            kept = type(self)(prepared.config_dir)
            self._auto_paper_service = kept
        return kept

    def _auto_document(self, profiles: Profiles, snap, *, mode: str, capital_usd: float | None,
                       loss_pause_pct: float | None, strategy: dict[str, Any] | None,
                       as_given: bool = False, template: str | None = None) -> dict[str, Any]:
        """The plan for an envelope and, when one is handed back, for the strategy it was shown with.

        A preview is not stored anywhere, so confirming one means building it again and finding
        it unchanged. A strategy a model suggested has to come back with the confirmation for
        that to be possible -- asking the model a second time would be asking a different
        question. Whatever comes back is clamped like any other strategy, so handing one in is
        never a way round the bounds.

        ``as_given`` takes the strategy handed in even when a template wrote it. The real
        account is started with the strategy that was simulated, and a template worked out
        again today -- for an account that has since changed size -- could be a different one.
        """
        bucket = snap.buckets[MICRO_BUCKET]
        equity = bucket.allocation_usd or bucket.equity
        envelope = plan.Envelope(
            mode=mode,
            capital_usd=float(capital_usd) if capital_usd is not None else round(equity, 2),
            loss_pause_pct=(float(loss_pause_pct) if loss_pause_pct is not None
                            else plan.DEFAULT_LOSS_PAUSE_PCT))
        suggestion, by, error = None, "template", None
        if strategy:
            # Why the model's own suggestion was not used, when it was asked and could not be:
            # part of what the person read, so part of what they are confirming.
            error = strategy.get("suggestion_error")
        if strategy and (as_given or str(strategy.get("suggested_by") or "template") != "template"):
            rationale = strategy.get("rationale") or {}
            try:
                suggestion = plan.Strategy(
                    **{key: strategy[key] for key in plan.Strategy.model_fields
                       if key in strategy and not key.startswith("rationale")},
                    rationale_zh=str(rationale.get("zh") or ""), rationale_en=str(rationale.get("en") or ""))
            except (TypeError, ValueError) as exc:
                raise ServiceError(f"the strategy handed back is not one a plan can use: {exc}") from exc
            by = str(strategy.get("suggested_by") or "template")[:40]
        try:
            # The template the person chose, or the one the strategy handed back was shown with.
            document = plan.build(profiles, snap, envelope, suggestion=suggestion, suggested_by=by,
                                  suggestion_error=error,
                                  template=template or (strategy or {}).get("template") or None)
        except plan.PlanError as exc:
            raise ServiceError(str(exc)) from exc
        document["deployment"] = self._auto_deployment_preview(document["strategy"])
        return document

    def _auto_deployment_preview(self, strategy: dict[str, Any]) -> dict[str, Any] | None:
        """A baseline plan's block before anything is bought under it: what research has said of
        each fund, read from the real account's runs, which both accounts are researched by."""
        from ..auto import runner, trader

        if not trader.is_baseline(strategy):
            return None
        deployment = strategy.get("deployment") or {}
        try:
            window = int((deployment.get("rules") or {}).get("veto_window_days") or 14)
            research = trader.research_by_fund(trader.records_in(self.profiles().project_dir / "runs"),
                                               window_days=window)
            return trader.preview_view(deployment, research=research,
                                       trade_date=runner.session_of(utcnow()))
        except Exception as exc:  # noqa: BLE001 - a block that cannot be read says so, never guesses
            logger.warning("the baseline plan's deployment could not be read: %s", exc)
            return _deployment_unavailable(exc)

    def _auto_deployment(self, db: Any, active: dict[str, Any] | None) -> dict[str, Any] | None:
        """A baseline plan's block for an account: as its last pass read it, or, before the plan's
        first pass, as a preview. Each account's own (contract 3.6)."""
        from ..auto import trader

        strategy = (active or {}).get("strategy") or {}
        if not trader.is_baseline(strategy):
            return None
        try:
            last = trader.last_deployment(db)
            if last and last.get("plan_id") == active.get("plan_id") \
                    and last.get("version") == active.get("opening_version"):
                return last
        except Exception as exc:  # noqa: BLE001 - as above
            logger.warning("the last pass's deployment could not be read: %s", exc)
            return _deployment_unavailable(exc)
        return self._auto_deployment_preview(strategy)

    def auto_start(self, *, plan_id: str, mode: str = modes.STEADY, capital_usd: float | None = None,
                   loss_pause_pct: float | None = None, strategy: dict[str, Any] | None = None,
                   scope: str = "paper", identity: str | None = None,
                   confirmed: bool = False, template: str | None = None) -> dict[str, Any]:
        """Put a previewed plan in force: in the simulated account, or behind every gate, the real one.

        ``plan_id`` is the plan the person was shown. It is built again from the same inputs and
        must come out the same -- if the account or the configuration moved in between, what
        they would be confirming is no longer what they read, and they are asked to look again.

        Starting the simulation changes nothing real: no setting in ``config/`` is written, no
        order can reach a venue (``auto.paper``), and the real account's books are not read
        for anything but their size. The simulated account opens with what the real one holds,
        so every limit that is a share of the account is the same share of the same amount.

        ``scope="live"`` is the real account and is :meth:`_auto_start_live`; ``confirmed`` says
        the caller re-verified the console password, which that path requires.
        """
        from ..auto import paper, runner, store

        if scope == "live":
            return self._auto_start_live(plan_id=plan_id, mode=mode, capital_usd=capital_usd,
                                         loss_pause_pct=loss_pause_pct, strategy=strategy,
                                         identity=identity, confirmed=confirmed)
        if scope != "paper":
            raise ServiceError(f"unknown scope {scope!r}; a plan runs in the simulated account "
                               "(paper) or the real one (live)")
        profiles = self.profiles()
        if mode == modes.INTRADAY:
            raise ServiceError("the intraday mode cannot be simulated yet: minute data, its "
                               "freshness checks and the in-session rule runner are not built")
        snap, _working = self._auto_snapshot(profiles)
        document = self._auto_document(profiles, snap, mode=mode, capital_usd=capital_usd,
                                       loss_pause_pct=loss_pause_pct, strategy=strategy,
                                       template=template)
        if document["plan_id"] != plan_id:
            raise ServiceConflict("the plan is no longer the one that was previewed -- the account "
                                  "or the configuration has moved since; preview it again")
        try:
            config = paper.ensure(profiles, changes=document["changes"], creates=PLAN_CREATES,
                                  plan_id=document["plan_id"])
            sim = self._auto_paper()
            sim_profiles = sim.profiles()
            session = runner.session_of(utcnow())
            opened = paper.open_account(sim_profiles, cash_usd=document["account"]["equity_usd"],
                                        as_of=session)
        except paper.PaperError as exc:
            raise ServiceError(str(exc)) from exc
        db = sim.state_db(sim_profiles)
        # Where the new plan's loss line is measured from: what automatic trading here has made
        # or lost so far. Unreadable is not zero -- the line is left where it was.
        result = None
        if store.versions(db):
            try:
                loss = self._auto_result(sim, sim_profiles, session)
                result = loss["total_usd"] if loss and loss.get("known") else None
            except Exception as exc:  # noqa: BLE001 - the plan is still confirmed; the line stays
                logger.warning("the simulated account's result could not be read at start: %s", exc)
        active = store.confirm_plan(db, sim_profiles, document, identity=identity or "console",
                                    scope="paper", result_usd=result if result is not None else (
                                        0.0 if not store.versions(db) else None))
        for target, where in ((sim, sim_profiles), (self, profiles)):
            target._journal(where, kind="auto_plan_started", subject=document["plan_id"],
                            actor=identity or "console", outcome="active",
                            detail={"scope": "paper", "mode": mode,
                                    "opening_version": active["opening_version"],
                                    "capital_usd": document["envelope"]["capital_usd"],
                                    "config": str(config), "account_opened": opened["opened"]})
        return {"started": True, "scope": "paper", "plan_id": document["plan_id"],
                "opening_version": active["opening_version"], "account_opened": opened["opened"],
                "simulation": self.auto_simulation()}

    # ---------------------------------------------------------------- the real account
    #
    # Everything below is about the Agentic account itself. None of it places an order: a plan
    # in force only means the pass in ``auto.runner`` reads research and writes proposals for
    # this account, which ``Service.autonomous_reductions`` then approves or does not, down the
    # one path every order takes (service/approvals.py). And none of it turns anything on that a
    # person did not: the settings a plan needs go through the console's own confirmation, with
    # its password and its day of waiting, exactly as if they had been typed into Settings.

    def _auto_real(self) -> Profiles:
        """The real account's profiles, refusing to be the simulated account's."""
        from ..auto import paper

        profiles = self.profiles()
        if paper.is_paper(profiles):
            raise ServiceError("this is the simulated account; the real account is run from its "
                               "own console")
        return profiles

    def _auto_account_facts(self, profiles: Profiles, document: dict[str, Any]) -> dict[str, Any]:
        """The real account as it stands, in the terms the gates ask about. Reads no venue."""
        from datetime import date

        from .. import autonomy
        from ..auto import positions
        from ..ledger_confirm import REPORT
        from ..portfolio import PortfolioError, load_portfolio
        from ..proposals import UNKNOWN, needs_reconciliation
        from ..schedule import ledger_for

        db = self.state_db(profiles)
        everything = self._store(profiles).all()
        unresolved = [p.proposal_id for p in everything if p.bucket == MICRO_BUCKET
                      and (needs_reconciliation(p) or p.status == UNKNOWN or p.reconciliation_error)]
        halt = autonomy.kill_switch_state(profiles)
        downgrade = autonomy.load_downgrade(profiles)

        # The last check of the book against the venue, whoever ran it. It has to have agreed,
        # and to be about a recent session: an agreement from last week says nothing about what
        # has been bought in the venue's own app since.
        report = db.read_meta(REPORT, {}) or {}
        row = next((item for item in report.get("ledgers") or []
                    if isinstance(item, dict) and item.get("ledger") == MICRO_BUCKET), None)
        book: dict[str, Any] = {"confirmed": False, "as_of": (row or {}).get("as_of"), "error": None}
        if row is None:
            book["error"] = str(report.get("detail") or report.get("error") or "") or None
        elif not row.get("confirmed"):
            book["error"] = str(row.get("error") or "the check did not agree")[:300]
        else:
            try:
                behind = (date.fromisoformat(_last_session()) - date.fromisoformat(str(row["as_of"]))).days
            except (TypeError, ValueError, KeyError):
                behind = None
            if behind is None or behind > BOOK_CHECK_MAX_AGE_DAYS:
                book["error"] = (f"the last check that agreed is dated {row.get('as_of')}, and the "
                                 f"last session is {_last_session()}")
            else:
                book["confirmed"] = True

        unexplained: list[str] = []
        try:
            ledger = load_portfolio(ledger_for(profiles, MICRO_BUCKET))
            held = positions.book(fills=self._fills_for_replay(profiles), proposals=everything,
                                  ledger=ledger, bucket=MICRO_BUCKET)
            unexplained = [view.symbol for view in held if not view.known]
        except (PortfolioError, OSError, ValueError) as exc:
            unexplained = [f"the ledger could not be read ({type(exc).__name__})"]
        under = document["under_plan"]
        return {"real_money_on": self._micro_enabled(profiles),
                "halted": (halt.get("reason") or "engaged") if halt.get("engaged") else None,
                "autonomy_withdrawn": (downgrade.reason or "withdrawn") if downgrade.tripped else None,
                "unresolved_orders": unresolved, "book": book, "unexplained": unexplained,
                "any_order": bool(under.get("any_order")),
                "why_no_order": None if under.get("any_order") else document.get("summary")}

    def _auto_simulated_record(self, version_id: str) -> dict[str, Any]:
        """What the simulated account has on record for one exact strategy version."""
        from ..auto import runner, store

        record: dict[str, Any] = {"exists": False, "counted": 0, "exits_verified": False,
                                  "simulated_version": None, "simulated_version_days": 0}
        sim = self._auto_paper(required=False)
        if sim is None:
            return record
        sim_profiles = sim.profiles()
        sim_db = sim.state_db(sim_profiles)
        running = store.active_plan(sim_db)
        record.update(exists=True, counted=store.simulated_days(sim_db, version_id),
                      exits_verified=runner.exits_verified(sim, sim_profiles, version_id))
        if running is not None:
            record.update(simulated_version=running["opening_version"],
                          simulated_version_days=store.simulated_days(sim_db, running["opening_version"]))
        return record

    def _auto_settings_needed(self, profiles: Profiles, document: dict[str, Any]) -> list[dict[str, Any]]:
        """The plan's settings that are not in force in the real configuration."""
        return [change for change in document["changes"] if not _already_covered(change, profiles)]

    def _auto_live_gates(self, profiles: Profiles, document: dict[str, Any]) -> list[Any]:
        """Every condition for putting this plan in force on the real account, as it stands now."""
        from .. import config_changes as cc
        from ..auto import gates as gating
        from ..auto import store

        mode = document["envelope"]["mode"]
        version_id = store.version_id(mode, document["strategy"],
                                      store.code_identity(profiles, document["strategy"]))
        waiting = tuple(change["path"] for change in self._auto_settings_needed(profiles, document))
        return gating.evaluate(
            profiles, mode=mode, version_id=version_id,
            record=self._auto_simulated_record(version_id),
            account=self._auto_account_facts(profiles, document),
            opened=store.opened_modes(self.state_db(profiles)),
            settings_waiting=waiting, cooling_hours=cc.COOLING_HOURS)

    def _auto_advisories(self, profiles: Profiles, document: dict[str, Any]) -> list[dict[str, Any]]:
        """What a person should know before real money that is not a condition of starting.

        Said in the plan rather than left to be discovered: none of these is something code can
        check, and none of them is something code should decide is fine.
        """
        from ..proposals import EXECUTED, PARTIALLY_FILLED

        out: list[dict[str, Any]] = []
        placed = [p for p in self._store(profiles).all() if p.bucket == MICRO_BUCKET
                  and p.status in (EXECUTED, PARTIALLY_FILLED) and p.client_order_id]
        if not placed:
            out.append({"code": "first_real_order", "text": {
                "zh": "这个账户还没有通过本系统成交过任何一笔真实订单。方案的第一笔自动订单，也会是这条实盘下单链路的第一笔真实订单。"
                      "如果你想先看它走一遍，可以先在批准页人工批准一笔小额订单。",
                "en": "No real order has yet been filled through this system in this account. The "
                      "plan's first automatic order would also be the first real order this "
                      "execution path has carried. To see it work first, approve one small order "
                      "by hand on the approvals page."}})
        venue = document.get("venue") or {}
        if venue and not venue.get("confirmed_for_account"):
            out.append({"code": "venue_requirements_unconfirmed", "text": {
                "zh": f"券商的下单要求（最低 ${float(venue.get('min_order_usd') or 0):,.2f} 等）读自其帮助页面，"
                      "没有在这个账户上向券商确认过。如果券商拒绝某笔订单，自动权限会被收回并等你处理，不会重试。",
                "en": "The venue's order requirements (a minimum of "
                      f"${float(venue.get('min_order_usd') or 0):,.2f} among them) are read from its "
                      "support pages and have not been confirmed for this account. If the venue "
                      "refuses an order, autonomous action is withdrawn and waits for you; nothing "
                      "is retried."}})
        out.append({"code": "pause_is_not_a_guarantee", "text": {
            "zh": f"亏损暂停线（${float(document['envelope']['loss_pause_usd']):,.2f}）是一个本地检查：到线后停止新开仓，"
                  "已有仓位仍按各自的止损和期限退出。它不是最大亏损的保证——跳空、停牌、行情或券商中断、"
                  "这台电脑离线时，实际亏损都可能超过这条线。",
            "en": f"The loss line (${float(document['envelope']['loss_pause_usd']):,.2f}) is a local "
                  "check: at it, nothing new is opened, and what is held still leaves on its own "
                  "stop and term. It is not a guarantee of the most that can be lost -- a gap, a "
                  "halt, an outage of the data or the venue, or this machine being offline can "
                  "each take the loss past it."}})
        return out

    def _auto_candidate(self, profiles: Profiles) -> dict[str, Any]:
        """What would be started on the real account: the plan the simulated account is running.

        The real account is not offered a plan of its own design. It is offered the one that has
        a record -- the same envelope and the same strategy, built again against the real
        account as it stands now -- with every gate beside it.
        """
        from ..auto import gates as gating
        from ..auto import store

        sim = self._auto_paper(required=False)
        running = store.active_plan(sim.state_db(sim.profiles())) if sim is not None else None
        if running is None:
            return {"available": False, "why": {
                "zh": "模拟账户里没有正在运行的方案。实盘启动的是已经模拟过的那个方案，所以要先启动模拟。",
                "en": "No plan is running in the simulated account. What starts on the real "
                      "account is the plan that has been simulated, so a simulation comes first."}}
        try:
            snap, _working = self._auto_snapshot(profiles)
            envelope = running["envelope"]
            document = self._auto_document(
                profiles, snap, mode=envelope["mode"], capital_usd=float(envelope["capital_usd"]),
                loss_pause_pct=float(envelope["loss_pause_pct"]), strategy=running["strategy"],
                as_given=True)
        except ServiceError as exc:
            return {"available": False, "why": {
                "zh": f"现在无法为实盘账户生成这个方案：{exc}",
                "en": f"The plan cannot be drawn for the real account at the moment: {exc}"}}
        document["landing"] = self._auto_landing(profiles, document["changes"])
        found = self._auto_live_gates(profiles, document)
        return {"available": True, "plan": document,
                "inputs": {"plan_id": document["plan_id"], "mode": envelope["mode"],
                           "capital_usd": float(envelope["capital_usd"]),
                           "loss_pause_pct": float(envelope["loss_pause_pct"]),
                           "strategy": document["strategy"]},
                "version_id": store.version_id(envelope["mode"], document["strategy"],
                                               store.code_identity(profiles, document["strategy"])),
                "gates": [gate.as_dict() for gate in found],
                "may_confirm": gating.may_confirm(found), "open": gating.is_open(found),
                "advisories": self._auto_advisories(profiles, document)}

    def auto_live(self) -> dict[str, Any]:
        """The real account under automatic trading: what is in force, what waits, what it would take.

        Read-only. ``candidate`` is the plan that could be started -- the simulated one, drawn
        against the real account -- with each condition as a line that holds or does not;
        ``request`` is a start a person confirmed that is waiting for its settings; ``plan`` is
        what is in force. At most one of the last two is ever present.
        """
        from ..auto import positions, replacement, store
        from ..portfolio import PortfolioError, load_portfolio
        from ..schedule import ledger_for

        profiles = self._auto_real()
        db = self.state_db(profiles)
        active = store.active_plan(db)
        record = store.plan_record(db)
        everything = self._store(profiles).all()
        fills = self._fills_for_replay(profiles)
        held: list[Any] = []
        try:
            ledger = load_portfolio(ledger_for(profiles, MICRO_BUCKET))
            held = positions.book(fills=fills, proposals=everything, ledger=ledger, bucket=MICRO_BUCKET)
        except (PortfolioError, OSError, ValueError) as exc:
            logger.warning("the Agentic ledger could not be read for the automatic-trading view: %s", exc)
        waiting = store.start_request(db)
        last = (record.get("history") or [None])[-1]
        marks = store.equity_marks(db)
        return {
            "checked_at": utcnow().isoformat(),
            "account": {"scope": "agentic", "account_id": MICRO_BUCKET},
            "plan": None if active is None else {
                key: active.get(key) for key in ("plan_id", "envelope", "strategy", "opening_version",
                                                 "managing", "confirmed_at", "confirmed_by",
                                                 "adjusted_at", "adjusted_by", "deployable_usd")},
            "state": _public_state(store.state(db)),
            "record": self._auto_record(db),
            "request": None if waiting is None else _public_request(waiting),
            # Why nothing is in force, when the last thing that happened was the plan ending.
            "ended": None if active is not None or not isinstance(last, dict)
            or last.get("status") != "withdrawn" else {
                key: last.get(key) for key in ("plan_id", "withdrawn_at", "withdrawn_by",
                                               "withdrawn_reason", "opening_version")},
            "positions": [view.as_dict() for view in held],
            "trades": positions.closed_trades(fills=fills, proposals=everything,
                                              bucket=MICRO_BUCKET)[-20:],
            "account_mark": marks[max(marks)] if marks else None,
            "last_pass": store.last_pass(db),
            "candidate": self._auto_candidate(profiles),
            "deployment": self._auto_deployment(db, active),
            # Whether a validated strategy takes over by itself under this plan, and what the
            # last look at one found. Off unless a person turned it on.
            "replacement": {"automatic": bool(store.option(db, "auto_replace", False)),
                            "last": (replacement.history(db) or [None])[-1],
                            "looked": len(replacement.history(db))},
        }

    def _auto_start_live(self, *, plan_id: str, mode: str, capital_usd: float | None,
                         loss_pause_pct: float | None, strategy: dict[str, Any] | None,
                         identity: str | None, confirmed: bool) -> dict[str, Any]:
        """Confirm a plan for the real account. Refused unless every condition holds.

        The order of what happens is the order of what could go wrong:

        1. the plan is built again and must be the one that was shown;
        2. every gate but the plan's own settings has to hold -- the simulated record of this
           exact strategy version, the account in order, the mode's turn. If one does not,
           nothing is written and the answer is the list of what does not;
        3. only then is the console password asked for;
        4. if the plan needs settings changed, they are submitted through the console's own
           confirmation and the start *waits*: nothing is in force, and what loosens a limit
           takes its day and can be withdrawn. The plan goes into force by itself once nothing
           is waiting and every gate holds at that moment (:meth:`_auto_live_activate`), or
           lapses if that does not happen in time;
        5. if nothing needs changing, it is in force now.
        """
        from ..auto import gates as gating
        from ..auto import store

        profiles = self._auto_real()
        if mode == modes.INTRADAY:
            raise ServiceError("automatic trading is not open on the real account for the "
                               "intraday mode: it is not built")
        snap, _working = self._auto_snapshot(profiles)
        document = self._auto_document(profiles, snap, mode=mode, capital_usd=capital_usd,
                                       loss_pause_pct=loss_pause_pct, strategy=strategy,
                                       as_given=True)
        if document["plan_id"] != plan_id:
            raise ServiceConflict("the plan is no longer the one that was previewed -- the account "
                                  "or the configuration has moved since; preview it again")
        found = self._auto_live_gates(profiles, document)
        if not gating.may_confirm(found):
            raise ServiceError("automatic trading is not open on the real account, and it cannot "
                               "be opened from here: "
                               + gating.refusal(found, ignore=(gating.SETTINGS_PENDING,)))
        if not confirmed:
            raise ServiceError("starting a plan on the real account needs the console password")
        db = self.state_db(profiles)
        who = identity or "console"
        needed = self._auto_settings_needed(profiles, document)
        if not needed:
            active = self._auto_put_in_force(profiles, document, identity=who, how="confirmed")
            return {"started": True, "scope": "live", "plan_id": document["plan_id"],
                    "opening_version": active["opening_version"], "waiting": None,
                    "live": self.auto_live()}

        submitted = self._auto_submit_settings(profiles, document, needed, identity=who)
        now = utcnow()
        from datetime import timedelta

        request = store.write_request(db, {
            "plan_id": document["plan_id"], "status": "waiting",
            "requested_at": now.isoformat(), "requested_by": who,
            "expires_at": (now + timedelta(hours=store.REQUEST_HOURS)).isoformat(),
            # What was confirmed, kept whole: the plan is built again from exactly this when its
            # settings land, and the document is what goes on record if it then goes into force.
            "inputs": {"mode": mode, "capital_usd": float(document["envelope"]["capital_usd"]),
                       "loss_pause_pct": float(document["envelope"]["loss_pause_pct"]),
                       "strategy": document["strategy"]},
            "version_id": store.version_id(mode, document["strategy"],
                                           store.code_identity(profiles, document["strategy"])),
            "document": document,
            "changes": [{"id": row["id"], "file": row["file"], "status": row["status"],
                         "apply_after": row.get("apply_after"), "paths": row.get("paths")}
                        for row in submitted],
            "waiting_for": [change["path"] for change in needed]})
        self._journal(profiles, kind="auto_plan_requested", subject=document["plan_id"], actor=who,
                      outcome="waiting",
                      detail={"scope": "live", "mode": mode, "version": request["version_id"],
                              "changes": [row["id"] for row in submitted],
                              "expires_at": request["expires_at"]})
        return {"started": False, "scope": "live", "plan_id": document["plan_id"],
                "opening_version": None, "waiting": _public_request(request),
                "live": self.auto_live()}

    def _auto_submit_settings(self, profiles: Profiles, document: dict[str, Any],
                              needed: list[dict[str, Any]], *, identity: str) -> list[dict[str, Any]]:
        """Put the plan's settings through the console's confirmation, one change per file.

        One change per file, so the settings of a plan land together or not at all: an
        automatic-buying switch that came on a day before the cap on what it may spend would be
        the opposite of what the plan asked for.
        """
        from .. import config_changes as cc

        by_file: dict[str, dict[str, Any]] = {}
        for change in needed:
            by_file.setdefault(change["file"], {})[change["path"]] = change["to"]
        out = []
        for file, updates in by_file.items():
            spec = cc.ChangeSpec(file=file, set=dict(updates),
                                 create=frozenset(PLAN_CREATES & set(updates)))
            out.append(self._submit_change(
                spec, reason=f"automatic-trading plan {document['plan_id']}", identity=identity,
                confirmed=True))
        return out

    def _auto_put_in_force(self, profiles: Profiles, document: dict[str, Any], *, identity: str,
                           how: str) -> dict[str, Any]:
        """Record the plan as in force on the real account. The gates were the caller's to hold."""
        from ..auto import runner, store, trader

        db = self.state_db(profiles)
        # Where the new plan's loss line is measured from: what automatic trading here has made
        # or lost so far. Unreadable is not zero -- the line is left where it was.
        result: float | None = 0.0
        if store.versions(db):
            result = None
            try:
                loss = trader.result_now(self, trade_date=runner.session_of(utcnow()))
                result = loss["total_usd"] if loss and loss.get("known") else None
            except Exception as exc:  # noqa: BLE001 - the plan is still confirmed; the line stays
                logger.warning("the real account's result could not be read at start: %s", exc)
        active = store.confirm_plan(db, profiles, document, identity=identity, scope="live",
                                    result_usd=result)
        self._journal(profiles, kind="auto_plan_started", subject=document["plan_id"], actor=identity,
                      outcome="active",
                      detail={"scope": "live", "mode": document["envelope"]["mode"], "how": how,
                              "opening_version": active["opening_version"],
                              "capital_usd": document["envelope"]["capital_usd"]})
        return active

    def _auto_live_activate(self) -> dict[str, Any] | None:
        """Look at a start that is waiting, and put it in force, leave it, or let it lapse.

        Called on every pass and decides the same way every time: the plan is built again from
        what was confirmed; it must still be that exact strategy version; none of its settings
        may still be waiting; and every gate must hold *now*. Then, and only then, it is in
        force. If the plan can no longer be what was confirmed -- its settings were withdrawn or
        failed, the strategy would come out different, the time ran out -- the request ends and
        a person is asked again. Nothing here is a way to start without the gates: it is the
        same question ``_auto_start_live`` asked, asked again at the moment it matters.
        """
        from datetime import datetime as _datetime

        from .. import config_changes as cc
        from ..auto import gates as gating
        from ..auto import store

        profiles = self._auto_real()
        db = self.state_db(profiles)
        request = store.start_request(db)
        if request is None:
            return None
        who = str(request.get("requested_by") or "console")

        def end(status: str, reason: str) -> dict[str, Any]:
            ended = store.end_request(db, status=status, by="auto:activation", reason=reason)
            self._journal(profiles, kind="auto_plan_request_ended", subject=request["plan_id"],
                          actor="auto:activation", outcome=status,
                          detail={"scope": "live", "reason": reason[:300]})
            return {"request": status, "reason": reason, "plan_id": (ended or request)["plan_id"]}

        records = {row["id"]: row for row in cc.records(db)}
        mine = [records.get(item["id"]) for item in request.get("changes") or []]
        gone = [row for row in mine if row is None or row["status"] in (cc.CANCELLED, cc.FAILED)]
        if gone:
            why = next((row.get("error") or row["status"] for row in gone if row), "no longer on record")
            return end("lapsed", f"a setting the plan needs was withdrawn or could not be applied: {why}")
        try:
            expires = _datetime.fromisoformat(str(request["expires_at"]))
        except (KeyError, ValueError):
            expires = None
        inputs = request["inputs"]
        try:
            snap, _working = self._auto_snapshot(profiles)
            document = self._auto_document(profiles, snap, mode=inputs["mode"],
                                           capital_usd=inputs["capital_usd"],
                                           loss_pause_pct=inputs["loss_pause_pct"],
                                           strategy=inputs["strategy"], as_given=True)
        except ServiceError as exc:
            if expires is not None and utcnow() >= expires:
                return end("lapsed", f"the plan could not be drawn again before it ran out: {exc}")
            store.note_request(db, last_checked_at=utcnow().isoformat(), last_reason=str(exc)[:300])
            return {"request": "waiting", "reason": str(exc)[:300]}
        version_id = store.version_id(inputs["mode"], document["strategy"],
                                      store.code_identity(profiles, document["strategy"]))
        if version_id != request.get("version_id"):
            return end("lapsed", "the plan would no longer be the strategy version that was "
                                 f"confirmed ({request.get('version_id')} then, {version_id} now)")
        found = self._auto_live_gates(profiles, document)
        if gating.is_open(found):
            # What goes on record is what the person confirmed -- its id, its settings, the day
            # its strategy was fixed -- with what it can put to work counted against the account
            # as it is now.
            confirmed = request.get("document") or document
            active = self._auto_put_in_force(
                profiles, {**confirmed, "under_plan": document["under_plan"]},
                identity=who, how="settings_in_force")
            store.end_request(db, status="activated", by="auto:activation",
                              reason="every setting was in force and every condition held",
                              extra={"opening_version": active["opening_version"]})
            return {"request": "activated", "plan_id": active["plan_id"],
                    "opening_version": active["opening_version"]}
        reason = gating.refusal(found)
        if expires is not None and utcnow() >= expires:
            return end("lapsed", f"not every condition held before the confirmation ran out: {reason}")
        store.note_request(db, last_checked_at=utcnow().isoformat(), last_reason=reason[:500])
        return {"request": "waiting", "reason": reason}

    def auto_withdraw_request(self, *, reason: str = "", identity: str | None = None) -> dict[str, Any]:
        """Take back a start that is waiting, and the settings it submitted that have not landed.

        A setting already in force stays as it is and is named in the answer: undoing it is a
        change like any other, made in Settings, and may itself be something a person wants.
        """
        from .. import config_changes as cc
        from ..auto import store

        profiles = self._auto_real()
        db = self.state_db(profiles)
        request = store.start_request(db)
        if request is None:
            return {"withdrawn": False, "reason": "no start is waiting"}
        who = identity or "console"
        records = {row["id"]: row for row in cc.records(db)}
        cancelled, standing = [], []
        for item in request.get("changes") or []:
            row = records.get(item["id"])
            if row is None:
                continue
            if row["status"] == cc.PENDING:
                self.cancel_config_change(row["id"], identity=who)
                cancelled.append(row["id"])
            elif row["status"] == cc.APPLIED:
                standing.extend(row.get("paths") or [])
        store.end_request(db, status="withdrawn", by=who, reason=reason.strip()[:500] or "withdrawn by hand")
        self._journal(profiles, kind="auto_plan_request_ended", subject=request["plan_id"], actor=who,
                      outcome="withdrawn",
                      detail={"scope": "live", "cancelled_changes": cancelled, "left_in_force": standing[:20]})
        return {"withdrawn": True, "plan_id": request["plan_id"], "cancelled_changes": cancelled,
                "left_in_force": standing}

    @staticmethod
    def _auto_record(db) -> dict[str, Any] | None:
        """What an account under a plan has come to, from what its own passes recorded.

        Return and drawdown on the unit value the comparisons use (``auto.metrics``), so money
        put in is never counted as money made. Read from the marks alone: no price is asked
        for, so a screen can show this as often as it likes. None before the first pass.
        """
        from ..auto import metrics, store

        marks = store.equity_marks(db)
        if not marks:
            return None
        equity = [{"date": day, **{key: marks[day].get(key) for key in (
            "equity_usd", "cash_usd", "invested_usd", "funded_usd", "positions")}}
                  for day in sorted(marks) if marks[day].get("equity_usd") is not None]
        if not equity:
            return None
        first, last = equity[0], equity[-1]
        funded = float(first.get("funded_usd") or 0.0)
        if funded > 0:
            # The book says what was put in: everything is measured against that.
            figures = metrics.summarize(equity, opening_usd=funded)
            base, measured = funded, "funding"
        else:
            # No funding is on record -- the real ledger's opening balance was never booked as
            # a deposit -- and equity less nothing would report the whole account as profit. So
            # it is measured from what the account was worth at the first pass made over it.
            opened = store.equity_opened(db) or first
            base, measured = float(opened.get("equity_usd") or first["equity_usd"]), "first_mark"
            start = {"date": str(opened.get("session") or first["date"]), "equity_usd": base,
                     "invested_usd": opened.get("invested_usd") or 0.0, "funded_usd": 0.0}
            figures = metrics.summarize([start, *equity])
        out = {key: figures.get(key) for key in (
            "from", "to", "net_return_pct", "max_drawdown_pct", "equity_usd", "funded_usd",
            "average_exposure_pct")}
        out["sessions"] = len(equity)
        # What it has made since then: the change in what it is worth, less money put in or
        # taken out along the way.
        flows = float(last.get("funded_usd") or 0.0) - funded
        out["result_usd"] = round(float(last["equity_usd"]) - base - flows, 2)
        out["measured_from"] = measured
        return out

    def _auto_target(self, scope: str):
        """(the service, its profiles) for an account a plan runs in: the simulated one or the real."""
        if scope == "live":
            return self, self._auto_real()
        if scope != "paper":
            raise ServiceError(f"unknown scope {scope!r}; a plan runs in the simulated account "
                               "(paper) or the real one (live)")
        sim = self._auto_paper()
        return sim, sim.profiles()

    @staticmethod
    def _auto_result(sim, sim_profiles, session: str) -> dict[str, Any] | None:
        from ..auto import trader

        return trader.result_now(sim, trade_date=session)

    def auto_pause(self, *, reason: str = "", identity: str | None = None,
                   scope: str = "paper") -> dict[str, Any]:
        """Stop a plan opening anything. What it holds is still looked after.

        The same act in either account, and in the real one it takes no password: stopping is
        one gesture, whoever is holding the phone.
        """
        from ..auto import store

        target, profiles = self._auto_target(scope)
        db = target.state_db(profiles)
        if scope == "live" and store.active_plan(db) is None:
            raise ServiceError("no plan is in force on the real account")
        state = store.pause(db, reason=reason.strip()[:500] or "paused by hand",
                            by=identity or "console")
        target._journal(profiles, kind="auto_paused", subject=scope, actor=identity or "console",
                        outcome="paused", detail={"scope": scope, "reason": state["reason"],
                                                  "kind": state.get("kind")})
        return {"paused": True, "scope": scope, "state": _public_state(state)}

    def auto_resume(self, *, note: str = "", identity: str | None = None, scope: str = "paper",
                    confirmed: bool = False) -> dict[str, Any]:
        """Let a plan open positions again.

        Out of a loss-line pause this moves where the line is measured from to the result as it
        stands (``auto.store.resume``): the answer says so, with both figures, because that is
        what the person resuming has just allowed. On the real account that is allowing real
        money to be lost again, so it takes the console password (``confirmed``) whatever paused
        it -- the same as restoring autonomy does.
        """
        from ..auto import runner, store, trader

        target, profiles = self._auto_target(scope)
        db = target.state_db(profiles)
        before = store.state(db)
        if not before["paused"]:
            return {"paused": False, "changed": False, "scope": scope, "state": _public_state(before)}
        if scope == "live" and not confirmed:
            raise ServiceError("resuming a plan on the real account needs the console password")
        try:
            state = trader.resume(target, note=note.strip()[:500] or "resumed by hand",
                                  by=identity or "console", trade_date=runner.session_of(utcnow()))
        except trader.TraderError as exc:
            raise ServiceError(str(exc)) from exc
        moved = state["history"][-1].get("loss_baseline_usd")
        target._journal(profiles, kind="auto_resumed", subject=scope, actor=identity or "console",
                        outcome="running", detail={"scope": scope, "cleared": before.get("reason"),
                                                   "kind": before.get("kind"),
                                                   "loss_baseline_usd": moved})
        return {"paused": False, "changed": True, "scope": scope, "state": _public_state(state),
                "loss_line_moved": moved}

    def auto_stop(self, *, reason: str = "", identity: str | None = None,
                  scope: str = "paper") -> dict[str, Any]:
        """Take a plan out of force. Nothing more is opened; what it bought is still closed on
        the terms it was bought under, for as long as the account allows that.

        On the real account a start that is still waiting is withdrawn with it: someone stopping
        automatic trading does not mean for it to begin tomorrow.
        """
        from ..auto import store

        target, profiles = self._auto_target(scope)
        withdrawn = None
        if scope == "live" and store.start_request(target.state_db(profiles)) is not None:
            withdrawn = self.auto_withdraw_request(reason=reason or "stopped by hand", identity=identity)
        ended = store.withdraw_plan(target.state_db(profiles), identity=identity or "console",
                                    reason=reason.strip()[:500] or "stopped by hand")
        if ended is None:
            if withdrawn is not None:
                return {"stopped": True, "scope": scope, "plan_id": withdrawn["plan_id"],
                        "request_withdrawn": withdrawn}
            return {"stopped": False, "scope": scope, "reason": "no plan is in force"}
        target._journal(profiles, kind="auto_plan_stopped", subject=ended["plan_id"],
                        actor=identity or "console", outcome="withdrawn",
                        detail={"scope": scope, "reason": ended["withdrawn_reason"]})
        answer = {"stopped": True, "scope": scope, "plan_id": ended["plan_id"],
                  "request_withdrawn": withdrawn}
        if scope == "paper":
            answer["simulation"] = self.auto_simulation()
        return answer

    def auto_fund(self, *, amount_usd: float, kind: str = "deposit", note: str = "",
                  identity: str | None = None) -> dict[str, Any]:
        """Put money into the simulated account, or take it out. Recorded as funding, not result.

        A deposit is not a gain and a withdrawal is not a loss: both are kept as what they are
        (``portfolio.record_cash_flow``), and everything that reports what the account has made
        measures against what was put in. Cash an order still working has reserved is not
        available to take out.
        """
        from ..auto import paper, runner
        from ..portfolio import PortfolioError, load_portfolio, record_cash_flow

        if kind not in ("deposit", "withdrawal"):
            raise ServiceError("funding is a deposit or a withdrawal")
        if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float)) or amount_usd <= 0:
            raise ServiceError("the amount must be a positive number of dollars")
        sim = self._auto_paper()
        profiles = sim.profiles()
        path = paper.ledger_path(profiles)
        try:
            if kind == "withdrawal":
                held = sum(float(row.get("cash_usd") or 0.0)
                           for row in sim.state_db(profiles).reservations(active_only=True)
                           if str(row.get("account_key")) == MICRO_BUCKET)
                cash = load_portfolio(path).buckets[MICRO_BUCKET].cash_usd
                if amount_usd > cash - held + 1e-9:
                    raise ServiceError(
                        f"the simulated account has ${cash:,.2f} in cash, ${held:,.2f} of it held "
                        f"by an order still working; ${amount_usd:,.2f} cannot be taken out")
            state = record_cash_flow(path, bucket=MICRO_BUCKET, amount_usd=round(float(amount_usd), 2),
                                     kind=kind, date=runner.session_of(utcnow()),
                                     note=(note.strip() or f"{kind} into the simulated account")[:200])
        except (PortfolioError, ValueError, KeyError) as exc:
            raise ServiceError(str(exc)) from exc
        sim._journal(profiles, kind="cashflow_recorded", subject=MICRO_BUCKET,
                     actor=identity or "console", outcome=kind,
                     detail={"scope": "paper", "amount_usd": round(float(amount_usd), 2)})
        return {"recorded": True, "kind": kind, "amount_usd": round(float(amount_usd), 2),
                "cash_usd": state.buckets[MICRO_BUCKET].cash_usd}

    def auto_adjust(self, *, strategy: dict[str, Any] | None = None, ask_model: bool = False,
                    reason: str = "", identity: str | None = None) -> dict[str, Any]:
        """Change the simulated plan's strategy inside the envelope a person confirmed.

        The parameters are the plan's, not a file's: nobody edits YAML for this. What is handed
        in -- by a person, or by the model when ``ask_model`` -- is merged onto the strategy in
        force and clamped to its bounds. It is refused outright if it would need any setting
        changed that the confirmed plan did not already carry: an adjustment is a strategy that
        works inside the boundaries as they are, and widening them is the person's, through a
        new plan. The result is a new strategy version with an empty record; the one it
        replaces goes on managing what it bought.
        """
        from ..auto import runner, store

        sim = self._auto_paper()
        profiles = sim.profiles()
        db = sim.state_db(profiles)
        current = store.active_plan(db)
        if current is None:
            raise ServiceError("no plan is in force in the simulated account")
        envelope = plan.Envelope(mode=current["envelope"]["mode"],
                                 capital_usd=float(current["envelope"]["capital_usd"]),
                                 loss_pause_pct=float(current["envelope"]["loss_pause_pct"]))
        snap, _working = sim._auto_snapshot(profiles)
        merged = {**current["strategy"], **{k: v for k, v in (strategy or {}).items() if v is not None}}
        by, error = "adjustment", None
        try:
            if ask_model:
                suggestion, by, error = sim._auto_suggestion(profiles, snap, envelope)
                if suggestion is None:
                    raise ServiceError(f"the model could not be asked for a strategy: {error}")
            else:
                if not strategy:
                    raise ServiceError("say what to change, or ask the model")
                rationale = merged.get("rationale") or {}
                suggestion = plan.Strategy(
                    **{key: merged[key] for key in plan.Strategy.model_fields
                       if key in merged and not key.startswith("rationale")},
                    rationale_zh=str(rationale.get("zh") or ""), rationale_en=str(rationale.get("en") or ""))
            document = plan.build(profiles, snap, envelope, suggestion=suggestion, suggested_by=by,
                                  suggestion_error=error)
        except plan.PlanError as exc:
            raise ServiceError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise ServiceError(f"that is not a strategy a plan can use: {exc}") from exc
        needed = [change for change in document["changes"] if not _already_covered(change, profiles)]
        if needed:
            paths = ", ".join(change["path"] for change in needed)
            raise ServiceError("this strategy would need settings the confirmed plan does not "
                               f"carry ({paths}); that is a new plan for you to confirm, not an "
                               "adjustment")
        if not document["under_plan"]["any_order"]:
            raise ServiceError("no order would be possible under this strategy: "
                               + document["summary"]["en"])
        updated = store.adjust_strategy(db, profiles, document["strategy"],
                                        identity=identity or "console",
                                        reason=reason.strip()[:500] or "adjusted within the confirmed plan")
        changed = updated["opening_version"] != current["opening_version"]
        if changed:
            sim._journal(profiles, kind="auto_strategy_adjusted", subject=updated["opening_version"],
                         actor=identity or "console", outcome="active",
                         detail={"replaces": current["opening_version"], "suggested_by": by,
                                 "clamps": document["clamps"],
                                 "session": runner.session_of(utcnow())})
        return {"changed": changed, "opening_version": updated["opening_version"],
                "replaces": current["opening_version"] if changed else None,
                "strategy": updated["strategy"], "clamps": document["clamps"],
                "managing": updated["managing"]}

    def auto_tick(self, *, real_account: bool = True, rebind: bool = True) -> dict[str, Any]:
        """One pass of automatic trading: the simulated account, and the real one's plan if any.

        The simulated account's pass reads the real account's research. It returns at once,
        having read one small file, while the simulated account has never been started -- which
        is every deployment until a person starts it. The real account's part is
        :meth:`_auto_live_pass` and is under ``live`` in the answer when there was anything of
        it to run; ``real_account=False`` leaves it out, for a caller that is only ever about
        the simulated account (``ct-auto tick``). ``rebind=False`` is for a pass started by hand
        from code that may not be the service's: see ``auto.runner.run``.
        """
        from ..auto import runner

        live = self._auto_live_pass() if real_account else None
        sim = self._auto_paper(required=False)
        if sim is None:
            out: dict[str, Any] = {"ran": False, "skipped": "the simulated account has not been started"}
        else:
            if rebind:
                self._auto_follow_configuration(sim)
            out = runner.run(sim, research_from=self.profiles().project_dir / "runs", rebind=rebind)
        if live is not None:
            out["live"] = live
        return out

    def _auto_follow_configuration(self, sim) -> None:
        """Keep the simulated account's configuration derived from the real one as it is now.

        Generated when a simulation starts, it would otherwise go on rehearsing the limits, the
        mandate and the calendar of that day. So it is rebuilt whenever the real configuration
        has moved -- a limit tightened in Settings, the mandate edited -- with the plan's own
        changes applied on top as before. Cheap when nothing moved: a digest of five small files.
        A rebuild that changes what the strategy runs on makes it a new strategy version on the
        next pass (``auto.runner.keep_version_current``), which is the rule working, not a fault.
        """
        from ..auto import paper, store

        try:
            record = store.plan_record(sim.state_db(sim.profiles()))
            source = record.get("active") or next(
                (row for row in reversed(record.get("history") or []) if isinstance(row, dict)), None)
            if not isinstance(source, dict):
                return
            paper.ensure(self.profiles(), changes=source.get("changes") or [], creates=PLAN_CREATES,
                         plan_id=source.get("plan_id"))
        except Exception as exc:  # noqa: BLE001 - the pass still runs, on the configuration it has
            logger.warning("the simulated account's configuration could not be refreshed: %s", exc)

    def _auto_live_pass(self) -> dict[str, Any] | None:
        """The real account's part of a pass, or None when there is nothing of it to run.

        None is the answer on every pass of every deployment where nobody has confirmed a plan
        for real money: two small reads, no ledger priced, no venue. With a start waiting, it is
        looked at. With a plan in force -- or one ended whose positions are still held -- the
        account gets the same pass the simulated one does, with one difference: it approves
        nothing itself. What it writes is picked up by the loop that has always approved for
        this account (``Service.autonomous_reductions``), so one thread decides and never two.
        """
        from ..auto import replacement, runner, store

        try:
            profiles = self._auto_real()
            db = self.state_db(profiles)
            waiting = store.start_request(db)
            if waiting is None and store.active_plan(db) is None and not store.versions(db):
                return None
            out: dict[str, Any] = {}
            if waiting is not None:
                out["activation"] = self._auto_live_activate()
            out["pass"] = runner.run(self, approve=False)
            # Once a week, and never while the market is open: whether the strategy being
            # simulated has earned the place of the one in force (auto.replacement). A look that
            # cannot be finished is tried again in a few hours, not in a minute.
            if (store.active_plan(db) is not None and replacement.due(db)
                    and not self.market_status().is_open):
                replacement.attempting(db)
                try:
                    out["evaluation"] = self.auto_evaluate_candidate(identity="auto:weekly")
                except ServiceError as exc:
                    out["evaluation"] = {"outcome": "failed", "error": str(exc)[:300]}
            return out
        except Exception as exc:
            # The simulated account's pass must still run, so this one's failure is its answer.
            logger.exception("the real account's automatic-trading pass failed")
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}

    def auto_simulation(self) -> dict[str, Any]:
        """The simulated account as it stands: the plan, its record, what it holds and why it waits.

        Read-only, and it prices nothing: what the account was worth is what the last pass
        recorded, with the time it recorded it. A screen asking how the simulation is doing
        must not cost a market-data read, and a figure with its own timestamp is honest about
        being as old as it is.
        """
        from ..auto import positions, store, trader
        from ..portfolio import PortfolioError, cash_flow_summary, load_portfolio

        sim = self._auto_paper(required=False)
        if sim is None:
            return {"exists": False, "plan": None, "version": None, "state": None,
                    "note": {"zh": "模拟账户还没有启动。预览一个方案后可以启动模拟。",
                             "en": "The simulated account has not been started. Preview a plan "
                                   "to start one."}}
        from ..auto import paper

        profiles = sim.profiles()
        db = sim.state_db(profiles)
        active = store.active_plan(db)
        version = store.version(db, active["opening_version"]) if active else None
        sessions = store.sessions_of(db, active["opening_version"]) if active else {}
        marks = store.equity_marks(db)
        last_mark = marks[max(marks)] if marks else None
        everything = sim._store(profiles).all()
        fills = sim._fills_for_replay(profiles)
        ledger = None
        try:
            ledger = load_portfolio(paper.ledger_path(profiles))
        except (PortfolioError, OSError, ValueError) as exc:
            logger.warning("the simulated ledger could not be read: %s", exc)
        held = positions.book(fills=fills, proposals=everything, ledger=ledger,
                              bucket=MICRO_BUCKET) if ledger is not None else []
        funding = None
        if ledger is not None:
            flows = [flow for flow in ledger.cash_flows if flow.bucket == MICRO_BUCKET]
            funding = {"net_usd": float(cash_flow_summary(ledger).get(MICRO_BUCKET, 0.0)),
                       "deposits_usd": round(sum(f.amount_usd for f in flows
                                                 if f.kind == "deposit"), 2),
                       "withdrawals_usd": round(-sum(f.amount_usd for f in flows
                                                     if f.kind == "withdrawal"), 2),
                       "cash_usd": ledger.buckets[MICRO_BUCKET].cash_usd}
        seen = trader.seen_signals(db)
        recent = sorted(({"run_id": run_id, **entry} for run_id, entry in seen.items()),
                        key=lambda row: str(row.get("at") or ""), reverse=True)[:20]
        return {
            "exists": True,
            "plan": None if active is None else {
                key: active.get(key) for key in ("plan_id", "envelope", "strategy", "opening_version",
                                                 "managing", "confirmed_at", "confirmed_by",
                                                 "adjusted_at", "adjusted_by", "deployable_usd")},
            "state": _public_state(store.state(db)),
            "record": self._auto_record(db),
            "version": None if version is None else {
                "version_id": version["version_id"], "mode": version["mode"],
                "created_at": version["created_at"], "parent": version.get("parent"),
                "validation": version["validation"], "code": version["code"],
                "evidence": len(version.get("evidence") or [])},
            "sessions": {
                "counted": sum(1 for row in sessions.values() if store.session_valid(row)),
                "needed_to_open": modes.SIMULATION_DAYS_TO_OPEN,
                "needed_to_replace": modes.SIMULATION_DAYS_TO_REPLACE,
                "recorded": len(sessions),
                "not_counted": [{"session": day, "problems": row.get("problems") or [],
                                 "open_passes": row.get("open_passes")}
                                for day, row in sorted(sessions.items())
                                if not store.session_valid(row)][-10:]},
            "account": last_mark,
            "funding": funding,
            "positions": [view.as_dict() for view in held],
            "trades": positions.closed_trades(fills=fills, proposals=everything,
                                              bucket=MICRO_BUCKET)[-20:],
            "signals": recent,
            "deployment": self._auto_deployment(db, active),
            "research": self._auto_research_coverage(
                (active or {}).get("strategy", {}).get("universe") or []),
        }

    def auto_intraday_checks(self, symbol: str = "SPY") -> dict[str, Any]:
        """What an intraday mode would need that is not there, measured now (``auto.intraday``).

        Reads the current session's minute bars for one liquid symbol, on request and never in
        the background. The mode is not built; this is what "not open" consists of for it.
        """
        from ..auto import intraday

        return intraday.run(self.profiles(), symbol=symbol)

    # ---------------------------------------------------------------- what it has come to
    def _auto_experiment_setup(self, sim, profiles: Profiles) -> dict[str, Any]:
        """What a comparison of the simulated plan is run under: one set of conditions for all."""
        from .. import evaluation, sizing
        from ..auto import backtest as bt
        from ..auto import store
        from ..portfolio import classify_instrument

        db = sim.state_db(profiles)
        active = store.active_plan(db)
        if active is None:
            raise ServiceError("no plan is in force in the simulated account, so there is no "
                               "strategy to measure")
        version = store.version(db, active["opening_version"])
        capital = float(active["envelope"]["capital_usd"])

        def limit_class_of(symbol: str) -> str | None:
            try:
                return classify_instrument(symbol, profiles, lambda _s: {"quote_type": "ETF"}).limit_class
            except Exception:  # noqa: BLE001 - unclassified takes the widest spread
                return None

        return {"db": db, "active": active, "version": version, "rules": bt.Rules.of(active["strategy"]),
                "mode": active["envelope"]["mode"], "capital": capital,
                "cap": float(active.get("deployable_usd") or capital),
                "weights": {str(k).upper(): float(v) for k, v in
                            (evaluation.settings(profiles).get("benchmark") or {}).items()},
                "minimum": float(sizing.minimum_order(profiles, MICRO_BUCKET)[0]),
                "limit_class_of": limit_class_of}

    @staticmethod
    def _auto_research_lifetime(profiles: Profiles, mode: str) -> float:
        """Minutes research stays actionable for a plan in ``mode``, as the live pass reads it.

        The same lookup ``auto.trader._expired`` makes -- the freshness class of the strategy tag
        the mode's proposals carry -- so a replay never keeps research alive longer than the
        simulated account would have (tests/test_auto_backtest.py holds the two together).
        """
        from ..auto import modes
        from ..proposals import freshness_class_of

        classes = (profiles.risk.get("proposal_freshness") or {}).get("classes") or {}
        tag = modes.TABLE[mode].strategy_tag
        return float((classes.get(freshness_class_of(profiles, tag)) or {}).get("ttl_minutes", 2880))

    @staticmethod
    def _auto_warm_from(start: str, mode: str, *rules: Any) -> str:
        """The first day of history to read so every average used in a window is whole on its start.

        The longer of the simple rule's average for the mode and any strategy's own trend
        window, in sessions, read back in calendar days with room for weekends and holidays.
        Sized by the simple rule alone it left a 60-session trend rule with too little history,
        and a replay refused its first purchases as "never above its own average".
        """
        from datetime import date, timedelta

        from ..auto import comparison

        sessions = max([comparison.AVERAGE_OVER.get(mode, 100),
                        *(int(getattr(rule, "trend_window", 0) or 0) for rule in rules)])
        return (date.fromisoformat(start) - timedelta(days=int(sessions * 1.6) + 10)).isoformat()

    def _auto_recorded_signals(self, start: str, end: str) -> tuple[list[Any], list[dict[str, Any]]]:
        """(the actionable research conclusions recorded live inside a window, every conclusion).

        Read from the real account's own runs, as they were written at the time. Nothing is
        regenerated for a past date: a model asked today about last spring has read last spring.
        """
        from ..auto import backtest as bt
        from ..auto import trader

        live = self.profiles()
        conclusions = trader.recorded_conclusions(live, live.project_dir / "runs")
        signals = []
        for found in conclusions:
            try:
                when = datetime.fromisoformat(str(found["at"]))
            except (TypeError, ValueError):
                continue
            when = when if when.tzinfo else when.replace(tzinfo=UTC)
            if found["action"] and start <= when.astimezone(bt.NEW_YORK).date().isoformat() <= end:
                signals.append(bt.Signal(when, found["symbol"], found["action"],
                                         confidence=found["confidence"], source=found["run_id"]))
        return signals, conclusions

    def auto_backtest(self, *, start: str | None = None, end: str | None = None,
                      identity: str | None = None) -> dict[str, Any]:
        """Run the simulated plan's strategy and its comparators over history, and keep the record.

        What is run: the passive answer, the simple rule and the 60/40 benchmark under the
        plan's own conditions, and -- only where research was recorded live inside the window --
        a replay of those conclusions under the strategy's rules. Research is never regenerated
        for a past date. The result is a card written beside the simulated account's state with
        every ledger it was computed from, attached to the strategy version as evidence, and
        checked against those ledgers before it is returned.

        The card says for itself whether the window could test the strategy at all: a window
        from before the strategy was fixed is data it could have been built on, and is labelled
        so. That is every window, for a strategy fixed today. What can test it is what comes
        after, which is the live simulation's record and takes as long as it takes.
        """
        from datetime import date, timedelta

        from ..auto import backtest as bt
        from ..auto import bars as bar_source
        from ..auto import comparison, experiments, paper, store
        from ..auto import costs as cost_model

        sim = self._auto_paper()
        profiles = sim.profiles()
        setup = self._auto_experiment_setup(sim, profiles)
        rules, mode = setup["rules"], setup["mode"]
        last = bar_source.last_completed_session()
        end = min(str(end)[:10], last) if end else last
        start = str(start)[:10] if start else (date.fromisoformat(end) - timedelta(days=730)).isoformat()
        if start >= end:
            raise ServiceError("a backtest needs a window that ends after it begins, on a session "
                               "that has finished")
        warm = self._auto_warm_from(start, mode, rules)
        symbols = list(dict.fromkeys([*rules.universe, *setup["weights"]]))
        try:
            history = bar_source.read(symbols, start=warm, end=end)
        except bar_source.BarsUnavailable as exc:
            raise ServiceError(f"the history for this backtest could not be read: {exc}") from exc
        shared = {"bars": history, "start": start, "end": end, "capital_usd": setup["capital"],
                  "opening_cash_usd": setup["capital"], "deploy_cap_usd": setup["cap"],
                  "costs": cost_model.STANDARD, "min_order_usd": setup["minimum"],
                  "limit_class_of": setup["limit_class_of"]}
        compared = comparison.run(mode=mode, rules=rules, benchmark_weights=setup["weights"], **shared)
        rows, ledgers = dict(compared["rows"]), dict(compared["ledgers"])

        # Research as it was recorded live, and only that.
        signals, conclusions = self._auto_recorded_signals(start, end)
        said = {"zh": "策略：按当时实时记录的研究结论，用方案的规则重放。",
                "en": "Strategy: the research conclusions recorded live at the time, replayed "
                      "under the plan's rules."}
        lifetime = self._auto_research_lifetime(profiles, mode)
        if signals:
            replay = bt.run(rules=rules, signals=signals, research_lifetime_minutes=lifetime, **shared)
            ledgers["strategy"] = replay.as_dict()
            rows["strategy"] = comparison.strategy_row(
                replay.equity, fills=replay.fills, trades=replay.trades, skipped=replay.skipped,
                opening_usd=setup["capital"], says=said)
        else:
            rows["strategy"] = {"figures": None, "says": said, "why_not": {
                "zh": f"这段区间里没有实时记录下来的可执行研究结论（共 {len(conclusions)} 条记录，"
                      "其中没有买入或卖出结论落在区间内）。研究不会为过去的日期重新生成，所以这里没有策略的回测结果。",
                "en": f"No actionable research conclusion was recorded live inside this window "
                      f"({len(conclusions)} record(s) in all, none of them a purchase or a sale "
                      "inside it). Research is not regenerated for past dates, so the strategy "
                      "has no backtest result here."}}
        conditions = {**compared["conditions"], "replayed_conclusions": len(signals),
                      "recorded_conclusions": len(conclusions), "research_lifetime_minutes": lifetime}
        card = experiments.build(
            setup["db"], kind=experiments.BACKTEST, version=setup["version"], start=start, end=end,
            data={"source": bar_source.SOURCE, "caliber": bar_source.CALIBER, "symbols": symbols,
                  "digest": bar_source.digest(history),
                  "first_bar": min(rows_[0].date for rows_ in history.values()),
                  "last_bar": max(rows_[-1].date for rows_ in history.values())},
            conditions=conditions, rows=rows,
            differences=(comparison.differences(rows["strategy"], compared["rows"])
                         if rows["strategy"].get("figures") else {}))
        directory = experiments.write(paper.root_of(self.profiles()) / "experiments", card, ledgers)
        experiments.register(setup["db"], card)
        store.add_evidence(setup["db"], card["version_id"], experiments.summary(card, directory))
        problems = experiments.verify(directory)
        sim._journal(profiles, kind="auto_experiment_recorded", subject=card["card_id"],
                     actor=identity or "console", outcome="grounded" if not problems else "ungrounded",
                     detail={"version": card["version_id"], "start": start, "end": end,
                             "sample": card["design"]["sample"],
                             "independent": card["design"]["independent"]})
        return {"card": card, "path": str(directory), "grounded": not problems, "problems": problems}

    # ---------------------------------------------------------------- a better strategy, proved
    def _auto_decisive_experiment(self, sim, sim_profiles: Profiles, *, candidate: dict[str, Any],
                                  incumbent: dict[str, Any], active: dict[str, Any],
                                  identity: str) -> dict[str, Any]:
        """The one experiment a candidate is held to: it and the strategy in force, like for like.

        Both are replayed over the same research conclusions, recorded live in the window, with
        the real plan's money and cap, the same prices and the same costs; beside them, buying
        and holding the candidate's own funds. The window is the sessions after the day the
        candidate was fixed, up to the last one that has finished. One card, the candidate's:
        the strategy in force is a row on it, not a second trial of its own.
        """

        from .. import evaluation, sizing
        from ..auto import backtest as bt
        from ..auto import bars as bar_source
        from ..auto import comparison, experiments, paper, replacement, store
        from ..auto import costs as cost_model
        from ..portfolio import classify_instrument

        live = self.profiles()
        sim_db = sim.state_db(sim_profiles)
        candidate_id, mode = candidate["version_id"], candidate["mode"]
        fixed = str(candidate.get("data_as_of") or "")[:10]
        sessions = sorted(store.sessions_of(sim_db, candidate_id))
        after = [day for day in sessions if day > fixed] if fixed else sessions
        end = bar_source.last_completed_session()
        if not after or after[0] >= end:
            raise ServiceError("the candidate has no finished session after the day it was fixed, "
                               "so there is no window that could test it")
        start = after[0]
        rules, old_rules = bt.Rules.of(candidate["strategy"]), bt.Rules.of(incumbent["strategy"])
        weights = {str(k).upper(): float(v) for k, v in
                   (evaluation.settings(live).get("benchmark") or {}).items()}
        symbols = list(dict.fromkeys([*rules.universe, *old_rules.universe, *weights]))
        warm = self._auto_warm_from(start, mode, rules, old_rules)
        try:
            history = bar_source.read(symbols, start=warm, end=end)
        except bar_source.BarsUnavailable as exc:
            raise ServiceError(f"the history for the comparison could not be read: {exc}") from exc

        def limit_class_of(symbol: str) -> str | None:
            try:
                return classify_instrument(symbol, live, lambda _s: {"quote_type": "ETF"}).limit_class
            except Exception:  # noqa: BLE001 - unclassified takes the widest spread
                return None

        # The real plan's money: what the person confirmed, and what it could actually deploy.
        capital = float(active["envelope"]["capital_usd"])
        shared = {"bars": history, "start": start, "end": end, "capital_usd": capital,
                  "opening_cash_usd": capital,
                  "deploy_cap_usd": float(active.get("deployable_usd") or capital),
                  "costs": cost_model.STANDARD,
                  "min_order_usd": float(sizing.minimum_order(live, MICRO_BUCKET)[0]),
                  "limit_class_of": limit_class_of}
        compared = comparison.run(mode=mode, rules=rules, benchmark_weights=weights, **shared)
        rows, ledgers = dict(compared["rows"]), dict(compared["ledgers"])
        signals, conclusions = self._auto_recorded_signals(start, end)
        told = {
            replacement.CANDIDATE: (rules, {
                "zh": f"候选策略 {candidate_id}：按区间内实时记录的研究结论重放。",
                "en": f"Candidate {candidate_id}: replayed on the research recorded live in the window."}),
            replacement.INCUMBENT: (old_rules, {
                "zh": f"现用策略 {incumbent['version_id']}：同样的研究结论、资金、价格和成本下重放。",
                "en": f"In force, {incumbent['version_id']}: replayed on the same research, money, "
                      "prices and costs."})}
        lifetime = self._auto_research_lifetime(sim_profiles, mode)
        for name, (how, said) in told.items():
            if signals:
                replay = bt.run(rules=how, signals=signals, research_lifetime_minutes=lifetime,
                                **shared)
                ledgers[name] = replay.as_dict()
                rows[name] = comparison.strategy_row(
                    replay.equity, fills=replay.fills, trades=replay.trades, skipped=replay.skipped,
                    opening_usd=capital, says=said)
            else:
                rows[name] = {"figures": None, "says": said, "why_not": {
                    "zh": "这段区间里没有实时记录下来的可执行研究结论，所以没有可比的结果。",
                    "en": "No actionable research conclusion was recorded live inside this window, "
                          "so there is no result to compare."}}
        card = experiments.build(
            sim_db, kind=experiments.REPLACEMENT, version=candidate, start=start, end=end,
            data={"source": bar_source.SOURCE, "caliber": bar_source.CALIBER, "symbols": symbols,
                  "digest": bar_source.digest(history),
                  "first_bar": min(bars[0].date for bars in history.values()),
                  "last_bar": max(bars[-1].date for bars in history.values())},
            conditions={**compared["conditions"], "replayed_conclusions": len(signals),
                        "recorded_conclusions": len(conclusions),
                        "research_lifetime_minutes": lifetime,
                        "incumbent": incumbent["version_id"],
                        "incumbent_strategy": incumbent.get("strategy")},
            rows=rows,
            differences=(comparison.differences(rows[replacement.CANDIDATE], {
                key: value for key, value in rows.items() if key != replacement.CANDIDATE})
                if rows[replacement.CANDIDATE].get("figures") else {}))
        directory = experiments.write(paper.root_of(live) / "experiments", card, ledgers)
        experiments.register(sim_db, card)
        store.add_evidence(sim_db, candidate_id, experiments.summary(card, directory))
        problems = experiments.verify(directory)
        sim._journal(sim_profiles, kind="auto_experiment_recorded", subject=card["card_id"],
                     actor=identity, outcome="grounded" if not problems else "ungrounded",
                     detail={"version": candidate_id, "start": start, "end": end,
                             "kind": experiments.REPLACEMENT, "sample": card["design"]["sample"],
                             "independent": card["design"]["independent"]})
        return {"card": card, "path": str(directory), "grounded": not problems, "problems": problems}

    def auto_evaluate_candidate(self, *, identity: str | None = None, apply: bool = False,
                                confirmed: bool = False) -> dict[str, Any]:
        """Look at the simulated candidate against the strategy in force on the real account.

        What a look does depends on where the candidate is, and only once is it more than
        reading: when the candidate has its 180 sessions and has not been put to its comparison,
        the comparison is run and kept (:meth:`_auto_decisive_experiment`). Before that the
        answer is "waiting", and after it the answer is whatever that experiment showed --
        it is not run again for a better one.

        A candidate that passes every line replaces the strategy in force only if the person
        chose that for this plan (:meth:`auto_replacement_setting`) or asks for it now with the
        console password (``apply`` and ``confirmed``). Otherwise it is reported as ready, and
        the strategy in force stays. Every look that found a candidate is kept, whatever it found.
        """
        import json as _json
        from pathlib import Path as _Path

        from ..auto import experiments, replacement, store
        from ..auto import gates as gating

        profiles = self._auto_real()
        db = self.state_db(profiles)
        who = identity or "console"
        active = store.active_plan(db)
        if active is None:
            return {"outcome": replacement.NOTHING, "lines": [],
                    "reason": "no plan is in force on the real account"}
        if apply and not confirmed:
            raise ServiceError("replacing the strategy on the real account needs the console password")
        incumbent = store.version(db, active["opening_version"])
        sim = self._auto_paper(required=False)
        sim_profiles = sim.profiles() if sim is not None else None
        sim_db = sim.state_db(sim_profiles) if sim is not None else None
        running = store.active_plan(sim_db) if sim_db is not None else None
        candidate = store.version(sim_db, running["opening_version"]) if running else None

        # The candidate as it would run here: the same strategy under the real plan's envelope.
        # If that comes out as anything but the version that was simulated, it is not the candidate.
        needed: tuple[str, ...] = ()
        document = None
        if candidate is not None and incumbent is not None and \
                candidate["version_id"] != incumbent["version_id"]:
            envelope = active["envelope"]
            snap, _working = self._auto_snapshot(profiles)
            document = self._auto_document(
                profiles, snap, mode=candidate["mode"], capital_usd=float(envelope["capital_usd"]),
                loss_pause_pct=float(envelope["loss_pause_pct"]), strategy=running["strategy"],
                as_given=True)
            needed = tuple(change["path"] for change in self._auto_settings_needed(profiles, document))
            here = store.version_id(candidate["mode"], document["strategy"],
                                    store.code_identity(profiles, document["strategy"]))
            if here != candidate["version_id"]:
                needed = (*needed, f"the strategy itself (it would run here as {here})")
        standing = replacement.standing_lines(candidate=candidate, incumbent=incumbent,
                                              settings_needed=needed)
        if not standing[0].ok:
            # No candidate: nothing is evaluated and nothing is logged but that a look was made.
            db.write_meta(replacement.LOG, {**(db.read_meta(replacement.LOG, {}) or {}),
                                            "last_evaluated_at": utcnow().isoformat()})
            return {"outcome": replacement.NOTHING, "lines": [line.as_dict() for line in standing],
                    "reason": standing[0].en}
        candidate_id = candidate["version_id"]
        record = self._auto_simulated_record(candidate_id)
        record_lines = replacement.record_lines(candidate_id, record)

        # The comparison is made once: when the candidate is eligible, has its sessions, and has
        # none on its record. After that, what it showed is read back and never made again.
        kept = (candidate.get("validation") or {}).get("replacement") or None
        card, grounded, unreadable = None, None, False
        if kept:
            try:
                card = _json.loads((_Path(kept["path"]) / "card.json").read_text(encoding="utf-8"))
                grounded = not experiments.verify(_Path(kept["path"]))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("the candidate's decisive experiment could not be read back: %s", exc)
                card, unreadable = None, True
        elif gating.is_open([*standing, *record_lines]):
            made = self._auto_decisive_experiment(sim, sim_profiles, candidate=candidate,
                                                  incumbent=incumbent, active=active, identity=who)
            card, grounded = made["card"], made["grounded"]
            store.update_validation(sim_db, candidate_id, replacement={
                "card_id": card["card_id"], "path": made["path"], "decided_at": utcnow().isoformat(),
                "against": incumbent["version_id"]})
        performance = (replacement.unreadable_lines((kept or {}).get("card_id")) if unreadable
                       else replacement.performance_lines(card, grounded=grounded))
        state = store.state(db)
        account = replacement.account_lines(
            self._auto_account_facts(profiles, document),
            paused=(state.get("reason") or "paused") if state["paused"] else None)
        lines = [*standing, *record_lines, *performance, *account]

        chosen = bool(store.option(db, "auto_replace", False))
        outcome = replacement.outcome_of(
            standing=standing, record=record_lines, performance=performance, account=account,
            compared=card is not None, unreadable=unreadable, wanted=chosen or apply)
        if outcome == replacement.REJECTED:
            store.update_validation(sim_db, candidate_id, status=store.REJECTED)
        elif outcome == replacement.PASSED:
            store.update_validation(sim_db, candidate_id, status=store.VALIDATED)

        answer: dict[str, Any] = {
            "outcome": outcome, "candidate": candidate_id, "incumbent": incumbent["version_id"],
            "card_id": (card or {}).get("card_id"), "automatic": chosen,
            "lines": [line.as_dict() for line in lines]}
        if outcome == replacement.REPLACED:
            updated = store.adjust_strategy(
                db, profiles, document["strategy"], identity=who if apply else "auto:replacement",
                reason=(f"replaced {incumbent['version_id']} after validation: 180 simulated "
                        f"sessions and experiment {card['card_id']}"),
                data_as_of=candidate.get("data_as_of"))
            store.update_validation(sim_db, candidate_id, status=store.VALIDATED)
            store.update_validation(db, updated["opening_version"], status=store.VALIDATED,
                                    simulated_days=int(record["counted"]), exits_verified=True,
                                    replaced=incumbent["version_id"], card_id=card["card_id"])
            self._journal(profiles, kind="auto_strategy_replaced", subject=updated["opening_version"],
                          actor=who if apply else "auto:replacement", outcome="active",
                          detail={"scope": "live", "replaces": incumbent["version_id"],
                                  "card_id": card["card_id"], "automatic": not apply})
            answer["managing"] = updated["managing"]
        answer = replacement.note(db, answer)
        self._journal(profiles, kind="auto_candidate_evaluated", subject=candidate_id, actor=who,
                      outcome=outcome, detail={"scope": "live", "incumbent": incumbent["version_id"],
                                               "card_id": answer["card_id"],
                                               "failing": [line.code for line in lines if not line.ok]})
        return answer

    def auto_replacement_setting(self, *, on: bool, identity: str | None = None,
                                 confirmed: bool = False) -> dict[str, Any]:
        """Choose, for the plan in force on the real account, whether a validated candidate takes
        over by itself. Off unless a person turned it on, which takes the console password;
        turning it off takes nothing. Either way every line in ``auto.replacement`` still has
        to hold -- this decides who presses the button, not what the button requires."""
        from ..auto import store

        profiles = self._auto_real()
        db = self.state_db(profiles)
        if store.active_plan(db) is None:
            raise ServiceError("no plan is in force on the real account")
        if on and not confirmed:
            raise ServiceError("letting a validated strategy take over by itself needs the "
                               "console password")
        updated = store.set_option(db, "auto_replace", bool(on), by=identity or "console")
        self._journal(profiles, kind="auto_strategy_adjusted", subject=updated["opening_version"],
                      actor=identity or "console", outcome="active",
                      detail={"scope": "live", "option": "auto_replace", "on": bool(on)})
        return {"auto_replace": bool(on), "plan_id": updated["plan_id"]}

    def auto_performance(self) -> dict[str, Any]:
        """The simulated account's own record beside what else the same money would have done.

        Two sections that are never merged. ``simulation`` is the forward record: what the
        account was worth session by session as its passes recorded it, with the passive answer,
        the simple rule, the 60/40 benchmark and cash over the same sessions, given the same
        money on the same days. ``backtests`` is the history: each experiment on record for the
        strategy version in force, with what its card says about whether it could test anything.

        Everything the record did and did not do is counted -- research that ended in no trade,
        runs that failed, orders that were turned back -- not only the trades. Nothing is asked
        of the market that the passes did not already record, except the history the comparators
        need; if that cannot be read the comparators are absent, with the reason, and the
        account's own figures still stand.
        """

        from .. import evaluation
        from ..auto import backtest as bt
        from ..auto import bars as bar_source
        from ..auto import comparison, metrics, paper, positions, store, trader, venue
        from ..auto import costs as cost_model

        sim = self._auto_paper(required=False)
        separate = {"zh": "实时模拟和历史回测是两种不同的记录：价格口径不同（当日实际价格、不计分红，对比复权日线），"
                          "成交方式也不同。它们分开列出，不相加，也不能直接比较。",
                    "en": "The live simulation and the historical backtests are different kinds of "
                          "record: different prices (the day's own, no dividends, against adjusted "
                          "daily bars) and different fills. They are listed apart, are never added "
                          "together, and are not directly comparable."}
        if sim is None:
            return {"exists": False, "simulation": None, "backtests": [], "note": separate}
        profiles = sim.profiles()
        db = sim.state_db(profiles)
        active = store.active_plan(db)
        version = store.version(db, active["opening_version"]) if active else None
        marks = store.equity_marks(db)
        equity = [{"date": day, **{key: marks[day].get(key) for key in (
            "equity_usd", "cash_usd", "invested_usd", "funded_usd", "positions")}, "stale": []}
                  for day in sorted(marks)]
        everything = sim._store(profiles).all()
        fills = sim._fills_for_replay(profiles)
        paid = venue.fill_costs(paper.fill_record_path(profiles))
        mine = [{"date": str(row.get("at") or "")[:10], "symbol": row.get("symbol"),
                 "side": row.get("side"), "quantity": row.get("quantity"), "price": row.get("price"),
                 "cost_usd": (paid.get(str(row.get("client_order_id"))) or {}).get("cost_usd", 0.0)}
                for row in fills if row.get("bucket") == MICRO_BUCKET]
        closed = positions.closed_trades(fills=fills, proposals=everything, bucket=MICRO_BUCKET)
        seen = trader.seen_signals(db)
        outcomes: dict[str, int] = {}
        for entry in seen.values():
            if entry.get("kind") == "tranche":
                continue                        # a tranche is not a research conclusion
            code = str((entry.get("diagnosis") or {}).get("category") or "unknown")
            outcomes[code] = outcomes.get(code, 0) + 1
        since = equity[0]["date"] if equity else ""
        research = [row for row in self.research_history(limit=500) if row.get("bucket") == MICRO_BUCKET]
        jobs = [row for row in self.research_jobs(limit=500) if row.get("bucket") == MICRO_BUCKET]
        simulation: dict[str, Any] = {
            "window": {"start": since or None, "end": equity[-1]["date"] if equity else None,
                       "sessions": len(equity)},
            "sessions_counted": store.simulated_days(db, active["opening_version"]) if active else 0,
            "rows": {}, "differences": {}, "conditions": None, "comparators_unavailable": None,
            # What the record did and did not do, all of it.
            "research": evaluation._decisions(research, jobs, [p.as_dict() for p in everything], since),
            "conclusions_by_outcome": outcomes,
            "trades": closed[-50:],
            "confidence": metrics.by_confidence(closed),
            "costs": cost_model.STANDARD.describe(),
            "caveat": {"zh": f"已记录 {len(equity)} 个交易日。这是发生过的事情的记录，不是策略有效的证据；"
                             "达到观察天数也不等于盈利能力已被证明。",
                       "en": f"{len(equity)} session(s) on record. This is a record of what "
                             "happened, not evidence that the strategy works; reaching the number "
                             "of sessions observed is not proof that it makes money."},
        }
        if equity:
            opening = float(equity[0]["funded_usd"] or 0.0)
            simulation["rows"]["strategy"] = comparison.strategy_row(
                equity, fills=mine, trades=closed, opening_usd=opening,
                says={"zh": "策略：模拟账户自己的记录（按研究结论和方案规则，经完整的风控与执行流程）。",
                      "en": "Strategy: the simulated account's own record -- research conclusions "
                            "under the plan's rules, through the whole risk and execution path."})
        if equity and active:
            try:
                setup = self._auto_experiment_setup(sim, profiles)
                start, end = equity[0]["date"], min(equity[-1]["date"], bar_source.last_completed_session())
                if end < start:
                    raise bar_source.BarsUnavailable(
                        "no session in the record has finished yet, so there is nothing to compare it with")
                # The same money, on the same days: whatever moved the account's funding between
                # two of its marks reaches every comparator on the later of them.
                flows = [bt.Flow(later["date"], round(float(later["funded_usd"]) - float(earlier["funded_usd"]), 2))
                         for earlier, later in pairwise(equity)
                         if abs(float(later["funded_usd"]) - float(earlier["funded_usd"])) > 0.005]
                warm = self._auto_warm_from(start, setup["mode"], setup["rules"])
                history = bar_source.read([*setup["rules"].universe, *setup["weights"]], start=warm, end=end)
                compared = comparison.run(
                    bars=history, start=start, end=end, mode=setup["mode"], rules=setup["rules"],
                    capital_usd=setup["capital"], opening_cash_usd=float(equity[0]["funded_usd"]),
                    deploy_cap_usd=setup["cap"], benchmark_weights=setup["weights"], flows=flows,
                    costs=cost_model.STANDARD, min_order_usd=setup["minimum"],
                    limit_class_of=setup["limit_class_of"])
                simulation["rows"].update(compared["rows"])
                simulation["conditions"] = compared["conditions"]
                simulation["differences"] = comparison.differences(
                    simulation["rows"]["strategy"], compared["rows"])
            except (bar_source.BarsUnavailable, ServiceError) as exc:
                simulation["comparators_unavailable"] = str(exc)[:300]
        return {"exists": True, "checked_at": utcnow().isoformat(),
                "version": None if version is None else {
                    "version_id": version["version_id"], "mode": version["mode"],
                    "validation": version["validation"]},
                "simulation": simulation,
                "backtests": list((version or {}).get("evidence") or []),
                "note": separate}

    def _auto_research_coverage(self, universe: list[str]) -> dict[str, Any]:
        """Whether scheduled research would ever look at what the plan may buy.

        The simulated account reads the real account's research rather than paying for its own,
        so a plan whose funds nobody researches is a plan that never gets a signal. That is said
        here, with the two ways to change it; neither is done from here.
        """
        from .. import schedule
        from ..advisor import SNAPSHOT

        profiles = self.profiles()
        cfg = schedule.settings(profiles)
        # What the schedule would study is the scheduler's own answer, cut as the scheduler cuts
        # it: the universe less what the account holds, by the schedule's cap and then the
        # task's. Reading the caps a second time here once let this name funds the study never
        # reaches, whenever the task's cap was the larger of the two.
        scheduled = ({task.kind: task for task in schedule.tasks(cfg, bucket=MICRO_BUCKET)}
                     if cfg.get("enabled") else {})
        study = scheduled.get(schedule.CANDIDATES)
        studied = ([str(s).upper() for s in schedule.subjects_for(profiles, study)][:study.max_tickers]
                   if study is not None else [])
        watched: list[str] = []
        if schedule.WATCHLIST in scheduled:
            snapshot = self.state_db(profiles).read_meta(SNAPSHOT, {}) or {}
            watched = schedule.watchlist_names(snapshot)
        # A fund the account holds is set aside by the study because the holdings review examines
        # it. That is research on it all the same, so it is not reported as unstudied.
        reviewed = ([str(s).upper() for s in schedule.subjects_for(profiles, scheduled[schedule.HOLDINGS])]
                    if schedule.HOLDINGS in scheduled else [])
        wanted = [str(symbol).upper() for symbol in universe]
        covered = [symbol for symbol in wanted
                   if symbol in studied or symbol in watched or symbol in reviewed]
        missing = [symbol for symbol in wanted if symbol not in covered]
        text = None
        if wanted and missing and study is not None:
            # The study is already on: it takes one fund from each exposure group, so switching
            # it on is not something left to do, and saying so again would send the owner back
            # to a switch they have just made.
            covers = {"zh": f"，它每周研究的是 {'、'.join(studied)}（每个类别一只）" if studied else "",
                      "en": f" and covers {', '.join(studied)} (one fund from each exposure group)"
                            if studied else ""}
            text = {"zh": (f"“候选 ETF”研究已经打开{covers['zh']}，"
                           f"不包括 {'、'.join(missing)}，所以模拟账户不会收到它们的买入信号。"
                           "想让它们也被研究，把它们加进你的 Robinhood 自选（现有的自选研究会轮到它们）。"),
                    "en": (f"The candidate-ETF study is on{covers['en']}, not {', '.join(missing)}, "
                           "so the simulated account will get no signal to buy them. To have them "
                           "studied as well, add them to your Robinhood watchlist, which the "
                           "existing watchlist study will reach.")}
        elif wanted and missing:
            text = {"zh": (f"每周研究目前不会研究 {'、'.join(missing)}，所以模拟账户不会收到它们的买入信号。"
                           "两个办法：把它们加进你的 Robinhood 自选（现有的自选研究会轮到它们），"
                           "或在设置里打开“候选 ETF”研究（每周最多多用 3 次研究额度）。"),
                    "en": (f"Scheduled research does not currently study {', '.join(missing)}, so the "
                           "simulated account will get no signal to buy them. Either add them to "
                           "your Robinhood watchlist, which the existing watchlist study will reach, "
                           "or switch on the candidate-ETF study in Settings (up to three more "
                           "analyses a week).")}
        return {"universe": wanted, "covered": covered, "missing": missing,
                "candidates_enabled": study is not None,
                "on_watchlist": [s for s in wanted if s in watched],
                "text": text}


def _deployment_unavailable(exc: Exception) -> dict[str, Any]:
    """The block when it cannot be worked out: no progress drawn, and the reason (contract 3.6)."""
    return {"template": plan.BASELINE, "targets": [], "regime": None, "as_of": None,
            "error": {"code": "deployment_unavailable",
                      "text": {"zh": f"现在读不出基准建仓的进度：{type(exc).__name__}。",
                               "en": f"Where the baseline plan stands cannot be read right now: "
                                     f"{type(exc).__name__}."}}}


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in ("paused", "reason", "kind", "at", "by",
                                            "loss_baseline_usd")}


def _public_request(request: dict[str, Any]) -> dict[str, Any]:
    """A waiting start as a screen sees it: what was confirmed and what it waits for, not the
    whole plan document kept beside it."""
    return {key: request.get(key) for key in (
        "plan_id", "status", "requested_at", "requested_by", "expires_at", "inputs", "version_id",
        "changes", "waiting_for", "last_checked_at", "last_reason")}


def _already_covered(change: dict[str, Any], profiles: Profiles) -> bool:
    """Whether a change a strategy asks for is one the configuration in force already satisfies.

    Two cases, and in both the plan would only be asking for something narrower than what was
    confirmed, which needs no one's leave:

    * a strategy that wants to be surer than the configuration requires. The configured floor is
      what the gate holds every automatic purchase to; the strategy's own, higher bar is held by
      the plan itself;
    * an allowance that would come out as a smaller share of the account, because the account
      has grown since -- a deposit, a gain. The dollar cap confirmed with the plan is what holds
      the amount; the share only ever needed to be at least this large.
    """
    if change.get("from") is None:
        return False
    try:
        if change["path"].endswith(".autonomy.open.min_confidence"):
            return int(change["to"]) >= int(change["from"])
        if change["path"].endswith(".autonomy.open.max_pct_of_account"):
            return float(change["to"]) <= float(change["from"])
    except (TypeError, ValueError):
        return False
    return False
