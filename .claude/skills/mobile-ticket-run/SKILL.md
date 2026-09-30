---
name: "mobile-ticket-run"
description: "Execute the process described in a Jira ticket on a mobile app (iOS simulator by default), keeping a run-notes file for continuity and reporting a verified stopping point."
---

# Mobile ticket run

Use when the user gives a Jira ticket key or URL that describes a process or workflow to be carried out on a mobile app.

## Inputs
- Jira ticket key or URL (required). Read it with the Jira connector.
- Target device: iOS simulator by default. Ask only if the ticket or user points elsewhere.
- App build and test account: only if the ticket needs them. Ask the user; never use real credentials.

## Before starting
1. Read the ticket in full, including description, steps, expected results and comments.
2. List the steps exactly as written. Do not add, infer or reorder steps. If something is unclear or missing, list it as an assumption and flag it in the final report, or ask the user if it blocks the run.
3. State the verified stopping point: the final state the ticket says the process ends in. If the ticket does not define one, ask the user.
4. Confirm device access. Claude cannot touch a simulator directly. It needs the desktop app linked to the user's computer, with either a shell (xcrun simctl) or computer use on the Simulator window. If neither is available, say so plainly and stop instead of pretending to run the steps.

## Run notes (the cache)
Create `run-notes.md` in the working directory at the start and re-read it before each new step block or after any interruption. Keep it short:
- Ticket key and title
- Steps as written (numbered)
- Verified stopping point
- Progress: last completed step, and the observed result of each completed step
- Friction points and how each was resolved
- Assumptions made

Update it after every step. If something goes off-script (unexpected screen, dialog, error), use the notes to return to the last completed step and continue from there. Do not restart from the beginning.

## Execution
1. Do the steps in order, exactly as written.
2. After each step, verify the outcome against the ticket's expected result (screenshot or UI state). Record it in the notes.
3. If a step fails: diagnose, fix or work around it if it is within scope, and note what happened. If the fix would change the documented process, ask the user first.
4. Follow the safety rules: no real credentials, no purchases, no sending messages or deleting data unless the user explicitly confirms in chat.
5. Stop at the verified stopping point and confirm the final state matches the ticket.

## Report
Keep it concise and free of filler:
- Outcome: reached the verified stopping point or not
- Friction points and fixes
- Assumptions made (clearly flagged)
- Anything in the ticket that was ambiguous or missing

## Notes
- One clean run by default. Do not perform deliberate wrong steps or repeat runs unless the user asks.
- If the user asks for multiple runs, keep using the same notes file so later runs start from what was learned.