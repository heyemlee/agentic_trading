"""ct-sources: the outside information sources, from the command line.

    ct-sources status                    which are on, what they reach, whether the SEC contact is set
    ct-sources check AAPL                read each switched-on source once, now, and say how it ended
    ct-sources compare                   runs with and without each source, side by side
    ct-sources flag RUN --source reddit --note "..."     write down that a source was wrong

``status``, ``compare`` and ``flag`` are offline. ``check`` reaches the sources themselves and
is the only thing here that does; it reads what research would read and changes nothing. None
of them can switch a source on: that is a setting in the risk profile, made in the console.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import sources
from ..env import load_env
from ..profile import ProfileError, load_profiles
from . import comparison, reddit, sec_form4


def _say(lang: str, zh: str, en: str) -> str:
    return zh if lang == "zh" else en


def status(profiles: Any, *, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Which sources are on, what they reach, and what each still needs. Reads nothing remote."""
    settings = sources.load(profiles)
    try:
        sec_form4.resolve_contact(settings.form4, environ)
        contact = {"set": True, "variable": settings.form4.contact_env}
    except sec_form4.NotConfigured as exc:
        contact = {"set": False, "variable": settings.form4.contact_env, "why": str(exc)}
    root = sources.cache_dir(profiles)
    kept = len(list((root / sources.FORM4 / "filings").glob("*.xml"))) if root.exists() else 0
    return {
        "sources": {
            sources.REDDIT: {"on": settings.reddit.enabled, "setting": sources.switch(sources.REDDIT),
                             "kind": reddit.KIND, "channel": reddit.CHANNEL,
                             "subreddits": list(settings.reddit.subreddits) or "upstream's default list",
                             "max_requests_per_read": settings.reddit.max_requests},
            sources.FORM4: {"on": settings.form4.enabled, "setting": sources.switch(sources.FORM4),
                            "kind": sec_form4.KIND, "channel": sec_form4.CHANNEL,
                            "contact": contact, "analyst": settings.form4.analyst,
                            "lookback_days": settings.form4.lookback_days,
                            "max_filings": settings.form4.max_filings, "filings_kept": kept},
        },
        "reaches": {kind: sources.in_effect(profiles, (kind,)) for kind in ("etf", "stock", "crypto")},
        "kept_in": str(root),
    }


