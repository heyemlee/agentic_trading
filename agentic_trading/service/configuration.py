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
        "investor_profile.yaml": ("universe.core.individual_stocks.watchlist",),
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
            "autonomy": {"armed": autonomy.is_armed(profiles, MICRO_BUCKET),
                         "withdrawn": downgrade.tripped, "reason": downgrade.reason,
                         "at": downgrade.at},
            "launchd_managed": bool(os.environ.get("CT_RELEASE_DIR")),
        }

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

    def preview_config_change(self, *, file: str, revision: str = "", key: str | None = None,
                              text: str | None = None, set: dict[str, Any] | None = None) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        spec = cc.ChangeSpec(file=file, key=key, text=text, set=dict(set or {}))
        current, new_text, verdict = self._prepare_change(profiles, spec, revision)
        return {"file": file, "diff": cc.unified_diff(current, new_text, file),
                "changed": current != new_text, "revision": cc.revision(current), **verdict}

    def submit_config_change(self, *, file: str, revision: str = "", key: str | None = None,
                             text: str | None = None, set: dict[str, Any] | None = None,
                             reason: str = "", identity: str | None = None,
                             confirmed: bool = False) -> dict[str, Any]:
        """Apply a change now, or park it for the cooling period when it loosens anything.

        ``confirmed`` says the caller re-verified the console password; a loosening change
        without it is refused before anything is written or recorded.
        """
        from .. import config_changes as cc

        profiles = self.profiles()
        spec = cc.ChangeSpec(file=file, key=key, text=text, set=dict(set or {}))
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
