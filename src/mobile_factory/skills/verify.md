# Verify — prove the fix on device, and that nothing next to it moved

The runner already ran lint, unit tests and installed the fixed build (`checks`). Output: `factory schema verify`.

1. Replay the same reproduction steps. At each point, `factory snap after <label>` with the **same labels** as before.
2. `factory snap diff`:
   - the defect label must show the expected change,
   - every adjacent label must say `unchanged`,
   - anything else changed → explain it in `notes` or set `adjacent_unchanged: false`.
3. Compare in words from the diff output; open PNGs only for visual defects or an unexplained change.

Be strict: `defect_fixed: true` only when the after state shows the correct behaviour, not just a different one.
A false here sends the run back to `fix` with your notes; after the attempt limit the runner rolls the change back.

When a HINT names `context/verify-scout.md`, read it first: searches, history and command output are
already there. Run your own searches only for what it lists under `Not found:` or clearly lacks.