def check(profiles: Any, ticker: str, *, as_of_date: str | None = None,
          environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read each switched-on source once for one ticker. Reaches the sources; changes nothing."""
    settings = sources.load(profiles)
    root = sources.cache_dir(profiles)
    today = datetime.now(UTC).date()
    end = as_of_date or today.isoformat()
    start = (datetime.fromisoformat(end).date() - timedelta(days=7)).isoformat()
    out: dict[str, Any] = {"ticker": ticker.upper(), "as_of": end, "reads": {}}
    _, reading = reddit.read(ticker, settings=settings.reddit, start_date=start, end_date=end, cache=root)
    out["reads"][sources.REDDIT] = reading
    _, reading = sec_form4.read(ticker, settings=settings.form4, cache=root, as_of_date=as_of_date,
                                environ=environ)
    out["reads"][sources.FORM4] = reading
    return out


def _print_status(found: dict[str, Any], lang: str) -> None:
    for name, row in found["sources"].items():
        state = _say(lang, "开启", "on") if row["on"] else _say(lang, "关闭", "off")
        print(f"{name}: {state}  ({row['setting']})")
        print(f"    {_say(lang, '性质', 'kind')}: {row['kind']}   {_say(lang, '渠道', 'channel')}: {row['channel']}")
        if name == sources.FORM4:
            contact = row["contact"]
            print("    " + (_say(lang, f"SEC 联系方式：已设置（{contact['variable']}）",
                                 f"SEC contact: set ({contact['variable']})") if contact["set"] else
                            _say(lang, f"SEC 联系方式：未设置。{contact['why']}",
                                 f"SEC contact: not set. {contact['why']}")))
            print("    " + _say(lang, f"交给的分析师：{row['analyst']}；回看 {row['lookback_days']} 天，"
                                      f"最多 {row['max_filings']} 份；本机已存 {row['filings_kept']} 份申报",
                                f"handed to: {row['analyst']} analyst; {row['lookback_days']} days back, "
                                f"at most {row['max_filings']} filings; {row['filings_kept']} filings kept here"))
    print(_say(lang, "进入哪类研究：", "reaches the research of:"))
    for kind, reads in found["reaches"].items():
        print(f"    {kind}: " + (", ".join(f"{k} ({v})" for k, v in reads.items())
                                 or _say(lang, "无", "none")))


def _print_check(found: dict[str, Any], lang: str) -> None:
    print(f"{found['ticker']}  ({_say(lang, '截至', 'as of')} {found['as_of']})")
    for name, reading in found["reads"].items():
        outcome = str(reading.get("outcome"))
        print(f"  {name}: {outcome} -- {sources.says(outcome)[lang]}"
              + (f" ({reading['detail']})" if reading.get("detail") else ""))
        print(f"      {_say(lang, '请求', 'requests')}: {len(reading.get('requests') or [])}, "
              f"{_say(lang, '条目', 'items')}: {len(reading.get('items') or [])}")
        for row in reading.get("requests") or []:
            print(f"      - {row.get('outcome')}: {row.get('url')}")


def _print_compare(found: dict[str, Any], lang: str) -> None:
    print(_say(lang, f"研究记录：{found['runs']} 条", f"decision records: {found['runs']}"))
    for name, row in found["variants"].items():
        usage = row["usage"]
        print(f"\n[{name}]  {_say(lang, '运行', 'runs')}: {row['runs']}   "
              f"{_say(lang, '结论', 'actions')}: {row['conclusions']['actions']}   "
              f"{_say(lang, '平均把握度', 'mean confidence')}: {row['conclusions']['mean_confidence']}")
        print(f"    {_say(lang, '每次运行', 'per run')}: {usage['calls_per_run']} "
              f"{_say(lang, '次模型调用', 'model calls')}, {usage['input_tokens_per_run']} in / "
              f"{usage['output_tokens_per_run']} out tokens, {usage['wall_seconds_per_run']} s")
        for source, reads in row["sources"].items():
            print(f"    {source}: {reads['reads']} {_say(lang, '次读取', 'reads')}, "
                  f"{_say(lang, '失败率', 'failure rate')} {reads['failure_rate_pct']}%, "
                  f"{reads['outcomes']}")
        if row["flagged_wrong"]:
            print(f"    {_say(lang, '被标记为错误信息', 'flagged as wrong')}: {row['flagged_wrong']}")
    pairs = found["pairs"]
    print("\n" + _say(lang, f"同题配对：{pairs['count']} 组", f"pairs on the same question: {pairs['count']}"))
    for name, effect in pairs["effects"].items():
        print(f"    {name}: {effect['pairs']} {_say(lang, '组', 'pairs')}, "
              f"{_say(lang, '结论改变', 'action changed')} {effect['action_changed']}, "
              f"{_say(lang, '把握度平均变化', 'mean confidence change')} {effect['mean_confidence_points']}")
    print("\n" + found["returns"][lang])
    print(found["verdict"][lang])


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="ct-sources",
        description="The outside information sources research may read: Reddit and SEC Form 4. "
                    "Nothing here switches one on, and nothing here can affect an order.")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--lang", choices=("zh", "en"), default="zh")
    parser.add_argument("--json", action="store_true", help="print the full document as JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="which sources are on and what each still needs (offline)")
    probe = commands.add_parser("check", help="read each switched-on source once (reaches them)")
    probe.add_argument("ticker")
    probe.add_argument("--date", default=None, help="YYYY-MM-DD: read as of this date")
    compare = commands.add_parser("compare", help="runs with and without each source (offline)")
    compare.add_argument("--runs-dir", default=None)
    compare.add_argument("--since", default=None, help="YYYY-MM-DD: only runs about this date or later")
    mark = commands.add_parser("flag", help="record that something a source said was wrong")
    mark.add_argument("run_id")
    mark.add_argument("--source", required=True, choices=(sources.REDDIT, sources.FORM4))
    mark.add_argument("--note", required=True)
    args = parser.parse_args(argv)

    try:
        profiles = load_profiles(args.config_dir)
    except ProfileError as exc:
        print(f"ct-sources: {exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        found = status(profiles)
        printer = _print_status
    elif args.command == "check":
        found = check(profiles, args.ticker, as_of_date=args.date)
        printer = _print_check
    elif args.command == "compare":
        from ..service import Service

        runs = Path(args.runs_dir) if args.runs_dir else profiles.project_dir / "runs"
        # A deployment with no state yet has no flags, and reads as none.
        flagged = comparison.flags(Service(profiles.config_dir).state_db(profiles))
        found = comparison.summarise(comparison.load_records(runs, since=args.since), flagged=flagged)
        printer = _print_compare
    else:
        from ..service import Service

        try:
            found = comparison.flag(Service(profiles.config_dir).state_db(profiles),
                                    run_id=args.run_id, source=args.source, note=args.note,
                                    identity=os.environ.get("USER", "local-operator"))
        except ValueError as exc:
            print(f"ct-sources: {exc}", file=sys.stderr)
            return 2

        def printer(row: dict[str, Any], lang: str) -> None:
            print(_say(lang, f"已记录：{row['run_id']} 的 {row['source']} —— {row['note']}",
                       f"recorded: {row['source']} in {row['run_id']} -- {row['note']}"))

    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
    else:
        printer(found, args.lang)
    if args.command == "check":
        asked = [r for r in found["reads"].values() if r.get("outcome") != sources.DISABLED]
        return 0 if all(r.get("outcome") in (*sources.ANSWERED, sources.PARTIAL,
                                             sources.NOT_APPLICABLE) for r in asked) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
