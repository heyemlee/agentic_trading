"""Reading and changing config/*.yaml through the console, with the cooling-off period.

A change that loosens a limit waits; one that tightens it applies at once. Which is which is
decided in config_changes.py, not here.
"""

from __future__ import annotations

import logging
import os
from datetime import date as calendar_date
from datetime import datetime
from typing import Any, ClassVar

from .. import autonomy
from ..micro_live import BUCKET as MICRO_BUCKET
from ..profile import Profiles
from ..proposals import utcnow
from .errors import ServiceConflict, ServiceError

#: Named for the package rather than this module: the split is internal, so a log
#: record must still read as it did, and getLogger returns the one same object.
logger = logging.getLogger("agentic_trading.service")


class ConfigurationMixin:
    """The configuration a console may read, preview, submit, withdraw and apply."""

    # ---------------------------------------------------------------- configuration
    #: Values the console's forms edit, read here so a form and the file cannot disagree.
    FORM_PATHS: ClassVar = {
        "risk_profile.yaml": (
            "research_schedule.enabled", "research_schedule.period",
            "research_schedule.launchd.weekday", "research_schedule.launchd.hour",
            "research_schedule.launchd.minute", "research_schedule.on_non_session_day",
            "research_schedule.tickers", "research_schedule.max_tickers_per_run",
            "research_schedule.max_analyses_per_day", "research_schedule.run_while_paused"),
        "investor_profile.yaml": (),
        "event_calendar.yaml": ("maintained_through", "events"),
    }

    def _config_text(self, profiles: Profiles, file: str) -> str:
        from .. import config_changes as cc

        if file not in cc.FILES:
            raise ServiceError(f"{file} is not a file the console may edit")
        return (profiles.config_dir / file).read_text(encoding="utf-8")

    def config_overview(self) -> dict[str, Any]:
        """The editable files, their sections, the forms' current values and the change trail."""
        from .. import config_changes as cc

        profiles = self.profiles()
        files = []
        values: dict[str, Any] = {}
        for name in cc.FILES:
            text = self._config_text(profiles, name)
            doc = yaml_load(text)
            files.append({
                "file": name, "revision": cc.revision(text),
                "sections": [{"key": s.key, "lines": s.lines,
                              "guarded": cc._guarded(name, s.key)} for s in cc.sections(text)],
            })
            for path in self.FORM_PATHS[name]:
                values[path] = _jsonable(cc.get_value(doc, path))
        records = cc.records(self.state_db(profiles))
        downgrade = autonomy.load_downgrade(profiles)
        return {
            "files": files,
            "values": values,
            "cooling_hours": cc.COOLING_HOURS,
            "pending": [cc.public(r) for r in records if r["status"] == cc.PENDING],
            "recent": [cc.public(r) for r in reversed(records) if r["status"] != cc.PENDING][:20],
            # Kept one round for the console that reads it; `authority` is the whole answer.
            "autonomy": {"armed": autonomy.is_armed(profiles, MICRO_BUCKET),
                         "withdrawn": downgrade.tripped, "reason": downgrade.reason,
                         "at": downgrade.at},
            "authority": self.effective_authority(),
            "research": self._research_overview(profiles),
            "launchd_managed": bool(os.environ.get("CT_RELEASE_DIR")),
        }

    def _research_overview(self, profiles: Profiles) -> dict[str, Any]:
        """The research switches as the Settings page draws them, and how often the job fires.

        Each switch is answered by the code that acts on it, so the page cannot show a study as
        on that the schedule would not run. The SEC contact is reported as set or not set, with
        the name of the variable: its value names the owner and never leaves ``.env``.
        """
        from .. import config_changes as cc
        from .. import schedule, sources
        from ..sources import sec_form4

        cfg = schedule.settings(profiles)
        account = (cfg.get("accounts") or {}).get(MICRO_BUCKET)
        scheduled = isinstance(account, dict) and bool(account)
        candidates: dict[str, Any] = {"on": False, "path": cc.RESEARCH_SWITCHES["candidates"],
                                      # Off until an account is scheduled by name: see
                                      # config_changes.research_switch.
                                      "available": scheduled}
        fires_every = str(cfg.get("period", "weekly"))
        try:
            block = (account or {}).get(schedule.CANDIDATES) if scheduled else None
            block = block if isinstance(block, dict) else {}
            study = schedule.Task(
                bucket=MICRO_BUCKET, kind=schedule.CANDIDATES,
                period=str(block.get("period", cfg.get("period", "weekly"))),
                max_tickers=int(block.get("max_tickers_per_run", cfg.get("max_tickers_per_run", 3))))
            candidates.update(
                on=any(task.kind == schedule.CANDIDATES
                       for task in schedule.tasks(cfg, bucket=MICRO_BUCKET)),
                period=study.period, max_tickers_per_run=study.max_tickers,
                # What the study would look at today: the universe less what is held.
                tickers=schedule.subjects_for(profiles, study)[:study.max_tickers])
            fires_every = schedule.finest_period(cfg)
        except (TypeError, ValueError) as exc:
            # A schedule the scheduler could not read either. This page is where the section is
            # put right, so it still has to open: the switch is withheld and says why.
            candidates.update(on=False, available=False,
                              why=f"research_schedule could not be read: {exc}")
        form4 = sources.load(profiles).form4
        try:
            sec_form4.resolve_contact(form4)
            contact = {"set": True, "variable": form4.contact_env}
        except sec_form4.NotConfigured as exc:
            contact = {"set": False, "variable": form4.contact_env, "why": str(exc)}

        def reads(name: str) -> bool:
            return profiles.risk_cfg(*sources.SETTINGS, name, "enabled", default=False) is True

        return {
            "fires_every": fires_every,
            "switches": {
                "candidates": candidates,
                sources.FORM4: {"on": reads(sources.FORM4), "path": cc.RESEARCH_SWITCHES[sources.FORM4],
                                "available": True, "contact": contact},
                sources.REDDIT: {"on": reads(sources.REDDIT), "path": cc.RESEARCH_SWITCHES[sources.REDDIT],
                                 "available": True},
            },
        }

    def effective_authority(self) -> dict[str, Any]:
        """What may happen to the real-money account, from every rule that decides it.

        One deterministic object (see :func:`autonomy.effective_authority`) for the settings
        screen, the risk screen, the autonomous loop and an approval to read alike. Opens no
        venue session: the preflight block is answered from the reading ``/micro-live`` keeps,
        and says so when there is none or it has aged past its window.
        """
        from .. import config_changes as cc
        from ..micro_live import preflight_from, run_preflight

        try:
            profiles = self.profiles()
        except ServiceError as exc:
            # The same three-field shape /health answers with when the config will not load.
            return {"checked_at": utcnow().isoformat(), "status": "CONFIG_ERROR", "detail": str(exc)}
        store = self._store(profiles)
        inputs = autonomy.authority_inputs(profiles, store.db)
        version = autonomy.version_of(inputs)
        history = autonomy.record_version(store.db, inputs, version)
        reading, stale = self._venue_reading.peek()
        preflight = (preflight_from(profiles, reading) if reading is not None
                     else run_preflight(profiles, None))
        ladder = self.risk_state().get(MICRO_BUCKET) or {}
        pending = [cc.public(r) for r in cc.records(store.db) if r["status"] == cc.PENDING]
        authority = autonomy.effective_authority(
            profiles, inputs=inputs, version=version, history=history, proposals=store.all(),
            preflight=preflight, preflight_stale=stale, pending_changes=pending,
            ladder_halted=bool(ladder.get("manual_reset_required")))
        authority["preflight"]["checked_at"] = reading.checked_at if reading is not None else None
        return authority

    def config_section(self, file: str, key: str) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        text = self._config_text(profiles, file)
        try:
            item = cc.section(text, key)
        except cc.ConfigChangeError as exc:
            raise ServiceError(str(exc)) from exc
        return {"file": file, "key": key, "text": item.text, "revision": cc.revision(text),
                "guarded": cc._guarded(file, key)}

    def _prepare_change(self, profiles: Profiles, spec, revision: str) -> tuple[str, str, dict[str, Any]]:
        """The current text, the rebuilt text and the verdict; every refusal is a ServiceError."""
        from .. import config_changes as cc

        current = self._config_text(profiles, spec.file)
        if revision and revision != cc.revision(current):
            raise ServiceConflict(f"{spec.file} changed since it was loaded; reload and try again")
        try:
            new_text = cc.build(current, spec)
            cc.validate(profiles.config_dir, spec.file, new_text)
            verdict = cc.classify(spec.file, current, new_text)
        except cc.ConfigChangeError as exc:
            raise ServiceError(str(exc)) from exc
        return current, new_text, verdict

    def _requested_change(self, profiles: Profiles, *, file: str, key: str | None,
                          text: str | None, set: dict[str, Any] | None,
                          switch: str | None, on: bool | None):
        """The change a request describes: one section's text, values for paths, or a switch by name.

        Text and values are built from the request as it came, so they can name no setting to
        create (see ChangeSpec.create). A switch is looked up by its name in
        ``config_changes.RESEARCH_SWITCHES``; the key it sets, and may create, is named there.
        """
        from .. import config_changes as cc

        if switch is None:
            if on is not None:
                raise ServiceError("on or off is said of a switch; name the switch")
            return cc.ChangeSpec(file=file, key=key, text=text, set=dict(set or {}))
        if key is not None or text is not None or set:
            raise ServiceError("describe the change as a switch or as text or values, not both")
        if on is None:
            raise ServiceError("a switch is turned on or off; say which")
        try:
            spec = cc.research_switch(self._config_text(profiles, "risk_profile.yaml"), switch, on)
        except cc.ConfigChangeError as exc:
            raise ServiceError(str(exc)) from exc
        if file != spec.file:
            raise ServiceError(f"the {switch} switch is a setting in {spec.file}")
        if on and switch == "sec_form4":
            # Refused here rather than read as on while every read ends "not configured": the
            # SEC is asked nothing until the deployment says who is asking, and the message
            # names the variable to set.
            from .. import sources
            from ..sources import sec_form4

            try:
                sec_form4.resolve_contact(sources.load(profiles).form4)
            except sec_form4.NotConfigured as exc:
                raise ServiceError(str(exc)) from exc
        return spec

    def preview_config_change(self, *, file: str, revision: str = "", key: str | None = None,
                              text: str | None = None, set: dict[str, Any] | None = None,
                              switch: str | None = None, on: bool | None = None) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        spec = self._requested_change(profiles, file=file, key=key, text=text, set=set,
                                      switch=switch, on=on)
        current, new_text, verdict = self._prepare_change(profiles, spec, revision)
        return {"file": file, "diff": cc.unified_diff(current, new_text, file),
                "changed": current != new_text, "revision": cc.revision(current), **verdict}

    def submit_config_change(self, *, file: str, revision: str = "", key: str | None = None,
                             text: str | None = None, set: dict[str, Any] | None = None,
                             switch: str | None = None, on: bool | None = None,
                             reason: str = "", identity: str | None = None,
                             confirmed: bool = False) -> dict[str, Any]:
        """Apply a change now, or park it for the cooling period when it loosens anything.

        ``confirmed`` says the caller re-verified the console password; a loosening change
        without it is refused before anything is written or recorded.
        """
        spec = self._requested_change(self.profiles(), file=file, key=key, text=text, set=set,
                                      switch=switch, on=on)
        return self._submit_change(spec, revision=revision, reason=reason, identity=identity,
                                   confirmed=confirmed)

    def _submit_change(self, spec, *, revision: str = "", reason: str = "",
                       identity: str | None = None, confirmed: bool = False) -> dict[str, Any]:
        """Put one change through the console's rules: classified, then applied or parked.

        The one way a configuration change is made from inside the service, whoever built the
        spec. A confirmed automatic-trading plan comes through here too (``service.auto``), which
        is what "the plan's settings go through the console's confirmation" means: the same
        classification, the same password for a loosening, the same day of waiting, the same
        record in Settings where it can be withdrawn.
        """
        from .. import config_changes as cc

        profiles = self.profiles()
        current, new_text, verdict = self._prepare_change(profiles, spec, revision)
        if current == new_text:
            raise ServiceError("nothing changed")
        if verdict["classification"] == cc.COOLING and not confirmed:
            raise ServiceError("this change loosens a limit; confirm it with the console password")
        db = self.state_db(profiles)
        record = cc.new_record(spec, base=cc.base_of(current, spec), verdict=verdict,
                               identity=identity or "console", reason=reason[:500])
        if record["status"] == cc.APPLIED:
            record["outcome"] = self._write_config(profiles, spec.file, new_text, record)
        with db.transaction() as conn:
            items = cc.records(db)
            items.append(record)
            cc.save(db, items, conn)
        self._journal(profiles,
                      kind="config_changed" if record["status"] == cc.APPLIED else "config_change_pending",
                      subject=record["id"], actor=identity or "console",
                      outcome=record["status"],
                      detail={"file": spec.file, "paths": record["paths"][:20],
                              "classification": record["classification"]})
        return cc.public(record)

    def autonomy_switch(self, block: str, on: bool, *, value: int | None = None, reason: str = "",
                        identity: str | None = None, confirmed: bool = False) -> dict[str, Any]:
        """Turn one of the Agentic account's two autonomy switches on or off, at once.

        On needs the console password (``confirmed``) and may set the switch's number in the
        same change; off needs neither. Both are written, recorded and committed like any other
        console change, so the trail and Settings' recent changes show them, and turning one on
        raises an urgent notice to every signed-in device (see notifications.changes). See
        ``config_changes.switch_change`` for why these two apply at once and nothing else does.
        """
        from .. import config_changes as cc

        profiles = self.profiles()
        if on and not confirmed:
            raise ServiceError("turning autonomy on needs the console password")
        file = "risk_profile.yaml"
        current = self._config_text(profiles, file)
        floor = int(profiles.confidence.minimum_to_open.get("autonomous_any", 80))
        try:
            spec, new_text, verdict = cc.switch_change(current, block, on, value, confidence_floor=floor)
            cc.validate(profiles.config_dir, file, new_text)
        except cc.ConfigChangeError as exc:
            raise ServiceError(str(exc)) from exc
        if current == new_text:
            # Asked twice, or from two devices: already so, and nothing to record.
            return {"block": block, "on": on, "changed": False, "change": None}
        db = self.state_db(profiles)
        if on:
            # A change to this switch still cooling would land on top of this one tomorrow, from
            # a premise that no longer holds, so it is withdrawn from Settings first. Never for
            # off: stopping is one gesture whatever else is waiting.
            prefix = f"buckets.{cc.SWITCH_BUCKET}.autonomy.{block}."
            for pending in cc.records(db):
                if pending["status"] == cc.PENDING and any(str(p).startswith(prefix)
                                                           for p in pending.get("paths") or []):
                    raise ServiceConflict(f"change {pending['id']} to this switch is still cooling; "
                                          "withdraw it in Settings first")
        record = cc.new_record(spec, base=cc.base_of(current, spec), verdict=verdict,
                               identity=identity or "console", reason=reason[:500])
        record["switch"] = {"block": block, "on": bool(on)}
        # The whole file is rewritten, so one written from elsewhere since it was read here would
        # be lost under this one. Asked again, the switch reads the file as it now is.
        if self._config_text(profiles, file) != current:
            raise ServiceConflict(f"{file} changed while the switch was being set; try again")
        record["outcome"] = self._write_config(profiles, file, new_text, record)
        with db.transaction() as conn:
            items = cc.records(db)
            items.append(record)
            cc.save(db, items, conn)
        self._journal(profiles, kind="config_changed", subject=record["id"],
                      actor=identity or "console", outcome=record["status"],
                      detail={"file": file, "paths": record["paths"][:20],
                              "classification": record["classification"], "switch": record["switch"]})
        return {"block": block, "on": on, "changed": True, "change": cc.public(record)}

    def _write_config(self, profiles: Profiles, file: str, new_text: str,
                      record: dict[str, Any]) -> dict[str, Any]:
        """Write the file, commit it locally, and re-register launchd if the schedule moved."""
        from .. import config_changes as cc
        from ..storage import atomic_write

        atomic_write(profiles.config_dir / file, new_text)
        paths = ", ".join(record["paths"][:6]) + (", …" if len(record["paths"]) > 6 else "")
        message = (f"config: {file} {paths} (console, {record['requested_by']})"
                   + (f"\n\n{record['reason']}" if record.get("reason") else ""))
        outcome = {"git": cc.commit(profiles.project_dir, file, message,
                                    author=f"{record['requested_by']} via console <console@agentic-trading.local>")}
        if any(p.startswith("research_schedule") for p in record["paths"]):
            outcome["launchd"] = self._reregister_research(profiles)
        return outcome

    def _reregister_research(self, profiles: Profiles) -> str:
        from .. import launchd

        try:
            return launchd.reregister_research(data_dir=profiles.project_dir)
        except Exception as exc:  # the file is written; this is reporting
            logger.exception("re-registering the research job failed")
            return f"launchd re-registration failed: {exc}"

    def cancel_config_change(self, change_id: str, identity: str | None = None) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        db = self.state_db(profiles)
        with db.transaction() as conn:
            items = cc.records(db)
            match = next((r for r in items if r["id"] == change_id), None)
            if match is None:
                raise ServiceError("no such change")
            if match["status"] != cc.PENDING:
                raise ServiceConflict(f"that change is already {match['status']}")
            match["status"] = cc.CANCELLED
            match["applied_at"] = utcnow().isoformat()
            match["outcome"] = {"cancelled_by": identity or "console"}
            cc.save(db, items, conn)
        self._journal(profiles, kind="config_change_cancelled", subject=change_id,
                      actor=identity or "console", detail={"file": match["file"]})
        return cc.public(match)

    def apply_config_changes(self) -> list[dict[str, Any]]:
        """Apply every parked change whose cooling period has ended. Runs on the API's loop."""
        from .. import config_changes as cc

        profiles = self.profiles()
        db = self.state_db(profiles)
        if not cc.due(cc.records(db), utcnow()):
            return []
        applied = []
        with db.transaction() as conn:
            items = cc.records(db)
            for record in cc.due(items, utcnow()):
                spec = cc.spec_of(record)
                current = self._config_text(profiles, spec.file)
                blocked = cc.moved(current, spec, record.get("base"))
                if blocked is None:
                    try:
                        new_text = cc.build(current, spec)
                        cc.validate(profiles.config_dir, spec.file, new_text)
                    except cc.ConfigChangeError as exc:
                        blocked = str(exc)
                record["applied_at"] = utcnow().isoformat()
                if blocked:
                    record["status"], record["error"] = cc.FAILED, blocked
                else:
                    record["status"] = cc.APPLIED
                    record["outcome"] = self._write_config(profiles, spec.file, new_text, record)
                applied.append(record)
            cc.save(db, items, conn)
        for record in applied:
            self._journal(profiles, kind="config_changed", subject=record["id"],
                          actor=record["requested_by"], outcome=record["status"],
                          detail={"file": record["file"], "paths": record["paths"][:20],
                                  "classification": record["classification"],
                                  "error": record.get("error")})
        return [cc.public(r) for r in applied]


# ---------------------------------------------------------------------- helpers
def yaml_load(text: str) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, calendar_date | datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value
