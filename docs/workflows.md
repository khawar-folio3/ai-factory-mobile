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

```bash
factory workflow list                         # what exists here, from where, which ticket types route to it
factory workflow show feature                 # its steps
factory workflow new hotfix --from bugfix     # your own, extending a built-in (add --global for every repo)
factory workflow validate                     # check them all
```

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
    scout: true                     # a Haiku scout gathers greps, history and CLI output first
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
| research | agent | question, answer, findings, options, recommendation |
| split, spec, architecture, scaffold | agent | stories · app spec · stack decisions · project + first slice |
| custom | agent | `summary`, `details`, `files`, `ok` — anything else; `ok: false` retries or stops |

`factory schema <step>` prints the JSON an agent step must hand in.
