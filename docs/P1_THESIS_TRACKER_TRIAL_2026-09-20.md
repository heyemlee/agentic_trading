# P1 Thesis Tracker fixed-evidence screening trial

## Purpose and boundary

This is a research-quality screening experiment, not a trading feature.  It compares two
no-tools prompts on the same twenty public, hypothetical, frozen evidence packets:

- `control`: the project's concise fixed-evidence thesis assessment;
- `thesis-tracker`: a constrained adaptation of Anthropic's Thesis Tracker workflow.

Neither route receives account data, a broker, market-data tools, web tools, a proposal store, or
an execution path.  Both may only return a thesis classification and whether a new full research
run is required.  Their output schema deliberately has no trade-action field.

The artifact is a pre-screen for a later queue integration.  It is not an authorization to add a
new production route, change `config/`, or submit a proposal.

## Upstream provenance

| Item | Value |
| --- | --- |
| Source | `anthropics/financial-services` |
| Commit | `35c80df9f4fb49f7888c681242a690dcde403f24` |
| Skill | `plugins/vertical-plugins/equity-research/skills/thesis-tracker/SKILL.md` |
| License | Apache-2.0 |
| Adaptation | retain thesis/pillar/risk/catalyst comparison; remove its trade-action suggestions and document-output requirements |

The upstream workflow asks for a falsifiable thesis, supporting pillars, invalidation risks,
catalysts and an update log.  This trial treats the frozen packet as evidence and requires equal
treatment of disconfirming evidence.  It does not reproduce the upstream skill text at runtime,
download it, or update it automatically.

## Case pack

`tests/fixtures/thesis_tracker_cases.json` holds exactly twenty fictional, public cases:

- stocks and ETFs;
- strengthened, intact, weakened, broken and insufficient-evidence outcomes;
- missing evidence, source conflict, stale evidence and thesis-invalidating developments.

Each case declares its facts-as-of date, the original thesis, pillars, invalidation risks,
catalysts, developments, expected classification, whether full research is required, and facts a
reviewer must mention.  The pack contains no portfolio, account, venue, personal, or live-market
data.

## Running it

Run each route separately into a dedicated directory outside `state/` and `runs/`:

```bash
uv run ct-thesis-trial \
  --cases tests/fixtures/thesis_tracker_cases.json \
  --route control \
  --output-dir /tmp/agentic-thesis-trial/control

uv run ct-thesis-trial \
  --cases tests/fixtures/thesis_tracker_cases.json \
  --route thesis-tracker \
  --output-dir /tmp/agentic-thesis-trial/thesis-tracker
```

Each call uses the existing Claude Code subscription adapter with built-in tools disabled and a
three-turn ceiling. The remaining turns are only the CLI's local `StructuredOutput` schema commit
and, where needed, one schema-validation retry; they are not web, market-data, broker or order
capabilities. The report separately lists `external_tool_calls`, which must be empty. Do not set
`ANTHROPIC_API_KEY`. Run when no production research job needs the same subscription quota. The
JSON reports retain model usage, elapsed time, the structured answer and automatic agreement with
the predeclared case labels.

## Review and decision

The automatic score is only an agreement check against frozen labels.  A human reviewer must
blind-review route-labelled copies after masking the route name and record:

1. factual correctness against the frozen packet;
2. whether required evidence and counter-evidence are represented;
3. format pass/fail;
4. edits needed to make the answer usable;
5. elapsed time, LLM calls, failures and quota interruptions.

The tracker is eligible for a later persistent-queue integration only if it has no tool or scope
violation, is not worse than the control on quality, and improves at least one of elapsed time,
maintenance, or human editing.  These twenty cases cannot demonstrate investment returns.

If it passes, the next design step is a separate contract for `task_type: thesis_check`,
`refers_to_run_id`, account binding and a read-only queue worker.  That step must still keep
`thesis_check` from creating executable proposals.

## Run 1 — 2026-09-20

Both routes ran once against all twenty frozen cases with `claude-sonnet-5`. Outputs were written
outside the checkout at `/tmp/agentic-thesis-trial-20260920-final/`. A case that exhausted the
three-turn response-format ceiling was retained as a failure and was not retried.

| Measure | Control | Thesis Tracker |
| --- | ---: | ---: |
| Cases completed | 19 / 20 | 19 / 20 |
| Format-ceiling failure | TT07 | TT15 |
| Fully matched predeclared automatic score | 13 / 20 | 12 / 20 |
| Correct classification | 15 / 20 | 14 / 20 |
| Correct `requires_research` | 17 / 20 | 17 / 20 |
| All required frozen facts mentioned | 18 / 20 | 17 / 20 |
| Completed-case elapsed time | 211.3 s | 241.5 s |
| Median completed-case time | 10.1 s | 12.3 s |
| Mean rationale length | 633 characters | 520 characters |
| External tool calls / proposals | 0 / 0 | 0 / 0 |

This automatic score is not a factual-accuracy or investment-return result. The planned blind
human review did not take place because the Tracker did not first clear the mechanical gate: it
was slower, had one response-format failure, and did not meet or exceed the control's automatic
agreement measures. Therefore it does **not** advance to a persistent `thesis_check` queue,
account binding, or any proposal-producing route. The reports remain available for diagnosis of
the shared label failures and response-format behaviour before a materially revised trial is
considered.
