# Reproduce — show the defect on a real device before touching code

Output: `factory schema reproduce`. Snapshots go to `<run>/snapshots/before/<label>.{png,txt}`.

1. `factory android install` (builds, installs, launches the configured variant; boots the AVD if needed).
2. `factory android where` → you need a signed-in, real screen, not a login or splash. Logged out → stop and ask the
   user to sign in with a test account. Never type credentials.
3. Reach the screen:
   - the control the ticket is about: `factory android tap "<label>"` (exact text, then contains)
   - fast path: `factory android open <deeplink>` (say which you used; deeplinks can bypass the bug)
   - poll, don't sleep: `factory android wait "<text>" 30`
   - a Maestro flow in `flows/` of the factory home (HOME line) for multi-step paths: `factory android flow <file>`
4. At the faulty state: `factory snap before <label>` (label = short, stable: `profile`, `settings-empty`).
   The `.txt` next to the PNG is the diffable state (activity, fragments, visible labels). Read the PNG only for visual defects.
5. Adjacent screens: every other path that shares the suspected code (same function, same screen class, sibling
   routes, one path that must keep failing, e.g. an unknown deeplink → 404). Snapshot at least one with its own label.
6. Submit `reproduced`, honest `confidence` (0.9 = seen every time; 0.5 = once; 0.2 = inferred), the `steps` you
   actually ran, and the labels.

Not reproducible after two honest tries → `reproduced: false` with what you tried in `notes`. The runner decides what
happens next; do not fix blind.
