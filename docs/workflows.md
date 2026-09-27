# Workflows

A workflow is the list of steps one kind of ticket goes through. The engine walks any workflow; it knows no
ticket types itself.

## Where they live

| Source | Scope | Path |
|---|---|---|
| Built-in | everyone | `src/mobile_factory/workflows/*.yaml` (bugfix, task, feature, spike, epic, new-app) |
| Yours, every repo | you | `~/.config/mobile-factory/workflows/<name>.yaml` |
| Yours, one repo | you | `~/.config/mobile-factory/projects/<owner>__<repo>/workflows/<name>.yaml` |

Later sources win by name, so `bugfix.yaml` in your folder replaces the built-in one. Nothing goes into the repo.

Copy a built-in or write your own; a broken workflow stops `factory run` with the reason.


Route ticket types to workflows in your `local.yaml`: `tracker: {pipelines: {Bug: hotfix}}`. A value of `parent`
means "the parent's workflow" (sub-tasks). The ticket's text can also override its type (`tracker.detect`).

## Format

```yaml
description: One line.
branch: feature/{key}-{slug}        # branch name ({key}, {slug} of the plan's summary)
pr_template: pr-body-feature.md     # pr-body.md (root cause) or pr-body-feature.md (acceptance criteria)
outcome: pr                         # pr | report | tickets
max_level: 4                        # autonomy cap for this kind of work (0 = a human at every gate)
artifacts:                          # which step's output plays each role for shared steps
  plan: plan                        #   the plan (acceptance criteria, summary for the branch)
  before: baseline                  #   screens before the change
  change: implement                 #   the change (commit subject, PR body, where retries go)
  after: accept                     #   the check afterwards (the PR's "Tested" section)
steps:
  - name: plan                      # unique in the workflow; `factory submit <name>`
    title: Plan & acceptance criteria
    kind: agent                     # agent (an AI does it) | auto (the runner does it)
    type: plan                      # what it does (below); default = name
    gate: plan                      # a human approval point after it (plan, repro, diff, review, pr, report,
                                    #   tickets, architecture, or your own name); autonomy decides who approves
    model: opus                     # haiku | sonnet | opus | inherit (agents.models overrides)
    scout: true                     # off by default: a Haiku scout gathers greps, history, CLI output first
    optional: true                  # runs only when factory.yaml `steps: {<name>: true}` (or --tests for unit_tests)
    alongside: [locate, history]    # read-only helpers started in the same message
    parallel: [review-correctness]  # parts started together, then this step merges them
    retry_to: implement             # where to go when this step fails (default: the `change` step)
    skill: plan                     # the instructions file (default: name); a <skill>.md next to your YAML wins
    task: >-                        # the one-paragraph brief the agent gets
      …
```

### Extending instead of copying

```yaml
extends: bugfix
max_level: 2
changes:
  - add: {name: analytics, kind: agent, type: custom, skill: analytics-check,
          task: "Check that every new screen logs its screen_view event."}
    after: fix
  - set: review
    model: sonnet
  - remove: history        # a step, by name
```

`add` needs `after:` or `before:`; `set` updates the named step's keys; `remove` drops it. Or list every step under
`steps:` to replace them all.

## Step types

| Type | Kind | Output / action |
|---|---|---|
| preflight | auto | clean tree, base branch, gh login (`git: false` skips them for no-code workflows) |
| intake | auto | reads the ticket, confirms the workflow |
| branch, checks, commit, pr_preview, publish, handoff | auto | branch · lint/tests/build · commit · PR preview · push draft PR · wrap up |
| report_preview, post_report | auto | spike report preview · comment on the ticket after approval |
| create_tickets | auto | creates the `stories` of the step named in `source:` |
| triage, reproduce, fix, verify | agent | the bug-fix steps |
| plan, baseline, implement, accept | agent | acceptance criteria · screens before · the change · every criterion with evidence |
| review | agent | guardrail findings |
| detectors | auto | light review: deterministic detector findings, all go to the PR as review notes |
| work | agent | light mode's one in-session step: `summary`, `acceptance_criteria` (met/blocked + evidence), `flow`, `notes` |
| research | agent | question, answer, findings, options, recommendation |
| split, spec, architecture, scaffold | agent | stories · app spec · stack decisions · project + first slice |
| custom | agent | `summary`, `details`, `files`, `ok` — anything else; `ok: false` retries or stops |

`factory next` shows an EXAMPLE of the JSON an agent step must hand in; an invalid `factory submit` lists its fields.

## Light mode

`mode: light` (the default in `factory.yaml`) runs Bug, Task, Story/Improvement (and a Sub-task of one) as the
`light` workflow: preflight → intake → branch → work → checks → review (detectors) → commit → pr_preview (the only
gate) → publish → handoff. The driving session does `work` itself: no subagents. `--tests` adds the unit-test
guidance to `work`. `factory run <KEY> --team` or `mode: team` runs bugfix/task/feature instead.

## Steering a run

`factory run <KEY> --direction "…"` (or `--direction-file`, or the wizard's first question) stores the developer's
direction in the run folder; `factory direct "…"` adds a timestamped entry mid-run. Every agent step gets it as a
high-priority DIRECTION line and the plan must say how it follows it. It never overrides gates, forbidden paths or the
no-secrets rule. Example:

> Stage data returns the same type for all meeting spaces; mock the corrected types/v3 response with
> `factory android mock` to rule out the backend.

`factory android mock <path-regex> <file.json | jq filter>` serves overridden responses through a local mitmproxy
(the device proxy is set with `adb reverse`; needs mitmproxy, its CA on the device and a debug build that trusts user
CAs). `factory android mock --off` restores the device; hand-off turns it off too. No app code changes.
