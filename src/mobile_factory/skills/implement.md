---
description: Implement — build the planned change so every acceptance criterion holds.
---

# Implement — meet the criteria, follow the house style

- Follow the plan and the local patterns (locate/history hints). Strings in every language, all affected flavours.
- Add tests for new logic; UI tests only where the project already has them. Do not commit.
- Never touch forbidden paths, add lint suppressions or delete tests. Keep the diff to what the criteria need.

`summary` is the commit subject without the ticket key: imperative, ≤ 72 chars.
