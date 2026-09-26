# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. You will get an acknowledgement within 5 working days.

## Scope

Mobile Factory runs AI agents against your codebase with access to your git remote, tracker and devices. Reports we
especially want:

- a way for ticket, PR or web content to make the runner perform an outbound write (push, PR, tracker change) that
  skipped a gate or differs from the approved preview,
- secrets leaking into logs, run folders, snapshots, PR bodies or generated MCP config,
- `factory.yaml` accepting a plaintext secret,
- rollback deleting files outside the change.

## Design notes

- Secrets live only in the environment or the per-machine secrets file (0600); `factory.yaml` refuses literals.
- Generated MCP config keeps `${VAR}` references, never values.
- Diffs handed to agents drop secret-like files (`.env`, keystores, `google-services.json`, …).
- Gate approval needs an interactive terminal and a one-time code. This stops an agent approving its own gate through
  a non-interactive shell; it is not a defence against a malicious local user.
